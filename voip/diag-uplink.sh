#!/usr/bin/env bash
# diag-uplink.sh — 真機通話時攔截上行音訊,定位你的聲音在哪消失。
# 用法:iPhone 撥 9+123(或撥給能聽的人),通話接通後在筆電跑本腳本。
# 它會暫停 uplink alsaloop,改用 arecord 錄下「baresip 送往 SCO 前」的音訊 5 秒。
# 這段期間請對著 iPhone 講話(或哼聲)。錄完自動恢復 bridge。
# 然後分析錄到的 RMS/FFT:有聲音=baresip 有把你的話送出,問題在 SCO;靜音=baresip 沒送。
set -u
echo "[diag] 暫停 uplink alsaloop(手動接管 PBLoop,1,0)..."
# 找 uplink alsaloop PID(capture=PBLoop,1,0)
UP=$(pgrep -f "alsaloop -C hw:PBLoop,1,0" | head -1)
[ -n "$UP" ] && kill -STOP "$UP" 2>/dev/null && echo "[diag] 已暫停 uplink PID=$UP"
sleep 0.3
echo "[diag] >>> 現在請對 iPhone 講話 5 秒 <<<"
timeout 5 arecord -D hw:PBLoop,1,0 -f S16_LE -r 16000 -c 1 /tmp/uplink_capture.wav 2>/dev/null
echo "[diag] 錄完,恢復 uplink..."
[ -n "$UP" ] && kill -CONT "$UP" 2>/dev/null
python3 -c "
import wave,struct,numpy as np
w=wave.open('/tmp/uplink_capture.wav','rb'); sr=w.getframerate()
d=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(float)
if len(d)<800: print('[diag] 錄到太少'); raise SystemExit
rms=float(np.sqrt((d**2).mean())); pk=int(np.abs(d).max())
seg=d[len(d)//4:len(d)//4+8192] if len(d)>12000 else d
sp=np.abs(np.fft.rfft(seg*np.hanning(len(seg)))); fr=np.fft.rfftfreq(len(seg),1/sr)
print(f'[diag] baresip 送往 SCO 前: frames={len(d)} rms={rms:.0f} peak={pk} dominant={fr[np.argmax(sp)]:.0f}Hz')
print('[diag] 判讀: rms>200=你的聲音有進到這裡(baresip→loopback通,問題在SCO段);rms≈0=baresip沒把你的話寫出(問題在SIP→baresip)')
"
