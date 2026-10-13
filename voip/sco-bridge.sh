#!/usr/bin/env bash
# sco-bridge.sh — 把 baresip 用的 snd-aloop loopback 與手機 BlueALSA SCO 雙向橋接。
#
# 為何要這層:baresip 單一 process 同時雙向開「同一個」bluealsa SCO device 會踩
# BlueALSA SCO full-duplex 雷(上行啞掉/對方聽不到,無錯誤訊息 — 見 bluez-alsa#333)。
# 解法:baresip 只開穩定的 snd-aloop(hw:PBLoop),音訊的「進 SCO」與「出 SCO」各由
# 一支獨立單向 process 搬運,徹底避開單 process 雙向開同一 SCO。
#
# 音訊路由(全程 mSBC 16kHz S16_LE mono,無重取樣):
#   上行 你→對方: baresip auplay→hw:PBLoop,0,0 ⟶loop⟶ hw:PBLoop,1,0 →(本橋)→ bluealsa SCO sink
#   下行 對方→你: bluealsa SCO source →(本橋)→ hw:PBLoop,1,1 ⟶loop⟶ hw:PBLoop,0,1 ←baresip ausrc
#
# 只在通話(SCO PCM 存在)時搬運;SCO 不在時 arecord/aplay 會失敗 → 退避重試。
set -u
MAC=A0:B0:BD:1A:BD:C7
SCO_SOURCE="bluealsa:DEV=$MAC,PROFILE=sco"   # 對方聲音(capture)
SCO_SINK="bluealsa:DEV=$MAC,PROFILE=sco"     # 你的聲音(playback)
LOOP_UP_RD="hw:PBLoop,1,0"    # 讀:baresip 寫到 0,0 的 → 本面讀出 → 送進 SCO sink
LOOP_DN_WR="hw:PBLoop,1,1"    # 寫:SCO source 讀到的 → 寫到此 → baresip 從 0,1 讀
RATE=16000; FMT=S16_LE; CH=1

log(){ echo "[sco-bridge] $*"; }

uplink(){   # baresip→手機
  while :; do
    arecord -D "$LOOP_UP_RD" -f "$FMT" -r "$RATE" -c "$CH" -q 2>/dev/null \
      | aplay -D "$SCO_SINK" -f "$FMT" -r "$RATE" -c "$CH" -q 2>/dev/null
    sleep 0.5
  done
}
downlink(){ # 手機→baresip
  while :; do
    arecord -D "$SCO_SOURCE" -f "$FMT" -r "$RATE" -c "$CH" -q 2>/dev/null \
      | aplay -D "$LOOP_DN_WR" -f "$FMT" -r "$RATE" -c "$CH" -q 2>/dev/null
    sleep 0.5
  done
}

trap 'kill 0' EXIT INT TERM
log "start (SCO=$MAC, loop=PBLoop, $RATE Hz mSBC)"
uplink &
downlink &
wait
