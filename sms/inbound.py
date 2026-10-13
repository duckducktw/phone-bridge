#!/usr/bin/env python3
"""
SMS Bridge — 入站轉換器（純 stdlib，無 uvicorn/fastapi 依賴）
收手機 SMS gateway app 的 webhook（收到新簡訊時），格式化後貼進 Discord #簡訊。

啟動：python3 inbound.py   (監聽 127.0.0.1:8099)
手機 app 的 webhook 目標（經 Cloudflare Tunnel）：https://sms.gdtw.qzz.io/inbound

支援 httpSMS webhook 與通用 payload（自動偵測）。
httpSMS 事件 message.phone.received 的 payload：
  {"data": {"from":"+886...","contents":"...","content":"..."}, ...}
"""
import os, json, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
UA = "DiscordBot (sms-bridge, 1.0)"


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
        if self.path.rstrip("/") != "/inbound":
            self._send(404, {"ok": False})
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(raw.decode() or "{}")
        except Exception:
            from urllib.parse import parse_qs
            payload = {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()}
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

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 8099), Handler)
    print("[inbound] 入站轉換器啟動 127.0.0.1:8099 webhook_configured=%s" % bool(WEBHOOK), flush=True)
    srv.serve_forever()
