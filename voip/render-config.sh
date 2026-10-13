#!/usr/bin/env bash
# render-config.sh — 由範本 + .sip-secrets.env 生成本機真實 pjsip.conf
# 真實 pjsip.conf 含明文密碼，被 .gitignore 忽略，絕不進 repo。
# 改密碼：編輯 voip/.sip-secrets.env 後重跑本腳本 + docker compose restart asterisk。
set -euo pipefail
cd "$(dirname "$0")"

SECRETS="./.sip-secrets.env"
TEMPLATE="./asterisk/pjsip.conf.template"
OUT="./asterisk/pjsip.conf"

[ -f "$SECRETS" ]  || { echo "ERROR: 找不到 $SECRETS"; exit 1; }
[ -f "$TEMPLATE" ] || { echo "ERROR: 找不到 $TEMPLATE"; exit 1; }

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

# 用 python 做置換:密碼/IP 含任何特殊字元($ & | \ 引號…)都安全(不經 shell/sed 解讀)。
umask 077   # 先設 umask,確保 OUT 一誕生就是 600,無 world-readable 窗口
SIP_BARESIP_PASS="$SIP_BARESIP_PASS" \
SIP_IPHONE_PASS="$SIP_IPHONE_PASS" \
TAILSCALE_IP="$TAILSCALE_IP" \
TEMPLATE="$TEMPLATE" OUT="$OUT" \
python3 - <<'PY'
import os
tpl = open(os.environ["TEMPLATE"], encoding="utf-8").read()
repl = {
    "__SIP_BARESIP_PASS__": os.environ["SIP_BARESIP_PASS"],
    "__SIP_IPHONE_PASS__":  os.environ["SIP_IPHONE_PASS"],
    "__TAILSCALE_IP__":     os.environ["TAILSCALE_IP"],
}
for k, v in repl.items():
    tpl = tpl.replace(k, v)
# 確認沒有殘留未置換的佔位符
import re
left = re.findall(r"__[A-Z_]+__", tpl)
if left:
    raise SystemExit(f"ERROR: 範本仍有未置換佔位符: {sorted(set(left))}")
with open(os.environ["OUT"], "w", encoding="utf-8") as f:
    f.write(tpl)
PY
chmod 600 "$OUT"
echo "已生成 $OUT (chmod 600, 已被 gitignore, bind=$TAILSCALE_IP:5060)"
