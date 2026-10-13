#!/bin/bash
# 等 SCO PCM 可用就錄 12 秒（通話中才有 SCO）。不靠外部 timeout 殺。
M="${PHONE_MAC:?請設 PHONE_MAC=你手機藍牙MAC}"
OUT=/tmp/call123_v2.wav
# 最多等 25 秒等 SCO 出現
for i in $(seq 1 50); do
  if arecord -D bluealsa:DEV=$M,PROFILE=sco -d 1 -f S16_LE -r 16000 -c 1 /tmp/_probe.wav 2>/dev/null; then
    # 探測成功 → SCO 可用，正式錄 12 秒
    echo "SCO_READY probe=$i"
    arecord -D bluealsa:DEV=$M,PROFILE=sco -d 12 -f S16_LE -r 16000 -c 1 "$OUT" 2>&1
    echo "RECORDED $(stat -c%s "$OUT") bytes"
    exit 0
  fi
  sleep 0.5
done
echo "SCO_NEVER_READY"
exit 1
