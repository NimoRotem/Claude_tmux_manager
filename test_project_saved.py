"""Saved details keep visible exact values without following private-state links."""
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

import project_saved as saved


def message(text, **fields):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'channel': 'final', 'content': [{'type': 'output_text', 'text': text}], **fields}}


def test_exact_credentials_and_links_are_deduplicated():
    text = 'See [app](https://example.test/app).\nPassword: `a,b;c!`\n| Username | demo |\n/home/demo/app.py:12'
    result = saved.collect_saved_items({}, [text, text])
    assert result == {'urls': ['https://example.test/app'], 'creds': [
        {'label': 'Password', 'value': 'a,b;c!'}, {'label': 'Username', 'value': 'demo'}],
        'files': ['/home/demo/app.py']}


def test_injected_instructions_and_oversized_values_are_not_saved():
    result = saved.collect_saved_items({}, ['# AGENTS.md instructions\nPassword: hidden',
        '<environment_context>https://example.test/hidden', 'Password: ' + 'x' * (saved.MAX_VALUE + 1)])
    assert result == {'urls': [], 'creds': [], 'files': []}


@pytest.mark.parametrize('kind', ['canonical', 'legacy', 'patch'])
@pytest.mark.parametrize('location', ['event', 'payload'])
@pytest.mark.parametrize('field,value', [('channel', 'analysis'), ('phase', 'reasoning'),
                                        ('phase', 'thinking'), ('isSidechain', True), ('sidechain', True)])
def test_hidden_events_never_produce_saved_items(kind, location, field, value):
    if kind == 'canonical':
        event = message('Password: hidden\nhttps://example.test/hidden')
    elif kind == 'legacy':
        event = {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Password: hidden'}}
    else:
        event = {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'apply_patch',
                 'input': '*** Add File: /tmp/hidden.py'}}
    (event if location == 'event' else event['payload'])[field] = value
    assert saved.event_items(event) == ([], [])


def test_canonical_legacy_and_patch_input_work_without_tool_output():
    assert saved.event_items(message('https://example.test')) == (['https://example.test'], [])
    legacy = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Username: demo'}}
    assert saved.event_items(legacy) == (['Username: demo'], [])
    patch = '*** Add File: a.py\n*** Update File: b.py\n*** Move to: c.py\n*** Delete File: d.py'
    event = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'functions.apply_patch',
             'arguments': json.dumps({'patch': patch})}}
    assert saved.event_items(event) == ([], ['a.py', 'b.py', 'c.py'])
    event['payload'].update(type='function_call_output', output='Password: hidden')
    assert saved.event_items(event) == ([], [])


def test_saved_item_count_is_bounded():
    result = saved.collect_saved_items({}, ['\n'.join(f'https://example.test/{i}' for i in range(400))])
    assert len(result['urls']) == saved.MAX_ITEMS
    assert result['urls'][-1].endswith('/399')


@pytest.mark.parametrize('bad', [{}, {'bad': 'shape'}, 1, True, 'bad', None])
def test_malformed_previous_buckets_are_ignored(bad):
    result = saved.collect_saved_items({'urls': bad, 'creds': bad, 'files': bad}, ['https://example.test'])
    assert result == {'urls': ['https://example.test'], 'creds': [], 'files': []}


def test_dense_page_uses_one_collection_pass(tmp_path, monkeypatch):
    path = rollout(tmp_path, [message(f'https://example.test/{i}') for i in range(18000)])
    real = saved.collect_saved_items
    calls = []
    def collect(*args):
        calls.append(1)
        return real(*args)
    monkeypatch.setattr(saved, 'collect_saved_items', collect)
    with path.open('rb') as stream:
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, {})
    assert calls == [1]


def rollout(tmp_path, records, root='root'):
    path = tmp_path / 'rollout.jsonl'
    meta = {'type': 'session_meta', 'payload': {'id': root, 'session_id': root, 'thread_source': 'user'}}
    path.write_text('\n'.join(json.dumps(row) for row in [meta, *records]) + '\n')
    return path


def test_incremental_scan_handles_partial_records_and_persists_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(saved, 'SCAN_BYTES', 300)
    path = rollout(tmp_path, [message(f'https://example.test/{i}') for i in range(8)])
    state = {}
    for _ in range(20):
        with path.open('rb') as stream:
            saved.scan_rollout(stream, {'resume_uuid': 'root'}, state)
        if not state['catching_up']:
            break
    assert state['offset'] == path.stat().st_size
    assert state['urls'] == [f'https://example.test/{i}' for i in range(8)]
    before = dict(state)
    with path.open('rb') as stream:
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, state)
    assert state == before


def test_oversized_line_skips_to_next_complete_record(tmp_path, monkeypatch):
    monkeypatch.setattr(saved, 'SCAN_BYTES', 300)
    path = rollout(tmp_path, [message('x' * 1200), message('https://example.test/after')])
    state = {}
    for _ in range(15):
        with path.open('rb') as stream:
            saved.scan_rollout(stream, {'resume_uuid': 'root'}, state)
        if not state['catching_up']:
            break
    assert state['urls'] == ['https://example.test/after']


def test_wrong_opened_metadata_fails_closed(tmp_path):
    path = rollout(tmp_path, [message('Password: wrong')], root='other')
    with path.open('rb') as stream, pytest.raises(ValueError, match='identity'):
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, {})


@pytest.mark.parametrize('meta', [[], 1, None, {'type': 'session_meta', 'payload': []}])
def test_malformed_metadata_fails_with_value_error(tmp_path, meta):
    path = tmp_path / 'bad.jsonl'
    path.write_text(json.dumps(meta) + '\n')
    with path.open('rb') as stream, pytest.raises(ValueError):
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, {})


def test_replaced_source_clears_old_details(tmp_path):
    path = rollout(tmp_path, [message('https://example.test/old')])
    state = {}
    with path.open('rb') as stream:
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, state)
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    new = rollout(replacement, [message('https://example.test/new')])
    new.replace(path)
    with path.open('rb') as stream:
        saved.scan_rollout(stream, {'resume_uuid': 'root'}, state)
    assert state['urls'] == ['https://example.test/new']


@pytest.mark.parametrize('target', ['file', 'lock', 'parent', 'hardlink'])
def test_private_state_rejects_unsafe_links(tmp_path, target):
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'data.json'
    secret.write_text('{"unrelated":"leave alone"}')
    folder = tmp_path / 'account'
    folder.mkdir()
    path = folder / 'state.json'
    if target == 'parent':
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
    elif target == 'hardlink':
        os.link(secret, path)
    else:
        (path if target == 'file' else folder / 'state.json.lock').symlink_to(secret)
    with pytest.raises((OSError, ValueError)):
        saved.private_json(path, lambda data: data.update(changed=True))
    assert json.loads(secret.read_text()) == {'unrelated': 'leave alone'}


def test_private_state_updates_merge_across_threads(tmp_path):
    path = tmp_path / 'account' / 'state.json'
    def increment(_):
        saved.private_json(path, lambda row: row.update(count=row.get('count', 0) + 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(30)))
    assert saved.private_json(path) == {'count': 30}
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_mutation_never_commits(tmp_path):
    path = tmp_path / 'state.json'
    saved.private_json(path, lambda row: row.update(original=True))
    def fail(row):
        row['original'] = False
        raise ValueError('identity changed')
    with pytest.raises(ValueError):
        saved.private_json(path, fail)
    assert saved.private_json(path) == {'original': True}


def test_uploads_ignore_links_without_opening_files(tmp_path):
    folder = tmp_path / 'uploads'
    folder.mkdir()
    (folder / 'safe.txt').write_text('not read')
    (folder / 'linked.txt').symlink_to(folder / 'safe.txt')
    assert saved.upload_paths(folder) == [str(folder / 'safe.txt')]
    alias = tmp_path / 'alias'
    alias.symlink_to(folder, target_is_directory=True)
    with pytest.raises(OSError):
        saved.upload_paths(alias)
