"""Settings restart waits only before writes and remains generation-bound."""
from contextlib import asynccontextmanager, contextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import Request
import pytest

import app


@pytest.fixture
def restart(tmp_path, monkeypatch):
    row = {'managed': True, 'owner_id': 'admin', 'generation': 'a' * 32,
           'desired_state': 'running', 'restore_on_startup': True,
           'resume_uuid': '01a035f8-3188-7c21-8cca-582b01ad3002',
           'cwd': str(tmp_path), 'had_conversation_input': True,
           'launch_options': {'model': 'gpt-5.6-terra', 'effort': 'low', 'no_fallback': True}}
    user = {'id': 'admin', 'username': 'admin', 'role': 'admin'}
    clock = [0.0]
    locks = {'tries': 0, 'busy': 0, 'released': 0}
    changes = []
    @contextmanager
    def operation(_):
        locks['tries'] += 1
        if locks['tries'] <= locks['busy']:
            raise app._SessionOperationBusy('busy')
        try:
            yield
        finally:
            locks['released'] += 1
    @asynccontextmanager
    async def tmux():
        yield
    async def sleep(duration):
        clock[0] += duration
        if changes:
            changes.pop(0)()
    def matches(name, **wanted):
        return all(row.get(k) == v for k, v in wanted.items() if k != 'desired_states') and (
            'desired_states' not in wanted or row['desired_state'] in wanted['desired_states'])
    monkeypatch.setattr(app, '_session_operation_lock', operation)
    monkeypatch.setattr(app, '_async_tmux_server_mutation_lock', tmux)
    monkeypatch.setattr(app.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(app.asyncio, 'sleep', sleep)
    monkeypatch.setattr(app, '_session_lifecycle', SimpleNamespace(get=lambda name: dict(row), matches=matches))
    monkeypatch.setattr(app, '_strict_session_owner', lambda name, owner='':
                        ('admin', user) if row['owner_id'] == 'admin' and owner in ('', 'admin') else None)
    checkpoint = Mock(return_value=row)
    monkeypatch.setattr(app, '_checkpoint_active_session', checkpoint)
    monkeypatch.setattr(app, '_active_session_root_thread_id', lambda *a: row['resume_uuid'])
    monkeypatch.setattr(app, '_validated_session_root_thread_id', lambda name, root, owner: root)
    monkeypatch.setattr(app, '_durable_session_cwd', lambda *a: str(tmp_path))
    monkeypatch.setattr(app, 'get_session_cwd', lambda name: str(tmp_path))
    monkeypatch.setattr(app, '_exact_tmux_session_id', lambda name: '$1')
    monkeypatch.setattr(app, '_tmux_session_matches_owner', lambda *a: True)
    monkeypatch.setattr(app, '_async_is_codex_running', AsyncMock(return_value=False))
    ensure = AsyncMock(return_value=True)
    monkeypatch.setattr(app, '_ensure_codex_running', ensure)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(app.subprocess, 'run', run)
    return SimpleNamespace(row=row, user=user, locks=locks, changes=changes, clock=clock,
                           run=run, ensure=ensure, checkpoint=checkpoint, path=tmp_path)


@pytest.mark.asyncio
async def test_transient_contention_retries_lock_then_restarts_once(restart):
    restart.locks['busy'] = 2
    assert await app._restart_codex_for_session('demo', 'admin') == (True, True)
    assert restart.locks == {'tries': 3, 'busy': 2, 'released': 1}
    restart.checkpoint.assert_called_once()
    restart.ensure.assert_awaited_once()
    assert sum(call.args[0][-1] == '/quit' for call in restart.run.call_args_list) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('raise_busy', [False, True])
async def test_exhausted_contention_has_no_restart_side_effects(restart, raise_busy):
    restart.locks['busy'] = 100
    if raise_busy:
        with pytest.raises(app._SessionOperationBusy):
            await app._restart_codex_for_session('demo', 'admin', raise_busy=True)
    else:
        assert await app._restart_codex_for_session('demo', 'admin') == (False, False)
    assert restart.clock[0] == pytest.approx(2.0)
    restart.checkpoint.assert_not_called()
    restart.ensure.assert_not_awaited()
    restart.run.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'generation'])
async def test_identity_changed_during_wait_is_not_checkpointed_or_restarted(restart, change):
    restart.locks['busy'] = 1
    restart.changes.append(lambda: restart.row.update(
        {'owner_id': 'other'} if change == 'owner' else {'generation': 'b' * 32}))
    assert await app._restart_codex_for_session('demo', 'admin') == (False, False)
    restart.checkpoint.assert_not_called()
    restart.run.assert_not_called()
    restart.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_generation_never_adopts_replacement(restart):
    restart.row.pop('generation')
    assert await app._restart_codex_for_session('demo', 'admin') == (False, False)
    assert restart.locks['tries'] == 0
    restart.checkpoint.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_terminal_write_is_not_retried(restart):
    restart.run.side_effect = OSError('uncertain delivery')
    assert await app._restart_codex_for_session('demo', 'admin') == (False, False)
    assert restart.locks['tries'] == 1
    restart.run.assert_called_once()
    restart.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_releases_acquired_operation_lock(restart):
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        async with app._settings_restart_operation_lock('demo'):
            raise asyncio.CancelledError
    assert restart.locks['released'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['model', 'effort'])
@pytest.mark.parametrize('busy_type', [app._SessionOperationBusy, app._TmuxMutationBusy])
async def test_api_busy_is_409_and_saved_settings_are_explicit(restart, monkeypatch, kind, busy_type):
    request = Request({'type': 'http', 'method': 'POST', 'headers': []})
    monkeypatch.setattr(app, '_current_user', lambda r: restart.user)
    monkeypatch.setattr(app, '_find_session_for_user', lambda *a: ([], {'name': 'demo'}))
    monkeypatch.setattr(app, '_model_catalog_file_for_user', lambda u: restart.path / 'models.json')
    monkeypatch.setattr(app, '_load_model_snapshot', lambda p:
        ([('gpt-5.6-terra', 'Terra')], {'gpt-5.6-terra': ['low']}, 1))
    write = Mock(return_value=('gpt-5.6-terra', 'low'))
    monkeypatch.setattr(app, '_write_validated_session_codex_settings', write)
    monkeypatch.setattr(app, '_session_owner_matches', lambda *a: True)
    monkeypatch.setattr(app, '_async_is_codex_running', AsyncMock(return_value=True))
    restart_call = AsyncMock(side_effect=busy_type('busy'))
    monkeypatch.setattr(app, '_restart_codex_for_session', restart_call)
    if kind == 'model':
        response = await app.api_set_session_model(request, 'demo', app.SetSessionModelBody(model='gpt-5.6-terra', restart=True))
    else:
        response = await app.api_set_session_effort(request, 'demo', app.SetSessionEffortBody(effort='low', restart=True))
    assert response.status_code == 409
    result = json.loads(response.body)
    assert result['saved'] is True and result['restarted'] is False and result['ok'] is False
    assert result['code'] == 'session_busy'
    assert 'Session is busy' in result['error']
    write.assert_called_once()
    restart_call.assert_awaited_once_with('demo', 'admin', expected_generation='a' * 32, raise_busy=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['model', 'effort'])
async def test_api_captures_generation_before_persistence_await(restart, monkeypatch, kind):
    request = Request({'type': 'http', 'method': 'POST', 'headers': []})
    monkeypatch.setattr(app, '_current_user', lambda r: restart.user)
    monkeypatch.setattr(app, '_find_session_for_user', lambda *a: ([], {'name': 'demo'}))
    monkeypatch.setattr(app, '_model_catalog_file_for_user', lambda u: restart.path / 'models.json')
    monkeypatch.setattr(app, '_load_model_snapshot', lambda p:
        ([('gpt-5.6-terra', 'Terra')], {'gpt-5.6-terra': ['low']}, 1))
    def write(*a, **kw):
        restart.row['generation'] = 'b' * 32
        return 'gpt-5.6-terra', 'low'
    monkeypatch.setattr(app, '_write_validated_session_codex_settings', write)
    monkeypatch.setattr(app, '_session_owner_matches', lambda *a: True)
    monkeypatch.setattr(app, '_async_is_codex_running', AsyncMock(return_value=True))
    if kind == 'model':
        await app.api_set_session_model(request, 'demo', app.SetSessionModelBody(model='gpt-5.6-terra', restart=True))
    else:
        await app.api_set_session_effort(request, 'demo', app.SetSessionEffortBody(effort='low', restart=True))
    restart.checkpoint.assert_not_called()
    restart.run.assert_not_called()
    restart.ensure.assert_not_awaited()
