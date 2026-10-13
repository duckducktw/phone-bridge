#!/usr/bin/env python3
"""
SMS Bridge — 出站服務（REST 輪詢版）
監聽 Discord #簡訊 頻道，解析 `電話:內容`，透過手機端 SMS gateway 發送。

★ 為何用 REST 輪詢而非 Gateway WebSocket：
  同一個 DISCORD_BOT_TOKEN 只允許一條 Gateway 連線。Hermes 主程式已用這個 token
  連著 Gateway；若本服務再開一條 WebSocket，會把 Hermes 踢下線（agent 全平台失聯）。
  REST 呼叫不佔 Gateway session，故安全。輪詢間隔數秒，對簡訊場景足夠。

手機端後端（SMS_BACKEND）：
  httpsms : httpSMS app 雲端 API（手機裝 app 即可，免自架）
  local   : 自架 / 區網 HTTP 端點

設定：~/sms-bridge/.secrets.env 與 ~/sms-bridge/config.env
  DISCORD_BOT_TOKEN  (自動從 ~/.hermes/.env 帶入)
  SMS_CHANNEL_ID     #簡訊 頻道 id
  SMS_BACKEND        httpsms | local
  HTTPSMS_API_KEY / HTTPSMS_FROM          (httpsms)
  LOCAL_SMS_URL / LOCAL_SMS_TOKEN         (local)
  POLL_INTERVAL      輪詢秒數（預設 4）
"""
import os, re, json, sys, time, urllib.request, urllib.parse, urllib.error

CFG_DIR = os.path.expanduser("~/sms-bridge")
STATE   = os.path.join(CFG_DIR, ".state.json")


def load_env():
    for fn in (".secrets.env", "config.env"):
        p = os.path.join(CFG_DIR, fn)
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("DISCORD_BOT_TOKEN"):
        hp = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(hp):
            for line in open(hp):
                if line.startswith("DISCORD_BOT_TOKEN"):
                    os.environ["DISCORD_BOT_TOKEN"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break


load_env()
TOKEN      = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("SMS_CHANNEL_ID", "")
BACKEND    = os.environ.get("SMS_BACKEND", "httpsms")
POLL       = float(os.environ.get("POLL_INTERVAL", "4"))
UA         = "DiscordBot (sms-bridge, 1.0)"
API        = "https://discord.com/api/v10"
DH         = {"Authorization": f"Bot {TOKEN}", "User-Agent": UA}

# 電話：允許 + 開頭、3~20 碼（含短碼 166/117/市話），可含 - 與空白
LINE_RE = re.compile(r"^\s*(\+?[0-9][0-9\-\s]{2,20})\s*[:：]\s*(.+)$", re.S)


def http_json(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else {})


def normalize_tw(num: str) -> str:
    n = re.sub(r"[\s\-]", "", num)
    if n.startswith("+"):
        return n
    if n.startswith("0"):
        return "+886" + n[1:]
    return n


def send_sms(to: str, content: str):
    to = normalize_tw(to)
    if BACKEND == "httpsms":
        key = os.environ.get("HTTPSMS_API_KEY", "")
        frm = os.environ.get("HTTPSMS_FROM", "")
        if not key or not frm:
            return False, "httpsms 未設定 HTTPSMS_API_KEY / HTTPSMS_FROM（手機裝 app 後填入）"
        try:
            st, resp = http_json(
                "POST", "https://api.httpsms.com/v1/messages/send",
                {"x-api-key": key, "Content-Type": "application/json"},
                {"content": content, "from": frm, "to": to},
            )
            return (200 <= st < 300), f"httpsms status={st} id={resp.get('data', {}).get('id', '?')}"
        except urllib.error.HTTPError as e:
            return False, f"httpsms HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, f"httpsms error: {e}"
    elif BACKEND == "local":
        url = os.environ.get("LOCAL_SMS_URL", "")
        if not url:
            return False, "local 未設定 LOCAL_SMS_URL"
        headers = {"Content-Type": "application/json"}
        t = os.environ.get("LOCAL_SMS_TOKEN", "")
        if t:
            headers["Authorization"] = f"Bearer {t}"
        try:
            st, _ = http_json("POST", url, headers, {"to": to, "message": content})
            return (200 <= st < 300), f"local status={st}"
        except urllib.error.HTTPError as e:
            return False, f"local HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:
            return False, f"local error: {e}"
    return False, f"未知 SMS_BACKEND={BACKEND}"


def react(mid, emoji):
    try:
        em = urllib.parse.quote(emoji)
        http_json("PUT", f"{API}/channels/{CHANNEL_ID}/messages/{mid}/reactions/{em}/@me", DH)
    except Exception:
        pass


def reply(mid, content):
    try:
        http_json("POST", f"{API}/channels/{CHANNEL_ID}/messages",
                  {**DH, "Content-Type": "application/json"},
                  {"content": content, "message_reference": {"message_id": mid},
                   "allowed_mentions": {"parse": []}})
    except Exception as e:
        print("reply err", e, file=sys.stderr)


def load_last():
    try:
        return json.load(open(STATE)).get("last_id")
    except Exception:
        return None


def save_last(mid):
    json.dump({"last_id": mid}, open(STATE, "w"))


def baseline():
    """首次啟動：記住當前最新訊息 id，避免回頭處理歷史訊息。
    空頻道時不能存 "0"(Discord after=0 會從頻道最初開始抓),改用「當下時間」的
    snowflake,確保後續 after= 不會回頭抓到舊訊息。"""
    st, msgs = http_json("GET", f"{API}/channels/{CHANNEL_ID}/messages?limit=1", DH)
    if msgs:
        last = msgs[0]["id"]
    else:
        # Discord snowflake = (毫秒 - 1420070400000) << 22
        last = str((int(time.time() * 1000) - 1420070400000) << 22)
    save_last(last)
    return last


def handle(d):
    author = d.get("author", {})
    if author.get("bot"):
        return
    content = d.get("content", "")
    mid = d["id"]
    m = LINE_RE.match(content)
    if not m:
        if ":" in content or "：" in content:
            reply(mid, "格式：`電話:內容`，例如 `0912345678:測試`")
        return
    phone, text = m.group(1).strip(), m.group(2).strip()
    react(mid, "📤")
    ok, detail = send_sms(phone, text)
    react(mid, "✅" if ok else "❌")
    if not ok:
        reply(mid, f"發送失敗：{detail}")
    print(f"[send] to={phone} ok={ok} {detail}", flush=True)


def poll_once():
    last = load_last()
    if last is None:
        last = baseline()
        print(f"[sms-bridge] baseline last_id={last}", flush=True)
        return
    st, msgs = http_json("GET", f"{API}/channels/{CHANNEL_ID}/messages?after={last}&limit=50", DH)
    if not msgs:
        return
    # 回傳新→舊，按時間序處理
    for d in sorted(msgs, key=lambda x: int(x["id"])):
        try:
            handle(d)
        except Exception as e:
            print("handle err", e, file=sys.stderr)
        save_last(d["id"])


if __name__ == "__main__":
    if not (TOKEN and CHANNEL_ID):
        print("缺 DISCORD_BOT_TOKEN 或 SMS_CHANNEL_ID", file=sys.stderr)
        sys.exit(1)
    print(f"[sms-bridge] 出站輪詢啟動 channel={CHANNEL_ID} backend={BACKEND} poll={POLL}s", flush=True)
    while True:
        try:
            poll_once()
        except urllib.error.HTTPError as e:
            print(f"[sms-bridge] discord HTTP {e.code}", file=sys.stderr)
            if e.code == 429:
                time.sleep(10)
        except Exception as e:
            print(f"[sms-bridge] poll error: {e}", file=sys.stderr)
        time.sleep(POLL)
