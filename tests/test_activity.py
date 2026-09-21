"""Captured real panes, classified by the real function."""
import sys, time
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
import app


#  A SCRIPT, NOT A PYTEST MODULE, despite the name. It sleeps 11 seconds between
#  polls on purpose (the staleness threshold is 20s) and it ends in SystemExit, so
#  collecting it as a test blows the whole run up with an INTERNALERROR before any
#  other file is reached. Behind a __main__ guard it still runs as
#  `python tests/test_activity.py` and no longer takes the suite with it.
def main() -> int:
    EXPECT = {
        "patentdrafting": "idle",   # prose "Still running:" must not read as a status
        "uspto":          "busy",   # real spinner + esc to interrupt
        "autoobservation":"idle",
        "mk20":           "idle",
        "stale_spinner":  "idle",   # leftover spinner, no esc to interrupt
        "live_spinner":   "busy",   # same spinner, with esc to interrupt
        #  The pill that would not go out: a finished turn, and under it a hint bar
        #  listing "esc to interrupt" plus a background-agent list whose timers tick,
        #  so the pane never sits still and no staleness wait could ever free it.
        "turn_done_agent_list": "idle",
    }
    #  A pane held byte-identical across all three polls IS a stale spinner, so a
    #  fixture meant to be LIVE has its stability entry dropped before every read: a
    #  real animating spinner never looks the same twice. uspto and live_spinner need
    #  that now the key hint bar no longer counts as a busy signal, because their text
    #  is stale_spinner's text plus that bar, and from one snapshot nothing else tells
    #  a running turn from a leftover frame.
    LIVE_PANE = {"uspto", "live_spinner", "turn_done_agent_list"}
    fail = 0
    for name, expect in EXPECT.items():
        text = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "panes", name + ".txt"), encoding="utf8", errors="replace").read()
        app._pane_stability.pop(name, None)
        # three polls of an unchanging pane: past the 20s staleness threshold
        for i in range(3):
            if i: time.sleep(11)
            if name in LIVE_PANE: app._pane_stability.pop(name, None)
            got = app._classify_pane(name, text, "bash" if name == "mk20" else "claude")
        ok = got["status"] == expect
        fail += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:16s} expected={expect:5s} got={got['status']:5s} {got['detail']}")
    print("  failures:", fail)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
