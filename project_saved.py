"""Bounded project details from visible conversation, with private local storage."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat

MAX_ITEMS = 250
MAX_VALUE = 8192
MAX_STATE_BYTES = 8_000_000
SCAN_BYTES = 4_000_000
SCAN_VERSION = 3
_URL = re.compile(r'https?://[^\s<>\[\]{}"\'`]+')
_PATH = re.compile(r'(?:^|[\s(\[|>*`])((?:~/|/(?:home|root|tmp|var|opt|srv|mnt|data)/)[^\n<>\[\]{}"\'`|)]+)')
_CREDENTIAL = re.compile(
    r'^\s*(?:[-*+]\s+|\d+\.\s*)?(?:\*\*)?'
    r'((?:(?:[\w.-]+)[ \t]+){0,3}(?:username|user name|user|password|passwd|login|email|api[ _-]?key|access[ _-]?token|secret))'
    r'(?:\*\*)?\s*[:=]\s*(?:\*\*)?\s*(.+?)\s*$', re.I)
_TABLE_CREDENTIAL = re.compile(r'^\s*\|\s*(?:\*\*)?([^|]+?)(?:\*\*)?\s*\|\s*(.*?)\s*\|\s*$')
_INJECTED = ('<environment_context>', '<INSTRUCTIONS>', '# AGENTS.md instructions', '<permissions instructions>')


def collect_saved_items(previous, texts, files=()):
    """Accumulate exact values without an LLM; bounded, newest values retained."""
    result = {key: [] for key in ('urls', 'creds', 'files')}
    seen = {key: set() for key in result}

    def add(key, value):
        if key == 'creds':
            if not isinstance(value, dict) or set(value) != {'label', 'value'}:
                return
            if not all(isinstance(v, str) and 0 < len(v) <= MAX_VALUE for v in value.values()):
                return
        elif not isinstance(value, str) or not 0 < len(value) <= MAX_VALUE:
            return
        signature = json.dumps(value, sort_keys=True)
        if signature not in seen[key]:
            result[key].append(value)
            seen[key].add(signature)

    for key in result:
        values = previous.get(key)
        for value in (values if isinstance(values, list) else [])[-MAX_ITEMS:]:
            add(key, value)
    for text in texts:
        if not isinstance(text, str) or any(marker in text for marker in _INJECTED):
            continue
        for url in _URL.findall(text):
            url = url.rstrip('.,;:!*')
            while url.endswith(')') and url.count(')') > url.count('('):
                url = url[:-1]
            add('urls', url)
        for path in _PATH.findall(text):
            path = re.sub(r':\d+(?::\d+)?$', '', path.strip().rstrip('.,;:*'))
            if ' ' in path and not re.search(r'\.[\w-]+$', path):
                path = path.split()[0]
            add('files', path)
        for line in text.splitlines():
            table = _TABLE_CREDENTIAL.match(line)
            if table:
                line = table[1].strip('* ') + ': ' + table[2]
            credential = _CREDENTIAL.match(line)
            if credential:
                value = credential[2].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '`"\'':
                    value = value[1:-1]
                if value and value.lower() not in {'none', 'n/a', 'redacted', '[redacted]', '***'}:
                    add('creds', {'label': credential[1].strip(), 'value': value})
    for path in files:
        add('files', path)
    return {key: values[-MAX_ITEMS:] for key, values in result.items()}


def event_items(event):
    """Only visible user/assistant prose and declared apply_patch file paths."""
    import chat_summaries
    if not isinstance(event, dict):
        return [], []
    payload = event.get('payload')
    if not isinstance(payload, dict):
        return [], []
    if any(row.get(flag) for row in (event, payload) for flag in ('isSidechain', 'is_sidechain', 'sidechain')):
        return [], []
    if any(str(row.get(key, '')).lower() in {'analysis', 'reasoning', 'thinking'}
           for row in (event, payload) for key in ('channel', 'phase')):
        return [], []
    visible = chat_summaries._visible(event)
    if visible:
        return [visible['text']], []
    if (event.get('type') != 'response_item' or payload.get('type') not in {'function_call', 'custom_tool_call'}
            or str(payload.get('name', '')).split('.')[-1] != 'apply_patch'):
        return [], []
    patch = payload.get('input') if payload.get('type') == 'custom_tool_call' else payload.get('arguments')
    if isinstance(patch, str):
        with contextlib.suppress(ValueError):
            decoded = json.loads(patch)
            if isinstance(decoded, dict):
                patch = decoded.get('patch', decoded.get('input', ''))
    if not isinstance(patch, str):
        return [], []
    return [], re.findall(r'^\*\*\* (?:Add File|Update File|Move to): (.+)$', patch, re.M)


def scan_rollout(stream, binding, state):
    """Scan a bounded forward page from an already safely opened exact root."""
    info = os.fstat(stream.fileno())
    stream.seek(0)
    line = stream.readline(65537)
    if len(line) > 65536:
        raise ValueError('Transcript metadata is too large')
    meta = json.loads(line)
    if not isinstance(meta, dict) or not isinstance(meta.get('payload'), dict):
        raise ValueError('Invalid transcript metadata')
    payload = meta['payload']
    if (meta.get('type') != 'session_meta' or payload.get('id') != binding['resume_uuid']
            or payload.get('session_id') != binding['resume_uuid'] or payload.get('thread_source') != 'user'):
        raise ValueError('Transcript identity changed')
    source = [binding['resume_uuid'], info.st_dev, info.st_ino]
    offset = state.get('offset', 0)
    if (state.get('source') != source or state.get('scan_version') != SCAN_VERSION
            or type(offset) is not int or not 0 <= offset <= info.st_size):
        state.clear()
        offset = 0
    stream.seek(offset)
    stop = offset + SCAN_BYTES
    visible_texts, patched_paths = [], []
    while stream.tell() < stop:
        start = stream.tell()
        line = stream.readline(min(SCAN_BYTES + 1, stop - start))
        if not line:
            break
        if not line.endswith(b'\n'):
            if len(line) >= SCAN_BYTES or state.get('discarding_line'):
                state['discarding_line'] = True
                continue
            stream.seek(start)
            break
        if state.pop('discarding_line', False):
            continue
        try:
            texts, files = event_items(json.loads(line))
        except (ValueError, TypeError, UnicodeError):
            continue
        visible_texts.extend(texts)
        patched_paths.extend(files)
    # Parse each page once. Rebuilding the retained set for every event makes
    # busy transcripts quadratic and blocks the composer's operation lock.
    state.update(collect_saved_items(state, visible_texts, patched_paths))
    state.update(source=source, offset=stream.tell(), scan_version=SCAN_VERSION,
                 catching_up=stream.tell() < info.st_size)


@contextlib.contextmanager
def private_directory(path, *, create=False):
    """Anchor each path component without following directory symlinks."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute private path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=fd)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _safe_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise PermissionError('Unsafe private state file')


def private_json(path, mutate=None):
    """Atomic process-safe state, rejecting symlink/hardlink files and locks."""
    path = Path(path)
    with private_directory(path.parent, create=True) as directory:
        lock = os.open(path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        try:
            _safe_file(lock)
            fcntl.flock(lock, fcntl.LOCK_EX if mutate else fcntl.LOCK_SH)
            value = {}
            try:
                fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                pass
            else:
                with os.fdopen(fd, 'rb') as stream:
                    _safe_file(stream.fileno())
                    raw = stream.read(MAX_STATE_BYTES + 1)
                if len(raw) > MAX_STATE_BYTES:
                    raise ValueError('Private state exceeds limit')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError('Invalid private state')
            if mutate is None:
                return value
            result = mutate(value)
            raw = json.dumps(value, ensure_ascii=False).encode()
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError('Private state exceeds limit')
            temporary = path.name + '.' + secrets.token_hex(12) + '.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            return result
        finally:
            os.close(lock)


def upload_paths(path):
    """List regular uploads without following parent or child symlinks."""
    try:
        with private_directory(path) as directory:
            result = []
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2000 or len(result) >= MAX_ITEMS:
                        break
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_nlink == 1:
                        result.append(str(Path(path) / entry.name))
            return result
    except FileNotFoundError:
        return []
