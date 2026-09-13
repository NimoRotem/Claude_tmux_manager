"""Session-scoped launch choices and bounded recovery from service failures."""
import json
import re

EFFORTS = ("none", "low", "medium", "high", "xhigh", "max", "ultra")


def compatible_effort(requested, supported):
    ceiling = EFFORTS.index(requested) if requested in EFFORTS else EFFORTS.index("max")
    return next((e for e in reversed(EFFORTS[:ceiling + 1]) if e in supported), None)


def recoverable_error(error):
    """Accept only Codex's structured operational failures, never conversation text."""
    if not isinstance(error, dict) or error.get("misalignment"):
        return False
    info = error.get("codex_error_info", error.get("codexErrorInfo"))
    if isinstance(info, str):
        if info in {"serverOverloaded", "internalServerError", "rateLimitExceeded"}:
            return True
        # Codex 0.153.4 persists HTTP 503 as `other`; only its exact transport
        # prefix with an operational status is accepted, not arbitrary prose.
        return info == "other" and bool(re.match(
            r"^unexpected status (?:408|429|500|502|503|504)\b", str(error.get("message") or "")
        ))
    if not isinstance(info, dict) or len(info) != 1:
        return False
    kind, details = next(iter(info.items()))
    if kind not in {"httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected", "responseTooManyFailedAttempts"}:
        return False
    if not isinstance(details, dict):
        return False
    status = details.get("httpStatusCode", details.get("http_status_code"))
    return status is None or (type(status) is int and status in {408, 429, 500, 502, 503, 504})


def latest_turn(lines):
    """Read only lifecycle events, with a newer start invalidating an old error."""
    latest = None
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("type") in {"task_started", "task_complete", "turn_aborted"}:
            latest = payload
    return latest


def next_model(current, effort, catalog, chain, tried):
    for model in chain:
        if model == current or model in tried or model not in catalog:
            continue
        level = compatible_effort(effort, catalog[model])
        if level:
            return model, level
    return None


def fallback_candidate(options, event, catalog, chain):
    if not options or options.get("no_fallback", True) or not event:
        return None
    state = options.get("fallback") or {}
    if (event.get("type") != "task_complete" or not event.get("turn_id")
            or not recoverable_error(event.get("error"))
            or state.get("turn_id") == event["turn_id"]
            or int(state.get("attempts") or 0) >= 2):
        return None
    return next_model(options["model"], options["effort"], catalog, chain, state.get("tried") or [])


def update_options(store, name, owner, generation, updates, *, expected=None):
    """Compare and update one live generation without touching account config."""
    def mutate(data):
        row = data.get("sessions", {}).get(name, {})
        if (row.get("owner_id") != owner or row.get("generation") != generation
                or row.get("desired_state") != "running"):
            raise ValueError("Session owner, generation or state changed")
        current = row.get("launch_options") or {}
        if expected is not None and current != expected:
            raise ValueError("Session model settings changed")
        row["launch_options"] = {**current, **updates}
        return dict(row["launch_options"])
    return store.store.update(mutate)[1]
