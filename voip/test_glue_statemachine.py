#!/usr/bin/env python3
"""狀態機單元測試:驗證 bridge-glue 的來電雙響搶接 + 反向掛斷邏輯(不碰真實連線)。
以 monkeypatch 取代 baresip_hangup/ofono_hangup_all/ofono_answer/baresip_active_calls,
記錄呼叫序列,斷言 C2 競態等情境行為正確。"""
import time
import importlib.util, sys, types, time

# 載入 bridge-glue 模組(不執行 main)
spec = importlib.util.spec_from_file_location("glue", "/home/user/projects/phone-bridge/voip/bridge-glue.py")
glue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glue)

# ---- monkeypatch 外部副作用,記錄呼叫 ----
calls_log = []
glue.baresip_hangup   = lambda: calls_log.append("baresip_hangup")
glue.ofono_hangup_all = lambda: calls_log.append("ofono_hangup_all")
glue.ofono_answer     = lambda: calls_log.append("ofono_answer")
glue.ofono_dial       = lambda n: calls_log.append(f"ofono_dial:{n}")
# baresip_active_calls 由各測試視情況設定

def reset():
    calls_log.clear()
    with glue._slock:
        glue._state["ringing"] = False
        glue._state["answered_mobile"] = False
        glue._state["expect_until"] = 0.0
        glue._state["need_reconcile"] = False

fails = 0
def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond: fails += 1

# ===== 情境 1:C2 核心 — 手機來電,使用者在「實體手機」上先接 =====
# 流程:來電 ringing → 手機端先接(我方設 expect_sip_close + 掛 iPhone 響鈴腿) →
#       baresip 回 CALL_CLOSED → 必須「不」觸發 ofono_hangup_all(否則掛掉剛接的通話)
print("情境1:手機端先接(C2 競態) — CALL_CLOSED 不可掛手機")
reset()
with glue._slock:
    glue._state["ringing"] = True          # 來電雙響中
# 模擬輪詢偵測「手機端先接」:設 expect_sip_close + 掛 iPhone 腿(鎖內決策)
with glue._slock:
    glue._state["ringing"] = False
    glue._state["expect_until"] = time.time() + glue.EXPECT_WINDOW
glue.baresip_hangup()
# baresip 隨後回 CALL_CLOSED
glue.handle_baresip_event({"event": True, "type": "CALL_CLOSED", "class": "call", "param": "normal"})
check("CALL_CLOSED 後沒有 ofono_hangup_all(沒掛掉手機通話)", "ofono_hangup_all" not in calls_log)
check("expect_sip_close 已清旗標", glue._state["expect_until"] == 0.0)

# ===== 情境 2:iPhone 掛斷(正常去電結束)→ 必須掛手機 =====
print("情境2:iPhone 掛斷 → 要掛手機")
reset()
# 無 ringing、無 expect_sip_close:純 iPhone 主動掛
glue.handle_baresip_event({"event": True, "type": "CALL_CLOSED", "class": "call", "param": "normal"})
check("CALL_CLOSED 觸發 ofono_hangup_all", "ofono_hangup_all" in calls_log)

# ===== 情境 3:iPhone 先接來電 → Answer 手機 =====
print("情境3:iPhone 先接來電 → Answer 手機並橋接")
reset()
with glue._slock:
    glue._state["ringing"] = True
glue.handle_baresip_event({"event": True, "type": "CALL_RTPESTAB", "class": "call", "param": "audio"})
check("iPhone 先接 → ofono_answer", "ofono_answer" in calls_log)
check("answered_mobile 設 True", glue._state["answered_mobile"] is True)
check("ringing 清除", glue._state["ringing"] is False)

# ===== 情境 4:iPhone 拒接/響鈴逾時(ringing 中 CALL_CLOSED)→ 保留手機來電,不掛 =====
print("情境4:iPhone 拒接 → 保留手機來電(不掛手機)")
reset()
with glue._slock:
    glue._state["ringing"] = True
glue.handle_baresip_event({"event": True, "type": "CALL_CLOSED", "class": "call", "param": "timeout"})
check("ringing 中 CALL_CLOSED 不掛手機", "ofono_hangup_all" not in calls_log)

# ===== 情境 5:待撥號碼佇列 TTL + 關聯(M6)=====
print("情境5:待撥號碼佇列 TTL")
reset()
glue.pending_put("123")
check("剛放入可取出", glue.pending_get() == "123")
check("取出後佇列空", glue.pending_get() is None)
glue.pending_put("456")
# 模擬逾時
import collections
with glue._plock:
    # 把時間戳改成過期
    q = glue._pending
    if q:
        num, ts = q[0]
        q[0] = (num, ts - glue.PENDING_TTL - 1)
check("逾時號碼不回傳", glue.pending_get() is None)

# ===== 情境 6:M-B 旗標衛生 — 遺失 CALL_CLOSED 後,下一通去電必須掛手機 =====
# 模擬:expect_until 被設成未來(上一通我方掛 iPhone 腿,但那則 CALL_CLOSED 遺失 → 黏滯)。
# 新去電 CALL_INCOMING 必須重置 expect_until → 這通結束的 CALL_CLOSED 才會正確掛手機。
print("情境6:M-B 遺失 CALL_CLOSED → 去電重置旗標 → 結束時掛手機(防持續計費)")
reset()
with glue._slock:
    glue._state["expect_until"] = time.time() + 999   # 黏滯(模擬遺失 CLOSED)
glue.pending_put("123")
glue.handle_baresip_event({"event": True, "type": "CALL_INCOMING", "class": "call", "param": ""})
check("去電 CALL_INCOMING 重置了黏滯的 expect_until", glue._state["expect_until"] == 0.0)
# 這通去電結束
glue.handle_baresip_event({"event": True, "type": "CALL_CLOSED", "class": "call", "param": "normal"})
check("去電結束 CALL_CLOSED 正確掛手機(未被黏滯旗標誤判為預期)", "ofono_hangup_all" in calls_log)

# ===== 情境 7:expect_until 時效 — 逾時的預期視窗不再擋掛機 =====
print("情境7:expect_until 逾時 → CALL_CLOSED 視為真掛,掛手機")
reset()
with glue._slock:
    glue._state["expect_until"] = time.time() - 1   # 已過期
glue.handle_baresip_event({"event": True, "type": "CALL_CLOSED", "class": "call", "param": "normal"})
check("過期 expect_until 不擋掛機", "ofono_hangup_all" in calls_log)

print()
print(f"=== 結果:{'全過 ✓' if fails == 0 else str(fails) + ' 個 FAIL ✗'} ===")
sys.exit(1 if fails else 0)
