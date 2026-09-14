"""Incremental, content-free metrics from one explicitly bound Codex root.

Only measured token counters and task boundaries are retained. Output TPS is
completed-turn output divided by turn wall time, including tool/wait time and
excluding idle gaps. It is not a measurement of streaming speed or throttling.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections import OrderedDict
from datetime import datetime


def empty_metrics() -> dict:
    return {
        "metrics_thread_id": "",
        "metrics_catching_up": False,
        "model": "",
        "cache_model": "",
        "last_input_tokens": None,
        "context_tokens": 0,
        "context_limit": 0,
        "cache_read_tokens": 0,
        "cache_last_activity": 0.0,
        "last_turn_end": 0.0,
        "last_turn_seconds": None,
        "session_input_tokens": None,
        "session_output_tokens": None,
        "session_total_tokens": None,
        "avg_tps": None,
        "tps_active_seconds": 0.0,
        "tps_output_tokens": 0,
        "tps_completed_turns": 0,
    }


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value) -> int | None:
    result = _number(value)
    return int(result) if result is not None and result.is_integer() else None


def _epoch(value) -> float:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return result if math.isfinite(result) and result > 0 else 0.0
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


class _RolloutState:
    def __init__(self, thread_id: str):
        self.metrics = empty_metrics()
        self.metrics["metrics_thread_id"] = thread_id
        self.offset = 0
        self.signature = None
        self.lock = threading.Lock()
        self.header_hash = None
        self.checkpoint = None
        self.skipping_line = False
        self.catching_up = False
        self.turn = None
        self.last_completed_id = ""
        self.last_usage_snapshot = None

    def consume(self, event: dict) -> None:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        metrics = self.metrics
        if event.get("type") == "turn_context":
            if isinstance(payload.get("model"), str):
                metrics["model"] = payload["model"]
            return
        if event.get("type") != "event_msg":
            return
        kind = payload.get("type")
        timestamp = _epoch(event.get("timestamp"))
        turn_id = str(payload.get("turn_id") or "")
        if kind == "task_started":
            limit = _count(payload.get("model_context_window"))
            if limit:
                metrics["context_limit"] = limit
            if self.turn and self.turn["id"] == turn_id:
                return
            self.turn = {
                "id": turn_id,
                "start": _epoch(payload.get("started_at")) or timestamp,
                "output": 0,
                "measured": False,
                "valid": True,
            }
            return
        if kind in ("task_complete", "turn_aborted"):
            # Replayed completion events cannot change either the idle epoch or
            # the averages. Metered requests have their own separate cache clock.
            if turn_id and turn_id == self.last_completed_id:
                return
            end = _epoch(payload.get("completed_at")) or timestamp
            duration_ms = _number(payload.get("duration_ms"))
            duration = duration_ms / 1000 if duration_ms is not None else None
            pending = self.turn if self.turn and self.turn["id"] == turn_id else None
            start = _epoch(payload.get("started_at")) or (pending or {}).get("start", 0)
            if duration is None and start and end >= start:
                duration = end - start
            if end >= metrics["last_turn_end"]:
                metrics["last_turn_end"] = end
                metrics["last_turn_seconds"] = duration
            if (
                kind == "task_complete"
                and pending
                and pending["valid"]
                and pending["measured"]
                and duration
                and duration > 0
            ):
                metrics["tps_active_seconds"] += duration
                metrics["tps_output_tokens"] += pending["output"]
                metrics["tps_completed_turns"] += 1
                metrics["avg_tps"] = metrics["tps_output_tokens"] / metrics["tps_active_seconds"]
            if pending:
                self.turn = None
            self.last_completed_id = turn_id
            return
        if kind != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        limit = _count(info.get("model_context_window"))
        if limit:
            metrics["context_limit"] = limit
        last = info.get("last_token_usage")
        total = info.get("total_token_usage")
        last = last if isinstance(last, dict) else {}
        total = total if isinstance(total, dict) else {}
        inp, out = _count(total.get("input_tokens")), _count(total.get("output_tokens"))
        last_in, last_out = _count(last.get("input_tokens")), _count(last.get("output_tokens"))
        if last_in is not None and last_out is not None:
            # Includes the latest generated output, matching Codex's context
            # accounting. Cached input and reasoning are subsets, never extras.
            metrics["context_tokens"] = last_in + last_out
            metrics["last_input_tokens"] = last_in
            metrics["cache_read_tokens"] = _count(last.get("cached_input_tokens")) or 0
        if inp is None or out is None:
            # No cumulative counter: display the context, but do not fabricate
            # a session total, throughput, or cache refresh from repeated deltas.
            return
        previous_in = metrics["session_input_tokens"]
        previous_out = metrics["session_output_tokens"]
        snapshot = (inp, out)
        changed = snapshot != self.last_usage_snapshot
        reset = previous_in is not None and (inp < previous_in or out < previous_out)
        metrics.update(
            {
                "session_input_tokens": inp,
                "session_output_tokens": out,
                "session_total_tokens": inp + out,
            }
        )
        if changed:
            # Identical snapshots accompany tool and status events. They did
            # not use the model and must not keep extending its cache estimate.
            if not reset and (inp + out) > 0:
                metrics["cache_last_activity"] = max(metrics["cache_last_activity"], timestamp)
                metrics["cache_model"] = metrics["model"]
            if self.turn:
                if reset:
                    self.turn["valid"] = False
                else:
                    self.turn["output"] += out - (previous_out or 0)
                    self.turn["measured"] = True
        self.last_usage_snapshot = snapshot


class CodexRolloutMetrics:
    """Cache numeric aggregates and read only appended complete JSONL records.

    The caller supplies an already securely opened binary stream and its exact
    owner/root binding. The first metadata record is checked again here before
    any metrics can be returned, including when a file was replaced.
    """

    _MARKERS = tuple(
        value.encode()
        for value in (
            '"turn_context"',
            '"token_count"',
            '"task_started"',
            '"task_complete"',
            '"turn_aborted"',
        )
    )
    MAX_PASS_BYTES = 8 * 1024 * 1024
    MAX_RECORD_BYTES = 256 * 1024

    def __init__(self, max_entries: int = 256):
        self._cache = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def read(self, stream, thread_id: str, cache_key: str) -> dict:
        key = (cache_key, thread_id)
        with self._lock:
            state = self._cache.setdefault(key, _RolloutState(thread_id))
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
        # I/O on one large root must not block telemetry for other sessions.
        with state.lock:
            return self._read_locked(stream, thread_id, state)

    @staticmethod
    def _checkpoint(stream, offset):
        start = max(0, offset - 1024)
        stream.seek(start)
        return hashlib.sha256(stream.read(offset - start)).digest()

    def _read_locked(self, stream, thread_id, state):
        info = os.fstat(stream.fileno())
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if state.signature == signature and not state.catching_up:
            return dict(state.metrics)
        stream.seek(0)
        raw_meta = stream.readline(self.MAX_RECORD_BYTES)
        try:
            meta = json.loads(raw_meta)
            payload = meta.get("payload")
            valid = (
                raw_meta.endswith(b"\n")
                and meta.get("type") == "session_meta"
                and isinstance(payload, dict)
                and payload.get("id") == thread_id
                and payload.get("session_id") == thread_id
                and payload.get("thread_source") == "user"
            )
        except (ValueError, UnicodeDecodeError, AttributeError):
            valid = False
        if not valid:
            state.signature = None
            return empty_metrics()
        header_end = stream.tell()
        header_hash = hashlib.sha256(raw_meta).digest()
        # The checkpoint also detects rewriting a file to a *larger* size
        # in the same inode, which must not be mistaken for normal append.
        if (
            state.signature is None
            or state.signature[:2] != signature[:2]
            or info.st_size < state.signature[2]
            or (info.st_size == state.signature[2] and state.signature != signature)
            or state.header_hash != header_hash
            or self._checkpoint(stream, state.offset) != state.checkpoint
        ):
            replacement = _RolloutState(thread_id)
            replacement.lock = state.lock
            state.__dict__.update(replacement.__dict__)
            state.offset = header_end
            state.header_hash = header_hash
        stream.seek(state.offset)
        pass_end = min(info.st_size, state.offset + self.MAX_PASS_BYTES)
        while stream.tell() < pass_end:
            offset = stream.tell()
            raw = stream.readline(min(self.MAX_RECORD_BYTES, pass_end - offset))
            if not raw.endswith(b"\n"):
                if len(raw) == self.MAX_RECORD_BYTES or state.skipping_line:
                    # Oversized tool output cannot contain a small numeric
                    # event. Skip it in chunks without retaining its text.
                    state.skipping_line = True
                    state.offset = stream.tell()
                    continue
                # A partial append or a pass boundary inside a record is
                # retried from that record's beginning on the next pass.
                break
            state.offset = stream.tell()
            if state.skipping_line:
                state.skipping_line = False
                continue
            if not any(marker in raw for marker in self._MARKERS):
                continue
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                state.consume(event)
        state.signature = signature
        state.catching_up = pass_end < info.st_size
        state.checkpoint = self._checkpoint(stream, state.offset)
        if state.catching_up:
            # Older context/idle values would mislead while a large first
            # scan catches up. Subsequent polls keep advancing this cursor.
            return {**empty_metrics(), "metrics_thread_id": thread_id, "metrics_catching_up": True}
        return dict(state.metrics)
