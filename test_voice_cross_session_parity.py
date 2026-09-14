"""Cross-session supervision uses explicit speech, frozen consent and private guards."""
import contextlib
import copy
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.responses import JSONResponse

import voice_mode as voice
import voice_session_control as control
from test_voice_mode import connection, forbid_real_provider_io, host


@pytest.fixture
def supervisor(connection):
    c = connection
    c.supervision_enabled = True
    c.other_targets['target'] = {'binding': dict(c.host.identities['beta']),
                                 'expires': time.time() + 300}
    voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows['alpha'].update(
        supervised=True, supervision_nonce=c.nonce, hook_ready=True))
    c.host.locks = []
    @contextlib.contextmanager
    def operation_lock(name):
        c.host.locks.append(('enter', name))
        try:
            yield
        finally:
            c.host.locks.append(('exit', name))
    c.host._session_operation_lock = operation_lock
    c.host._voice_supervision_available = Mock(return_value=True)
    async def enable(request, name):
        assert name == 'beta'
        assert voice.request_target_matches(c.host, request, name)
        source = request.state.voice_source_guard
        assert source['root'] == 'root' and source['revision'] == 1
        assert source['supervision_nonce'] == c.nonce
        assert request.state.voice_target_guard['supervised'] is True
        assert voice.check_hook(c.host.MESSAGES_DIR, name, 'one', {
            'generation': c.host.identities[name]['generation'],
            'payload': {'session_id': 'beta-root', 'hook_event_name': 'SessionStart'}})['ok']
        return True
    c.host._voice_enable_supervision = AsyncMock(side_effect=enable)
    async def send(request, name, *args):
        assert voice.request_target_matches(c.host, request, name)
        return JSONResponse({'ok': True})
    c.host.api_send_command = AsyncMock(side_effect=send)
    c.host.api_interrupt_session = AsyncMock(side_effect=send)
    return c


def target_row(c, **changes):
    row = dict(active=False, connected=False, supervised=True, hook_ready=True,
               owner_id='one', generation=c.binding['generation'], root='beta-root',
               nonce='target-nonce', revision=3, paused=False)
    row.update(changes)
    voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows.update(beta=row))
    return row


async def prepare(c, action='supervise', text=None):
    text = text or {'supervise': 'Enable supervision for Other project.',
                    'pause': 'Pause Other project.', 'resume': 'Resume Other project.',
                    'instruct': 'Change the title in Other project.'}[action]
    c.transcripts = [{'role': 'user', 'text': text, 'time': time.time()}]
    return await c.session_control.prepare({'target_id': 'target', 'action': action,
        'summary': 'Only the requested task; preserve all project rules.', 'request_quote': text})


def approve(c, offer, answer='yes', question=None):
    c.transcripts.extend([
        {'role': 'assistant', 'text': question or offer['confirmation'], 'time': time.time()},
        {'role': 'user', 'text': answer, 'time': time.time()}])
    return {'confirmation_id': offer['confirmation_id'], 'approval_quote': answer}


@pytest.mark.asyncio
async def test_supervision_discloses_restart_waits_for_hook_and_never_sends_setup(supervisor):
    c = supervisor
    offer = await prepare(c)
    assert 'restart the coding worker' in offer['confirmation']
    assert 'Other project' in offer['confirmation']
    c.host._voice_enable_supervision.assert_not_awaited()
    args = approve(c, offer)
    result = await c.session_control.confirm(args)
    assert result['ok'] and 'enabled' in result['message']
    row = voice.store(c.host.MESSAGES_DIR).read()['beta']
    assert row['supervised'] and row['hook_ready'] and not row['active'] and not row['connected']
    assert not row['paused'] and row['root'] == 'beta-root'
    assert c.host.locks == [('enter', 'alpha'), ('enter', 'beta'), ('exit', 'beta'), ('exit', 'alpha')]
    c.host._voice_enable_supervision.assert_awaited_once()
    c.host._restart_codex_for_session.assert_not_awaited()
    c.host.api_send_command.assert_not_awaited()
    c.host.api_interrupt_session.assert_not_awaited()
    with pytest.raises(voice.VoiceError):
        await c.session_control.confirm(args)
    c.host._voice_enable_supervision.assert_awaited_once()


@pytest.mark.parametrize('text,quote', [
    ('Do not restart Other project.', 'restart Other project'),
    ('Do not restart Other project.', 'Do not restart Other project.'),
    ('What would restarting Other project do?', 'What would restarting Other project do?'),
    ('Check Other project status.', 'Check Other project status.'),
    ('Restart that worker.', 'Restart that worker.'),
    ('Supervise Wrong project.', 'Supervise Wrong project.'),
    ('Restart Other project. Do not deploy.', 'Restart Other project.'),
    ('Supervise Other project without restarting it.', 'Supervise Other project without restarting it.'),
    ('Supervise Other project, but do not restart it.', 'Supervise Other project, but do not restart it.'),
])
@pytest.mark.asyncio
async def test_supervision_requires_complete_actual_positive_target_request(supervisor, text, quote):
    c = supervisor
    c.transcripts = [{'role': 'user', 'text': text, 'time': time.time()}]
    with pytest.raises(voice.VoiceError):
        await c.session_control.prepare({'target_id': 'target', 'action': 'supervise',
            'summary': 'Restart only the named worker.', 'request_quote': quote})
    assert 'beta' not in voice.store(c.host.MESSAGES_DIR).read()
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_request_preserves_separate_negative_constraints(supervisor):
    offer = await prepare(supervisor, text='Restart Other project. Do not deploy.')
    assert 'restart' in offer['confirmation']


@pytest.mark.asyncio
async def test_supervision_cannot_use_old_request_or_passive_background_update(supervisor):
    c = supervisor
    c.transcripts = [
        {'role': 'user', 'text': 'Restart Other project.', 'time': time.time() - 30},
        {'role': 'assistant', 'text': 'Other project asks for help.', 'time': time.time() - 20},
        {'role': 'user', 'text': 'Just give me its status.', 'time': time.time() - 10}]
    with pytest.raises(voice.VoiceError):
        await c.session_control.prepare({'target_id': 'target', 'action': 'supervise',
            'summary': 'Restart.', 'request_quote': 'Restart Other project.'})
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.parametrize('when', ['prepare', 'confirm'])
@pytest.mark.asyncio
async def test_supervision_needs_current_source_opt_in(supervisor, when):
    c = supervisor
    if when == 'prepare':
        c.supervision_enabled = False
        with pytest.raises(voice.VoiceError):
            await prepare(c)
    else:
        offer = await prepare(c)
        c.supervision_enabled = False
        with pytest.raises(voice.VoiceError):
            await c.session_control.confirm(approve(c, offer))
    assert 'beta' not in voice.store(c.host.MESSAGES_DIR).read()
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.parametrize('changes', [{'active': True}, {'connected': True}, {},
                                   {'owner_id': 'other'}, {'root': 'other-root'}, {'generation': 'old'}])
@pytest.mark.asyncio
async def test_existing_voice_and_mismatched_durable_guards_are_never_replaced(supervisor, changes):
    c = supervisor
    original = target_row(c, **changes)
    with pytest.raises(voice.VoiceError):
        await prepare(c)
    assert voice.store(c.host.MESSAGES_DIR).read()['beta'] == original
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.parametrize('answer,question', [('yes, but do not restart', None), ('no', None),
                                          ('yes', 'Can I read Other project?')])
@pytest.mark.asyncio
async def test_confirmation_is_exact_and_standalone(supervisor, answer, question):
    c = supervisor
    offer = await prepare(c)
    with pytest.raises(voice.VoiceError):
        await c.session_control.confirm(approve(c, offer, answer, question))
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.parametrize('changed', ['source_root', 'source_generation', 'source_nonce',
                                   'source_revision', 'supervision_nonce', 'target_root', 'target_guard'])
@pytest.mark.asyncio
async def test_final_under_lock_fence_rejects_late_source_or_target_changes(supervisor, changed):
    c = supervisor
    offer = await prepare(c)
    original_lock = c.host._session_operation_lock
    @contextlib.contextmanager
    def change_before_lock(name):
        with original_lock(name):
            if name == 'beta':
                if changed in ('source_root', 'source_generation'):
                    key = 'resume_uuid' if changed == 'source_root' else 'generation'
                    c.host.identities['alpha'][key] = 'replacement'
                elif changed == 'target_root':
                    c.host.identities['beta']['resume_uuid'] = 'replacement'
                elif changed == 'target_guard':
                    voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows.update(beta={'nonce': 'newer'}))
                else:
                    key = {'source_nonce': 'nonce', 'source_revision': 'revision',
                           'supervision_nonce': 'supervision_nonce'}[changed]
                    voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows['alpha'].update({key: 'newer'}))
            yield
    c.host._session_operation_lock = change_before_lock
    with pytest.raises(voice.VoiceError):
        await c.session_control.confirm(approve(c, offer))
    assert voice.store(c.host.MESSAGES_DIR).read().get('beta', {}) == ({'nonce': 'newer'} if changed == 'target_guard' else {})
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_compare_and_update_rejects_concurrent_source_replacement(supervisor, monkeypatch):
    c = supervisor
    offer = await prepare(c)
    backing = voice.store(c.host.MESSAGES_DIR)
    class ConcurrentStore:
        def read(self):
            return backing.read()
        def update(self, change):
            backing.update(lambda rows: rows['alpha'].update(nonce='replacement'))
            return backing.update(change)
    monkeypatch.setattr(voice, 'store', lambda directory: ConcurrentStore())
    with pytest.raises(voice.VoiceError, match='connection changed'):
        await c.session_control.confirm(approve(c, offer))
    assert 'beta' not in backing.read()
    assert backing.read()['alpha']['nonce'] == 'replacement'
    c.host._voice_enable_supervision.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_replacement_during_handshake_is_never_overwritten(supervisor):
    c = supervisor
    replacement = {}
    async def replaced(request, name):
        row = voice.store(c.host.MESSAGES_DIR).read()['beta']
        row.update(nonce='replacement', hook_ready=True, paused=True)
        replacement.update(row)
        voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows.update(beta=row))
        return True
    c.host._voice_enable_supervision.side_effect = replaced
    offer = await prepare(c)
    with pytest.raises(voice.VoiceError, match='changed during restart'):
        await c.session_control.confirm(approve(c, offer))
    assert voice.store(c.host.MESSAGES_DIR).read()['beta'] == replacement
    c.host.api_send_command.assert_not_awaited()
    assert not any(call.args[0].get('type') == 'target_action' for call in c.ws.send_json.await_args_list)


@pytest.mark.asyncio
async def test_failed_restart_is_not_retried_and_does_not_claim_ready(supervisor):
    c = supervisor
    c.host._voice_enable_supervision.side_effect = None
    c.host._voice_enable_supervision.return_value = False
    offer = await prepare(c)
    args = approve(c, offer)
    with pytest.raises(voice.VoiceError, match='not confirmed'):
        await c.session_control.confirm(args)
    with pytest.raises(voice.VoiceError):
        await c.session_control.confirm(args)
    row = voice.store(c.host.MESSAGES_DIR).read()['beta']
    assert row['supervised'] and not row['hook_ready'] and not row['paused']
    assert not voice.supervised(c.host.MESSAGES_DIR, 'beta', row['generation'])
    assert not any(call.args[0].get('type') == 'target_action' for call in c.ws.send_json.await_args_list)
    c.host.api_send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_hook_ack_never_claims_ready(supervisor, monkeypatch):
    c = supervisor
    c.host._voice_enable_supervision.side_effect = None
    c.host._voice_enable_supervision.return_value = True
    monkeypatch.setattr(control.asyncio, 'sleep', AsyncMock())
    offer = await prepare(c)
    with pytest.raises(voice.VoiceError, match='did not acknowledge'):
        await c.session_control.confirm(approve(c, offer))
    c.host.api_send_command.assert_not_awaited()
    assert not any(call.args[0].get('type') == 'target_action' for call in c.ws.send_json.await_args_list)


@pytest.mark.parametrize('action', ['pause', 'resume'])
@pytest.mark.asyncio
async def test_unsupervised_pause_resume_remain_ordinary(supervisor, action):
    c = supervisor
    c.supervision_enabled = False
    offer = await prepare(c, action)
    result = await c.session_control.confirm(approve(c, offer))
    assert result['ok'] and 'held' not in result['message']
    assert 'beta' not in voice.store(c.host.MESSAGES_DIR).read()
    c.host._voice_enable_supervision.assert_not_awaited()
    (c.host.api_interrupt_session if action == 'pause' else c.host.api_send_command).assert_awaited_once()


@pytest.mark.parametrize('status', ['idle', 'busy', 'unknown', 'stalled'])
@pytest.mark.asyncio
async def test_durable_pause_survives_voice_disconnect_without_false_idle(supervisor, status):
    c = supervisor
    target_row(c)
    c.host.async_detect_activity.return_value = {'status': status}
    offer = await prepare(c, 'pause')
    assert 'hold future tool calls' in offer['confirmation']
    result = await c.session_control.confirm(approve(c, offer))
    row = copy.deepcopy(voice.store(c.host.MESSAGES_DIR).read()['beta'])
    assert row['paused'] and 'held' in result['message']
    assert ('stopped' in result['message']) is (status == 'idle')
    c.finish_connection_state()
    assert voice.store(c.host.MESSAGES_DIR).read()['beta'] == row
    assert voice.supervised(c.host.MESSAGES_DIR, 'beta', row['generation'])


@pytest.mark.asyncio
async def test_failed_interrupt_keeps_confirmed_durable_hold(supervisor):
    c = supervisor
    target_row(c)
    c.host.api_interrupt_session.side_effect = None
    c.host.api_interrupt_session.return_value = JSONResponse({'error': 'Unavailable'}, status_code=503)
    offer = await prepare(c, 'pause')
    with pytest.raises(voice.VoiceError, match='remain held'):
        await c.session_control.confirm(approve(c, offer))
    assert voice.store(c.host.MESSAGES_DIR).read()['beta']['paused']


@pytest.mark.asyncio
async def test_durable_resume_releases_only_confirmed_target_and_reports_send_failure(supervisor):
    c = supervisor
    target_row(c, paused=True)
    source = copy.deepcopy(c.state())
    offer = await prepare(c, 'resume')
    assert 'release the explicit tool hold' in offer['confirmation']
    c.host.api_send_command.side_effect = None
    c.host.api_send_command.return_value = JSONResponse({'error': 'Unavailable'}, status_code=503)
    with pytest.raises(voice.VoiceError, match='hold was released'):
        await c.session_control.confirm(approve(c, offer))
    row = voice.store(c.host.MESSAGES_DIR).read()['beta']
    assert not row['paused'] and row['supervised'] and row['hook_ready']
    assert c.state() == source


@pytest.mark.asyncio
async def test_unverified_hold_cannot_be_released_by_resume(supervisor):
    c = supervisor
    original = target_row(c, paused=True, hook_ready=False)
    offer = await prepare(c, 'resume')
    with pytest.raises(voice.VoiceError, match='no verified hook'):
        await c.session_control.confirm(approve(c, offer))
    assert voice.store(c.host.MESSAGES_DIR).read()['beta'] == original
    c.host.api_send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_supervised_hold_still_has_explicit_resume(supervisor):
    c = supervisor
    target_row(c, active=True, paused=True)
    voice.store(c.host.MESSAGES_DIR).update(lambda rows: rows['beta'].pop('supervised'))
    offer = await prepare(c, 'resume')
    result = await c.session_control.confirm(approve(c, offer))
    assert result['ok'] and not voice.store(c.host.MESSAGES_DIR).read()['beta']['paused']
    c.host.api_send_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_instructions_cannot_silently_release_existing_hold(supervisor):
    c = supervisor
    original = target_row(c, paused=True)
    with pytest.raises(voice.VoiceError, match='Explicitly resume'):
        await prepare(c, 'instruct')
    assert voice.store(c.host.MESSAGES_DIR).read()['beta'] == original
    c.host.api_send_command.assert_not_awaited()
