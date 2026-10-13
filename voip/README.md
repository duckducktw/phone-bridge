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
| `render-config.sh` | 由範本 + `.sip-secrets.env` 生成 `pjsip.conf` |
| `.sip-secrets.env` | SIP 密碼（**gitignore**，不進 repo） |

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
