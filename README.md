# phone-bridge

把一支留在家、插著 SIM 的 Android 手機，變成**簡訊 + 通話的網路閘道**，讓你在外用其他裝置（iPhone / 電腦）以**原門號**收發簡訊、接打電話。純軟體為主，不需越獄、不需 root。

兩條獨立子系統：

| 子系統 | 通道 | 說明 |
|---|---|---|
| **簡訊** | httpSMS app → webhook → Discord | Discord 頻道打 `電話:內容` 發簡訊；收到簡訊自動貼回頻道 |
| **語音** | 藍牙 HFP (BlueALSA + oFono) → 電腦 | 電腦當手機的藍牙免持單元，接管通話音訊（可錄製/轉發）、控制撥號 |

> ⚠️ 實測環境：Ubuntu（PipeWire 1.6 / WirePlumber 0.5）+ Samsung Android 16 + 中華電信。其他組合參數可能要調。

---

## 簡訊子系統

### 架構
```
發簡訊：Discord #簡訊 打 `0912345678:內容`
        → outbound.py (REST 輪詢) → httpSMS 雲端 → 手機發出
收簡訊：手機收到 → httpSMS webhook → inbound.py → Discord #簡訊
```

### 為什麼出站用 REST 輪詢而非 Gateway WebSocket
同一個 Discord bot token 只允許一條 Gateway 連線。若主程式已用該 token 連 Gateway，再開第二條會把前者踢下線。REST 輪詢不佔 Gateway session，零衝突。輪詢間隔幾秒對簡訊完全可接受。

### 設定
1. 手機裝 [httpSMS](https://httpsms.com) app，登入取得 API key
2. 複製 `config.env.example` → `config.env`，填 Discord 頻道/webhook + httpSMS key/門號
3. 出站：`python3 sms/outbound.py`（建議做成 systemd 服務常駐）
4. 入站：`python3 sms/inbound.py`（監聽 127.0.0.1:8099），用 Cloudflare Tunnel 之類給它公開 URL，在 httpSMS 註冊 webhook 指向 `https://<你的網域>/inbound`

格式支援：`0912345678:hi`（09 自動轉 +886）、`+886912...:hi`、`166:短碼`、全形 `：` 皆可。

---

## 語音子系統（重點：如何讓電腦真的收到手機通話音訊）

### 核心原理
手機通話音訊被 Android/手機 OEM 層層保護，**非 root 無法直接截取**。唯一合法的導出管道是**藍牙 HFP（Hands-Free Profile）**——手機把電腦當成藍牙免持耳機（AG↔HF），通話音訊（SCO）就合法流到電腦。

### ✅ 成功配方（踩了很多坑才找到）
關鍵是用 **BlueALSA + oFono**，**不是** PipeWire 內建的 native HFP backend。

```
手機(AG) ──藍牙HFP──> 電腦
                       ├─ oFono (hfp_hf_bluez5): HF 角色 + 通話控制(撥號/接聽)
                       ├─ BlueALSA (-p hfp-ofono): SCO 音訊 PCM
                       └─ 停用 PipeWire 的 bluez monitor（避免搶 HFP）
```

安裝與設定見 [`docs/voice-setup.md`](docs/voice-setup.md)。簡述：
1. `sudo apt install ofono bluez-alsa-utils`
2. BlueALSA 啟動加 `-p hfp-ofono`（drop-in override，見 `scripts/setup_bluealsa_hfp.sh`）
3. 停用 PipeWire/WirePlumber 的藍牙掌控（`scripts/` 的 WirePlumber conf）
4. 啟動順序：**oFono → BlueALSA → 重連手機**
5. 手機藍牙設定把電腦的「通話音訊(Calls)」開關打開

### 驗證音訊通道
```bash
export PHONE_MAC=你手機藍牙MAC
# 通話中錄 hfphf/source（對方的聲音）
arecord -D bluealsa:DEV=$PHONE_MAC,PROFILE=sco -f S16_LE -r 16000 -c 1 out.wav
```
通話中會出現 PCM：`hfphf/source`（對方→你，可錄）、`hfphf/sink`（你的麥克風→對方）。

### ⚠️ 踩坑筆記
- **PipeWire native HFP backend 抓不到可錄製的 source** → 通話聲會被 PipeWire 直接 route 到喇叭（聽得到），但沒有可捕捉的音訊節點。換 BlueALSA + oFono 才解決。
- **兩個 HFP backend 會搶 BlueZ profile UUID**（`UUID already registered`）→ 必須停用 PipeWire 的 bluez monitor，讓 BlueALSA 獨佔 HFP。
- **SCO 只在通話中存在** → 未通話時 `arecord` 開 PCM 回 `Resource temporarily unavailable` 屬正常。
- **手機端通話音訊預設不路由到藍牙** → 通話畫面要手動把音訊輸出切到電腦（或 Android 通話畫面選藍牙裝置）。
- **oFono 的 HFP HF plugin（hfp_hf_bluez5）** 載入後若立刻 unregister，多半是 backend 衝突或啟動順序不對 → 照「oFono→BlueALSA→重連」順序。

---

## 下一步（規劃中）
- 語音接 SIP（Asterisk / baresip），經 Tailscale 轉到外出裝置的 softphone，保留原號來電顯示（參考 oFono + SIP 橋接做法）
- systemd 持久化全套服務

## 檔案
- `sms/outbound.py` — Discord→簡訊 出站輪詢
- `sms/inbound.py` — 簡訊→Discord 入站 webhook 接收（純 stdlib HTTP server）
- `scripts/setup_bluealsa_hfp.sh` — BlueALSA 加 hfp-ofono profile 的 drop-in
- `scripts/bt_conn.py` — 藍牙連線小工具
- `scripts/rec_sco.sh` — 通話中錄 SCO 音訊
- `config.env.example` — 設定範本

## License
MIT
