import json
import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app
import terminal_history as history


BINDING = {'owner_id': 'admin', 'generation': 'a' * 32, 'resume_uuid': 'root',
           'session_name': 'demo', 'owner': {'id': 'admin'}}


def message(text, role='assistant', **extra):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
            'content': [{'type': 'output_text', 'text': text}], **extra}}


def write_records(path, records):
    path.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in records))


def all_pages(path, **options):
    pages, cursor = [], ''
    with path.open('rb') as source:
        for _ in range(10000):
            page = history.read_page(source.fileno(), BINDING, cursor, **options)
            pages.insert(0, page['entries'])
            if page['at_start']:
                return [entry for entries in pages for entry in entries]
            assert page['cursor'] and page['cursor'] != cursor
            cursor = page['cursor']
    raise AssertionError('History pagination did not reach the beginning')


def test_history_reaches_original_prompt_beyond_both_former_limits(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    original = 'The very first request'
    long_reply = '\n'.join('Line %d: café 🌍' % i for i in range(6500))
    write_records(path, [message(original, 'user'), message(long_reply), message('Latest reply')])
    entries = all_pages(path, page_chars=2300)
    grouped = {}
    for entry in entries:
        grouped[entry['id']] = grouped.get(entry['id'], '') + entry['text']
    assert list(grouped.values()) == [original, long_reply, 'Latest reply']


def test_non_display_records_do_not_create_a_false_beginning(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [message('First', 'user')] + [{'type': 'token_usage_record'}] * 900 + [message('Last')])
    assert [e['text'] for e in all_pages(path, max_records=40)] == ['First', 'Last']


def test_duplicate_notifications_setup_and_reasoning_are_not_displayed(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [message('# AGENTS.md instructions for /example', 'user'),
        message('<environment_context>setup</environment_context>', 'user'),
        message('Secret reasoning', phase='analysis'),
        {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 'Secret reasoning'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'Visible reply'}},
        message('Actual prompt', 'user'), message('Visible reply')])
    assert [e['text'] for e in all_pages(path)] == ['Actual prompt', 'Visible reply']


def test_tools_are_available_in_exact_view_and_binary_images_are_not_expanded(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [message('First', 'user'),
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'functions.exec', 'input': 'read something'}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'output': [
            {'type': 'input_text', 'text': 'complete text result'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,not-for-rendering'}]}}, message('Done')])
    assert len(all_pages(path)) == 2
    entries = all_pages(path, include_tools=True)
    assert [e['kind'] for e in entries] == ['user', 'tool', 'tool', 'assistant']
    assert entries[2]['text'] == 'complete text result\n[Image attachment]'


def test_cursor_survives_append_but_rejects_another_owner_root_or_mode(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [message('First'), message('Last')])
    with path.open('rb') as source:
        page = history.read_page(source.fileno(), BINDING, page_chars=4)
        with path.open('ab') as output:
            output.write(json.dumps(message('New reply')).encode() + b'\n')
        next_page = history.read_page(source.fileno(), BINDING, page['cursor'])
        assert [e['text'] for e in next_page['entries']] == ['First']
        for binding in (dict(BINDING, owner_id='other'), dict(BINDING, resume_uuid='other'), dict(BINDING, generation='b' * 32)):
            with pytest.raises(history.HistoryChanged):
                history.read_page(source.fileno(), binding, page['cursor'])
        with pytest.raises(history.HistoryChanged):
            history.read_page(source.fileno(), BINDING, page['cursor'], include_tools=True)


@pytest.mark.parametrize('cursor', ['invalid!', 'W10', 'bnVsbA', 'a' * 301])
def test_invalid_cursors_are_rejected(tmp_path, cursor):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [message('First')])
    with path.open('rb') as source, pytest.raises(history.HistoryChanged):
        history.read_page(source.fileno(), BINDING, cursor)


@pytest.fixture
def client(monkeypatch):
    user = {'id': 'admin', 'username': 'Tester', 'role': 'admin'}
    monkeypatch.setattr(app, '_current_user', lambda request: user)
    monkeypatch.setattr(app, '_user_can_access_session', lambda user, name: name == 'demo')
    monkeypatch.setattr(app, '_terminal_history_binding', lambda *args: dict(BINDING))
    return TestClient(app.app, cookies={app.AUTH_COOKIE: app._make_token('admin')})


def test_history_api_checks_ownership_before_opening(client, monkeypatch):
    read = Mock(side_effect=AssertionError('Other account transcript opened'))
    monkeypatch.setattr(app, '_read_terminal_history', read)
    assert client.get('/api/sessions/other/terminal-history').status_code == 404
    read.assert_not_called()


def test_history_api_discards_response_if_root_changes_during_read(client, monkeypatch):
    bindings = iter([dict(BINDING), dict(BINDING, resume_uuid='replacement')])
    monkeypatch.setattr(app, '_terminal_history_binding', lambda *args: next(bindings))
    monkeypatch.setattr(app, '_read_terminal_history', lambda *args: {'entries': [{'text': 'Must not escape'}]})
    response = client.get('/api/sessions/demo/terminal-history')
    assert response.status_code == 409
    assert 'Must not escape' not in response.text


def test_history_api_is_non_cacheable_and_pages_exact_owner_file(client, monkeypatch):
    read = Mock(return_value={'entries': [{'text': 'First request'}], 'at_start': True, 'cursor': ''})
    monkeypatch.setattr(app, '_read_terminal_history', read)
    response = client.get('/api/sessions/demo/terminal-history?tools=true')
    assert response.status_code == 200
    assert 'no-store' in response.headers['cache-control']
    read.assert_called_once_with(BINDING, '', True)


def test_opened_transcript_metadata_is_checked_again(tmp_path, monkeypatch):
    path = tmp_path / 'rollout.jsonl'
    write_records(path, [{'type': 'session_meta', 'payload': {'id': 'other', 'session_id': 'other', 'thread_source': 'user'}}, message('Other conversation')])
    monkeypatch.setattr(app, '_open_session_close_rollout', lambda binding: (os.open(path, os.O_RDONLY), '', ()))
    with pytest.raises(history.HistoryChanged):
        app._read_terminal_history(BINDING, '')
