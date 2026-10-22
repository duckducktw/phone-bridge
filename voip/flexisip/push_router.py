#!/usr/bin/env python3
"""push_router — Flexisip external-push-flexiapi → Linphone 官方推播 API 的轉送器。

為何需要:
  官方 App Store 版 Linphone iOS 只收得到 Belledonne 伺服器發的 VoIP 推播(PushKit 憑證綁
  官方 Bundle ID)。第三方 SIP 伺服器要叫醒它,只能 POST
  https://subscribe.linphone.org/api/push_notification(帶 sip.linphone.org 帳號的 API key)。

為何不直接把 key 填進 Flexisip:
  1. FlexiAPI 的 user API key 綁「產生它的來源 IP」(AuthenticateKey: apiKey->ip == request->ip()),
     本機出口走 Cloudflare WARP,IP 可能變。
  2. 伺服器每日排程刪掉「60 分鐘沒用過」的 user key(ClearApiKeys 60)。
  → 手填的 key 遲早 401。本服務改用帳號密碼走 Digest 呼叫 GET /api/accounts/me/api_key
    自己產生 key(自動綁定當下出口 IP),遇 401/403 就重產再重送一次。

介面:
  Flexisip 只接受 https 的 global::flexiapi/url,所以前面有 nginx(TLS+h2,自簽,Flexisip 不驗證書)
  反代到這裡 127.0.0.1:8090。Flexisip 以 x-api-key 帶「內部金鑰」(ROUTER_KEY),本服務驗它。

只用 stdlib。設定由環境變數(systemd EnvironmentFile=.sip-secrets.env)提供:
  LINPHONE_USER  sip.linphone.org 帳號名(不含 @domain)
  LINPHONE_PASS  該帳號密碼
  LINPHONE_DOMAIN 預設 sip.linphone.org
  ROUTER_KEY     Flexisip→本服務的內部金鑰
"""
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "subscribe.linphone.org"
PUSH_PATH = "/api/push_notification"
KEY_PATH = "/api/accounts/me/api_key"
LISTEN = ("127.0.0.1", int(os.environ.get("PUSH_ROUTER_PORT", "8090")))
KEY_CACHE = os.environ.get(
    "LINPHONE_KEY_CACHE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".linphone-api-key"))

USER = os.environ.get("LINPHONE_USER", "")
PASS = os.environ.get("LINPHONE_PASS", "")
DOMAIN = os.environ.get("LINPHONE_DOMAIN", "sip.linphone.org")
ROUTER_KEY = os.environ.get("ROUTER_KEY", "")
FROM = f"sip:{USER}@{DOMAIN}"

_key_lock = threading.Lock()
_api_key = None


def log(*a):
    print("[push-router]", *a, flush=True)


# ---------- HTTPS(強制 IPv4:key 綁 IPv4 出口,v6 位址會輪換) ----------
class _V4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        addr = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(addr, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _request(method, path, headers, body=None, timeout=15):
    conn = _V4HTTPSConnection(HOST, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        return r.status, r.getheaders(), r.read()
    finally:
        conn.close()


# ---------- Digest 產生 API key ----------
_DIGEST_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')


def _parse_challenges(headers):
    out = []
    for k, v in headers:
        if k.lower() == "www-authenticate" and v.lower().startswith("digest"):
            out.append({m[0].lower(): (m[1] or m[2]) for m in _DIGEST_RE.findall(v[6:])})
    return out


def generate_api_key():
    """Digest 登入後 GET /api/accounts/me/api_key → 回新 key(綁本次出口 IP)。"""
    if not (USER and PASS):
        raise RuntimeError("LINPHONE_USER/LINPHONE_PASS 未設定")
    base = {"Accept": "application/json", "From": FROM}
    st, hd, _ = _request("GET", KEY_PATH, base)
    chals = _parse_challenges(hd)
    if st != 401 or not chals:
        raise RuntimeError(f"預期 401 digest challenge,實得 {st}")
    # 優先 SHA-256(伺服器預設),否則 MD5
    ch = next((c for c in chals if c.get("algorithm", "").upper() == "SHA-256"), chals[0])
    alg = ch.get("algorithm", "MD5").upper()
    H = (lambda s: hashlib.sha256(s.encode()).hexdigest()) if alg == "SHA-256" \
        else (lambda s: hashlib.md5(s.encode()).hexdigest())
    realm, nonce, opaque = ch["realm"], ch["nonce"], ch["opaque"]
    nc, cnonce, qop = "00000001", secrets.token_hex(8), "auth"
    ha1 = H(f"{USER}:{realm}:{PASS}")
    ha2 = H(f"GET:{KEY_PATH}")
    resp = H(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    auth = (f'Digest username="{USER}",realm="{realm}",nonce="{nonce}",uri="{KEY_PATH}",'
            f'algorithm={alg},response="{resp}",opaque="{opaque}",qop={qop},nc={nc},cnonce="{cnonce}"')
    st, _, body = _request("GET", KEY_PATH, dict(base, Authorization=auth))
    key = body.decode(errors="replace").strip().strip('"')
    if st != 200 or not re.fullmatch(r"[A-Za-z0-9]{20,80}", key):
        raise RuntimeError(f"產生 API key 失敗 HTTP {st}: {body[:120]!r}")
    fd = os.open(KEY_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    log(f"已產生新 API key(長度 {len(key)},已快取)")
    return key


def get_key(force=False):
    global _api_key
    with _key_lock:
        if force:
            _api_key = generate_api_key()
        elif _api_key is None:
            try:
                with open(KEY_CACHE) as f:
                    _api_key = f.read().strip() or None
            except FileNotFoundError:
                pass
            if _api_key is None:
                _api_key = generate_api_key()
        return _api_key


# ---------- 推播轉送 ----------
_ALLOWED = {"pn_provider", "pn_param", "pn_prid", "type", "call_id"}


def push(payload: dict):
    body = json.dumps({k: v for k, v in payload.items() if k in _ALLOWED and v is not None}).encode()
    for attempt in (0, 1):
        key = get_key(force=(attempt == 1))
        st, _, rb = _request("POST", PUSH_PATH, {
            "Accept": "application/json", "Content-Type": "application/json",
            "From": FROM, "x-api-key": key}, body)
        if st in (401, 403) and attempt == 0:
            log(f"push HTTP {st}(key 失效/出口 IP 變了)→ 重產 key 重送")
            continue
        return st, rb
    return st, rb


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        self._send(404)

    def do_POST(self):
        if self.path.split("?")[0] != PUSH_PATH:
            return self._send(404)
        if not ROUTER_KEY or not hmac.compare_digest(self.headers.get("x-api-key", ""), ROUTER_KEY):
            return self._send(401, b'{"error":"bad router key"}')
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            return self._send(400)
        try:
            payload = json.loads(self.rfile.read(n))
            st, rb = push(payload)
        except Exception as e:  # 網路/key 產生失敗
            log("push 失敗:", repr(e)[:200])
            return self._send(502, b'{"error":"upstream"}')
        cid = str(payload.get("call_id") or "-")[:40]
        log(f"push type={payload.get('type')} provider={payload.get('pn_provider')} call_id={cid} → HTTP {st}")
        self._send(200 if 200 <= st < 300 else 502, rb if 200 <= st < 300 else b'{"error":"rejected"}')

    def log_message(self, *a):
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--genkey":
        get_key(force=True)
        print("ok")
        return
    if not ROUTER_KEY:
        sys.exit("ROUTER_KEY 未設定")
    log(f"listening on {LISTEN[0]}:{LISTEN[1]} account={'set' if USER else 'MISSING'}")
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()


if __name__ == "__main__":
    main()
