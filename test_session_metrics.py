"""Synthetic-only metrics tests. Importing the live-state app is unnecessary."""

import ast
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from session_metrics import CodexRolloutMetrics, empty_metrics

ROOT = "01a0738a-5715-7580-88e4-a2cd1730d37d"
OTHER = "01a0738a-5715-7580-88e4-a2cd1730d37e"


def meta(thread_id=ROOT, source="user", session_id=None):
    return {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": session_id or thread_id,
            "thread_source": source,
            "cwd": "/synthetic/shared",
        },
    }


def event(kind, timestamp, **payload):
    return {"type": "event_msg", "timestamp": timestamp, "payload": {"type": kind, **payload}}


def model(name="gpt-6-astra"):
    return {"type": "turn_context", "payload": {"model": name}}


def usage(timestamp, inp, out, last_in=None, last_out=None, cached=50):
    return event(
        "token_count",
        timestamp,
        info={
            "total_token_usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cached_input_tokens": cached,
                "reasoning_output_tokens": 20,
            },
            "last_token_usage": {
                "input_tokens": inp if last_in is None else last_in,
                "output_tokens": out if last_out is None else last_out,
                "cached_input_tokens": cached,
                "reasoning_output_tokens": 10,
            },
            "model_context_window": 10000,
        },
    )


def save(path, rows, mode="w"):
    with path.open(mode) as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def read(reader, path, thread_id=ROOT, cache_key="owner/generation/root"):
    with path.open("rb") as stream:
        return reader.read(stream, thread_id, cache_key)


def test_totals_and_weighted_output_tps_do_not_double_count_snapshots(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(
        path,
        [
            meta(),
            model(),
            event("task_started", 100, turn_id="one", started_at=100),
            usage(104, 2000, 100),
            usage(107, 2000, 100),
            event("task_complete", 500, turn_id="one", completed_at=110, duration_ms=10000),
            event("task_complete", 999, turn_id="one", completed_at=110, duration_ms=10000),
            event("task_started", 1000, turn_id="two"),
            usage(1015, 5000, 500, last_in=3000, last_out=400),
            event("task_complete", 1020, turn_id="two", duration_ms=20000),
            event("task_started", 2000, turn_id="unfinished"),
            usage(2010, 9000, 1000, last_in=4000, last_out=500),
        ],
    )
    result = read(CodexRolloutMetrics(), path)
    assert result["session_total_tokens"] == 10000
    assert result["session_input_tokens"] == 9000
    assert result["session_output_tokens"] == 1000
    assert result["context_tokens"] == 4500
    assert result["context_limit"] == 10000
    assert result["last_input_tokens"] == 4000
    assert result["last_turn_end"] == 1020
    assert result["last_turn_seconds"] == 20
    assert result["cache_last_activity"] == 2010
    assert result["cache_model"] == "gpt-6-astra"
    assert result["tps_completed_turns"] == 2
    assert result["tps_active_seconds"] == 30
    assert result["tps_output_tokens"] == 500
    assert result["avg_tps"] == pytest.approx(500 / 30)


def test_duplicate_counters_and_model_change_do_not_refresh_previous_cache(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(path, [meta(), model(), usage(104, 2000, 100), model("gpt-5.6-sol"), usage(900, 2000, 100)])
    result = read(CodexRolloutMetrics(), path)
    assert result["cache_last_activity"] == 104
    assert result["model"] == "gpt-5.6-sol"
    assert result["cache_model"] == "gpt-6-astra"
    assert result["last_turn_end"] == 0
    assert result["avg_tps"] is None


def test_interruptions_and_counter_resets_do_not_fabricate_tps(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(
        path,
        [
            meta(),
            model(),
            event("task_started", 100, turn_id="a"),
            usage(105, 2000, 100),
            event("turn_aborted", 110, turn_id="a"),
            event("task_started", 200, turn_id="b"),
            usage(210, 500, 10),
            event("task_complete", 220, turn_id="b"),
        ],
    )
    result = read(CodexRolloutMetrics(), path)
    assert result["last_turn_end"] == 220
    assert result["last_turn_seconds"] == 20
    assert result["avg_tps"] is None
    assert result["tps_completed_turns"] == 0
    assert result["session_total_tokens"] == 510
    assert result["cache_last_activity"] == 105


def test_missing_or_malformed_usage_remains_unknown(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(
        path,
        [
            meta(),
            model(),
            event("token_count", 100, info=None),
            event("token_count", 100, info=[]),
            event("token_count", 101, info={"total_token_usage": {"input_tokens": "NaN", "output_tokens": -1}}),
            event("token_count", 102, info={"last_token_usage": {"input_tokens": 1200, "output_tokens": 300}}),
            event("token_count", 103, info={"last_token_usage": {"input_tokens": {}, "output_tokens": True}}),
        ],
    )
    result = read(CodexRolloutMetrics(), path)
    assert result["context_tokens"] == 1500
    assert result["session_total_tokens"] is None
    assert result["avg_tps"] is None
    assert result["cache_last_activity"] == 0


def test_iso_timestamps_and_task_duration_are_measured(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(
        path,
        [
            meta(),
            model(),
            event("task_started", "2026-09-05T10:00:00Z", turn_id="a"),
            usage("2026-09-05T10:00:04Z", 2000, 100),
            event("task_complete", "2026-09-05T10:00:10Z", turn_id="a"),
        ],
    )
    result = read(CodexRolloutMetrics(), path)
    assert result["last_turn_seconds"] == 10
    assert result["avg_tps"] == 10
    assert result["last_turn_end"] - result["cache_last_activity"] == 6


def test_incremental_reader_only_parses_new_complete_records(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    save(path, [meta(), model(), usage(100, 2000, 100)])
    reader = CodexRolloutMetrics()
    first = read(reader, path)
    loads = Mock(wraps=json.loads)
    monkeypatch.setattr("session_metrics.json.loads", loads)
    assert read(reader, path) == first
    loads.assert_not_called()
    raw = json.dumps(usage(200, 3000, 200)) + "\n"
    with path.open("a") as stream:
        stream.write(raw[: len(raw) // 2])
    assert read(reader, path) == first
    assert loads.call_count == 1  # Revalidate only the immutable root metadata.
    with path.open("a") as stream:
        stream.write(raw[len(raw) // 2 :])
    assert read(reader, path)["session_total_tokens"] == 3200
    assert loads.call_count == 3  # Metadata again, plus the newly complete record.


def test_truncated_or_replaced_file_loses_cached_metrics(tmp_path):
    path = tmp_path / "rollout.jsonl"
    reader = CodexRolloutMetrics()
    save(path, [meta(), model(), usage(100, 2000, 100)])
    assert read(reader, path)["session_total_tokens"] == 2100
    save(path, [meta()])
    assert read(reader, path)["session_total_tokens"] is None
    replacement = tmp_path / "replacement.jsonl"
    save(replacement, [meta(), model(), usage(200, 3000, 200)])
    replacement.replace(path)
    assert read(reader, path)["session_total_tokens"] == 3200


def test_larger_same_inode_rewrite_discards_prior_turn_aggregates(tmp_path):
    path = tmp_path / "rollout.jsonl"
    reader = CodexRolloutMetrics()
    save(
        path,
        [
            meta(),
            model(),
            event("task_started", 100, turn_id="a"),
            usage(105, 2000, 100),
            event("task_complete", 110, turn_id="a"),
        ],
    )
    assert read(reader, path)["avg_tps"] == 10
    inode = path.stat().st_ino
    save(path, [meta(), model(), {"type": "response_item", "payload": "x" * 2000}, usage(200, 10, 2)])
    assert path.stat().st_ino == inode
    result = read(reader, path)
    assert result["session_total_tokens"] == 12
    assert result["avg_tps"] is None
    assert result["last_turn_end"] == 0


def test_large_records_are_skipped_in_bounded_passes(tmp_path):
    path = tmp_path / "rollout.jsonl"
    reader = CodexRolloutMetrics()
    reader.MAX_PASS_BYTES = 1024
    reader.MAX_RECORD_BYTES = 1024
    save(path, [meta(), model(), {"type": "response_item", "payload": "x" * 10000}, usage(100, 2000, 100)])
    result = read(reader, path)
    assert result["metrics_catching_up"] is True
    assert result["session_total_tokens"] is None
    for _ in range(15):
        result = read(reader, path)
        if not result["metrics_catching_up"]:
            break
    assert result["metrics_catching_up"] is False
    assert result["session_total_tokens"] == 2100


def test_compacted_context_without_new_usage_does_not_refresh_cache(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(path, [meta(), model(), usage(100, 4000, 100), usage(200, 4000, 100, last_in=500, last_out=0, cached=0)])
    result = read(CodexRolloutMetrics(), path)
    assert result["context_tokens"] == 500
    assert result["session_total_tokens"] == 4100
    assert result["cache_last_activity"] == 100


@pytest.mark.parametrize("header", [meta(OTHER), meta(source="subagent"), meta(session_id=OTHER), []])
def test_wrong_root_and_inherited_subagent_history_are_rejected(tmp_path, header):
    path = tmp_path / "rollout.jsonl"
    save(path, [header, meta(), model(), usage(100, 2000, 100)])
    assert read(CodexRolloutMetrics(), path) == empty_metrics()


def _app_functions(*names, **namespace):
    # App import creates live state stores. Extract the real pure boundary
    # functions so these tests never touch account data or runtime processes.
    source = Path(__file__).with_name("app.py").read_text()
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    namespace.update(
        {
            "time": time,
            "os": os,
            "asyncio": asyncio,
            "logger": logging.getLogger(__name__),
            "empty_metrics": empty_metrics,
        }
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return SimpleNamespace(**namespace)


def _facts_boundary(tmp_path):
    path = tmp_path / "rollout.jsonl"
    save(path, [meta(), model(), usage(100, 2000, 100)])
    owner = {"id": "owner"}
    lifecycle = {"generation": "generation", "resume_uuid": ROOT}
    state = {"owner": owner, "lifecycle": lifecycle}

    def strict(name, expected=""):
        current = state["owner"]
        return (current["id"], current) if current and (not expected or expected == current["id"]) else None

    def opened(binding):
        assert binding["owner_id"] == "owner"
        assert binding["resume_uuid"] == ROOT
        return os.open(path, os.O_RDONLY), path.name, ()

    api = _app_functions(
        "_codex_session_facts",
        "_codex_session_payload",
        _strict_session_owner=strict,
        _session_lifecycle=SimpleNamespace(get=lambda name: state["lifecycle"]),
        _codex_facts_cache={},
        _codex_rollout_metrics=CodexRolloutMetrics(),
        _active_session_root_thread_id=Mock(return_value=ROOT),
        _find_session_transcript_uuid=Mock(return_value=ROOT),
        _open_session_close_rollout=Mock(side_effect=opened),
        _codex_cache_ttl=lambda name: 1800 if name else 0,
        _codex_cache_min_tokens=lambda name: 1024 if name else 0,
        _cache_keepalive_active=lambda name: False,
        _SessionCloseError=RuntimeError,
    )
    return api, state


def test_facts_use_exact_root_and_expose_session_metrics(tmp_path):
    api, state = _facts_boundary(tmp_path)
    result = api._codex_session_payload("tab")
    assert result["session_total_tokens"] == 2100
    assert result["metrics_thread_id"] == ROOT
    assert result["detect_sure"] is True
    assert result["last_input_tokens"] == 2000
    assert result["cache_last_activity"] == 100
    api._find_session_transcript_uuid.assert_not_called()
    api._codex_session_payload("tab")
    assert api._open_session_close_rollout.call_count == 1
    state["owner"] = None
    assert api._codex_session_payload("tab")["session_total_tokens"] is None


def test_missing_root_returns_unknown_instead_of_borrowing_same_cwd(tmp_path):
    api, _state = _facts_boundary(tmp_path)
    api._active_session_root_thread_id.return_value = None
    api._find_session_transcript_uuid.return_value = None
    result = api._codex_session_payload("tab")
    assert result["session_total_tokens"] is None
    assert result["detect_sure"] is False
    api._open_session_close_rollout.assert_not_called()


def test_parked_session_uses_its_validated_recorded_root(tmp_path):
    api, _state = _facts_boundary(tmp_path)
    api._active_session_root_thread_id.return_value = None
    assert api._codex_session_payload("tab")["session_total_tokens"] == 2100
    api._find_session_transcript_uuid.assert_called_once_with("tab")


def test_generation_change_during_read_discards_metrics(tmp_path):
    api, state = _facts_boundary(tmp_path)
    original = api._codex_rollout_metrics.read

    def changing(*args):
        result = original(*args)
        state["lifecycle"] = {"generation": "replacement", "resume_uuid": OTHER}
        return result

    api._codex_rollout_metrics.read = changing
    assert api._codex_session_payload("tab")["session_total_tokens"] is None


def test_cache_deadline_uses_request_clock_and_cacheable_input_only():
    api = _app_functions("_cache_deadline")
    values = {
        "cache_ttl": 1800,
        "cache_min_tokens": 1024,
        "cache_last_activity": 100,
        "last_turn_end": 600,
        "last_input_tokens": 2000,
    }
    assert api._cache_deadline(values) == 1900
    assert api._cache_deadline({**values, "last_input_tokens": 10, "context_tokens": 8000}) == 0
    assert api._cache_deadline({**values, "cache_last_activity": 0}) == 0
    assert api._cache_deadline({**values, "cache_min_tokens": 2048, "last_input_tokens": 1500}) == 0
    assert api._cache_deadline({**values, "cache_min_tokens": 2048, "last_input_tokens": 2048}) == 1900


def test_status_poll_delivers_metrics_with_existing_response_binding():
    rows = [{"name": "tab"}]
    fields = {"session_total_tokens": 123, "avg_tps": 4.5, "cache_last_activity": 100}
    current = AsyncMock(return_value=True)
    api = _app_functions(
        "api_status",
        Request=object,
        _session_list_for_request=lambda request, sessions: (sessions, "mine"),
        get_tmux_sessions=lambda: rows,
        _bind_session_response_rows=lambda sessions, user: "bound",
        _current_user=lambda request: {"id": "owner"},
        _session_list_changed_response=lambda: {"error": "changed"},
        async_detect_activity=AsyncMock(return_value={"status": "idle", "detail": ""}),
        _codex_session_payload=lambda name: fields,
        _session_tab_label_rows=lambda: {},
        _session_lifecycle=SimpleNamespace(get=lambda name: {}),
        _session_tab_label=lambda name, labels: "",
        _get_autopush_mode=lambda name: "off",
        _session_model_fields=lambda name: {"model": "gpt-6-astra"},
        _session_response_bindings_are_current=current,
        JSONResponse=lambda values, **kwargs: values,
    )
    result = asyncio.run(api.api_status(object()))
    assert result[0]["session_total_tokens"] == 123
    assert result[0]["avg_tps"] == 4.5
    assert result[0]["cache_last_activity"] == 100
    current.assert_awaited_once_with("bound")
    current.return_value = False
    assert asyncio.run(api.api_status(object())) == {"error": "changed"}
