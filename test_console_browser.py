"""What the console browser has already got wrong once, pinned so it cannot again.

Everything here was a real defect found by driving the thing rather than reading
it, on 2026-09-05, and every one of them was silent: no exception, no log line,
just a browser that behaved slightly wrongly in a way you would blame on the
website. That is what makes them worth a test.

  · A dot typed into a login form vanished. `ord(".")` is 46 and 46 is the
    virtual key code for Delete, so `nimo@test.com` arrived as `nimo@testcom`.
  · The exit-IP check ran in whatever tab was on screen, and facebook.com's
    Content-Security-Policy refuses the fetch, so a perfectly healthy network was
    reported to the user as "no internet on this exit".
  · Opening a tab used POST. Chrome has wanted PUT on /json/new since 111 and
    answers POST with 405, which surfaced as a 500 from the page.
  · The page is built with str.replace, not %, because it is mostly CSS and every
    `width:100%` in it is a format specifier to the other one.

No network and no Chrome: these read the tables and the source.
Run: python3 -m pytest test_console_browser.py
"""
import re

import console_browser as cb


# --- typing ----------------------------------------------------------------
def test_the_dot_is_not_the_delete_key():
    """The bug, as one assertion. 46 is Delete; the dot is 190 on a US layout."""
    assert cb._printable_vk(".") == 190
    assert cb._printable_vk(".") != ord(".")


def test_no_printable_character_maps_onto_a_named_key():
    """A wrong code fires some OTHER key's behaviour, which is worse than none:
    that is how the dot became a delete."""
    named = {v for k, v in cb._VK.items() if k not in ("Space",)}
    collisions = []
    for ch in "abzABZ019!@#$%^&*()-_=+[]{};:'\",.<>/?`~\\| ":
        vk = cb._printable_vk(ch)
        if vk and vk in named and vk not in (32,):
            collisions.append((ch, vk))
    assert collisions == [], collisions


def test_letters_and_digits_use_their_ascii_codes():
    assert cb._printable_vk("a") == cb._printable_vk("A") == 65
    assert cb._printable_vk("7") == ord("7")


def test_an_unknown_character_gets_no_code_rather_than_a_wrong_one():
    """Zero is safe: `text` still inserts the character."""
    assert cb._printable_vk("é") == 0
    assert cb._printable_vk("中") == 0


# --- the exit check --------------------------------------------------------
def test_the_exit_check_asks_a_blank_tab_not_the_visible_one():
    src = open(cb.__file__, encoding="utf-8").read()
    body = src.split("async def exit_ip(")[1].split("\n# ---")[0]
    assert "_probe_tab(" in body
    assert "targets(" not in body, "back to asking whatever page is on screen"


def test_the_probe_tab_is_hidden_from_the_tab_list():
    """It must never be streamable or closable by the person using the page:
    it is ours, and it always says about:blank."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def tabs(")[1].split("\ndef ")[0]
    assert "probe_target" in fn and "include_probe" in fn


def test_park_leaves_the_probe_tab_alone():
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def _park(")[1].split("\n# ---")[0]
    assert 't["id"] != probe' in fn


def test_park_only_opens_a_blank_tab_when_there_is_not_one():
    """It used to open one every time, so parking an already parked browser added
    a tab. Nothing complains, the count just creeps up."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def _park(")[1].split("\n# ---")[0]
    assert "if keep is None:" in fn
    assert fn.count("_cdp_new_tab") == 1


def test_park_waits_for_the_tabs_to_actually_go():
    """/json/close returns "Target is closing" before the tab is gone, so a
    listing taken straight after still shows it and any caller that checks its
    own work reads a stale answer."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def _park(")[1].split("\n# ---")[0]
    assert "deadline" in fn and "time.sleep" in fn


# --- talking to Chrome -----------------------------------------------------
def test_new_tab_uses_put():
    """POST is 405 on Chrome 111 and later, and it reads as a 500 to the user."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def _cdp_new_tab(")[1].split("\ndef ")[0]
    assert 'method="PUT"' in fn
    assert "data=b" not in fn


def test_nothing_opens_a_tab_the_old_way():
    """Every caller goes through the helper, so the PUT is decided in one place."""
    src = open(cb.__file__, encoding="utf-8").read()
    helper = src.split("def _cdp_new_tab(")[1].split("\ndef ")[0]
    elsewhere = src.replace(helper, "")
    assert "/json/new" not in elsewhere


# --- egress ----------------------------------------------------------------
def test_direct_is_the_default_and_really_means_no_proxy():
    assert cb.DEFAULT_EGRESS == "direct"
    assert cb.EGRESS["direct"]["arg"] is None


def test_a_browser_on_direct_is_launched_with_no_proxy_server():
    """Absence is not enough: Chrome picks up http_proxy from the environment,
    and this app's environment has carried one."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def start(")[1].split("\ndef ")[0]
    assert "--no-proxy-server" in fn
    assert 'env.pop("http_proxy"' in fn


def test_every_egress_is_labelled_and_explained():
    for key, row in cb.EGRESS.items():
        assert row["label"] and row["note"], key


# --- pacing ----------------------------------------------------------------
class _Cast(cb.InteractiveCast):
    """Constructed without touching a socket: only the pacing is under test."""

    def __init__(self, **kw):
        cb.browser_live.Screencast.__init__(self, "ws://127.0.0.1:1/x", fps=kw.get("fps", 5.0))
        self._fast = 1.0 / kw.get("active_fps", 20.0)
        self._active_until = 0.0


def test_the_frame_rate_rises_on_input_and_falls_back():
    """Flat 15 fps cost 542 KB/s sitting on business.facebook.com with nobody
    touching it: that page animates for ever, so de-duplication never fires."""
    c = _Cast(fps=5.0, active_fps=20.0)
    idle = c._interval
    c._poke()
    assert c._interval < idle, "an input must buy a faster stream"
    c._active_until = 0.0
    assert c._interval == idle, "and it must expire"


def test_the_idle_rate_is_the_one_the_preset_asked_for():
    c = _Cast(fps=4.0)
    assert abs(c._interval - 0.25) < 1e-9


def test_every_preset_is_complete():
    for key, p in cb.PRESETS.items():
        assert set(p) == {"label", "quality", "w", "h", "fps"}, key
        assert 20 <= p["quality"] <= 95 and p["w"] >= 640 and p["fps"] > 0, key
    assert cb.DEFAULT_PRESET in cb.PRESETS


# --- the page --------------------------------------------------------------
def test_the_page_renders_and_carries_the_prefix():
    html = cb.page_html("/build")
    assert '"/build/console"' in html
    assert "__BASE__" not in html and "__LINKS__" not in html
    assert "%(base)s" not in html


def test_the_page_keeps_its_css_percentages():
    """The reason substitution is replace() and not %."""
    assert "width:100%" in cb.page_html("")


def test_the_websocket_url_is_built_from_the_prefix_not_from_the_path():
    html = cb.page_html("")
    assert "BASE + \"/ws\"" in html


def test_no_em_dash_anywhere_a_person_will_read():
    """House rule, and the page is UI copy."""
    src = open(cb.__file__, encoding="utf-8").read()
    assert "—" not in src


def test_paste_and_the_modifier_keys_are_wired():
    html = cb.page_html("")
    assert "paste" in html and "clipboardData" in html
    assert "altKey" in html and "ctrlKey" in html and "shiftKey" in html


def test_press_and_release_travel_separately():
    """One combined click cannot drag or select text."""
    html = cb.page_html("")
    assert "mousePressed" in html and "mouseReleased" in html
    assert "touchstart" in html, "it has to work from a phone too"


# --- shutting down ---------------------------------------------------------
def test_stop_never_matches_on_a_bare_process_name():
    """A broad pkill on these shared boxes has killed other people's sessions."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def stop(")[1].split("\ndef ")[0]
    assert "pkill" not in fn
    assert re.search(r"killpg\(os\.getpgid", fn), "kill the pids we wrote down, nothing else"


def test_an_idle_browser_is_parked_rather_than_killed():
    """Parking keeps the profile, and the profile is the login. Killing it would
    throw away the thing the browser exists to hold."""
    src = open(cb.__file__, encoding="utf-8").read()
    fn = src.split("def park_if_idle(")[1].split("\ndef ")[0]
    assert "_park(" in fn and "stop(" not in fn
