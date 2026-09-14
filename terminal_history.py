"""Page the visible conversation from a securely opened Codex rollout.

The caller owns authorization and opens the exact owner-bound root. Cursors
are offsets in that root, never filenames. Reasoning, system instructions,
credentials in session metadata, and binary image payloads are not rendered.
"""
import base64
import hashlib
import json
import os


class HistoryChanged(ValueError):
    pass


def identity(binding, info):
    parts = [binding[k] for k in ('owner_id', 'generation', 'resume_uuid')]
    parts += [info.st_dev, info.st_ino]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _cursor(history_id, end, head=None):
    raw = json.dumps([history_id, end, head], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _position(cursor, history_id, size):
    if not cursor:
        return size, None
    try:
        if len(cursor) > 300:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        saved_id, end, head = value
        if saved_id != history_id or type(end) is not int or not 0 <= end <= size:
            raise ValueError()
        if head is not None and (type(head) is not int or head < 0):
            raise ValueError()
        return end, head
    except (ValueError, TypeError, UnicodeError):
        raise HistoryChanged('Session history changed; reload its history') from None


def _previous_record(fd, end):
    """Read one complete record backwards, retaining its exact byte boundary."""
    stop = end
    if stop and os.pread(fd, 1, stop - 1) == b'\n':
        stop -= 1
    chunks = []
    while stop:
        start = max(0, stop - 65536)
        chunk = os.pread(fd, stop - start, start)
        newline = chunk.rfind(b'\n')
        if newline >= 0:
            chunks.append(chunk[newline + 1:])
            return b''.join(reversed(chunks)), start + newline + 1
        chunks.append(chunk)
        stop = start
    return b''.join(reversed(chunks)), 0


def _text_parts(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get('type', '')
        if kind in ('text', 'input_text', 'output_text', 'Text'):
            text = part.get('text', '')
            if isinstance(text, str):
                parts.append(text)
        elif kind in ('image', 'input_image', 'output_image'):
            parts.append('[Image attachment]')
        elif kind in ('audio', 'input_audio', 'output_audio'):
            parts.append('[Audio attachment]')
    return '\n'.join(parts)


def visible_record(event):
    """Use canonical response items so duplicate event notifications stay out."""
    if not isinstance(event, dict) or event.get('type') != 'response_item':
        return None
    p = event.get('payload')
    if not isinstance(p, dict):
        return None
    kind = p.get('type')
    label = ''
    if kind == 'message' and p.get('role') in ('user', 'assistant'):
        if p.get('phase') == 'analysis' or p.get('channel') == 'analysis':
            return None
        role = p['role']
        parts = p.get('content') or []
        if role == 'user' and isinstance(parts, list):
            # CLI-injected workspace instructions are not submitted prompts.
            parts = [x for x in parts if not isinstance(x, dict) or not str(x.get('text', '')).lstrip().startswith(
                ('# AGENTS.md instructions for ', '<environment_context>'))]
        text = _text_parts(parts)
    elif kind in ('function_call', 'custom_tool_call'):
        role = 'tool'
        label = 'Tool: ' + str(p.get('name') or 'call')
        text = p.get('arguments') if kind == 'function_call' else p.get('input')
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        role = 'tool'
        label = 'Tool result'
        value = p.get('output')
        if isinstance(value, (str, list)):
            text = _text_parts(value)
        elif isinstance(value, dict):
            text = _text_parts(value.get('content', []))
        else:
            text = ''
    else:
        return None
    if not text or not text.strip():
        return None
    return {'kind': role, 'label': label, 'text': text,
            'timestamp': str(event.get('timestamp') or '')}


def read_page(fd, binding, cursor='', page_chars=60000, max_records=200, include_tools=False):
    info = os.fstat(fd)
    history_id = identity(binding, info) + ('t' if include_tools else 'm')
    end, head = _position(cursor, history_id, info.st_size)
    entries = []
    remaining = page_chars
    scanned = 0
    while end and remaining > 0 and scanned < max_records:
        record_end = end
        raw, start = _previous_record(fd, end)
        scanned += 1
        try:
            item = visible_record(json.loads(raw))
        except (ValueError, UnicodeError):
            item = None
        if not item or (item['kind'] == 'tool' and not include_tools):
            end, head = start, None
            continue
        text = item['text']
        available = len(text) if head is None else head
        if available > len(text):
            raise HistoryChanged('Session history was rewritten')
        take_from = max(0, available - remaining)
        # Split huge messages across pages, rather than truncating their text.
        item.update(text=text[take_from:available], id=str(start),
                    continued_before=take_from > 0, continued_after=available < len(text))
        entries.append(item)
        remaining -= available - take_from
        if take_from:
            end, head = record_end, take_from
            break
        end, head = start, None
    return {'history_id': history_id, 'entries': list(reversed(entries)),
            'cursor': _cursor(history_id, end, head) if end else '',
            'has_more': bool(end), 'at_start': not end}
