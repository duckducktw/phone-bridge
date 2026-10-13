#!/usr/bin/env python3
"""
bridge-glue: 連接 baresip(SIP) 與 oFono(手機HFP) 的撥號膠水。

去電: iPhone(6002) 撥 9+號碼 → Asterisk Dial PJSIP/6001 → baresip 收到 INCOMING
      → 本膠水從待撥佇列取號碼 → oFono.Dial → 手機撥出
      → baresip accept(把 SIP 音訊接到 snd-aloop↔BlueALSA SCO)

來電: 手機來電 → oFono 出現 state=incoming → 本膠水叫 baresip dial mobile-in
      → 響 iPhone(手機本身也響) → 誰先接誰贏:
        - iPhone 先接 → baresip RTPESTAB/ESTABLISHED → ofono_answer() 接手機來電
        - 手機端先接(incoming→active 非我方 Answer) → 掛掉 iPhone 響鈴腿

控制:
  - oFono: DBus system bus, org.ofono.VoiceCallManager / VoiceCall
  - baresip: ctrl_tcp 127.0.0.1:4444 (netstring 包 JSON)

並發模型(重要):
  三條 thread(oFono 輪詢 / baresip 事件 / HTTP)共用 _state,所有「讀狀態→決策→
  改狀態」都在單一 _slock 區段內完成,避免 TOCTOU。來電雙響「手機端先接」時掛 iPhone
  響鈴腿,會讓 baresip 送 CALL_CLOSED;為避免該 CLOSED 回頭觸發 ofono_hangup_all()
  把剛接通的手機通話掛掉,用帶時效的時間戳 _state['expect_until'] 讓 CLOSED handler
  認得「這次 SIP 腿關閉是我方預期的、別掛手機」;時效(5s)過後自動失效,避免遺失的
  CLOSED 讓旗標黏滯到下一通去電而漏掛手機(持續計費)。
"""
import os, socket, json, time, threading, subprocess, sys, re
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# ---- 設定(可用環境變數覆蓋;MAC 等裝置識別碼不寫死進 repo) ----
PHONE_MAC   = os.environ.get("PHONE_MAC", "A0:B0:BD:1A:BD:C7")
OFONO_MODEM = os.environ.get("OFONO_MODEM",
                             "/hfp/org/bluez/hci0/dev_" + PHONE_MAC.replace(":", "_"))
BARESIP_CTRL = (os.environ.get("BARESIP_HOST", "127.0.0.1"),
                int(os.environ.get("BARESIP_CTRL_PORT", "4444")))
ASTERISK = os.environ.get("ASTERISK_HOST", "127.0.0.1")
GLUE_HTTP_PORT = int(os.environ.get("GLUE_HTTP_PORT", "4455"))
PENDING_TTL = 10.0     # 待撥號碼有效秒數
POLL_INTERVAL = 0.5    # oFono 輪詢間隔
GONE_CONFIRM = 2       # 通話需連續這麼多輪看不到才判定「消失」(防 dbus 抖動誤掛)

# ---------- baresip ctrl_tcp (netstring, 單一持久連線) ----------
# 重要:baresip 的 ctrl_tcp 只接受「一條」連線,第二條連上會踢掉第一條。故命令與事件
# 必須共用同一條連線。本類持有唯一連線,背景讀 thread 把 stream 分流:
#   - 命令回應(含 response:true) → 用 token 配對喚醒對應的 baresip_cmd 呼叫
#   - 事件(event:true)          → 丟給 handle_baresip_event
def netstring_encode(data: bytes) -> bytes:
    return str(len(data)).encode() + b":" + data + b","

class BaresipCtrl:
    def __init__(self, addr):
        self.addr = addr
        self.sock = None
        self.lock = threading.Lock()       # 保護 sock 寫入 + 連線重建
        self.resp_lock = threading.Lock()
        self.resp_cond = threading.Condition(self.resp_lock)
        self.responses = {}                # token -> decoded response dict(只存有人等的)
        self._waiters = set()              # 正在等回應的 token;非等待中的 response 直接丟棄
        self._tok = 0
        self.connected = threading.Event()

    def _next_token(self):
        self._tok += 1
        return str(self._tok)

    def connect(self):
        """(重)建連線。呼叫者需持有 self.lock。"""
        if self.sock:
            try: self.sock.close()
            except Exception: pass
            self.sock = None
        s = socket.create_connection(self.addr, timeout=5)
        s.settimeout(None)
        self.sock = s
        self.connected.set()

    def reader_loop(self):
        """唯一的 ctrl_tcp 讀取迴圈(背景 thread):連線、分流 event/response、斷線重連。"""
        buf = b""
        while True:
            try:
                with self.lock:
                    if self.sock is None:
                        self.connect()
                        print("[glue] ctrl_tcp 已連線", flush=True)
                        on_ctrl_connected()
                sock = self.sock
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("ctrl_tcp EOF")
                buf += data
                buf = self._drain(buf)
            except Exception as e:
                print(f"[glue] ctrl_tcp 重連: {e}", flush=True)
                self.connected.clear()
                with self.lock:
                    if self.sock:
                        try: self.sock.close()
                        except Exception: pass
                        self.sock = None
                # 喚醒所有等待中的 cmd,讓它們立即失敗(不必空等到各自 timeout)
                with self.resp_cond:
                    self.resp_cond.notify_all()
                buf = b""
                time.sleep(3)

    def _drain(self, buf: bytes) -> bytes:
        """解析 buf 內所有完整 netstring;遇壞資料跳到下一個邊界不整段清空。"""
        while b":" in buf:
            pre, rest = buf.split(b":", 1)
            if not pre.isdigit():
                buf = rest
                continue
            ln = int(pre)
            if len(rest) < ln + 1:
                return pre + b":" + rest
            payload = rest[:ln]
            buf = rest[ln + 1:]
            self._dispatch(payload)
        return buf

    def _dispatch(self, payload: bytes):
        try:
            msg = json.loads(payload.decode(errors="ignore"))
        except Exception:
            return
        if msg.get("response") is True:
            tok = msg.get("token", "")
            with self.resp_cond:
                # 只保留有人等的 token 的回應;其餘(want_response=False 的命令回應)直接丟棄,
                # 避免 responses dict 在熱路徑(dial/accept/hangup)無界洩漏。
                if tok in self._waiters:
                    self.responses[tok] = msg
                    self.resp_cond.notify_all()
        elif msg.get("event") is True:
            handle_baresip_event(msg)

    def cmd(self, command: str, params: str = "", want_response: bool = False, timeout: float = 3.0):
        """發送命令。want_response=True 時用 token 等回應(回 dict),否則回 None。"""
        tok = None
        with self.lock:
            if self.sock is None:
                print("[glue] cmd 時無連線,略過", flush=True)
                return None
            tok = self._next_token()
            obj = {"command": command, "params": params, "token": tok}
            try:
                self.sock.sendall(netstring_encode(json.dumps(obj).encode()))
            except Exception as e:
                print(f"[glue] cmd 送出失敗: {e}", flush=True)
                return None
        if not want_response:
            return None
        with self.resp_cond:
            self._waiters.add(tok)
        deadline = time.time() + timeout
        with self.resp_cond:
            while tok not in self.responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._waiters.discard(tok)
                    self.responses.pop(tok, None)   # 清掉可能遲到塞入的
                    return None
                self.resp_cond.wait(remaining)
            self._waiters.discard(tok)
            return self.responses.pop(tok)

_ctrl = BaresipCtrl(BARESIP_CTRL)

def baresip_cmd(command: str, params: str = "", read_response: bool = False):
    r = _ctrl.cmd(command, params, want_response=read_response)
    return r

def baresip_hangup():
    _ctrl.cmd("hangup", want_response=False)

def baresip_active_calls() -> int:
    """查 baresip 目前 active call 數(ctrl_tcp listcalls,走同一持久連線)。
    回 -1 表查詢失敗。"""
    r = _ctrl.cmd("listcalls", want_response=True)
    if not r:
        return -1
    data = str(r.get("data", ""))
    m = re.search(r"Active calls \((\d+)\)", data)
    return int(m.group(1)) if m else -1

# ---------- oFono via dbus-send ----------
def _dbus(dest_path: str, method: str, *args, timeout: float = 5.0):
    """統一的 dbus-send 呼叫:一律帶 --print-reply(否則 fire-and-forget,訊息可能在送達
    ofono 前就隨 process 退出被丟棄),且帶 timeout(ofono/BlueZ 卡住時不讓呼叫線程無限阻塞,
    預設 dbus-send 要 ~25s 才超時 → reader 線程會假死)。回 (rc, stdout);逾時回 (-1, "")。"""
    cmd = ["dbus-send", "--system", "--print-reply", "--dest=org.ofono", dest_path, method] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        print(f"[glue] dbus 逾時({timeout}s): {method}", flush=True)
        return -1, "", "timeout"
    except Exception as e:
        print(f"[glue] dbus 例外: {method}: {e}", flush=True)
        return -1, "", str(e)

def ofono_dial(number: str):
    """用 oFono 叫手機撥號。"""
    print(f"[glue] ofono dial {number}", flush=True)
    rc, _, err = _dbus(OFONO_MODEM, "org.ofono.VoiceCallManager.Dial",
                       f"string:{number}", "string:default")
    if rc != 0:
        print(f"[glue] ofono dial failed: {err.strip()[:200]}", flush=True)
        return False
    return True

def get_all_call_paths():
    """回傳所有 voicecall 物件路徑清單。"""
    rc, out, _ = _dbus(OFONO_MODEM, "org.ofono.VoiceCallManager.GetCalls")
    if rc != 0:
        return []
    return re.findall(r'(/hfp/\S+/voicecall\d+)', out)

def ofono_answer():
    """接聽手機來電(對 incoming 的 VoiceCall 下 Answer)。"""
    ok = False
    for path in get_all_call_paths():
        rc, _, err = _dbus(path, "org.ofono.VoiceCall.Answer")
        if rc == 0:
            ok = True
        else:
            print(f"[glue] answer {path} failed: {err.strip()[:160]}", flush=True)
    return ok

def ofono_hangup_all():
    # 必須帶 --print-reply(見 _dbus)。且 VoiceCallManager.HangupAll 只掛得掉 active(已接通)
    # 通話；對 dialing/alerting(去電對方還沒接) 無效 → 必須對每個 VoiceCall 物件下 Hangup。
    # 策略:先逐一 per-call Hangup(涵蓋 dialing/alerting/active)，再補一次 HangupAll 保險。
    ok = True
    for path in get_all_call_paths():
        rc, _, err = _dbus(path, "org.ofono.VoiceCall.Hangup")
        if rc != 0:
            print(f"[glue] hangup {path} failed: {err.strip()[:160]}", flush=True)
            ok = False
    _dbus(OFONO_MODEM, "org.ofono.VoiceCallManager.HangupAll")
    return ok

def get_all_calls():
    """回傳 {call_path: state}；dbus 查詢失敗回 None(區分「查詢失敗」與「真的沒通話」,
    避免瞬時失敗被當成通話消失而誤掛健在通話)。"""
    rc, out, _ = _dbus(OFONO_MODEM, "org.ofono.VoiceCallManager.GetCalls")
    if rc != 0:
        return None
    calls = {}
    cur = None
    for m in re.finditer(r'object path "([^"]+)"|string "State"\s+variant\s+string "(\w+)"', out):
        if m.group(1):
            cur = m.group(1)
        elif m.group(2) and cur:
            calls[cur] = m.group(2)
    return calls

# ---------- 通話協調狀態 ----------
# 來電雙響「誰先接誰贏」+ 掛斷雙向同步共用這組狀態。所有讀寫都在 _slock 內。
_state = {
    "ringing": False,          # 手機來電→已撥 iPhone,雙方都在響,等誰先接
    "answered_mobile": False,  # 我們已對手機來電下過 Answer(iPhone 先接的情形)
    "expect_until": 0.0,       # 我方主動掛 iPhone 腿後,此時刻前到來的 CALL_CLOSED 視為「預期」,
                               # 不觸發 ofono_hangup_all(否則掛掉剛在手機上接的通話)。用時間戳
                               # 而非永久 bool:若那則 CALL_CLOSED 遺失,旗標會自動逾時失效,不會
                               # 黏滯到「下一通去電結束時誤判為預期 → 不掛手機 → 持續計費」。
    "need_reconcile": False,   # ctrl_tcp (重)連上 → 請輪詢線程在鎖外補一次狀態收斂
}
EXPECT_WINDOW = 5.0            # expect_until 時效(秒)
_slock = threading.Lock()

def on_ctrl_connected():
    """ctrl_tcp (重)連上時由 reader_loop 呼叫(此時持有 _ctrl.lock)。
    只設旗標,真正的收斂(需呼叫 listcalls,會再取 _ctrl.lock)延到輪詢線程的鎖外做,
    避免在 reader_loop 持鎖時又去 cmd() 造成自我死鎖。"""
    with _slock:
        _state["need_reconcile"] = True

# 待撥號碼佇列(Asterisk 經 HTTP 告知 → baresip 來電時取用);用 queue 而非單槽,
# 避免連續兩通去電時 A 用到 B 的號碼。每筆帶時間戳,取用時丟棄逾時者。
_pending = deque()
_plock = threading.Lock()

def pending_put(num: str):
    with _plock:
        _pending.append((num, time.time()))

def pending_get():
    """取最早一筆仍在 TTL 內的待撥號碼;清掉過期的。回 None 表無有效號碼。"""
    with _plock:
        now = time.time()
        while _pending:
            num, ts = _pending.popleft()
            if now - ts < PENDING_TTL:
                return num
        return None

# ---------- 監聽 oFono 通話 (輪詢 GetCalls) ----------
def _watch_ofono_calls():
    print("[glue] polling oFono call states", flush=True)
    known = {}        # call_path -> state(上一輪確認存在的)
    gone_count = {}   # call_path -> 連續看不到的輪數(debounce)
    while True:
        time.sleep(POLL_INTERVAL)

        # ctrl_tcp (重)連上要求的狀態收斂(在此鎖外做,避免 reader_loop 持鎖時死鎖):
        # 若 baresip 其實已無通話,清掉殘留旗標(斷線期間可能漏掉 CALL_CLOSED)。
        with _slock:
            do_reconcile = _state["need_reconcile"]
            _state["need_reconcile"] = False
        if do_reconcile:
            if baresip_active_calls() == 0:
                with _slock:
                    _state["ringing"] = False
                    _state["answered_mobile"] = False
                    _state["expect_until"] = 0.0
                print("[glue] ctrl_tcp 連線對帳:baresip 無通話,狀態已收斂", flush=True)

        calls = get_all_calls()
        if calls is None:
            # dbus 查詢失敗 → 本輪跳過,保持 known 不變,絕不當成通話消失
            continue

        # (1) 新的 incoming 手機來電 → 響 iPhone(手機本身也響)
        for path, state in calls.items():
            if path not in known and state == "incoming":
                print(f"[glue] 手機來電 {path} → 同時響 iPhone(誰先接誰贏)", flush=True)
                with _slock:
                    _state["ringing"] = True
                    _state["answered_mobile"] = False
                    _state["expect_until"] = 0.0
                baresip_cmd("dial", f"sip:mobile-in@{ASTERISK}", read_response=False)

        # (2) 來電期間某通 incoming→active 且非我方 Answer → 手機端先接 → 掛 iPhone 響鈴腿。
        #     整段決策在單一鎖區內完成(讀 ringing/answered → 設 expect_until/清 ringing),
        #     消除 TOCTOU;baresip_hangup() 在鎖外呼叫(避免持鎖做網路 I/O)但旗標已先落定。
        mobile_answered = any(
            state == "active" and known.get(path) == "incoming"
            for path, state in calls.items()
        )
        do_hangup_sip = False
        with _slock:
            if _state["ringing"] and not _state["answered_mobile"] and mobile_answered:
                print("[glue] 手機端先接 → 掛掉 iPhone 響鈴腿(預期 CALL_CLOSED,不掛手機)", flush=True)
                _state["ringing"] = False
                _state["expect_until"] = time.time() + EXPECT_WINDOW
                do_hangup_sip = True
        if do_hangup_sip:
            baresip_hangup()

        # (3) 反向掛斷同步:通話連續 GONE_CONFIRM 輪消失(對方掛/蜂窩斷) → 若 baresip 還有
        #     通話,掛掉 iPhone/SIP 腿。debounce 防 dbus 單輪抖動誤掛。
        for path in list(known):
            if path not in calls:
                gone_count[path] = gone_count.get(path, 0) + 1
            else:
                gone_count.pop(path, None)
        confirmed_gone = [p for p, c in gone_count.items() if c >= GONE_CONFIRM]
        if confirmed_gone:
            # 先在鎖外查 baresip(走 _ctrl.lock,不可在 _slock 內呼叫),再進單鎖區決策。
            active = baresip_active_calls()
            do_hangup = False
            with _slock:
                ringing = _state["ringing"]
                # active != 0 涵蓋 >0(確有通話)與 -1(查詢失敗,不確定時偏向掛斷,
                # 寧可多掛也別留著 SIP 腿空耗);ringing 時也要收掉 iPhone 響鈴。
                if active != 0 or ringing:
                    _state["expect_until"] = time.time() + EXPECT_WINDOW  # 我方發起的關閉
                    do_hangup = True
                _state["ringing"] = False
                _state["answered_mobile"] = False
            if do_hangup:
                print(f"[glue] 手機側通話消失 {confirmed_gone} (baresip active={active}) → 掛斷 iPhone/SIP 腿", flush=True)
                baresip_hangup()
            for p in confirmed_gone:
                gone_count.pop(p, None)

        known = calls

def watch_ofono_calls():
    _guard(_watch_ofono_calls, "watch_ofono_calls")

# ---------- 監聽 baresip 來電 (ctrl_tcp event stream) ----------
# 註:事件讀取已由 BaresipCtrl.reader_loop(單一持久連線)統一處理,事件經 _dispatch →
# handle_baresip_event。不再另開連線讀事件(會踢掉彼此)。

def handle_baresip_event(ev: dict):
    etype = ev.get("type") or ev.get("event")
    klass = ev.get("class", "")
    param = ev.get("param", "")
    if not etype:
        return
    print(f"[glue] baresip event: {etype} {klass} {param}", flush=True)

    # 來電(iPhone 經 Asterisk 撥進 baresip 的 6001): CALL_INCOMING
    # 被叫號碼不在 baresip 事件裡(它只認自己 6001),改由 Asterisk 撥號前經 HTTP 告知(佇列)
    if "CALL_INCOMING" in str(etype) or "CALL_INCOMING" in str(klass):
        num = pending_get()
        if num:
            # 每通新去電開始先重置協調旗標:避免上一通遺失的 CALL_CLOSED 讓 expect_until
            # 黏滯,導致這通去電結束時被誤判為「預期關閉」→ 不掛手機 → 持續計費。
            with _slock:
                _state["ringing"] = False
                _state["answered_mobile"] = False
                _state["expect_until"] = 0.0
            print(f"[glue] iPhone→撥外線 {num}", flush=True)
            baresip_cmd("accept", read_response=False)  # baresip 接(建立 SIP 音訊到 iPhone)
            time.sleep(0.5)
            ofono_dial(num)                              # 手機用中華卡實際撥出
        else:
            print("[glue] 來電但無有效待撥號碼(可能是來電雙響的 mobile-in,忽略)", flush=True)

    # 通話建立: baresip 側 SIP 腿接通。
    # 注意:去電(baresip 收 INCOMING→我方 accept)接聽後 baresip 只發 CALL_RTPESTAB,
    # 不發 CALL_ESTABLISHED(那是主叫方才有)。故兩者都認。
    # 若此刻處於來電雙響(ringing)→ 代表 iPhone 先接了 mobile-in,要去 Answer 手機來電。
    if any(k in str(etype) or k in str(klass) for k in ("CALL_ESTABLISHED", "CALL_RTPESTAB")):
        do_answer = False
        with _slock:
            if _state["ringing"]:
                _state["answered_mobile"] = True
                _state["ringing"] = False
                do_answer = True
        if do_answer:
            print("[glue] iPhone 先接來電 → Answer 手機來電並橋接", flush=True)
            ofono_answer()

    # 掛斷(SIP 側 CALL_CLOSED): 決定是否同步掛手機。
    # 若在 expect_until 時限內(我方剛主動掛 iPhone 腿:手機端先接、或反向掛斷已處理)→ 只清旗標,
    # 絕不 ofono_hangup_all(否則把剛在手機上接的通話掛掉 — C2 競態)。時限外則視為 iPhone 真掛。
    if "CALL_CLOSED" in str(etype) or "CALL_CLOSED" in str(klass):
        with _slock:
            expected = time.time() < _state["expect_until"]
            ringing = _state["ringing"]
            _state["expect_until"] = 0.0
            _state["answered_mobile"] = False
            if ringing or expected:
                _state["ringing"] = False
        if expected:
            print("[glue] CALL_CLOSED(我方預期:手機端先接/反向掛斷)→ 不動手機", flush=True)
        elif ringing:
            # 來電雙響期間 iPhone 側腿關閉(iPhone 拒接/響鈴逾時):放棄 iPhone,
            # 保留手機來電(使用者可能想在手機上接)。僅清 ringing(上面已清)。
            print("[glue] iPhone 側響鈴結束(拒接/逾時)→ 保留手機來電", flush=True)
        else:
            print("[glue] SIP 通話結束(iPhone 掛斷)→ ofono 掛斷手機", flush=True)
            ofono_hangup_all()

# ---------- HTTP endpoint: Asterisk 撥號前告知目標號碼 ----------
class GlueHTTP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        num = re.sub(r"\D", "", (q.get("num") or [""])[0])
        if num:
            pending_put(num)
            print(f"[glue] HTTP 收到待撥號碼: {num}", flush=True)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(400); self.end_headers(); self.wfile.write(b"no num")

def _run_http():
    srv = HTTPServer(("127.0.0.1", GLUE_HTTP_PORT), GlueHTTP)
    print(f"[glue] HTTP endpoint on 127.0.0.1:{GLUE_HTTP_PORT}", flush=True)
    srv.serve_forever()

def run_http():
    _guard(_run_http, "run_http")

# ---------- thread 守護:worker 掛掉時讓整個 process 退出,systemd Restart=always 重啟 ----------
def _guard(fn, name):
    try:
        fn()
    except Exception as e:
        import traceback
        print(f"[glue] FATAL thread {name} 例外,退出讓 systemd 重啟: {e}", flush=True)
        traceback.print_exc()
        os._exit(1)

def ctrl_reader():
    _guard(_ctrl.reader_loop, "ctrl_reader")

if __name__ == "__main__":
    print("[glue] bridge-glue 啟動", flush=True)
    # ctrl_reader: 唯一的 baresip ctrl_tcp 連線(事件+命令共用);watch_ofono_calls: oFono 輪詢;
    # run_http: Asterisk 告知待撥號碼。任一 worker 例外 → _guard 讓 process 退出,systemd 重啟。
    for target in (ctrl_reader, watch_ofono_calls, run_http):
        threading.Thread(target=target, daemon=True).start()
    while True:
        time.sleep(3600)
