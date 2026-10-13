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

sed -e "s|__SIP_BARESIP_PASS__|${SIP_BARESIP_PASS}|g" \
    -e "s|__SIP_IPHONE_PASS__|${SIP_IPHONE_PASS}|g" \
    "$TEMPLATE" > "$OUT"
chmod 600 "$OUT"
echo "已生成 $OUT (chmod 600, 已被 gitignore)"
