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
        "turn_done":      "idle",   # "✻ Cooked for 10m 38s · done 6:34 PM"
        "turn_done_then_busy": "busy",  # that line scrolled up, a NEW turn running
        "compacting":     "busy",   # its own detail, and its own colour in the UI
        "compacting_in_prose": "idle",  # an agent WRITING the word is not a state
        "subagent_wait":  "busy",   # live `*` spinner frame, a glyph this scan missed
        "running_tool_only": "busy",  # no spinner at all, only "⎿  Running…"
        # The pill that would not go out. A finished turn, and under it a hint bar
        # listing "esc to interrupt" and a background-agent list whose timers tick,
        # so the pane never sits byte-identical and the staleness escape hatch could
        # never fire. Captured from this box, transcript rows and chrome both.
        "turn_done_agent_list": "idle",
    }
    # The detail is what the pill paints, so a state is only really reported if
    # this matches too. "Compacting" is the one the UI gives its own bar.
    DETAIL = {"compacting": "Compacting", "running_tool_only": "Waiting on a tool",
              "subagent_wait": "Working", "uspto": "Working", "live_spinner": "Working"}
    # turn_done is what lets detect_activity settle busy → idle without waiting on
    # the transcript, which keeps growing for a minute or two after the pane is
    # done. It must be set on the finished pane and NOT on the one where the same
    # completion line is merely scrollback above a live turn.
    TURN_DONE = {"turn_done": True, "turn_done_then_busy": False,
                 "turn_done_agent_list": True,
                 "patentdrafting": True, "autoobservation": True,
                 # "● Done. The redraft is in and the checks pass." is the agent's
                 # own prose, not Claude Code's end-of-turn line. It must not count.
                 "stale_spinner": False, "live_spinner": False, "uspto": False}
    # Fixtures whose point is that the pane is ALIVE. The three-poll loop below
    # holds one unchanging string, which is how a stale spinner is tested, so for
    # these the stability entry is dropped before each read: a real animating
    # spinner never looks the same twice and must not be gated off as leftover.
    #
    # uspto and live_spinner joined the set when the key hint bar stopped counting
    # as a busy signal. Their pane text is the same as stale_spinner's apart from
    # that bar, so after the change the ONLY thing separating a live spinner from a
    # leftover one is whether the pane repaints, which is the real difference and
    # the one a snapshot cannot see. turn_done_agent_list is here for the opposite
    # reason: it is a repainting pane that must still read idle, because what is
    # moving is a background-agent timer and not the turn.
    LIVE_PANE = {"subagent_wait", "running_tool_only", "uspto", "live_spinner",
                 "turn_done_agent_list"}
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
        if name in TURN_DONE:
            ok = ok and bool(got.get("turn_done")) == TURN_DONE[name]
        if name in DETAIL:
            ok = ok and got.get("detail") == DETAIL[name]
        fail += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:20s} expected={expect:5s} got={got['status']:5s}"
              f" turn_done={bool(got.get('turn_done'))} {got['detail']}")
    print("  failures:", fail)
    return 1 if fail else 0


def hysteresis() -> int:
    """detect_activity, with the pane and the transcript clock both faked.

    The case that put a finished session on a pulsing red 'Working' pill: the
    transcript goes on growing for a minute or two after the turn ends (the Stop
    hook's result, then the session summary), and the quiet gate read that as
    'still going'. turn_done is the pane saying otherwise, and it has to win.
    """
    cases = [
        # (label, raw status, turn_done, transcript quiet secs, expected)
        ("busy → idle, turn_done, transcript still being written", "idle", True,  0.4, "idle"),
        ("busy → idle, no turn_done, transcript still being written", "idle", False, 0.4, "busy"),
        ("busy → idle, no turn_done, transcript quiet", "idle", False, 99.0, "busy"),  # needs the count too
        ("still busy", "busy", False, 0.4, "busy"),
    ]
    real_raw, real_quiet = app._detect_activity_raw, app._transcript_quiet_seconds
    fail = 0
    try:
        for label, status, done, quiet, expect in cases:
            name = "hyst_" + str(abs(hash(label)))
            app._activity_state[name] = {"status": "busy", "since": time.time() - 60,
                                         "consecutive_idle": 0, "idle_since": 0,
                                         "raw": {"status": "busy", "command": "claude", "detail": "Working"}}
            app._detect_activity_raw = lambda n, s=status, d=done: {
                "status": s, "command": "claude", "detail": "", "turn_done": d}
            app._transcript_quiet_seconds = lambda n, q=quiet: q
            got = app.detect_activity(name)["status"]
            ok = got == expect
            fail += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  expected={expect:5s} got={got:5s}  {label}")
            app._activity_state.pop(name, None)
    finally:
        app._detect_activity_raw, app._transcript_quiet_seconds = real_raw, real_quiet
    print("  failures:", fail)
    return 1 if fail else 0


def esc_stale() -> int:
    """The end-of-turn line under a KEY HINT BAR that says "esc to interrupt".

    That bar lists what the keys do and it lists this on a finished pane too, so
    it is not evidence of work and the pane reads idle at any age. It used to take
    ESC_STALE_SECONDS of byte-identical pane to escape, which never arrived on a
    session with a background-agent list ticking at the foot: that is the red pill
    that would not go out. Compaction, the state the wait was protecting, prints
    its own row and is caught before any of this (see the compacting fixture).
    """
    import hashlib, os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panes", "esc_stale.txt")
    text = open(path, encoding="utf8", errors="replace").read()
    digest = hashlib.md5(text.encode()).hexdigest()
    cases = [
        ("repainting, one poll old", 5.0, "idle"),
        ("frozen well past the threshold", app.ESC_STALE_SECONDS + 60, "idle"),
    ]
    fail = 0
    for label, age, expect in cases:
        name = "escstale_" + str(int(age))
        app._pane_stability[name] = (digest, time.time() - age, 9)
        got = app._classify_pane(name, text, "claude")
        ok = got["status"] == expect
        fail += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  expected={expect:5s} got={got['status']:5s}  {label}")
        app._pane_stability.pop(name, None)
    print("  failures:", fail)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main() + hysteresis() + esc_stale())
