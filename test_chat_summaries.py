"""Owner-bound chat ingestion, persistent progress cadence and race regressions."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app

_REAL_SHORT_SUMMARY = app._chat_short_summary
_REAL_READ_SNAPSHOT = app._read_chat_snapshot


@pytest.fixture
def chat(tmp_path, monkeypatch):
    owner = {"id": "alice"}
    lifecycle = {"generation": "a" * 32, "owner_id": "alice", "desired_state": "running"}
    owners = {"demo": "alice"}
    path = tmp_path / "messages.json"
    path.write_text(json.dumps({"demo": [{"role": "user", "text": "Fix mobile chat", "ts": 1000}]}))
    snapshot = {
        "user_text": "Fix mobile chat", "started_at": 1000, "turn_id": "source-turn",
        "text": "Found missing replies. Checking the message reader.", "final_text": "",
        "source": "codex", "_root": "root", "_generation": "a" * 32,
    }
    activity = {"status": "busy"}
    monkeypatch.setattr(app, "cache", {})
    monkeypatch.setattr(app, "_chat_checked", {})
    monkeypatch.setattr(app, "_chat_backfilled", {})
    monkeypatch.setattr(app, "_load_session_owners", lambda: owners)
    monkeypatch.setattr(app, "_strict_session_owner", lambda name, expected="": ("alice", owner) if owners.get(name) == "alice" and expected in {"", "alice"} else None)
    monkeypatch.setattr(app, "_find_user_by_id", lambda uid: owner if uid == "alice" else None)
    monkeypatch.setattr(app, "_session_lifecycle", SimpleNamespace(get=lambda _name: lifecycle))
    monkeypatch.setattr(app, "_user_messages_file", lambda _owner: path)
    monkeypatch.setattr(app, "_read_chat_snapshot", lambda *_: copy.deepcopy(snapshot))
    monkeypatch.setattr(app, "async_detect_activity", AsyncMock(side_effect=lambda _: dict(activity)))
    summarizer = AsyncMock(side_effect=lambda text, kind: text.split("\n")[-1])
    monkeypatch.setattr(app, "_chat_short_summary", summarizer)
    monkeypatch.setattr(app, "_extract_turn_links", lambda _: [])
    return SimpleNamespace(path=path, owner=owner, owners=owners, lifecycle=lifecycle,
                           snapshot=snapshot, activity=activity, summarize=summarizer)


def rows(chat):
    return json.loads(chat.path.read_text())["demo"]


@pytest.mark.asyncio
async def test_one_hour_run_has_two_progress_updates_and_distinct_final(chat):
    await app._refresh_session_chat("demo", "alice", now=2199)
    assert len(rows(chat)) == 1
    await app._refresh_session_chat("demo", "alice", now=2215)
    assert [m.get("kind") for m in rows(chat)] == [None, "progress"]
    chat.snapshot["text"] = "Replies now appear. Testing the mobile layout."
    await app._refresh_session_chat("demo", "alice", now=3400)
    assert len(rows(chat)) == 2
    await app._refresh_session_chat("demo", "alice", now=3420)
    chat.snapshot["text"] += "\nMobile chat fixed. All checks passed."
    chat.snapshot["final_text"] = "Mobile chat fixed. All checks passed."
    await app._refresh_session_chat("demo", "alice", now=4600)
    messages = rows(chat)
    assert [m.get("kind") for m in messages] == [None, "progress", "progress", "final"]
    assert len({m["id"] for m in messages}) == 4
    assert len({m["turn_id"] for m in messages[1:]}) == 1
    await app._refresh_session_chat("demo", "alice", now=4630)
    assert len(rows(chat)) == 4
    assert chat.summarize.await_count == 3


@pytest.mark.asyncio
async def test_unchanged_output_does_not_repeat_after_restart(chat, monkeypatch):
    await app._refresh_session_chat("demo", "alice", now=2200)
    monkeypatch.setattr(app, "_chat_checked", {})
    await app._refresh_session_chat("demo", "alice", now=3410)
    assert len(rows(chat)) == 2
    assert chat.summarize.await_count == 1


@pytest.mark.asyncio
async def test_final_same_as_progress_still_gets_own_bubble(chat):
    await app._refresh_session_chat("demo", "alice", now=2200)
    chat.snapshot["final_text"] = chat.snapshot["text"]
    await app._refresh_session_chat("demo", "alice", now=2240)
    assert [m.get("kind") for m in rows(chat)] == [None, "progress", "final"]


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_summarize_once(chat):
    await asyncio.gather(*(app._refresh_session_chat("demo", "alice", now=2200) for _ in range(4)))
    assert chat.summarize.await_count == 1
    assert len(rows(chat)) == 2


@pytest.mark.asyncio
async def test_send_during_summary_preserves_new_user_and_discards_old_reply(chat):
    async def send_during_summary(text, kind):
        app.LockedJsonStore(chat.path, dict).update(
            lambda value: value["demo"].append({"role": "user", "text": "Different request", "ts": 2201}))
        return "Old response"
    chat.summarize.side_effect = send_during_summary
    await app._refresh_session_chat("demo", "alice", now=2200)
    assert [m["role"] for m in rows(chat)] == ["user", "user"]
    assert rows(chat)[-1]["text"] == "Different request"


@pytest.mark.asyncio
async def test_direct_terminal_prompt_change_discards_pending_summary(chat):
    async def raw_send(text, kind):
        chat.snapshot["turn_id"] = "new-source-turn"
        chat.snapshot["user_text"] = "Different request"
        return "Old response"
    chat.summarize.side_effect = raw_send
    await app._refresh_session_chat("demo", "alice", now=2200)
    assert len(rows(chat)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "generation", "deleting"])
async def test_identity_change_during_summary_fails_closed(chat, change):
    async def changed(text, kind):
        if change == "owner":
            chat.owners["demo"] = "bob"
        elif change == "generation":
            chat.lifecycle["generation"] = "b" * 32
        else:
            chat.lifecycle["desired_state"] = "deleting"
        return "Old response"
    chat.summarize.side_effect = changed
    assert await app._refresh_session_chat("demo", "alice", now=2200) == []
    assert len(rows(chat)) == 1


@pytest.mark.asyncio
async def test_idle_fallback_that_resumes_is_not_frozen_as_final(chat):
    chat.activity["status"] = "idle"
    chat.snapshot["source"] = "pane"
    async def resumes(text, kind):
        chat.activity["status"] = "busy"
        return "Still checking"
    chat.summarize.side_effect = resumes
    await app._refresh_session_chat("demo", "alice", now=2200)
    assert len(rows(chat)) == 1


@pytest.mark.asyncio
async def test_source_user_newer_than_dashboard_user_is_imported_once(chat):
    chat.snapshot.update(user_text="New request from terminal", started_at=1300,
                         turn_id="direct-turn", final_text="New request handled.")
    await app._refresh_session_chat("demo", "alice", now=1400)
    await app._refresh_session_chat("demo", "alice", now=1450)
    assert [m["role"] for m in rows(chat)] == ["user", "user", "assistant"]
    assert rows(chat)[1]["text"] == "New request from terminal"


@pytest.mark.asyncio
async def test_repeated_identical_direct_prompt_is_a_new_user_and_reply(chat):
    chat.snapshot["final_text"] = "First request handled."
    await app._refresh_session_chat("demo", "alice", now=1100)
    first_id = rows(chat)[-1]["turn_id"]
    chat.snapshot.update(started_at=1200, turn_id="second-identical-turn",
                         final_text="Second request handled.")
    await app._refresh_session_chat("demo", "alice", now=1300)
    assert [row["role"] for row in rows(chat)] == ["user", "assistant", "user", "assistant"]
    assert rows(chat)[-1]["turn_id"] != first_id


@pytest.mark.asyncio
async def test_clipped_tail_keeps_cadence_and_final_turn_identity(chat):
    await app._refresh_session_chat("demo", "alice", now=2200)
    first_id = rows(chat)[-1]["turn_id"]
    chat.snapshot.update(has_user=False, turn_id="synthetic-tail-id", text="Mobile layout now fixed. Checking links.")
    await app._refresh_session_chat("demo", "alice", now=2600)
    assert len(rows(chat)) == 2
    await app._refresh_session_chat("demo", "alice", now=3410)
    chat.snapshot["final_text"] = "Links work. Mobile chat is ready."
    await app._refresh_session_chat("demo", "alice", now=4600)
    assert [row.get("kind") for row in rows(chat)] == [None, "progress", "progress", "final"]
    assert all(row["turn_id"] == first_id for row in rows(chat)[1:])


@pytest.mark.asyncio
async def test_legacy_agent_message_is_not_a_final_while_busy(chat):
    chat.snapshot.update(source="legacy", final_text="Still checking the mobile layout.", explicit_final=False)
    await app._refresh_session_chat("demo", "alice", now=1100)
    assert len(rows(chat)) == 1
    await app._refresh_session_chat("demo", "alice", now=2200)
    assert rows(chat)[-1]["kind"] == "progress"


@pytest.mark.asyncio
async def test_long_no_llm_run_summarizes_new_late_progress(chat, monkeypatch):
    monkeypatch.setattr(app, "_chat_short_summary", _REAL_SHORT_SUMMARY)
    monkeypatch.setattr(app, "client", None)
    chat.snapshot["text"] = ("Initial investigation found multiple possible causes and the current message reader needs closer inspection. " * 10)
    await app._refresh_session_chat("demo", "alice", now=2200)
    chat.snapshot["text"] += "\nReplies are fixed. Testing the mobile button now."
    await app._refresh_session_chat("demo", "alice", now=3410)
    assert len(rows(chat)) == 3
    assert "mobile button" in rows(chat)[-1]["text"]
    assert len(rows(chat)[-1]["text"].split()) <= 55


@pytest.mark.asyncio
async def test_recent_user_only_history_backfills_without_llm_calls(chat, monkeypatch):
    previous = {"user_text": "Earlier request", "started_at": 100,
                "turn_id": "earlier-turn", "has_user": True,
                "text": "Earlier repair done.", "final_text": "Earlier repair done.",
                "last_output_at": 150}
    chat.snapshot["_recent_turns"] = [previous, dict(chat.snapshot)]
    monkeypatch.setattr(app, "_terminal_history_binding", lambda *_: {"resume_uuid": "root"})
    await app._refresh_session_chat("demo", "alice", now=1100)
    await app._refresh_session_chat("demo", "alice", now=1140)
    assert [(row["role"], row["text"]) for row in rows(chat)] == [
        ("user", "Earlier request"), ("assistant", "Earlier repair done."), ("user", "Fix mobile chat")]
    chat.summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_prompt_does_not_receive_previous_reply(chat):
    chat.snapshot.update(user_text="Old request", started_at=500,
                         turn_id="old-turn", final_text="Old request handled.")
    await app._refresh_session_chat("demo", "alice", now=1400)
    assert len(rows(chat)) == 1


def test_atomic_merge_preserves_controller_reply_and_other_workers_prompt(chat):
    entry = app._bound_session_cache_entry("demo", "alice")
    entry["messages"] = rows(chat)
    # Both API workers held an old snapshot when the controller wrote progress.
    app.LockedJsonStore(chat.path, dict).update(lambda data: data["demo"].append(
        {"id": "progress-1", "role": "assistant", "kind": "progress", "text": "Working", "ts": 2200}))
    entry["messages"].append({"role": "user", "text": "Also test it", "ts": 2201})
    app._save_messages()
    assert [m["role"] for m in rows(chat)] == ["user", "assistant", "user"]
    app._save_messages()
    assert len(rows(chat)) == 3


def test_stale_generation_cache_cannot_persist(chat):
    entry = app._bound_session_cache_entry("demo", "alice")
    entry["messages"] = [{"role": "user", "text": "stale", "ts": 100}]
    chat.lifecycle["generation"] = "b" * 32
    app._save_messages()
    assert rows(chat)[0]["text"] == "Fix mobile chat"


@pytest.mark.asyncio
async def test_stale_api_save_preserves_source_mapping_for_clipped_tail(chat):
    entry = app._bound_session_cache_entry("demo", "alice")
    entry["messages"] = rows(chat)
    await app._refresh_session_chat("demo", "alice", now=2200)
    user = rows(chat)[0]
    assert user["source_turn_id"] == "source-turn"
    app._save_messages()
    assert rows(chat)[0]["source_turn_id"] == "source-turn"
    chat.snapshot.update(has_user=False, turn_id="clipped", final_text="Chat fixed.")
    await app._refresh_session_chat("demo", "alice", now=2300)
    assert rows(chat)[-1]["turn_id"] == user["turn_id"]


@pytest.mark.parametrize("pane", [
    "› Fix mobile chat\n• Ran pytest\n  └ hidden tool output\n• Chat fixed. Checks pass.\n› ",
    "❯ Fix mobile chat\n● Bash(pytest)\n  ⎿ hidden tool output\n● Chat fixed. Checks pass.\n❯ ",
])
def test_codex_and_claude_pane_fallback_only_extract_assistant_prose(pane):
    prompt, prose = app._chat_pane_prose(pane)
    assert prompt == "Fix mobile chat"
    assert prose == "Chat fixed. Checks pass."


@pytest.mark.asyncio
async def test_no_llm_fallback_is_short_code_free_and_preserves_question(monkeypatch):
    monkeypatch.setattr(app, "client", None)
    answer = await app._chat_short_summary(
        "```python\nraise RuntimeError('bad')\n```\nMobile chat fixed. Which layout do you prefer?", "final")
    assert "RuntimeError" not in answer
    assert "Which layout" in answer
    assert len(answer.split()) <= 55


@pytest.mark.asyncio
async def test_background_worker_handles_owned_sessions_without_browser(chat, monkeypatch):
    monkeypatch.setattr(app, "get_tmux_sessions", lambda: [{"name": "demo"}, {"name": "foreign"}])
    refresh = AsyncMock()
    monkeypatch.setattr(app, "_refresh_session_chat", refresh)
    await app._session_chat_summary_pass()
    refresh.assert_awaited_once_with("demo", "alice")


@pytest.mark.parametrize("invalid", [False, True])
def test_exact_root_reader_validates_opened_metadata_and_excludes_hidden_content(chat, monkeypatch, tmp_path, invalid):
    root = {"owner_id": "alice", "session_name": "demo", "resume_uuid": "exact-root",
            "generation": "a" * 32, "owner": chat.owner}
    transcript = tmp_path / "rollout.jsonl"
    def item(role, text, **extra):
        return {"type": "response_item", "timestamp": 1000,
                "payload": {"type": "message", "role": role,
                            "content": [{"type": "output_text", "text": text}], **extra}}
    records = [{"type": "session_meta", "payload": {"id": "other-root" if invalid else "exact-root",
               "session_id": "exact-root", "thread_source": "user"}},
               item("user", "Fix mobile chat"), item("assistant", "Hidden reasoning", channel="analysis"),
               {"type": "response_item", "payload": {"type": "function_call_output", "output": "Secret tool output"}},
               item("assistant", "Chat fixed.", channel="final")]
    transcript.write_text("\n".join(json.dumps(record) for record in records))
    monkeypatch.setattr(app, "_terminal_history_binding", lambda *_: root)
    monkeypatch.setattr(app, "_open_session_close_rollout", lambda _: (os.open(transcript, os.O_RDONLY), "", ()))
    monkeypatch.setattr(app, "_terminal_binding", lambda *_: pytest.fail("Invalid known roots must not fall through to pane"))
    result = _REAL_READ_SNAPSHOT("demo", "alice", rows(chat))
    if invalid:
        assert result is None
    else:
        assert result["text"] == "Chat fixed."
        assert result["explicit_final"]


def test_chat_reader_does_not_open_another_owners_root(chat, monkeypatch):
    opened = []
    monkeypatch.setattr(app, "_terminal_history_binding", lambda *_: opened.append(True))
    assert _REAL_READ_SNAPSHOT("demo", "bob", []) is None
    assert not opened


@pytest.mark.asyncio
async def test_controller_chat_refresh_rejects_other_owner(chat):
    result = await app._controller_dispatch({"op": "chat_refresh", "session": "demo", "owner_id": "bob"})
    assert result["_status"] == 404
    assert not result["ok"]


@pytest.mark.asyncio
async def test_api_worker_relays_chat_refresh_to_single_controller(chat, monkeypatch):
    monkeypatch.setattr(app, "PROCESS_ROLE", "api")
    caller = AsyncMock(return_value={"ok": True, "messages": rows(chat)})
    monkeypatch.setattr(app, "_controller_call", caller)
    assert await app._get_chat_messages("demo", "alice") == rows(chat)
    caller.assert_awaited_once_with("chat_refresh", session="demo", owner_id="alice")


def test_wrapped_prompt_in_claude_pane_is_preserved_for_matching():
    prompt, prose = app._chat_pane_prose("❯ Fix mobile chat and preserve\n  my messages please\n● Replies now appear.\n❯ ")
    assert app._norm_text(prompt) == "fix mobile chat and preserve my messages please"
    assert prose == "Replies now appear."
