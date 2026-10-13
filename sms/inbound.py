#!/usr/bin/env python3
"""
SMS Bridge — 入站轉換器（純 stdlib，無 uvicorn/fastapi 依賴）
收手機 SMS gateway app 的 webhook（收到新簡訊時），格式化後貼進 Discord #簡訊。

啟動：python3 inbound.py   (監聽 127.0.0.1:8099)
手機 app 的 webhook 目標經 Cloudflare Tunnel 對外（實際網域設在本機 config，不寫入 repo）。

安全:webhook 對公網開放,故要求驗證才接受(否則任何人可偽造「來訊」到 Discord,
可冒銀行短碼釣魚)。驗證方式(擇一,由 SMS_INBOUND_SECRET 環境變數啟用):
  - HTTP header  X-Webhook-Secret: <secret>   (推薦)
  - 或 URL query ?secret=<secret>
  - 或 URL path  /inbound/<secret>
未設 SMS_INBOUND_SECRET 時「拒絕所有」(fail-closed),避免忘了設就裸奔。
httpSMS 本身支援簽章,若改用其簽章驗證可再強化。
"""
import os, json, hmac, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CFG_DIR = os.path.expanduser("~/sms-bridge")


def load_env():
    for fn in (".secrets.env", "config.env"):
        p = os.path.join(CFG_DIR, fn)
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()
WEBHOOK = os.environ.get("SMS_DISCORD_WEBHOOK", "")
INBOUND_SECRET = os.environ.get("SMS_INBOUND_SECRET", "")
UA = "DiscordBot (sms-bridge, 1.0)"


def check_auth(path: str, headers) -> bool:
    """驗證 webhook 來源。未設 secret → fail-closed(全拒)。"""
    if not INBOUND_SECRET:
        return False
    # header
    h = headers.get("X-Webhook-Secret", "")
    if h and hmac.compare_digest(h, INBOUND_SECRET):
        return True
    # query ?secret=
    q = parse_qs(urlparse(path).query)
    qs = (q.get("secret") or [""])[0]
    if qs and hmac.compare_digest(qs, INBOUND_SECRET):
        return True
    # path /inbound/<secret>
    parts = urlparse(path).path.rstrip("/").split("/")
    if len(parts) >= 3 and parts[1] == "inbound" and hmac.compare_digest(parts[2], INBOUND_SECRET):
        return True
    return False


def post_discord(frm: str, text: str):
    content = f"📩 **來自 {frm}**\n{text}"
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def extract(payload: dict):
    """從各種 app payload 取出 (from, text)。"""
    if isinstance(payload.get("data"), dict):
        d = payload["data"]
        frm = d.get("from") or d.get("contact") or d.get("owner") or "?"
        text = d.get("content") or d.get("contents") or d.get("message") or ""
        if text:
            return frm, text
    frm = payload.get("from") or payload.get("sender") or payload.get("phone") or "?"
    text = (payload.get("message") or payload.get("text") or payload.get("content")
            or payload.get("body") or "")
    return frm, text


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True, "webhook_configured": bool(WEBHOOK)})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        # 接受 /inbound 或 /inbound/<secret>
        if not (p == "/inbound" or p.startswith("/inbound/")):
            self._send(404, {"ok": False})
            return
        if not check_auth(self.path, self.headers):
            print("[inbound] 拒絕:webhook 驗證失敗(secret 不符或未設)", flush=True)
            self._send(401, {"ok": False, "reason": "unauthorized"})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            from urllib.parse import parse_qs as _pqs
            payload = {k: v[0] for k, v in _pqs(raw.decode(errors="replace")).items()}
        frm, text = extract(payload)
        if not text:
            self._send(200, {"ok": False, "reason": "no text", "keys": list(payload.keys())})
            return
        try:
            st = post_discord(str(frm), str(text))
            print(f"[inbound] from={frm} -> discord {st}", flush=True)
            self._send(200, {"ok": 200 <= st < 300, "discord_status": st})
        except urllib.error.HTTPError as e:
            print(f"[inbound] discord HTTP {e.code}", flush=True)
            self._send(200, {"ok": False, "discord_http": e.code})
        except (urllib.error.URLError, OSError) as e:
            print(f"[inbound] discord 連線失敗: {e}", flush=True)
            self._send(200, {"ok": False, "error": "discord unreachable"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8099), Handler)
    print("[inbound] 入站轉換器啟動 127.0.0.1:8099 webhook_configured=%s" % bool(WEBHOOK), flush=True)
    srv.serve_forever()
