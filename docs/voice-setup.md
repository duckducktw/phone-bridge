# 語音子系統完整設定（BlueALSA + oFono HFP）

目標：讓 Linux 電腦當手機的藍牙免持單元（HF），接管通話音訊（SCO）與撥號控制，不需 root。

實測環境：Ubuntu（PipeWire 1.6.2 / WirePlumber 0.5.13）、BlueZ 5.85、Samsung Android 16、oFono 2.18、bluez-alsa-utils 4.3.1。

## 1. 安裝

```bash
sudo apt-get install -y ofono bluez-alsa-utils
sudo systemctl enable --now ofono
```

## 2. BlueALSA 加 hfp-ofono profile

預設 bluez-alsa 只開 a2dp。用 systemd drop-in 加 `hfp-ofono`：

```bash
sudo mkdir -p /etc/systemd/system/bluealsa.service.d
sudo tee /etc/systemd/system/bluealsa.service.d/override.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa -S -p a2dp-source -p a2dp-sink -p hfp-ofono
EOF
```

把你的使用者加進 `audio` 群組（存取 BlueALSA 的 DBus 介面；需重新登入或用 `sg audio`）：
```bash
sudo usermod -aG audio $USER
```

## 3. 停用 PipeWire / WirePlumber 的藍牙掌控

PipeWire 的 native HFP backend 會跟 BlueALSA 搶 BlueZ 的 HFP profile UUID，而且它自己抓不到可錄製的 SCO source。讓 BlueALSA 獨佔藍牙：

`~/.config/wireplumber/wireplumber.conf.d/53-disable-bluez.conf`：
```
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
    monitor.bluez-midi = disabled
  }
}
```

（若你還想用 PipeWire 放 A2DP 音樂，可改成只讓 BlueALSA 管 HFP、PipeWire 管 A2DP，但最省事是全交給 BlueALSA。）

## 4. 啟動順序（重要）

```bash
sudo systemctl daemon-reload
sudo systemctl restart ofono
sudo systemctl restart bluealsa
systemctl --user restart wireplumber   # 載入停用 bluez 的設定
```

然後**重連手機**（配對需先完成）：
```bash
bluetoothctl connect <PHONE_MAC>
```

## 5. 手機端

- 先完成藍牙配對（電腦 discoverable + pairable，手機確認 passkey）。
- 手機藍牙 → 該電腦裝置設定 → 把「通話音訊 / Calls」開關打開。
- 通話中，把音訊輸出切到電腦（通話畫面選藍牙裝置）。

## 6. 驗證

```bash
# oFono 看到 modem 且 Online
dbus-send --system --print-reply --dest=org.ofono \
  /hfp/org/bluez/hci0/dev_<MAC底線> org.ofono.Modem.GetProperties | grep -A1 Online

# HF audio card（SCO）
dbus-send --system --print-reply --dest=org.ofono / \
  org.ofono.HandsfreeAudioManager.GetCards

# BlueALSA HFP PCM（通話中才完整）
bluealsa-cli list-pcms   # 應看到 hfphf/source 與 hfphf/sink
```

成功的 oFono log 會出現：
```
plugins/hfp_hf_bluez5.c:profile_new_connection()
src/modem.c:modem_change_state() ... new state: 3
Service level connection established
```

## 7. 錄製 / 轉發通話音訊

```bash
# 對方的聲音（下行）
arecord -D bluealsa:DEV=<MAC>,PROFILE=sco -f S16_LE -r 16000 -c 1 out.wav
# 你的麥克風（上行）送進通話
aplay   -D bluealsa:DEV=<MAC>,PROFILE=sco in.wav
```

SCO 是 8kHz(CVSD) 或 16kHz(mSBC/LC3) 窄/寬頻。通話結束 SCO 關閉，`arecord` 會報 PCM 不可用，屬正常。

## 8. 撥號控制（不需 adb）

oFono 的 VoiceCallManager 介面可直接撥號/接聽/掛斷：
```bash
# 撥號
dbus-send --system --print-reply --dest=org.ofono \
  /hfp/org/bluez/hci0/dev_<MAC底線> \
  org.ofono.VoiceCallManager.Dial string:"0912345678" string:"default"
```

## 疑難排解

| 症狀 | 原因 / 解法 |
|---|---|
| `arecord ... Resource temporarily unavailable` | 未通話，SCO 未建立。通話中才有。 |
| `UUID already registered in BlueZ` | PipeWire 還抓著藍牙 → 確認步驟 3 的停用設定生效並重啟 wireplumber |
| oFono modem `Powered` SetProperty `Operation failed` / `ProfileUnavailable` | HFP SLC 沒建起來 → 照步驟 4 的 ofono→bluealsa→重連順序；確認手機 Calls 開關開 |
| 聽得到聲音但錄不到 | 多半還在 PipeWire native backend → 改用 BlueALSA |
| oFono 載入 hfp_hf 後立刻 unregister | backend 衝突或順序錯 → 重跑步驟 4 |
