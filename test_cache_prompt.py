"""Cache mode drafts the next step from the conversation, and vets it.

It used to send one fixed "Continue. re-verify your own work" line every hour.
It now reads the owner's messages and the agent's replies (the Chat tab's own
store) and drafts the step the owner would most likely have asked for. These
tests pin what the drafting model is shown, what is refused before anything is
typed, and when the chain stops.
"""
import asyncio
import os
import time

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app  # noqa: E402


@pytest.fixture
def convo(monkeypatch):
    """A session whose Chat store holds a small real-shaped conversation."""
    now = time.time()
    msgs = [
        {"role": "user", "text": "Build a CSV export for the orders page, one row per order.", "ts": now - 7200},
        {"role": "assistant", "text": "Added a CSV export button on /orders.", "ts": now - 7000,
         "full": "I added the export. Tests for the date filter are not written yet."},
        {"role": "user", "text": "Also include the refund column.", "ts": now - 5000},
        {"role": "assistant", "text": "Refund column added; the header test still fails.", "ts": now - 4800,
         "full": "Refund column added. One header test still fails and I have not fixed it."},
    ]
    # Never the real ~/.tmux-dashboard/messages.json: app writes the whole cache.
    monkeypatch.setattr(app, "_save_messages", lambda: None)
    monkeypatch.setitem(app.cache, "orders", {"messages": msgs})
    monkeypatch.setattr(app, "get_session_cwd", lambda name: "/home/u/orders-app")
    monkeypatch.setattr(app, "_extract_last_assistant_turn", lambda *a, **k: "")
    app._human_input_at.pop("orders", None)
    return msgs


def test_the_model_sees_the_owners_words_and_the_latest_reply_in_full(convo):
    ctx = app._cache_prompt_context("orders", [])
    assert "[Owner] Build a CSV export for the orders page" in ctx
    assert "[Owner] Also include the refund column." in ctx
    assert "[Agent, recap] Refund column added" in ctx
    assert "One header test still fails and I have not fixed it." in ctx, "latest reply in full"
    assert "/home/u/orders-app" in ctx


def test_what_was_already_sent_is_shown_so_it_is_not_repeated(convo):
    ctx = app._cache_prompt_context("orders", ["Fix the failing header test."])
    assert "do not repeat" in ctx and "Fix the failing header test." in ctx


def test_a_session_with_no_owner_message_has_nothing_to_draft_from(monkeypatch):
    monkeypatch.setitem(app.cache, "empty", {"messages": []})
    assert app._cache_prompt_context("empty", []) == ""
    prompt, source = asyncio.run(app._compose_cache_prompt("empty", []))
    assert (prompt, source) == (app.CACHE_KEEPALIVE_PROMPT, "fixed")


def test_a_good_draft_is_sent_with_the_away_note(convo, monkeypatch):
    async def fake(system, user):
        assert "Do NOT" in system and "new feature" in system
        return '{"prompt": "Fix the failing header test in test_export.py, then add tests for the date filter."}'
    monkeypatch.setattr(app, "_draft_json", fake)
    prompt, source = asyncio.run(app._compose_cache_prompt("orders", []))
    assert source == "drafted"
    assert prompt.startswith("Fix the failing header test in test_export.py")
    assert prompt.endswith(app._CACHE_PROMPT_AWAY_NOTE)


@pytest.mark.parametrize("draft", [
    "/clear and start over",                               # a slash command
    "/exit",
    "Drop the orders table and rebuild it from scratch.",  # destructive
    "Force-push the branch to main.",
    "Delete the old user records so the export starts clean.",
    "Email the customers the new CSV so they can try it.",
    "Upgrade the plan so the export job has more workers.",
    "All tests passed, mark the task complete.",           # asserts a result
    "ok",                                                  # too short to be a step
    "",
])
def test_an_unsafe_or_empty_draft_falls_back_to_the_fixed_line(convo, monkeypatch, draft):
    async def fake(system, user):
        return '{"prompt": %s}' % __import__("json").dumps(draft)
    monkeypatch.setattr(app, "_draft_json", fake)
    prompt, source = asyncio.run(app._compose_cache_prompt("orders", []))
    assert (prompt, source) == (app.CACHE_KEEPALIVE_PROMPT, "fixed")


def test_garbage_from_the_model_falls_back(convo, monkeypatch):
    async def fake(system, user):
        return "sure! here is a prompt"
    monkeypatch.setattr(app, "_draft_json", fake)
    assert asyncio.run(app._compose_cache_prompt("orders", []))[1] == "fixed"


@pytest.mark.parametrize("draft", [
    "Add tests for the date filter in test_export.py and run the whole suite.",
    "Remove the unused helper you left in export.py and tidy the CSV header code.",
    "Check the export on a phone-width screen and fix the button wrapping.",
    "Write the missing refund column test, then QA the download end to end.",
])
def test_ordinary_next_steps_pass(draft):
    assert app._vet_cache_prompt(draft) == draft


def test_a_long_draft_is_cut_at_a_sentence():
    text = "Add tests for the export. " * 80
    out = app._vet_cache_prompt(text)
    assert len(out) <= app._CACHE_PROMPT_MAX_CHARS and out.endswith(".")


# ── the chain ───────────────────────────────────────────────────────────────

def test_the_chain_is_capped():
    assert 1 <= app.CACHE_KEEPALIVE_MAX_CHAIN <= 12


def test_a_person_typing_after_a_push_restarts_the_chain(convo):
    pushed_at = time.time() - 100
    assert not app._human_spoke_since("orders", pushed_at)
    app._note_human_input("orders")
    assert app._human_spoke_since("orders", pushed_at)


def test_a_message_the_dashboard_sent_itself_is_not_a_person(convo, monkeypatch):
    pushed_at = time.time() - 100
    app._record_auto_message("orders", "Add the date filter tests.", "cache")
    assert not app._human_spoke_since("orders", pushed_at)
    last = app.cache["orders"]["messages"][-1]
    assert last["auto"] == "cache" and last["role"] == "user"
    convo.append({"role": "user", "text": "thanks, also sort by date", "ts": time.time()})
    assert app._human_spoke_since("orders", pushed_at)


def test_auto_messages_are_not_read_as_the_owners_words(convo):
    convo.append({"role": "user", "text": "Add the date filter tests.", "ts": time.time(), "auto": "cache"})
    ctx = app._cache_prompt_context("orders", [])
    assert "[Sent for the owner while away] Add the date filter tests." in ctx
    assert "[Owner] Add the date filter tests." not in ctx


def test_the_loop_fires_only_in_the_push_window_and_never_on_a_cold_cache():
    src = open(app.__file__).read()
    body = src[src.index("async def _cache_keepalive_loop"):]
    body = body[:body.index("\n_LOGIN_WATCHDOG_INTERVAL")]
    assert "if now < deadline - CACHE_PUSH_LEAD:" in body
    assert "if now >= deadline:" in body
    assert "_human_spoke_since(name, st[\"at\"])" in body
    assert body.count("_has_pending_user_input(visible)") == 2, "re-checked after drafting"
    assert "_record_auto_message(name, prompt, \"cache\")" in body
