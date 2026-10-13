#!/usr/bin/env python3
"""
bridge-glue: 連接 baresip(SIP) 與 oFono(手機HFP) 的撥號膠水。

去電: iPhone(6002) 撥 9+號碼 → Asterisk Dial PJSIP/6001 → baresip 收到 INCOMING
      → 本膠水從 SIP 來電的 '目標' 取出要撥的手機號碼 → oFono.Dial → 手機撥出
      → 接通後 baresip 自動 answer(把 SIP 音訊接到 BlueALSA SCO)

來電: 手機來電 → oFono CallAdded(state=incoming) → 本膠水叫 baresip dial 6002@asterisk
      的 mobile-in → 響 iPhone → iPhone 接 → baresip answer → SCO 接通

控制:
  - oFono: DBus system bus, org.ofono.VoiceCallManager / VoiceCall
  - baresip: ctrl_tcp 127.0.0.1:4444 (netstring 包 JSON)
"""
import socket, json, time, threading, subprocess, sys, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

OFONO_MODEM = "/hfp/org/bluez/hci0/dev_A0_B0_BD_1A_BD_C7"
BARESIP_CTRL = ("127.0.0.1", 4444)
ASTERISK = "127.0.0.1"
IPHONE_EXT = "6002"
GLUE_HTTP_PORT = 4455   # Asterisk 撥號前 curl 此埠告知目標號碼

# 待撥號碼(Asterisk 經 HTTP 告知 → baresip 來電時取用)
_pending_number = {"num": "", "ts": 0.0}
_lock = threading.Lock()

# ---------- baresip ctrl_tcp (netstring) ----------
def netstring_encode(data: bytes) -> bytes:
    return str(len(data)).encode() + b":" + data + b","

def baresip_cmd(command: str, params: str = ""):
    """送一個 baresip 命令 (netstring 包 JSON)。"""
    msg = json.dumps({"command": command, "params": params}).encode()
    try:
        s = socket.create_connection(BARESIP_CTRL, timeout=5)
        s.sendall(netstring_encode(msg))
        s.settimeout(2)
        try:
            resp = s.recv(8192)
        except socket.timeout:
            resp = b""
        s.close()
        return resp
    except Exception as e:
        print(f"[glue] baresip_cmd error: {e}", flush=True)
        return None

# ---------- oFono via dbus-send ----------
def ofono_dial(number: str):
    """用 oFono 叫手機撥號。"""
    print(f"[glue] ofono dial {number}", flush=True)
    r = subprocess.run([
        "dbus-send", "--system", "--print-reply",
        "--dest=org.ofono", OFONO_MODEM,
        "org.ofono.VoiceCallManager.Dial",
        f"string:{number}", "string:default"
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[glue] ofono dial failed: {r.stderr.strip()[:200]}", flush=True)
        return False
    return True

def get_all_call_paths():
    """回傳所有 voicecall 物件路徑清單。"""
    r = subprocess.run([
        "dbus-send", "--system", "--print-reply",
        "--dest=org.ofono", OFONO_MODEM,
        "org.ofono.VoiceCallManager.GetCalls"
    ], capture_output=True, text=True)
    return re.findall(r'(/hfp/\S+/voicecall\d+)', r.stdout)

def ofono_hangup_all():
    # 必須帶 --print-reply：否則 dbus-send 是 fire-and-forget，process 會在訊息
    # 真正送達 ofono 前就退出，訊息被丟棄 → exit=0 但手機沒掛(掛斷不同步 bug 真因)。
    #
    # 且 VoiceCallManager.HangupAll 只掛得掉 active(已接通)通話；對 dialing/alerting
    # (去電對方還沒接) 無效 → 必須對每個 VoiceCall 物件下 Hangup。
    # 策略:先逐一 per-call Hangup(涵蓋 dialing/alerting/active)，再補一次 HangupAll 保險。
    ok = True
    for path in get_all_call_paths():
        r = subprocess.run([
            "dbus-send", "--system", "--print-reply", "--dest=org.ofono",
            path, "org.ofono.VoiceCall.Hangup"
        ], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[glue] hangup {path} failed: {r.stderr.strip()[:160]}", flush=True)
            ok = False
    subprocess.run([
        "dbus-send", "--system", "--print-reply", "--dest=org.ofono", OFONO_MODEM,
        "org.ofono.VoiceCallManager.HangupAll"
    ], capture_output=True, text=True)
    return ok

# ---------- 監聽 oFono 來電 (dbus-monitor) ----------
def watch_ofono_incoming():
    """輪詢 oFono GetCalls，手機來電(incoming)時叫 baresip 撥 iPhone。
    用輪詢而非 dbus-monitor（後者 BecomeMonitor 需 root 權限）。"""
    print("[glue] polling oFono for incoming calls", flush=True)
    known = {}  # call_path -> state
    while True:
        calls = get_all_calls()
        # 新出現且 incoming 的通話 → 轉給 iPhone
        for path, state in calls.items():
            if path not in known and state == "incoming":
                print(f"[glue] oFono incoming call {path} → ring iPhone", flush=True)
                baresip_cmd("dial", f"sip:mobile-in@{ASTERISK}")
        known = calls
        time.sleep(1)

def get_all_calls():
    """回傳 {call_path: state}。"""
    r = subprocess.run([
        "dbus-send", "--system", "--print-reply",
        "--dest=org.ofono", OFONO_MODEM,
        "org.ofono.VoiceCallManager.GetCalls"
    ], capture_output=True, text=True)
    out = r.stdout
    # 解析 object path 與其後的 State
    calls = {}
    cur = None
    for m in re.finditer(r'object path "([^"]+)"|string "State"\s+variant\s+string "(\w+)"', out):
        if m.group(1):
            cur = m.group(1)
        elif m.group(2) and cur:
            calls[cur] = m.group(2)
    return calls

# ---------- 監聽 baresip 來電 (ctrl_tcp event stream) ----------
def watch_baresip_events():
    """持續連 ctrl_tcp 讀事件;iPhone 撥進來(CALL_INCOMING)時取號碼叫 ofono 撥。"""
    print("[glue] watching baresip ctrl_tcp events", flush=True)
    buf = b""
    while True:
        try:
            s = socket.create_connection(BARESIP_CTRL, timeout=5)
            s.settimeout(None)
            while True:
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                # 解析 netstring: LEN:payload,
                while b":" in buf:
                    try:
                        pre, rest = buf.split(b":", 1)
                        ln = int(pre)
                    except ValueError:
                        buf = b""
                        break
                    if len(rest) < ln + 1:
                        break
                    payload = rest[:ln]
                    buf = rest[ln+1:]
                    handle_baresip_event(payload)
        except Exception as e:
            print(f"[glue] baresip event reconnect: {e}", flush=True)
            time.sleep(3)

def handle_baresip_event(payload: bytes):
    try:
        ev = json.loads(payload.decode(errors="ignore"))
    except Exception:
        return
    etype = ev.get("type") or ev.get("event")
    klass = ev.get("class", "")
    param = ev.get("param", "")
    if not etype:
        return
    print(f"[glue] baresip event: {etype} {klass} {param}", flush=True)
    # 來電(iPhone 經 Asterisk 撥進 baresip 的 6001): CALL_INCOMING
    # 被叫號碼不在 baresip 事件裡(它只認自己 6001),改由 Asterisk 撥號前經 HTTP 告知
    if "CALL_INCOMING" in str(etype) or "CALL_INCOMING" in str(klass):
        with _lock:
            num = _pending_number["num"]
            age = time.time() - _pending_number["ts"]
            _pending_number["num"] = ""   # 取用後清掉
        if num and age < 10:
            print(f"[glue] iPhone→撥外線 {num} (HTTP通知)", flush=True)
            baresip_cmd("accept")      # baresip 接(建立 SIP 音訊路徑到 iPhone)
            time.sleep(0.5)
            ofono_dial(num)            # 手機用中華卡實際撥出
        else:
            print(f"[glue] 來電但無待撥號碼(num='{num}' age={age:.0f}s)", flush=True)

    # 掛斷(iPhone 掛掉 → SIP 側 CALL_CLOSED): 同步叫 ofono 掛手機,否則手機通話卡到 timeout
    if "CALL_CLOSED" in str(etype) or "CALL_CLOSED" in str(klass):
        print("[glue] SIP 通話結束 → ofono 掛斷手機", flush=True)
        ofono_hangup_all()

# ---------- HTTP endpoint: Asterisk 撥號前告知目標號碼 ----------
class GlueHTTP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        num = re.sub(r"\D", "", (q.get("num") or [""])[0])
        if num:
            with _lock:
                _pending_number["num"] = num
                _pending_number["ts"] = time.time()
            print(f"[glue] HTTP 收到待撥號碼: {num}", flush=True)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(400); self.end_headers(); self.wfile.write(b"no num")

def run_http():
    srv = HTTPServer(("127.0.0.1", GLUE_HTTP_PORT), GlueHTTP)
    print(f"[glue] HTTP endpoint on 127.0.0.1:{GLUE_HTTP_PORT}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    print("[glue] bridge-glue 啟動", flush=True)
    t1 = threading.Thread(target=watch_ofono_incoming, daemon=True)
    t2 = threading.Thread(target=watch_baresip_events, daemon=True)
    t3 = threading.Thread(target=run_http, daemon=True)
    t1.start(); t2.start(); t3.start()
    while True:
        time.sleep(3600)
