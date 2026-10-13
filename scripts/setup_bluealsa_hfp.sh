#!/bin/bash
set -e
# 1. 改 bluealsa service 加 hfp-ofono profile(drop-in override)
mkdir -p /etc/systemd/system/bluealsa.service.d
cat > /etc/systemd/system/bluealsa.service.d/override.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa -S -p a2dp-source -p a2dp-sink -p hfp-ofono
EOF
# 2. 把 user 加進 audio 群組(存取 bluealsa dbus)
usermod -aG audio user
# 3. 重載+重啟
systemctl daemon-reload
systemctl restart bluealsa
sleep 2
echo "=== bluealsa 新參數 ==="
ps aux | grep "[b]luealsa " | head -1
echo "=== 完成。user 需重新登入才吃到 audio 群組,或用 sg audio 臨時生效 ==="
