#!/usr/bin/env bash
# sco-bridge.sh — 把 baresip 用的 snd-aloop loopback 與手機 BlueALSA SCO 雙向橋接。
#
# 為何要這層:baresip 單一 process 同時雙向開「同一個」bluealsa SCO device 會踩
# BlueALSA SCO full-duplex 雷(上行啞/對方聽不到,無錯誤 — 見 bluez-alsa#333)。
# 解法:baresip 只開穩定的 snd-aloop(hw:PBLoop),音訊的「進 SCO」與「出 SCO」各由
# 一支獨立單向搬運,徹底避開單 process 雙向開同一 SCO。
#
# 為何用 alsaloop 而非 arecord|aplay:baresip(player/capture) 與本橋是兩個獨立開啟
# snd-aloop 的 client,各有獨立時脈。snd-aloop 要求 playback/capture 嚴格同步,兩個
# 獨立時脈漂移會累積→loopback buffer 塞爆→baresip 寫入被拒(log:wrote 16 of 320)→
# 上行大量丟棄+殘片錯位進 SCO→對方喇叭有震但人耳難辨。alsaloop --sync=auto 用 PCM
# Rate Shift 動態吸收時脈漂移,防 xrun/buffer 爆。
#
# 音訊路由(全程 16kHz S16_LE mono;SCO=mSBC 16k):
#   上行 你→對方: baresip→(pb_play)→hw:PBLoop,0,0 ⟶loop⟶ hw:PBLoop,1,0 →(alsaloop)→ SCO sink
#   下行 對方→你: SCO source →(alsaloop)→ hw:PBLoop,1,1 ⟶loop⟶ hw:PBLoop,0,1 ←(pb_cap)←baresip
#
# 只在通話(SCO PCM 存在)時搬運;SCO 不在時 alsaloop 失敗 → 退避重試。
set -u
MAC="${PHONE_MAC:-A0:B0:BD:1A:BD:C7}"   # 可用環境變數 PHONE_MAC 覆蓋(裝置識別碼不寫死)
# alsaloop 錯誤預設靜音(SCO 不在時每輪都噴會洗版 journald);設 SCO_BRIDGE_DEBUG=1 時
# 把 alsaloop stderr 留到 journald 方便除錯。
ERR=/dev/null
[ -n "${SCO_BRIDGE_DEBUG:-}" ] && ERR=/dev/stderr
# alsaloop 用 -n(alsa-lib resample) 處理 loopback(固定16k) 與 SCO 原生 rate 的轉換。
# 不能用 "plug:bluealsa:DEV=..." 前綴(ALSA 無法 parse 帶參數的 plug: device)。
# SCO codec: CVSD=8kHz(Intel AX201 mSBC over USB 封包損毀,已用 bluealsa --codec=-mSBC
# 強制 CVSD);-n 讓 16k↔8k 自動轉換,未來換 adapter 回 mSBC 16k 一樣通用。
SCO="bluealsa:DEV=$MAC,PROFILE=sco"
RATE=16000; FMT=S16_LE; CH=1   # RATE 指 loopback 端(baresip 要 16k);SCO 端由 -n resample
TLAT=100000   # 目標 latency 100ms(HFP 語音夠用,留餘裕防 xrun)
SYNC=5        # auto:自動選 captshift/playshift 做時脈同步

log(){ echo "[sco-bridge] $*"; }

uplink(){   # baresip→手機: 讀 loopback 1,0 → 寫 SCO sink
  while :; do
    alsaloop -C hw:PBLoop,1,0 -P "$SCO" -r $RATE -n -f $FMT -c $CH -S $SYNC -t $TLAT 2>"$ERR"
    sleep 0.5
  done
}
downlink(){ # 手機→baresip: 讀 SCO source → 寫 loopback 1,1
  while :; do
    alsaloop -C "$SCO" -P hw:PBLoop,1,1 -r $RATE -n -f $FMT -c $CH -S $SYNC -t $TLAT 2>"$ERR"
    sleep 0.5
  done
}

trap 'kill 0' EXIT INT TERM
log "start (SCO=$MAC, loop=PBLoop, loop=$RATE Hz, SCO codec=CVSD, alsaloop -n resample, sync=$SYNC)"
uplink &
downlink &
wait
