import pexpect, time, sys
import os
M = os.environ.get("PHONE_MAC", "XX:XX:XX:XX:XX:XX")  # 你手機藍牙 MAC
c = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=60)
for cmd in ["agent KeyboardDisplay", "default-agent", "connect " + M]:
    c.sendline(cmd); time.sleep(0.4)
end = time.time() + 20
ok = False
while time.time() < end:
    i = c.expect([r"Connection successful", r"Failed to connect", pexpect.TIMEOUT], timeout=6)
    if i == 0:
        ok = True; print("CONNECTED", flush=True); break
    elif i == 1:
        print("CONNECT_FAILED", flush=True); break
c.sendline("quit")
print("done ok=%s" % ok, flush=True)
