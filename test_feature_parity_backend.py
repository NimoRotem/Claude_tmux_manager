"""Account preferences, safe names, and owner/generation-bound project details."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Request, UploadFile
from pydantic import ValidationError
import pytest

import app
import project_saved


@pytest.fixture
def state(tmp_path, monkeypatch):
    user = {'id': 'alice', 'username': 'Alice', 'role': 'user'}
    owners = {'demo': 'alice'}
    life = {'owner_id': 'alice', 'generation': 'a' * 32, 'managed': True,
            'desired_state': 'running', 'resume_uuid': 'root'}
    root = {'resume_uuid': 'root'}
    def strict(name, owner=''):
        return ('alice', user) if owners.get(name) == 'alice' and owner in ('', 'alice') else None
    def binding(name, owner):
        if not strict(name, owner) or not root['resume_uuid']:
            return None
        return {'session_name': name, 'owner_id': owner, 'owner': user,
                'generation': life['generation'], 'resume_uuid': root['resume_uuid']}
    @contextmanager
    def lock(_):
        yield
    monkeypatch.setattr(app, 'cache', {})
    monkeypatch.setattr(app, '_user_period_stats_cache', {})
    monkeypatch.setattr(app, '_current_user', lambda _: user)
    monkeypatch.setattr(app, '_strict_session_owner', strict)
    monkeypatch.setattr(app, '_session_owner_id', lambda name: owners.get(name))
    monkeypatch.setattr(app, '_find_user_by_id', lambda uid: user if uid == 'alice' else None)
    monkeypatch.setattr(app, '_session_lifecycle', SimpleNamespace(get=lambda name: life))
    monkeypatch.setattr(app, '_terminal_history_binding', binding)
    monkeypatch.setattr(app, '_validated_session_root_thread_id', lambda *a: root['resume_uuid'])
    monkeypatch.setattr(app, '_session_operation_lock', lock)
    monkeypatch.setattr(app, '_user_data_dir', lambda u: tmp_path / u['id'])
    monkeypatch.setattr(app, '_user_uploads_dir', lambda u: tmp_path / u['id'] / 'uploads')
    monkeypatch.setattr(app, '_session_tab_labels', app.LockedJsonStore(tmp_path / 'labels.json', dict))
    source = tmp_path / 'source.jsonl'
    records = [
        {'type': 'session_meta', 'payload': {'id': 'root', 'session_id': 'root', 'thread_source': 'user'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'channel': 'final',
          'content': [{'type': 'output_text', 'text': 'https://example.test/app\nPassword: demo-value'}]}},
    ]
    source.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
    monkeypatch.setattr(app, '_open_session_close_rollout', lambda _: (os.open(source, os.O_RDONLY), 'source', (0, 0, 0, 0)))
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b''})
    return SimpleNamespace(user=user, owners=owners, life=life, root=root, path=tmp_path,
                           source=source, request=request, binding=binding)


def data(response):
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_preference_default_true_then_account_local_update(state):
    response = await app.api_preferences(state.request)
    assert data(response) == {'auto_session_names': True}
    assert 'no-store' in response.headers['cache-control']
    await app.api_save_preferences(state.request, app.UserPreferences(auto_session_names=False))
    assert data(await app.api_preferences(state.request)) == {'auto_session_names': False}
    assert app._user_preferences({'id': 'bob'}) == {'auto_session_names': True}
    assert not app._auto_session_names_enabled('alice')
    assert not app._auto_session_names_enabled('missing')


@pytest.mark.parametrize('value', ['false', 0, None])
def test_preferences_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        app.UserPreferences(auto_session_names=value)


@pytest.mark.asyncio
async def test_preferences_rejects_unauthenticated_and_symlink(state, monkeypatch):
    folder = state.path / 'alice'
    folder.mkdir()
    (folder / 'preferences.json').symlink_to(state.source)
    assert (await app.api_preferences(state.request)).status_code == 409
    monkeypatch.setattr(app, '_current_user', lambda _: None)
    assert (await app.api_preferences(state.request)).status_code == 401
    assert (await app.api_save_preferences(state.request, app.UserPreferences(auto_session_names=False))).status_code == 401


@pytest.mark.asyncio
async def test_manual_name_keeps_stable_identity_and_wins_over_auto(state):
    before = dict(state.life)
    response = await app.api_rename_session(state.request, 'demo', app.RenameSessionBody(name=' My project '))
    assert data(response) == {'ok': True, 'name': 'demo', 'tab_label': 'My project'}
    assert state.life == before
    assert app._session_tab_label('demo') == 'My project'
    assert app._queue_session_tab_label('demo', 'alice', 'Change the title') == ''
    await app.api_save_preferences(state.request, app.UserPreferences(auto_session_names=False))
    assert app._session_tab_label('demo') == 'My project'


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['', ' \t ', 'bad\nname', 'bad\x7fname'])
async def test_manual_name_rejects_controls_and_empty(state, name):
    assert (await app.api_rename_session(state.request, 'demo', app.RenameSessionBody(name=name))).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'generation', 'deleting'])
async def test_rename_revalidates_inside_operation_lock(state, monkeypatch, change):
    @contextmanager
    def changed(_):
        if change == 'owner':
            state.owners['demo'] = 'bob'
        elif change == 'generation':
            state.life['generation'] = 'b' * 32
        else:
            state.life['desired_state'] = 'deleting'
        yield
    monkeypatch.setattr(app, '_session_operation_lock', changed)
    response = await app.api_rename_session(state.request, 'demo', app.RenameSessionBody(name='Label'))
    assert response.status_code == 409
    assert not app._session_tab_label_rows()


@pytest.mark.asyncio
async def test_preference_or_manual_change_during_naming_await_is_not_overwritten(state, monkeypatch):
    app._queue_session_tab_label('demo', 'alice', 'Fix the mobile login', started_at=1000)
    monkeypatch.setattr(app, '_latest_tab_label_prompt', lambda *a: 'Fix mobile login')
    monkeypatch.setattr(app, '_detect_activity_raw', lambda _: {'status': 'busy'})
    async def summary(*_):
        await app.api_rename_session(state.request, 'demo', app.RenameSessionBody(name='My chosen name'))
        return 'Mobile Login'
    monkeypatch.setattr(app, '_summarize_two_word_tab_label', summary)
    assert await app._session_tab_label_pass(now=1200, live_session_names={'demo'}) == 0
    assert app._session_tab_label('demo') == 'My chosen name'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['idle', 'disabled', 'generation'])
async def test_auto_name_postawait_guards(state, monkeypatch, change):
    app._queue_session_tab_label('demo', 'alice', 'Fix mobile login', started_at=1000)
    status = {'status': 'busy'}
    monkeypatch.setattr(app, '_latest_tab_label_prompt', lambda *a: 'Fix mobile login')
    monkeypatch.setattr(app, '_detect_activity_raw', lambda _: status)
    async def summary(*_):
        if change == 'idle':
            status['status'] = 'idle'
        elif change == 'disabled':
            await app.api_save_preferences(state.request, app.UserPreferences(auto_session_names=False))
        else:
            state.life['generation'] = 'b' * 32
        return 'Mobile Login'
    monkeypatch.setattr(app, '_summarize_two_word_tab_label', summary)
    assert await app._session_tab_label_pass(now=1200, live_session_names={'demo'}) == 0
    assert app._session_tab_label('demo') == ''


@pytest.mark.asyncio
async def test_idle_before_deadline_cancels_old_name_request(state, monkeypatch):
    app._queue_session_tab_label('demo', 'alice', 'Fix mobile login', started_at=1000)
    monkeypatch.setattr(app, '_detect_activity_raw', lambda _: {'status': 'idle'})
    summary = AsyncMock()
    monkeypatch.setattr(app, '_summarize_two_word_tab_label', summary)
    assert await app._session_tab_label_pass(now=1060, live_session_names={'demo'}) == 0
    assert 'pending' not in app._session_tab_label_rows()['demo']
    monkeypatch.setattr(app, '_detect_activity_raw', lambda _: {'status': 'busy'})
    assert await app._session_tab_label_pass(now=1140, live_session_names={'demo'}) == 0
    summary.assert_not_awaited()


def test_stale_manual_name_does_not_cross_generation(state):
    app._session_tab_labels.update(lambda data: data.update(sessions={'demo': {
        'owner_id': 'alice', 'generation': 'b' * 32, 'manual_label': 'Old conversation'}}))
    assert app._queue_session_tab_label('demo', 'alice', 'Fresh task')
    assert 'manual_label' not in app._session_tab_label_rows()['demo']


@pytest.mark.parametrize('prefix', ['[Voice instructions] ', '  [VOICE INSTRUCTIONS]\n'])
def test_voice_task_label_uses_task_not_transport_marker(prefix):
    assert app._fallback_two_word_tab_label(prefix + 'Fix homepage buttons') == 'Homepage Buttons'


@pytest.mark.asyncio
@pytest.mark.parametrize('generated', [True, False])
async def test_new_generated_retry_id_is_not_manual_label(state, monkeypatch, generated):
    monkeypatch.setattr(app, '_validated_new_session_options', lambda *a: {})
    monkeypatch.setattr(app, '_user_codex_config_dir', lambda u: state.path / 'codex')
    monkeypatch.setattr(app, '_uses_private_account_runtime', lambda u: False)
    monkeypatch.setattr(app, '_ensure_codex_auth_with_fallback', lambda *a: None)
    monkeypatch.setattr(app, '_codex_cli_readiness', lambda: (True, '', {}))
    monkeypatch.setattr(app, '_exact_tmux_session_id', lambda name: '')
    monkeypatch.setattr(app, '_durable_session_name_reserved', lambda name: False)
    calls = []
    def run(command, **kw):
        calls.append(command)
        return SimpleNamespace(stdout='$1\tdemo\n', returncode=0, stderr='')
    monkeypatch.setattr(app.subprocess, 'run', run)
    monkeypatch.setattr(app, '_finish_created_session', AsyncMock(return_value=app.JSONResponse({'ok': True})))
    response = await app._api_create_session_tmux_locked(state.request,
        app.CreateSession(name='demo', name_generated=generated))
    assert response.status_code == 200
    assert calls[0][calls[0].index('-s') + 1] == 'demo'
    label = app._session_tab_label_rows().get('demo', {}).get('manual_label')
    assert label == (None if generated else 'demo')


@pytest.mark.asyncio
async def test_saved_items_canonical_prose_is_private_and_deduplicated(state):
    for _ in range(2):
        response = await app.api_session_saved_items(state.request, 'demo')
        assert response.status_code == 200
        assert 'no-store' in response.headers['cache-control']
        assert data(response) == {'urls': ['https://example.test/app'],
                                  'creds': [{'label': 'Password', 'value': 'demo-value'}],
                                  'files': [], 'catching_up': False}


@pytest.mark.asyncio
async def test_saved_items_cross_owner_and_unauthed_fail_closed(state, monkeypatch):
    state.user['id'] = 'bob'
    assert (await app.api_session_saved_items(state.request, 'demo')).status_code == 404
    monkeypatch.setattr(app, '_current_user', lambda _: None)
    assert (await app.api_session_saved_items(state.request, 'demo')).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'generation', 'root', 'deleting'])
async def test_saved_items_identity_change_before_commit_does_not_persist(state, monkeypatch, change):
    def uploads(_):
        if change == 'owner':
            state.owners['demo'] = 'bob'
        elif change == 'generation':
            state.life['generation'] = 'b' * 32
        elif change == 'root':
            state.root['resume_uuid'] = 'different'
        else:
            state.life['desired_state'] = 'deleting'
        return []
    monkeypatch.setattr(project_saved, 'upload_paths', uploads)
    response = await app.api_session_saved_items(state.request, 'demo')
    assert response.status_code == 409
    assert not list((state.path / 'alice' / 'saved-projects').glob('*.json'))


@pytest.mark.asyncio
async def test_saved_items_bad_metadata_fails_closed(state):
    state.source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'wrong'}}) + '\n')
    assert (await app.api_session_saved_items(state.request, 'demo')).status_code == 409


@pytest.mark.asyncio
async def test_rootless_new_session_lists_own_uploads_but_invalid_known_root_fails(state):
    state.root['resume_uuid'] = None
    assert (await app.api_session_saved_items(state.request, 'demo')).status_code == 409
    state.life['resume_uuid'] = ''
    uploads = state.path / 'alice' / 'uploads' / 'demo'
    uploads.mkdir(parents=True)
    (uploads / 'image.png').write_bytes(b'example')
    response = await app.api_session_saved_items(state.request, 'demo')
    assert response.status_code == 200
    assert data(response)['files'] == [str(uploads / 'image.png')]


@pytest.mark.asyncio
async def test_saved_items_revalidates_after_worker_returns(state, monkeypatch):
    real = app._collect_session_saved_items
    def changed(*args):
        result = real(*args)
        state.life['generation'] = 'b' * 32
        return result
    monkeypatch.setattr(app, '_collect_session_saved_items', changed)
    assert (await app.api_session_saved_items(state.request, 'demo')).status_code == 409


@pytest.mark.parametrize('period,days', [('today', 0), ('2', 2), ('5', 5), ('7', 7)])
def test_usage_period_uses_utc_and_each_account_home(state, monkeypatch, period, days):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 14, 15, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(app, 'datetime', Clock)
    seen = []
    monkeypatch.setattr(app, '_prompt_counts_by_user', lambda cutoff: {'alice': {period: 3}})
    monkeypatch.setattr(app, '_user_codex_config_dir', lambda user: state.path / user['id'])
    def tokens(home, cutoffs):
        seen.append((home, cutoffs))
        return {window: {'totalTokens': 42} for window in cutoffs}
    monkeypatch.setattr(app, '_token_usage_for_home', tokens)
    result = app._user_period_stats([state.user, {'id': 'bob'}], period)
    assert result['alice'] == {'total_prompts': 3, 'total_tokens': 42}
    assert result['bob'] == {'total_prompts': 0, 'total_tokens': 42}
    cutoff = datetime.fromisoformat(seen[0][1][period])
    assert cutoff.day == 14 - days
    assert cutoff.hour == (0 if period == 'today' else 15)
    assert [p for p, _ in seen] == [state.path / 'alice', state.path / 'bob']


def test_usage_filters_share_scan_and_return_independent_data(state, monkeypatch):
    calls = []
    monkeypatch.setattr(app, '_prompt_counts_by_user', lambda windows: {})
    monkeypatch.setattr(app, '_user_codex_config_dir', lambda user: state.path / user['id'])
    def tokens(home, windows):
        calls.append(home)
        return {window: {'totalTokens': 42} for window in windows}
    monkeypatch.setattr(app, '_token_usage_for_home', tokens)
    first = app._user_period_stats([state.user], 'today')
    first['alice']['total_tokens'] = -1
    assert app._user_period_stats([state.user], 'today')['alice']['total_tokens'] == 42
    assert app._user_period_stats([state.user], '7')['alice']['total_tokens'] == 42
    assert calls == [state.path / 'alice']
    app._user_period_stats([{'id': 'bob'}], '7')
    assert calls == [state.path / 'alice', state.path / 'bob']


def test_usage_filter_cache_expires(state, monkeypatch):
    clock = [1000]
    calls = []
    monkeypatch.setattr(app.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(app, '_prompt_counts_by_user', lambda windows: {})
    monkeypatch.setattr(app, '_user_codex_config_dir', lambda user: state.path / user['id'])
    def tokens(home, windows):
        calls.append(home)
        return {window: {'totalTokens': 42} for window in windows}
    monkeypatch.setattr(app, '_token_usage_for_home', tokens)
    app._user_period_stats([state.user], 'today')
    clock[0] = 1119
    app._user_period_stats([state.user], '2')
    assert len(calls) == 1
    clock[0] = 1120
    app._user_period_stats([state.user], '5')
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_usage_filters_remain_admin_only_and_validate_period(state, monkeypatch):
    assert (await app.api_admin_list_users(state.request)).status_code == 403
    state.user['role'] = 'admin'
    request = Request({**state.request.scope, 'query_string': b'usage_period=unknown'})
    assert (await app.api_admin_list_users(request)).status_code == 400
    monkeypatch.setattr(app, '_load_users', lambda: [])
    stats = AsyncMock(return_value={})
    monkeypatch.setattr(app, '_user_period_stats', lambda *a: {})
    response = await app.api_admin_list_users(state.request)
    assert data(response) == {'users': [], 'usage_period': 'all'}


@pytest.mark.asyncio
async def test_dictation_uses_managed_key_without_environment_key(state, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setattr(app, '_managed_openai_key', lambda: 'test-managed-value')
    audio = SimpleNamespace(read=AsyncMock(return_value=b''))
    response = await app.api_transcribe(audio)
    assert response.status_code == 400
    assert data(response)['error'] == 'Empty audio.'
