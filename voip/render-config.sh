#!/usr/bin/env bash
# render-config.sh — 由範本 + .sip-secrets.env 生成本機真實 pjsip.conf
# 真實 pjsip.conf 含明文密碼，被 .gitignore 忽略，絕不進 repo。
# 改密碼：編輯 voip/.sip-secrets.env 後重跑本腳本 + docker compose restart asterisk。
set -euo pipefail
cd "$(dirname "$0")"

SECRETS="./.sip-secrets.env"

[ -f "$SECRETS" ]  || { echo "ERROR: 找不到 $SECRETS"; exit 1; }

# shellcheck disable=SC1090
set -a; source "$SECRETS"; set +a
: "${SIP_BARESIP_PASS:?SIP_BARESIP_PASS 未設}"
: "${SIP_IPHONE_PASS:?SIP_IPHONE_PASS 未設}"

# Tailscale IP:優先用 .sip-secrets.env 的 TAILSCALE_IP,否則自動偵測 tailscale0。
if [ -z "${TAILSCALE_IP:-}" ]; then
  TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  [ -z "$TAILSCALE_IP" ] && TAILSCALE_IP="$(ip -4 addr show tailscale0 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1 || true)"
fi
[ -z "$TAILSCALE_IP" ] && { echo "ERROR: 偵測不到 Tailscale IP,請在 $SECRETS 設 TAILSCALE_IP=100.x.x.x"; exit 1; }

# Flexisip→push_router 的內部金鑰:沒有就產生一把並寫回 secrets(之後固定)。
if [ -z "${ROUTER_KEY:-}" ]; then
  ROUTER_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  printf '\nROUTER_KEY="%s"\n' "$ROUTER_KEY" >> "$SECRETS"
  echo "已產生 ROUTER_KEY 並寫入 $SECRETS"
fi

# 用 python 做置換:密碼/IP 含任何特殊字元($ & | \ 引號…)都安全(不經 shell/sed 解讀)。
umask 077   # 先設 umask,確保輸出一誕生就是 600,無 world-readable 窗口
SIP_BARESIP_PASS="$SIP_BARESIP_PASS" \
SIP_IPHONE_PASS="$SIP_IPHONE_PASS" \
TAILSCALE_IP="$TAILSCALE_IP" \
ROUTER_KEY="$ROUTER_KEY" \
python3 - <<'PYIN'
import os, re
repl = {
    "__SIP_BARESIP_PASS__": os.environ["SIP_BARESIP_PASS"],
    "__SIP_IPHONE_PASS__":  os.environ["SIP_IPHONE_PASS"],
    "__TAILSCALE_IP__":     os.environ["TAILSCALE_IP"],
    "__ROUTER_KEY__":       os.environ["ROUTER_KEY"],
}
pairs = [
    ("./asterisk/pjsip.conf.template",      "./asterisk/pjsip.conf"),
    ("./flexisip/flexisip.conf.template",   "./flexisip/flexisip.conf"),
    ("./flexisip/routes.conf.template",     "./flexisip/routes.conf"),
]
for tpl_path, out in pairs:
    tpl = open(tpl_path, encoding="utf-8").read()
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    left = re.findall(r"__[A-Z_]+__", tpl)
    if left:
        raise SystemExit(f"ERROR: {tpl_path} 仍有未置換佔位符: {sorted(set(left))}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tpl)
    os.chmod(out, 0o600)
    print(f"已生成 {out}")
PYIN

# nginx 自簽憑證(只給 127.0.0.1 上的 Flexisip→nginx 用;Flexisip 不驗證書)
if [ ! -f ./flexisip/tls/cert.pem ]; then
  mkdir -p ./flexisip/tls
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=127.0.0.1" \
    -keyout ./flexisip/tls/key.pem -out ./flexisip/tls/cert.pem 2>/dev/null
  echo "已生成 flexisip/tls 自簽憑證"
fi
echo "完成 (Flexisip 對外 bind=$TAILSCALE_IP:5060)"
