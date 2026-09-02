"""Captured real panes, classified by the real function."""
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import app

EXPECT = {
    "patentdrafting": "idle",   # prose "Still running:" must not read as a status
    "uspto":          "busy",   # real spinner + esc to interrupt
    "autoobservation":"idle",
    "mk20":           "idle",
    "stale_spinner":  "idle",   # leftover spinner, no esc to interrupt
    "live_spinner":   "busy",   # same spinner, with esc to interrupt
}
fail = 0
for name, expect in EXPECT.items():
    text = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "panes", name + ".txt"), encoding="utf8", errors="replace").read()
    app._pane_stability.pop(name, None)
    # three polls of an unchanging pane: past the 20s staleness threshold
    for i in range(3):
        if i: time.sleep(11)
        got = app._classify_pane(name, text, "bash" if name == "mk20" else "claude")
    ok = got["status"] == expect
    fail += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:16s} expected={expect:5s} got={got['status']:5s} {got['detail']}")
print("  failures:", fail)
raise SystemExit(1 if fail else 0)
