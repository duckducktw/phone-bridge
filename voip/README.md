# VoIP 子系統（SIP ⇄ HFP 橋）

讓外出的 iPhone 經 Tailscale 用家裡手機的門號收發通話。SIP 側（Asterisk + baresip）接到既有的 BlueALSA/oFono HFP 音訊通道（見 `../docs/voice-setup.md`）。

## 架構

```
iPhone(SIP 6002) ──Tailscale──> Asterisk(docker, host network, :5060)
                                    │  撥 9+號碼 → System(curl) 告知膠水目標號碼 → Dial PJSIP/6001
                                    ↓
                             baresip(6001, SIP :5070, ctrl_tcp :4444, 音訊=BlueALSA SCO)
                                    │
                             bridge-glue.py (HTTP :4455 收號碼 + ctrl_tcp 事件 + oFono 撥號/掛斷)
                                    ↓ 藍牙 HFP
                             Android 手機(中華 SIM) —— 用原門號實際撥出/接聽
```

- **去電**：iPhone 撥 `9+號碼` → Asterisk 先 curl 膠水 HTTP(:4455) 告知目標號碼 → `Dial(PJSIP/6001)` → baresip CALL_INCOMING → 膠水 `accept` 並叫 oFono 用手機撥該號碼 → 手機接通後 SCO 音訊橋到 SIP。
- **來電**：手機來電 → oFono CallAdded(incoming) → 膠水叫 baresip `dial mobile-in@asterisk` → 響 iPhone。
- **掛斷同步**：iPhone 掛斷 → baresip CALL_CLOSED → 膠水 `ofono_hangup_all()` 掛手機（見下方 bug 註記）。

## 檔案

| 檔 | 作用 |
|---|---|
| `docker-compose.yml` | Asterisk 容器（host network） |
| `asterisk/pjsip.conf.template` | SIP 分機範本（密碼用佔位符，**進 repo**） |
| `asterisk/pjsip.conf` | 渲染後含明文密碼的真檔（**gitignore**，由腳本生成） |
| `asterisk/extensions.conf` | 撥號計畫（撥 9 觸發膠水、mobile-in 響 iPhone） |
| `bridge-glue.py` | SIP↔oFono 膠水 |
| `sco-bridge.sh` | SCO↔snd-aloop 音訊橋（解 baresip full-duplex 雷，見下） |
| `render-config.sh` | 由範本 + `.sip-secrets.env` 生成 `pjsip.conf` |
| `.sip-secrets.env` | SIP 密碼（**gitignore**，不進 repo） |

## 音訊橋接：為何不能讓 baresip 直開 BlueALSA SCO

**症狀**：iPhone 聽得到對方、但對方聽不到你（上行啞），且 iPhone 有迴音。

**真因**：baresip 單一 process 同時雙向開「同一個」`bluealsa:DEV=...,PROFILE=sco` device，會踩 BlueALSA SCO 的 full-duplex 雷——playback（你→對方）啞掉，**且無任何錯誤訊息**（見 bluez-alsa#333，有人回報一字不差的症狀）。baresip log 只會看到 `stream: audio: dropping N bytes ... (Operation already in progress)`，那是 RTP 層因 player 寫不進 SCO 而丟棄上行音訊。

**解法（已實測 full-duplex 雙向通）**：插一層 `snd-aloop` kernel loopback，讓 baresip 只開穩定的 loopback device，SCO 的讀/寫各由一支**獨立單向 process** 搬運（兩個獨立 process 各開一向不會踩雷）：

```
上行 你→對方: baresip auplay→hw:PBLoop,0,0 ⟶loop⟶ hw:PBLoop,1,0 →arecord|aplay→ bluealsa SCO sink
下行 對方→你: bluealsa SCO source →arecord|aplay→ hw:PBLoop,1,1 ⟶loop⟶ hw:PBLoop,0,1 ←baresip ausrc
```

SCO 實際走 **mSBC 16kHz S16_LE mono**，全程不需重取樣。`sco-bridge.sh` 實作這兩支 mover（systemd user service `phone-sco-bridge`，SCO 不在時退避重試）。

### 系統設定（repo 外，一次性）
```bash
# 1. snd-aloop 固定 index + 名稱(供 baresip/bridge 用 hw:PBLoop 穩定存取)
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf
printf 'options snd-aloop index=10 id=PBLoop pcm_substreams=2\n' | sudo tee /etc/modprobe.d/snd-aloop.conf
sudo modprobe -r snd-aloop; sudo modprobe snd-aloop

# 2. 讓 WirePlumber 忽略 loopback 卡(否則 PipeWire 當普通音效卡抓走,持 handle 擋重載且與 hw: 直接存取衝突)
#    檔: ~/.config/wireplumber/wireplumber.conf.d/54-disable-loopback.conf
#    規則: device.name ~ alsa_card.platform-snd_aloop.* → device.disabled = true
systemctl --user restart wireplumber
```

### baresip config（`~/.baresip/config`）
```
audio_player   alsa,pb_play
audio_source   alsa,pb_cap
```

## 上行斷、對方聽不到你（Intel AX201 mSBC-over-USB 坑）

**症狀**：iPhone 聽得到對方（下行 OK），但對方聽不到你（上行斷）。對方喇叭懟著聽「有震但人耳難辨」。

**真因**：手機協商 **mSBC 寬頻語音**，但 **Intel AX201 + btusb 對 mSBC transparent SCO over USB 傳輸有 bug** → `dmesg` 狂噴 `corrupted SCO packet` + `hciconfig` TX errors 暴增 → 上行音訊封包損毀。bluez-alsa 維護者 arkq 證實：「mSBC over USB 上行串流本就不可靠」。這是硬體/驅動層，不是音訊管線。

**定位法**（硬證據，不靠猜）：
- `hciconfig hci0` 的 `sco:N` = SCO TX 封包計數。送音訊到 SCO sink 時 N 應增長 → 確認音訊有無飛向手機。
- `dmesg | grep "corrupted SCO"` → mSBC-over-USB 損毀鐵證。
- 攔截 `hw:PBLoop,1,0`（baresip 送往 SCO 前）錄音算 RMS → 確認你的聲音有無到 loopback。

**修法：強制 CVSD**（窄頻 8kHz，走標準 SCO、不需 transparent air mode，Intel AX201 穩定，corrupted=0 實測）：
```bash
# 1. bluealsa 停用 mSBC/LC3（override 已在 systemd/bluealsa-override.conf）
#    ExecStart=... -p hfp-ofono --codec=-mSBC --codec=-LC3-SWB
# 2. btmgmt 關 WBS（開機持久化 → systemd/phone-wbs-off.service, enable 到 bluetooth.target）
#    btmgmt power off; btmgmt wbs off; btmgmt power on
# 3. sco-bridge.sh 的 alsaloop 加 -n（alsa-lib resample）處理 loopback 16k ↔ SCO 8k
sudo systemctl restart bluealsa; sudo systemctl restart ofono; bluetoothctl connect <MAC>
```
注意：`btmgmt wbs off` 對 oFono 2.18 的 codec 協商無效（oFono 自己宣告 mSBC），**真正降 CVSD 靠 bluealsa `--codec=-mSBC`**。
注意：alsaloop **不能**用 `plug:bluealsa:DEV=...` 前綴（ALSA 無法 parse 帶參數的 `plug:` device），要用 `-n`。

## 首次部署

```bash
cd voip
# 1. 建密碼檔（值用雙引號包，可含特殊字元）
cat > .sip-secrets.env <<'EOF'
SIP_BARESIP_PASS="你的baresip密碼"
SIP_IPHONE_PASS="你的iPhone密碼"
EOF
chmod 600 .sip-secrets.env

# 2. 渲染 pjsip.conf（含明文密碼、chmod 600、gitignore）
bash render-config.sh

# 3. 起 Asterisk
docker compose up -d
```

baresip（分機 6001）由 user systemd service `phone-baresip.service` 管理；膠水由 `phone-bridge-glue.service` 管理，兩者 `Restart=always`。改膠水後：`systemctl --user restart phone-bridge-glue`。

## iPhone 端（Linphone）

- Username=`6002`、Domain=`<Tailscale IP>`、Transport=`UDP`
- **關閉 STUN/ICE**（避免 RTP 走非直連 candidate 導致音訊收不到 → 固定數秒後 `Connection reset by peer` 早掛）

## 坑

- **docker bind mount inode**：用編輯器改 `pjsip.conf` 會換 inode，容器讀到舊檔 → 必須 `docker compose restart asterisk`，`dialplan reload` 不夠。
- **改密碼流程**：編輯 `.sip-secrets.env` → `bash render-config.sh` → `docker compose restart asterisk`。
- **oFono HangupAll 必須帶 `--print-reply`**：`dbus-send` 不帶 `--print-reply` 是 fire-and-forget，process 會在訊息送達 oFono 前退出 → exit=0 但手機沒掛（掛斷不同步 bug 真因）。見 `bridge-glue.py` `ofono_hangup_all()`。
- **別手動背景跑膠水/baresip**：已轉 systemd user service，手動跑會衝突。
