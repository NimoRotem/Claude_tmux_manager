"""Pure, code-free chat extraction from already-authorized transcript records.

This module never locates or opens transcripts. The caller must provide records
from the exact owner-bound session, and decide when a turn has actually finished.
In particular, a Claude ``final_text`` is a final *candidate*, not a completion
signal: Claude does not label its plain assistant messages with a final channel.
"""

import hashlib
import json
import math
import re
from collections import deque
from datetime import datetime, timezone

_INJECTED_TAGS = (
    'environment_context', 'system-reminder', 'system_reminder',
    'system-notification', 'system_notification', 'system_notice', 'system-notice',
    'developer_instructions', 'local-command-caveat', 'local-command-stdout',
    'local-command-stderr', 'command-name', 'command-message', 'command-args',
    'available-deferred-tools',
)
_INJECTED_BLOCK = re.compile(
    r'<(?P<tag>' + '|'.join(_INJECTED_TAGS) + r')\b[^>]*>.*?</(?P=tag)\s*>',
    re.IGNORECASE | re.DOTALL,
)
_INJECTED_START = re.compile(r'^<(?:' + '|'.join(_INJECTED_TAGS) + r')\b', re.IGNORECASE)
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
_FENCE = re.compile(r'^\s*(`{3,}|~{3,})')
_CODE_LINE = re.compile(
    r'^(?:async\s+def\b|def\s+\w+\s*\(|class\s+\w+\s*[:(]|'
    r'(?:from\s+[\w.]+\s+)?import\s+|(?:const|let|var)\s+\w+\s*[=:]|'
    r'(?:export\s+)?(?:async\s+)?function\b|return(?:\s+|;|$)|'
    r'assert\s+|(?:if|elif|while|for|with|except)\b.*:\s*$|'
    r'(?:if|while|for)\s*\(|console\.\w+\(|print\s*\(|'
    r'[\w.\[\]"\x27]+\s*(?:=|:=)\s*[^=]|'
    r'[{}\[\]();,]+$|["\x27][^"\x27]+["\x27]\s*:\s*|'
    r'\$\s+\S|>>>\s|\.\.\.\s)'
)
_TOOL_LINE = re.compile(
    r'^(?:[●⏺•·]\s*(?:Ran|Called|Explored)\s+|(?:[●⏺•·]\s*)?(?:(?:Ran|Called|Explored|Edited|Added|Deleted|Updated)\s+'
    r'.*(?:\(\+?\d|functions\.|command|tool)|'
    r'(?:Bash|Read|Write|Edit|MultiEdit|Glob|Grep|Task|WebFetch|WebSearch)\s*\(|'
    r'Chunk ID:|Wall time:|Process exited with code|Final output:|'
    r'Output exceeded|Script running with cell ID))'
)
_TRACE_LINE = re.compile(
    r'^(?:Traceback \(most recent call last\):|File ["\x27].*["\x27], line \d+|'
    r'(?:[\w.]+(?:Error|Exception)|AssertionError)(?::|$)|'
    r'[\w./\\-]+:\d+(?::\d+)?:\s*(?:[\w.]*Error|[\w.]*Exception)|'
    r'E\s{2,}|>\s+(?:assert|def|return|import|from)\b|'
    r'_{3,}.*(?:test_|Test)|={3,}.*(?:FAILURES|ERRORS|test session)|'
    r'(?:FAILED|ERROR)\s+\S+::)'
)
_DIFF_LINE = re.compile(r'^(?:diff --git\b|index [0-9a-f]+\.\.|@@\s|---\s|\+\+\+\s|\d+\s*[+−-]\s|[+−-](?!\s))')
_LINK = re.compile(r'\[[^\]\n]+\]\([^\s)]+\)|https?://[^\s<>]+')


def _timestamp(value):
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def _user_part(text):
    text = text.strip()
    if text.startswith('# AGENTS.md instructions for '):
        return ''
    text = _INJECTED_BLOCK.sub('', text).strip()
    # An incomplete setup record in a bounded tail must not become a prompt.
    return '' if _INJECTED_START.match(text) else text


def _content_text(content, user=False):
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [part['text'] for part in content if isinstance(part, dict)
                 and part.get('type') in ('text', 'input_text', 'output_text', 'Text')
                 and isinstance(part.get('text'), str)]
    else:
        return ''
    if user:
        parts = [_user_part(part) for part in parts]
    return '\n'.join(part.strip() for part in parts if part.strip()).strip()


def _visible(event):
    if not isinstance(event, dict) or any(event.get(flag) for flag in ('isSidechain', 'is_sidechain', 'sidechain')):
        return None
    kind = event.get('type')
    if kind == 'response_item':
        message = event.get('payload')
        if not isinstance(message, dict) or message.get('type') != 'message':
            return None
        source = 'codex'
    elif kind == 'event_msg':
        message = event.get('payload')
        if not isinstance(message, dict) or message.get('type') not in ('user_message', 'agent_message'):
            return None
        message = dict(message, role='user' if message['type'] == 'user_message' else 'assistant',
                       content=message.get('message', ''))
        source = 'legacy'
    elif kind in ('user', 'assistant'):
        message = event.get('message')
        if not isinstance(message, dict):
            return None
        source = 'claude'
    else:
        return None
    if any(message.get(flag) for flag in ('isSidechain', 'is_sidechain', 'sidechain')):
        return None
    role = message.get('role')
    if role not in ('user', 'assistant'):
        return None
    channels = {str(message.get(key) or '').lower() for key in ('channel', 'phase')}
    channels.update(str(event.get(key) or '').lower() for key in ('channel', 'phase'))
    if channels.intersection(('analysis', 'reasoning', 'thinking')):
        return None
    if role == 'assistant' and channels.difference(('', 'commentary', 'final', 'final_answer')):
        return None
    text = _content_text(message.get('content'), user=role == 'user')
    if not text:
        return None
    return {
        'role': role, 'source': source, 'text': text,
        'timestamp': _timestamp(event.get('timestamp') or message.get('timestamp')),
        'identifier': event.get('uuid') or event.get('id') or message.get('id') or '',
        'final': source in ('claude', 'legacy') or bool(channels.intersection(('final', 'final_answer'))),
        'explicit_final': source == 'codex' and bool(channels.intersection(('final', 'final_answer'))),
    }


def _turn_identity(user):
    # A record id survives timestamp formatting changes. No id means timestamp
    # plus exact prompt distinguishes repeated submissions of the same words.
    seed = [str(user['identifier']), user['text']] if user['identifier'] else [user['timestamp'], user['text']]
    return hashlib.sha256(json.dumps(seed, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()[:32]


def _completed_event(event):
    if (not isinstance(event, dict) or event.get('type') != 'event_msg'
            or any(event.get(flag) for flag in ('isSidechain', 'is_sidechain', 'sidechain'))):
        return False
    payload = event.get('payload')
    return (isinstance(payload, dict) and payload.get('type') == 'task_complete'
            and not any(payload.get(flag) for flag in ('isSidechain', 'is_sidechain', 'sidechain')))


def _turn_groups(records, fallback_user='', fallback_ts=0):
    """Normalize each event once and yield real user turns chronologically."""
    user = {'text': _user_part(fallback_user) if isinstance(fallback_user, str) else '',
            'timestamp': _timestamp(fallback_ts), 'identifier': '', 'source': 'fallback'}
    outputs = []
    has_user = False
    completed = False
    for event in records:
        if _completed_event(event):
            completed = True
            continue
        item = _visible(event)
        if item is None:
            continue
        if item['role'] == 'user':
            duplicate = (has_user and not outputs and not completed and item['text'] == user['text']
                         and {item['source'], user['source']} == {'codex', 'legacy'})
            if duplicate:
                if item['source'] == 'codex':
                    user = item
                continue
            if has_user or outputs:
                yield user, outputs, has_user, completed, True
            user = item
            has_user = True
            outputs = []
            completed = False
        else:
            outputs.append(item)
    yield user, outputs, has_user, completed, False


def _summarize_turn(user, outputs, has_user, completed, ended_by_user):
    canonical = [item for item in outputs if item['source'] != 'legacy']
    selected = canonical or outputs
    visible = []
    final_parts = []
    last_output_at = 0.0
    explicit_final = completed
    for item in selected:
        prose = clean_prose(item['text'])
        if not prose:
            continue
        explicit_final = explicit_final or item['explicit_final']
        if not visible or prose != visible[-1]:
            visible.append(prose)
        if item['final']:
            if item['source'] == 'claude':
                final_parts = [prose]
            elif not final_parts or prose != final_parts[-1]:
                final_parts.append(prose)
        last_output_at = max(last_output_at, item['timestamp'])
    return {
        'user_text': user['text'], 'started_at': user['timestamp'],
        'turn_id': _turn_identity(user), 'has_user': has_user,
        'source': selected[-1]['source'] if selected else user['source'],
        'text': '\n\n'.join(visible), 'final_text': '\n\n'.join(final_parts),
        'last_output_at': last_output_at, 'explicit_final': explicit_final,
        'ended_by_user': ended_by_user,
    }


def latest_turn(records, fallback_user='', fallback_ts=0):
    """Return visible prose for the newest real user turn in a record tail.

    Canonical Codex messages win over duplicate legacy notifications, but a
    legacy-only turn still works. ``has_user`` is false for a clipped tail, so
    callers can retain their previously established turn identity. Without an
    observed user, unchanged fallback inputs produce a stable synthetic id.
    ``explicit_final`` requires a canonical final or a task-complete event;
    ordinary legacy or Claude messages alone do not establish completion.
    """
    last = None
    for group in _turn_groups(records, fallback_user, fallback_ts):
        last = group
    return _summarize_turn(*last)


def recent_turns(records, limit=30):
    """Return up to 30 observed user turns for deterministic history backfill.

    The newest turn is included even while still running. Earlier entries have
    ``ended_by_user`` set, but that does not imply their requested work succeeded
    or finished: only ``explicit_final`` records a completion signal. Orphaned
    output at the start of a clipped tail has no observed prompt and is excluded.
    Records are consumed once, and duplicate legacy notifications do not create
    extra user turns. The caller need not make any model calls for this history.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    groups = deque(maxlen=min(limit, 30))
    for group in _turn_groups(records):
        if group[2]:
            groups.append(group)
    return [_summarize_turn(*group) for group in groups]


def _inline_prose(match):
    value = match.group(2).strip()
    # Filenames and short labels are prose; expressions and command examples
    # are not. Preserve the former without their code formatting.
    return value if re.fullmatch(r'[A-Za-z0-9_./:@-]+', value) else ''


def clean_prose(text):
    """Remove code and terminal noise, without inventing a successful outcome."""
    if not isinstance(text, str):
        return ''
    text = _ANSI.sub('', text).replace('\r', '')
    lines = []
    fence = ''
    fence_length = 0
    for raw in text.splitlines():
        mark = _FENCE.match(raw)
        if mark:
            token = mark.group(1)
            if not fence:
                fence, fence_length = token[0], len(token)
            elif token[0] == fence and len(token) >= fence_length:
                fence = ''
            continue
        if fence:
            continue
        line = raw.strip()
        if not line:
            if lines and lines[-1]:
                lines.append('')
            continue
        if (raw.startswith(('    ', '\t')) and not re.match(r'\s*(?:[-*+] |\d+[.)] )', raw)):
            continue
        if (line.startswith(('⎿', '└', '╰', '│')) or _CODE_LINE.match(line) or _TOOL_LINE.match(line)
                or _TRACE_LINE.match(line) or _DIFF_LINE.match(line)
                or re.match(r'^(?:\{.*\}|\[.*\])[,;]?$', line)):
            continue
        line = re.sub(r'^#{1,6}\s+', '', line)
        line = re.sub(r'^(?:[-*+] |\d+[.)] )', '', line)
        line = re.sub(r'(`+)([^`]*?)\1', _inline_prose, line)
        line = re.sub(r'(?<!\*)\*\*([^*]+)\*\*(?!\*)', r'\1', line)
        line = re.sub(r'\s+', ' ', line).strip()
        line = re.sub(r'\s+([.,;:!?])', r'\1', line)
        if line and re.search(r'[\w\u0080-\uffff]', line):
            lines.append(line)
    return '\n'.join(lines).strip()


def compact_summary(text, max_words=55, max_sentences=3):
    """Small extractive fallback; recent outcomes outrank opening commentary."""
    if not isinstance(max_words, int) or not isinstance(max_sentences, int) or max_words <= 0 or max_sentences <= 0:
        return ''
    prose = clean_prose(text)
    if not prose:
        return ''
    links = []

    def protect(match):
        links.append(match.group(0))
        return f'\x00LINK{len(links) - 1}\x00'

    protected = _LINK.sub(protect, prose)
    sentences = [part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', protected) if part.strip()]
    substantive = [part for part in sentences if not re.match(
        r'^(?:Let me know\b|Would you like\b|Want me to\b|I can also\b)', part, re.IGNORECASE)]
    sentences = substantive or sentences
    chosen = sentences[-max_sentences:]
    # Keep the latest useful outcome even if earlier selected prose fills the
    # budget. The fallback is deliberately concise, not a transcript preview.
    selected = []
    remaining = max_words
    for sentence in reversed(chosen):
        words = sentence.split()
        if len(words) <= remaining:
            selected.insert(0, sentence)
            remaining -= len(words)
        elif not selected:
            selected.insert(0, ' '.join(words[:remaining]).rstrip('.,;:') + '…')
            break
    result = ' '.join(selected)
    for index, link in enumerate(links):
        result = result.replace(f'\x00LINK{index}\x00', link)
    return result
