"""Advanced Voice remains opt-in, scoped, cancellable and free of external I/O."""
import ast
import asyncio
import contextlib
import json
import os
import re
import secrets
import stat
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

import voice_checkin as checkin
import voice_mode as voice
from runtime_hooks import voice_hook
from test_voice_mode import connection, host, forbid_real_provider_io, GENERATION


@pytest.fixture(autouse=True)
def synthetic_lifecycle_fence(host):
    host._session_operation_lock = lambda name: contextlib.nullcontext()


def consent(conn, option, enabled=True):
    return {'option': option, 'enabled': enabled, 'consent': True, 'restart_acknowledged': True,
            'nonce': conn.nonce, 'generation': conn.binding['generation'], 'root': conn.binding['resume_uuid']}


def test_new_connection_has_no_advanced_authority(connection):
    assert not connection.supervision_enabled and not connection.checkin_enabled
    assert connection.feature_checkin.pending is None


@pytest.mark.parametrize('field,value', [('consent', False), ('root', 'other'),
    ('generation', 'other'), ('nonce', 'other'), ('enabled', 'true')])
@pytest.mark.asyncio
async def test_advanced_rejects_stale_or_missing_explicit_consent(connection, field, value):
    message = consent(connection, 'supervision')
    message[field] = value
    with pytest.raises(voice.VoiceError):
        await connection.set_advanced(message)
    connection.host.api_send_command.assert_not_awaited()
    connection.host._restart_codex_for_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_supervision_restarts_only_after_optin_and_hook_ack(connection):
    connection.host._voice_supervision_available = lambda name: True
    async def restart(request, name):
        assert voice.request_target_matches(connection.host, request, name)
        connection.mutate(lambda row: row.update(hook_ready=True))
        return True
    connection.host._voice_enable_supervision = AsyncMock(side_effect=restart)
    await connection.set_advanced(consent(connection, 'supervision'))
    assert connection.supervision_enabled and connection.state()['supervised']
    connection.host._voice_enable_supervision.assert_awaited_once()
    connection.host.api_send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_supervision_restart_needs_its_own_disclosure(connection):
    message = consent(connection, 'supervision')
    message['restart_acknowledged'] = False
    connection.host._voice_supervision_available = lambda name: True
    connection.host._voice_enable_supervision = AsyncMock()
    with pytest.raises(voice.VoiceError):
        await connection.set_advanced(message)
    connection.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_restart_restores_preexisting_hold(connection):
    connection.mutate(lambda row: row.update(supervised=True, paused=True, hook_ready=True))
    connection.host._voice_supervision_available = lambda name: True
    connection.host._voice_enable_supervision = AsyncMock(return_value=False)
    with pytest.raises(voice.VoiceError):
        await connection.set_advanced(consent(connection, 'supervision'))
    assert connection.state()['paused'] and connection.state()['supervised']
    assert not connection.supervision_enabled


@pytest.mark.asyncio
async def test_checkin_optin_does_not_enable_supervision_or_restart(connection):
    await connection.set_advanced(consent(connection, 'checkin'))
    assert connection.checkin_enabled and not connection.supervision_enabled
    connection.host._restart_codex_for_session.assert_not_awaited()
    command = connection.host.api_send_command.await_args.args[2].command
    assert checkin.CHECKIN_QUESTION in command and 'grants no deployment' in command


@pytest.mark.asyncio
async def test_initial_checkin_preference_cannot_release_new_hold(connection):
    async def delayed(request, name, command):
        connection.mutate(lambda row: row.update(paused=True))
        await connection.validate()  # A concurrent heartbeat must not weaken this action snapshot.
        assert request is not connection.request
        assert request.state.voice_target_guard['paused'] is False
        assert not voice.request_target_matches(connection.host, request, name)
        return JSONResponse({'error': 'Changed'}, status_code=409)
    connection.host.api_send_command.side_effect = delayed
    with pytest.raises(voice.VoiceError):
        await connection.set_advanced(consent(connection, 'checkin'))
    assert connection.state()['paused'] and not connection.checkin_enabled


@pytest.mark.asyncio
async def test_received_optout_revokes_restart_permission_before_waiting_for_action_lock(connection):
    connection.supervision_enabled = True
    connection.mutate(lambda row: row.update(supervised=True, supervision_nonce=connection.nonce))
    await connection.validate()
    request = Request({**connection.request.scope, 'state': dict(connection.request.scope['state'])})
    request.state.voice_source_guard = {**request.state.voice_source_guard, 'supervision_nonce': connection.nonce}
    assert voice.request_target_matches(connection.host, request, 'alpha')
    async def waiting_control(message):
        assert connection.action_lock.locked()
        assert not voice.request_target_matches(connection.host, request, 'alpha')
    connection.set_advanced = waiting_control
    connection.ws.receive_json = AsyncMock(side_effect=[
        {'type': 'advanced', **consent(connection, 'supervision', False)}, {'type': 'stop'}])
    async with connection.action_lock:
        await connection.browser_events()
    assert not connection.supervision_enabled and connection.state()['supervision_nonce'] is None


def test_disconnect_preserves_hold_but_clears_connection_optins(connection):
    connection.supervision_enabled = connection.checkin_enabled = True
    connection.mutate(lambda row: row.update(supervised=True, paused=True, hook_ready=True))
    connection.finish_connection_state()
    assert connection.state()['paused'] and voice.supervised_row(connection.state())
    assert not connection.supervision_enabled and not connection.checkin_enabled
    assert not voice.check_hook(connection.host.MESSAGES_DIR, 'alpha', 'one', {
        'payload': {'session_id': 'root', 'hook_event_name': 'PreToolUse'}})['ok']


@pytest.mark.asyncio
async def test_explicit_release_clears_only_own_hold_without_sending_work(connection):
    connection.mutate(lambda row: row.update(supervised=True, paused=True, hook_ready=True))
    await connection.set_advanced(consent(connection, 'supervision', False))
    assert not connection.state()['paused'] and not connection.state()['supervised']
    connection.host.api_send_command.assert_not_awaited()


def test_legacy_unpaused_disconnected_guard_does_not_suspend_autopush_forever(connection):
    connection.mutate(lambda row: row.update(active=True, connected=True, hook_ready=True, heartbeat=0))
    assert not voice.supervised(connection.host.MESSAGES_DIR, 'alpha', GENERATION)
    connection.mutate(lambda row: row.update(paused=True))
    assert voice.supervised(connection.host.MESSAGES_DIR, 'alpha', GENERATION)


@pytest.mark.asyncio
async def test_plain_reconnect_does_not_reenable_old_unpaused_supervision(connection):
    connection.mutate(lambda row: row.update(supervised=True, hook_ready=True, connected=False, heartbeat=0))
    with pytest.raises(AssertionError, match='Unexpected provider HTTP'):
        await connection.run('v=0')
    assert not connection.state()['supervised'] and not connection.supervision_enabled
    connection.host._restart_codex_for_session.assert_not_awaited()


@pytest.fixture
def timer(connection, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(checkin, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    conn = connection
    conn.ready = conn.checkin_enabled = True
    conn.mutate(lambda row: row.update(connected=True))
    conn.context = AsyncMock(return_value={'spoken_name': 'Current project', 'entries': [{'kind': 'assistant', 'text': checkin.CHECKIN_QUESTION}]})
    conn.send = AsyncMock(return_value={'ok': True})
    return conn, conn.feature_checkin, now


@pytest.mark.asyncio
async def test_exact_checkin_waits_180_seconds_then_sends_once_under_original_rules(timer):
    conn, feature, now = timer
    await feature.observe(checkin.CHECKIN_QUESTION)
    now[0] = 179.999
    await feature.observe(checkin.CHECKIN_QUESTION)
    conn.send.assert_not_awaited()
    now[0] = 180
    await feature.observe(checkin.CHECKIN_QUESTION)
    await feature.observe(checkin.CHECKIN_QUESTION)
    conn.send.assert_awaited_once()
    text = conn.send.await_args.args[0]
    assert 'not deployment consent' in text and 'every project' in text and 'already-requested testing' in text


@pytest.mark.asyncio
async def test_real_observer_recognizes_exact_pretest_question_with_speaker_off(timer):
    conn, feature, now = timer
    conn.proactive_updates = False
    await conn.observe_once()
    assert feature.pending is not None
    now[0] = 180
    await conn.observe_once()
    conn.send.assert_awaited_once()


@pytest.mark.parametrize('reason', ['input', 'hold', 'optout', 'changed', 'failed', 'disconnect'])
@pytest.mark.asyncio
async def test_timer_cancellation_and_activity_cannot_authorize_old_work(timer, reason):
    conn, feature, now = timer
    await feature.observe(checkin.CHECKIN_QUESTION)
    now[0] = 179
    if reason == 'input': feature.activity('one more detail')
    if reason == 'hold': feature.activity('please wait')
    if reason == 'optout': conn.checkin_enabled = False
    if reason == 'changed': conn.mutate(lambda row: row.update(revision=2))
    if reason == 'failed': feature.cancel()
    if reason == 'disconnect': conn.ended = True
    now[0] = 180
    await feature.observe(checkin.CHECKIN_QUESTION)
    conn.send.assert_not_awaited()


@pytest.mark.parametrize('text', ['Ready to deploy.', '> '+checkin.CHECKIN_QUESTION,
    'Later: '+checkin.CHECKIN_QUESTION, '```\n'+checkin.CHECKIN_QUESTION+'\n```'])
@pytest.mark.asyncio
async def test_quoted_or_nonquestion_output_does_not_start_checkin(timer, text):
    conn, feature, now = timer
    await feature.observe(voice.user_decision_request(text))
    now[0] = 1000
    await feature.observe(voice.user_decision_request(text))
    assert feature.pending is None
    conn.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_timer_consumes_uncertain_delivery_without_retry(timer):
    conn, feature, now = timer
    await feature.observe(checkin.CHECKIN_QUESTION)
    conn.send.side_effect = OSError('Synthetic uncertain delivery')
    now[0] = 180
    with pytest.raises(OSError): await feature.observe(checkin.CHECKIN_QUESTION)
    await feature.observe(checkin.CHECKIN_QUESTION)
    conn.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_after_expiry_invalidates_private_action_permission(timer):
    conn, feature, now = timer
    await feature.observe(checkin.CHECKIN_QUESTION)
    async def delayed(*args, **kwargs):
        predicate = conn.request.state.voice_permission_check
        assert predicate()
        feature.cancel()
        assert not predicate()
    conn.send.side_effect = delayed
    now[0] = 180
    await feature.observe(checkin.CHECKIN_QUESTION)


@pytest.mark.parametrize('row', [None, [], {}, {'root': 'other', 'generation': GENERATION, 'paused': True}])
def test_managed_hook_cannot_verify_missing_or_changed_pause_state(tmp_path, row):
    directory = tmp_path / '.tmux-dashboard'
    directory.mkdir()
    (directory/'voice-guards.json').write_text(json.dumps({'alpha': row}))
    result = voice_hook.pause_fallback({'session_id': 'root'}, {'TMUX_DASH_HOST_HOME': str(tmp_path),
        'TMUX_DASH_VOICE_SESSION': 'alpha', 'TMUX_DASH_VOICE_GENERATION': GENERATION})
    assert not result['ok']


def test_verified_disabled_hook_has_no_deployment_regex_gate(tmp_path):
    directory = tmp_path / '.tmux-dashboard'
    directory.mkdir()
    (directory/'voice-guards.json').write_text(json.dumps({'alpha': {'root': 'root', 'generation': GENERATION,
        'active': False, 'supervised': False, 'paused': False}}))
    result = voice_hook.pause_fallback({'session_id': 'root', 'tool_input': {'command': 'git push && deploy'}},
        {'TMUX_DASH_HOST_HOME': str(tmp_path), 'TMUX_DASH_VOICE_SESSION': 'alpha', 'TMUX_DASH_VOICE_GENERATION': GENERATION})
    assert result['ok']


@pytest.mark.parametrize('change,expected', [('valid', True), ('async', False), ('limited', False)])
def test_managed_registration_requires_synchronous_catchall_hooks(change, expected):
    source = Path(__file__).with_name('runtime_hooks').joinpath('voice_requirements.toml').read_text()
    if change == 'async': source = source.replace('timeout = 6', 'timeout = 6\nasync = true')
    if change == 'limited': source = source.replace('[[hooks.PreToolUse]]', '[[hooks.PreToolUse]]\nmatcher = "Bash"')
    class TrustedPath:
        def __init__(self, value): self.value = value
        def lstat(self): return SimpleNamespace(st_mode=stat.S_IFREG|0o644, st_uid=0)
        def resolve(self, **kwargs): return self
        def read_text(self): return source
        def __str__(self): return self.value
        @property
        def parent(self): return TrustedPath(str(Path(self.value).parent))
    module = ast.parse(Path(__file__).with_name('app.py').read_text())
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == '_voice_supervision_available')
    namespace = {'Path': TrustedPath, 'stat': stat, 'tomllib': tomllib}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual managed registration check>', 'exec'), namespace)
    assert namespace['_voice_supervision_available']('alpha') is expected


def test_restart_action_ids_remain_consumed_after_a_later_action():
    module = ast.parse(Path(__file__).with_name('app.py').read_text())
    function = next(node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and node.name == 'consume_voice_restart')
    namespace = {'session_name': 'alpha', 'action_id': 'first'}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual restart idempotency check>', 'exec'), namespace)
    rows = {'alpha': {}}
    assert namespace['consume_voice_restart'](rows)
    namespace['action_id'] = 'second'
    assert namespace['consume_voice_restart'](rows)
    namespace['action_id'] = 'first'
    assert not namespace['consume_voice_restart'](rows)


@pytest.mark.parametrize('cancel_at', ['after_fence', 'after_paste', 'stranded', 'uncertain'])
@pytest.mark.parametrize('timed', [False, True])
@pytest.mark.asyncio
async def test_timed_send_rechecks_permission_after_async_work_before_submit(cancel_at, timed):
    """Execute the actual route body with inert services; never import the live app."""
    module = ast.parse(Path(__file__).with_name('app.py').read_text())
    function = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'api_send_command')
    function.decorator_list = []
    allowed, calls = [True], []
    async def controller(op, **kwargs):
        if op == 'session_input_check' and cancel_at == 'after_fence': allowed[0] = False
        return {'ok': True}
    async def sleep(_seconds):
        if cancel_at == 'after_paste': allowed[0] = False
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)
    namespace = {'Request': Request, 'SendCommand': SimpleNamespace, 'JSONResponse': JSONResponse,
        'asyncio': SimpleNamespace(to_thread=asyncio.to_thread, sleep=sleep),
        'voice_mode': SimpleNamespace(request_target_matches=lambda *args: allowed[0], note_prompt=Mock()),
        'sys': SimpleNamespace(modules={'voice_route_test': SimpleNamespace()}), '__name__': 'voice_route_test',
        '_current_user': lambda request: {'id': 'one'}, '_find_session_for_user': lambda *args: ({'id': 'one'}, {'name': 'alpha'}),
        '_controller_call': controller, '_wait_for_codex_input_ready': AsyncMock(return_value=True),
        '_session_operation_lock': lambda name: contextlib.nullcontext(), '_SessionOperationBusy': RuntimeError,
        '_terminal_binding': lambda *args: {'session_id': '$1', 'generation': GENERATION},
        '_account_instruction_refresh_for_prompt': lambda name,text: (text,None),
        '_voice_submission_state': AsyncMock(return_value=cancel_at), 'logger': Mock(),
        'subprocess': SimpleNamespace(run=run), 'tempfile': tempfile, 'secrets': secrets, 'os': os,
        'MESSAGES_DIR': Path('/synthetic-unused')}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual api_send_command>', 'exec'), namespace)
    guard = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == '_voice_input_run')
    exec(compile(ast.Module(body=[guard], type_ignores=[]), '<actual final input guard>', 'exec'), namespace)
    request = Request({'type': 'http', 'headers': [], 'state': {'voice_checkin': timed, 'voice_target_binding': {'session_name': 'alpha'}}})
    result = await namespace['api_send_command'](request, 'alpha', SimpleNamespace(command='A scoped test continuation. '*20))
    assert result.status_code == 409
    assert any('C-m' in command for command in calls) is (cancel_at in ('stranded', 'uncertain'))
    namespace['voice_mode'].note_prompt.assert_not_called()


@pytest.mark.parametrize('visible,expected', [
    ('› Do the requested tests.\n  gpt-6-astra xhigh · ~/project', 'stranded'),
    ('› [Pasted Content 28 chars]\n  gpt-6-astra xhigh · ~/project', 'stranded'),
    ('›\n  gpt-6-astra xhigh · ~/project', 'submitted'),
    ('› An unrelated new draft\n  gpt-6-astra xhigh · ~/project', 'submitted'),
    ('│ Ambiguous footer content\n  gpt-6-astra xhigh · ~/project', 'uncertain'),
    ('Historical output without a live footer', 'uncertain'),
    (None, 'uncertain'),
])
@pytest.mark.asyncio
async def test_voice_submission_is_read_only_and_reports_parked_or_unverifiable_input(visible, expected):
    module = ast.parse(Path(__file__).with_name('app.py').read_text())
    names = {'_voice_submission_state', '_active_codex_composer_state', '_active_codex_composer_text',
             '_codex_composer_matches_prompt'}
    nodes = [node for node in module.body if
        (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names) or
        (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
            target.id.startswith(('_CODEX_COMPOSER_', '_CODEX_PASTE_')) for target in node.targets))]
    capture = Mock(return_value=visible)
    if visible is None: capture.side_effect = OSError('Synthetic pane unavailable')
    # Deliberately no subprocess or input functions: the verifier must only read.
    namespace = {'re': re, 'asyncio': SimpleNamespace(to_thread=asyncio.to_thread, sleep=AsyncMock()),
                 'capture_pane_recent': capture, 'logger': Mock()}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual read-only Voice delivery check>', 'exec'), namespace)
    assert await namespace['_voice_submission_state']('alpha', 'Do the requested tests.', '$1') == expected
    capture.assert_called_once_with('$1', 210)


@pytest.mark.asyncio
async def test_interrupt_rechecks_source_permission_after_controller_preflight():
    module = ast.parse(Path(__file__).with_name('app.py').read_text())
    functions = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in ('api_interrupt_session', '_voice_input_run')]
    for function in functions: function.decorator_list = []
    allowed, calls = [True], []
    async def controller(op, **kwargs):
        if op == 'session_input_check': allowed[0] = False
        return {'ok': True}
    namespace = {'Request': Request, 'JSONResponse': JSONResponse, 'asyncio': asyncio,
        'voice_mode': SimpleNamespace(request_target_matches=lambda *args: allowed[0]),
        'sys': SimpleNamespace(modules={'voice_route_test': SimpleNamespace()}), '__name__': 'voice_route_test',
        '_find_session': lambda name: (None, {'name': name}), '_current_user': lambda request: {'id': 'one'},
        '_controller_call': controller, '_session_operation_lock': lambda name: contextlib.nullcontext(),
        '_SessionOperationBusy': RuntimeError, '_terminal_binding': lambda *args: {'session_id': '$1'},
        'subprocess': SimpleNamespace(run=lambda command,**kwargs: calls.append(command))}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual interrupt route>', 'exec'), namespace)
    result = await namespace['api_interrupt_session'](Request({'type': 'http', 'headers': [], 'state': {}}), 'alpha')
    assert result.status_code == 409 and not calls
