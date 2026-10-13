# VoIP 子系統（SIP ⇄ HFP 橋）

讓外出的 iPhone 經 Tailscale 用家裡手機的門號收發通話。SIP 側（Asterisk + baresip）接到既有的 BlueALSA/oFono HFP 音訊通道（見 `../docs/voice-setup.md`）。

## 架構

```
iPhone(SIP 6002) ──Tailscale──> Asterisk(docker, host network)
   對外 transport 只綁 Tailscale IP :5060 + 全域 ACL(只放行 loopback+Tailscale)
                                    │  撥 9+號碼 → FILTER 消毒 → CURL() 告知膠水目標號碼 → Dial PJSIP/6001
                                    ↓  (另有 127.0.0.1:5060 loopback transport 給本機 baresip)
                             baresip(6001, SIP 綁 127.0.0.1:5070, ctrl_tcp :4444, 音訊=snd-aloop)
                                    │  ctrl_tcp 單一持久連線:事件+命令共用(token 配對)
                             bridge-glue.py (HTTP :4455 收號碼 + oFono 撥號/接聽/掛斷)
                                    │  ↕ sco-bridge.sh (snd-aloop ⇄ BlueALSA SCO,兩支 alsaloop)
                                    ↓ 藍牙 HFP (CVSD 8kHz)
                             Android 手機(中華 SIM) —— 用原門號實際撥出/接聽
```

- **去電**：iPhone 撥 `9+號碼` → Asterisk 用 `FILTER(0-9,...)` 消毒號碼 + `CURL()`(不經 shell) 告知膠水 HTTP(:4455) → `Dial(PJSIP/6001)` → baresip CALL_INCOMING → 膠水 `accept` 並叫 oFono 用手機撥該號碼 → 手機接通後 SCO 音訊橋到 SIP。
- **來電（雙響，誰先接誰贏）**：手機來電 → oFono incoming → 膠水叫 baresip `dial mobile-in@asterisk` 讓 iPhone 也響（手機本身天生也響）。
  - iPhone 先接 → baresip CALL_RTPESTAB → 膠水 `ofono_answer()` 接起手機來電並橋接。
  - 手機端先接（直接在實體手機上接）→ oFono call 變 active（非我方 Answer）→ 膠水掛掉 iPhone 響鈴腿。
  - 兩邊都沒接 → 來電消失 → 清響鈴 + 掛 iPhone 腿。
- **掛斷雙向同步**：
  - iPhone 掛 → baresip CALL_CLOSED（非預期）→ 膠水 `ofono_hangup_all()` 掛手機。
  - 手機側掛（對方掛/蜂窩斷/你在手機上掛）→ oFono call 連續 2 輪消失 → 膠水查 baresip 仍有通話 → 掛 iPhone 腿。
  - 「我方主動掛 iPhone 腿」時設 `expect_sip_close` 旗標,隨之而來的 CALL_CLOSED **不**反過來掛手機（避免把剛在手機上接的通話掛掉 — 競態）。

## 檔案

| 檔 | 作用 |
|---|---|
| `docker-compose.yml` | Asterisk 容器（host network） |
| `asterisk/pjsip.conf.template` | SIP 分機範本（密碼/Tailscale IP 用佔位符，**進 repo**） |
| `asterisk/pjsip.conf` | 渲染後含明文密碼的真檔（**gitignore**，由腳本生成） |
| `asterisk/extensions.conf` | 撥號計畫（撥 9 經 FILTER+CURL 觸發膠水、mobile-in 響 iPhone、600 Echo 測試） |
| `bridge-glue.py` | SIP↔oFono 膠水（通話協調狀態機） |
| `sco-bridge.sh` | SCO↔snd-aloop 音訊橋（解 baresip full-duplex 雷，見下） |
| `render-config.sh` | 由範本 + `.sip-secrets.env` 生成 `pjsip.conf`（Python 安全置換 + 自動填 Tailscale IP） |
| `.sip-secrets.env` | SIP 密碼（**gitignore**，不進 repo，值用雙引號包） |
| `asoundrc.example` | baresip 用的固定-16k snd-aloop PCM 定義（放 `~/.asoundrc`） |
| `systemd/` | 四個 unit：`phone-baresip`、`phone-bridge-glue`、`phone-sco-bridge`、`phone-wbs-off` |
| `test_glue_statemachine.py` | 狀態機單元測試（來電搶接 / 反向掛斷 / 佇列；不碰真實連線） |

## 安全（CC review 後強化）

- **SIP 不暴露**：`pjsip.conf` 的對外 transport 只 `bind` 到 Tailscale IP（非 `0.0.0.0`），SIP 根本不在實體 LAN/Wi-Fi/公網監聽 → 擋 toll-fraud 掃描。另有一個 `127.0.0.1:5060` loopback transport 給本機 baresip。
- **全域 ACL**：`[acl-sources]`（pjsip `type=acl` 自動全域套用）只放行 `127.0.0.0/8` + `100.64.0.0/10`。注意 baresip 必須綁 `sip_listen 127.0.0.1:5070`，否則來源 IP 會是實體網卡 IP 被 ACL 擋（403 Forbidden）。
- **dialplan 無注入**：號碼先 `FILTER(0-9,...)` 只留數字,再用 `CURL()` function（**不經 `/bin/sh`**），而非 `System(curl ...)`。
- **密碼**：`pjsip.conf.template` 用佔位符進 repo，真檔經 `render-config.sh`（Python 安全置換，密碼含任何特殊字元都不破檔）渲染 + `chmod 600` + gitignore。
- **SMS inbound webhook**：對公網開放,要求 `SMS_INBOUND_SECRET` 驗證（header `X-Webhook-Secret` / query `?secret=` / path `/inbound/<secret>`），未設則 fail-closed（全拒），防偽造簡訊釣魚。

## 音訊橋接：為何不能讓 baresip 直開 BlueALSA SCO

**症狀**：iPhone 聽得到對方、但對方聽不到你（上行啞），且 iPhone 有迴音。

**真因**：baresip 單一 process 同時雙向開「同一個」`bluealsa:DEV=...,PROFILE=sco` device，會踩 BlueALSA SCO 的 full-duplex 雷——playback（你→對方）啞掉，**且無任何錯誤訊息**（見 bluez-alsa#333）。baresip log 只會看到 `stream: audio: dropping N bytes ... (Operation already in progress)`。

**解法（已實測 full-duplex 雙向通）**：插一層 `snd-aloop` kernel loopback，baresip 只開穩定的 loopback device，SCO 的讀/寫各由一支**獨立單向 process**（`alsaloop`，`--sync=auto` 吸收時脈漂移、`-n` 處理 16k↔8k resample）搬運：

```
上行 你→對方: baresip(pb_play)→hw:PBLoop,0,0 ⟶loop⟶ hw:PBLoop,1,0 →alsaloop→ bluealsa SCO sink
下行 對方→你: bluealsa SCO source →alsaloop→ hw:PBLoop,1,1 ⟶loop⟶ hw:PBLoop,0,1 ←baresip(pb_cap)
```

`sco-bridge.sh` 實作這兩支 mover（systemd user service `phone-sco-bridge`，SCO 不在時退避重試）。

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

# 3. baresip 用的固定-16k PCM(避免 SIP codec rate 傳染 loopback;見下方 rate 坑)
cp asoundrc.example ~/.asoundrc
```

### baresip config（`~/.baresip/config`）
```
sip_listen     127.0.0.1:5070       # 必須綁 loopback,否則來源 IP 被 ACL 擋
audio_player   alsa,pb_play         # pb_play/pb_cap 在 ~/.asoundrc,固定 16k
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

### rate 不匹配坑（對方聽到變調 + 迴音）
baresip 1.1.0 alsa 模組用 SIP codec 的 rate 開 ALSA device（PCMU=8000Hz），忽略 `auplay_srate`。但 snd-aloop 兩端 rate 必須一致（mover 16k）→ baresip 寫 8k 被當 16k 讀 → 變調。解法：`~/.asoundrc` 定義固定-16k 的 `pb_play`/`pb_cap`（`plug→rate(16000)→hw:PBLoop`），baresip 送什麼 rate 都 resample 成 16k 進 loopback。

## 首次部署

```bash
cd voip
# 1. 建密碼檔（值用雙引號包，可含特殊字元）
cat > .sip-secrets.env <<'EOF'
SIP_BARESIP_PASS="你的baresip密碼"
SIP_IPHONE_PASS="你的iPhone密碼"
EOF
chmod 600 .sip-secrets.env

# 2. 渲染 pjsip.conf（自動偵測 Tailscale IP 填入 bind；含明文密碼、chmod 600、gitignore）
bash render-config.sh                 # 或 TAILSCALE_IP=100.x.x.x bash render-config.sh 手動指定

# 3. 起 Asterisk
docker compose up -d

# 4. 裝並啟用 systemd unit(user + system)
cp systemd/phone-baresip.service systemd/phone-bridge-glue.service systemd/phone-sco-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now phone-sco-bridge phone-baresip phone-bridge-glue
sudo cp systemd/phone-wbs-off.service /etc/systemd/system/ && sudo systemctl enable phone-wbs-off

# 5. SMS inbound 若對外:設 webhook secret
#    在 ~/sms-bridge/.secrets.env 加 SMS_INBOUND_SECRET="長隨機字串",手機 app webhook 帶 ?secret= 或 header
```

改膠水後：`systemctl --user restart phone-bridge-glue`。改 `pjsip.conf`/`.sip-secrets.env` 後：`bash render-config.sh && docker compose restart asterisk`。

## iPhone 端（Linphone）

- Username=`6002`、Domain=`<本機 Tailscale IP>`、Transport=`UDP`
- **關閉 STUN/ICE**（避免 RTP 走非直連 candidate 導致音訊收不到 → 固定數秒後 `Connection reset by peer` 早掛）

## 測試

```bash
python3 test_glue_statemachine.py     # 狀態機單元測試(10 項:搶接/反向掛斷/競態/佇列)
# Echo 回路測試 baresip 音訊路徑:讓 baresip 撥分機 600(Answer+Echo)
```

## 坑

- **baresip ctrl_tcp 只接受一條連線**：第二條連上會踢掉第一條。故膠水的命令與事件**共用單一持久連線**（`BaresipCtrl`，token 配對命令回應）。別另開連線發命令（會造成 event stream 斷→重連→無限迴圈）。
- **pjsip `type=acl` 是全域**：不需掛 endpoint 就自動套用成全域 SIP 來源過濾。baresip 綁實體網卡時來源 IP 非 loopback 會被擋（403）→ baresip 必須 `sip_listen 127.0.0.1`。
- **docker bind mount inode**：用編輯器改 `pjsip.conf` 會換 inode，容器讀到舊檔 → 必須 `docker compose restart asterisk`，`dialplan reload` 不夠。
- **oFono 掛斷必須帶 `--print-reply`**：`dbus-send` 不帶是 fire-and-forget，process 會在訊息送達 oFono 前退出 → exit=0 但手機沒掛。且 `HangupAll` 只掛得掉 active 通話,dialing/alerting 要對每個 VoiceCall 下 `Hangup`。
- **反向掛斷靠 listcalls 不靠事件旗標**：去電時 baresip ctrl_tcp 不發 CALL_ESTABLISHED/RTPESTAB（只發 CALL_RTCP），故「baresip 是否還有通話」用 `listcalls` 實查，不靠追蹤事件維護的旗標（會漏）。
- **GetCalls debounce**：oFono 通話消失需連續 2 輪確認才判定（防 dbus 單輪抖動誤掛健在通話）。dbus 查詢失敗回 `None`，該輪跳過不當消失。
- **別手動背景跑膠水/baresip**：已轉 systemd user service，手動跑會衝突。
