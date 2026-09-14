"""Bounded transport metadata only. Never accept speech, SDP or raw error messages."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import time

MAX_BYTES = 100 * 1024 * 1024
RETENTION_DAYS = 7
EVENTS = {'start', 'ready', 'disconnect', 'reconnect', 'transport', 'provider_attached', 'provider_error', 'approval_step'}
FIELDS = {'id', 'previous_id', 'owner', 'session', 'generation', 'event', 'leg', 'reason',
          'worker', 'close_code', 'duration_ms', 'heartbeat_age_ms', 'browser_age_ms',
          'provider_age_ms', 'ready', 'stopping', 'exception', 'http_status', 'request_id', 'revision', 'phase',
          'event_id', 'delegation_id', 'response_id'}
TEXT = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')


def append(directory, event, *, now=None):
    """Serialized across workers; daily files, seven days and 100 MiB total at most."""
    if event.get('event') not in EVENTS:
        return
    row = {k: v for k, v in event.items() if k in FIELDS and (
        isinstance(v, bool) or (type(v) is int and 0 <= v <= 10**12)
        or (isinstance(v, str) and TEXT.fullmatch(v)))}
    stamp = time.time() if now is None else now
    today = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).date()
    row['ts'] = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()
    raw = (json.dumps(row, sort_keys=True) + '\n').encode()
    root = Path(directory) / 'voice-diagnostics'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        files = sorted(root.glob('????-??-??.jsonl'))
        cutoff = today - dt.timedelta(days=RETENTION_DAYS - 1)
        for p in files[:]:
            if p.stem < cutoff.isoformat():
                p.unlink(); files.remove(p)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total + len(raw) <= MAX_BYTES:
                break
            size = p.stat().st_size
            p.unlink(); total -= size
        if len(raw) > MAX_BYTES:
            return
        target = root / (today.isoformat() + '.jsonl')
        fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'ab') as out:
            out.write(raw)
