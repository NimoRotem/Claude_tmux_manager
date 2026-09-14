import io
import json
from unittest.mock import patch

import pytest

import app
from runtime_control import SessionLifecycleStore


def test_create_defaults_are_astra_max():
    body = app.CreateSession(name="work")
    assert (body.model, body.effort, body.no_fallback) == ("gpt-6-astra", "max", False)


def test_new_session_settings_survive_checkpoints_without_changing_account(tmp_path):
    store = SessionLifecycleStore(tmp_path / "state.json")
    options = {"model": "gpt-5.6-luna", "effort": "high", "no_fallback": True}
    row = store.register_active("a", cwd=str(tmp_path), owner_id="admin", launch_options=options)
    store.checkpoint_active("a", cwd=str(tmp_path), owner_id="admin", expected_generation=row["generation"])
    with patch.object(app, "_session_lifecycle", store), patch.object(app, "_session_owner_id", return_value="admin"):
        assert app._saved_session_model_effort("a", "admin") == ("gpt-5.6-luna", "high")
        assert store.get("a")["launch_options"]["no_fallback"] is True


def test_new_session_selection_validated_before_tmux():
    with patch.object(app, "_load_model_snapshot", return_value=([["gpt-6-astra", "Astra"]], {"gpt-6-astra": ["max"]}, 1)):
        with pytest.raises(ValueError, match="support"):
            app._validated_new_session_options(app.CreateSession(effort="ultra"), {"id": "admin"})


@pytest.mark.parametrize("info", ["serverOverloaded", "internalServerError", "rateLimitExceeded", {"responseTooManyFailedAttempts": {"httpStatusCode": 503}}])
def test_operational_failures_allow_recovery(info):
    from session_model_policy import recoverable_error
    assert recoverable_error({"codex_error_info": info, "message": "Service error"})


@pytest.mark.parametrize("info", ["cyberPolicy", "misalignmentPolicyViolation", "unauthorized", "sessionBudgetExceeded", "usageLimitExceeded", "badRequest", "sandboxError", "contextWindowExceeded", "other", {"httpConnectionFailed": {"httpStatusCode": 403}}])
def test_policy_access_budget_and_unknown_errors_never_swap(info):
    from session_model_policy import recoverable_error
    assert not recoverable_error({"codex_error_info": info, "message": "Service error"})


def test_observed_codex_503_event_recovers_but_quoted_error_does_not():
    from session_model_policy import latest_turn, recoverable_error
    failure = {"type": "task_complete", "turn_id": "turn-1", "completed_at": 123, "error": {"codex_error_info": "other", "message": "unexpected status 503 Service Unavailable: Synthetic service unavailable, url: http://localhost/v1/responses"}}
    lines = [json.dumps({"type": "event_msg", "payload": failure})]
    assert recoverable_error(latest_turn(lines)["error"])
    assert latest_turn([json.dumps({"type": "response_item", "payload": {"content": lines}})]) is None
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}}))
    assert latest_turn(lines)["type"] == "task_started"


def test_fallback_uses_verified_models_and_never_raises_effort():
    from session_model_policy import next_model
    catalog = {"gpt-5.6-sol": ["low", "high", "ultra"], "gpt-5.6-luna": ["high", "max"]}
    assert next_model("gpt-6-astra", "max", catalog, ["missing", "gpt-5.6-sol"], []) == ("gpt-5.6-sol", "high")
    assert next_model("gpt-6-astra", "max", catalog, ["gpt-5.6-sol"], ["gpt-5.6-sol"]) is None


def test_opt_out_and_exhaustion_prevent_fallback():
    from session_model_policy import fallback_candidate
    event = {"type": "task_complete", "turn_id": "t", "error": {"codex_error_info": "serverOverloaded"}}
    catalog = {"gpt-5.6-sol": ["max"]}
    options = {"model": "gpt-6-astra", "effort": "max", "no_fallback": True}
    assert fallback_candidate(options, event, catalog, ["gpt-5.6-sol"]) is None
    options.update(no_fallback=False, fallback={"attempts": 2})
    assert fallback_candidate(options, event, catalog, ["gpt-5.6-sol"]) is None


def test_session_settings_cannot_cross_owner_or_recycled_generation(tmp_path):
    from session_model_policy import update_options
    store = SessionLifecycleStore(tmp_path / "state.json")
    row = store.register_active("a", cwd=str(tmp_path), owner_id="alice")
    for owner, generation in [("bob", row["generation"]), ("alice", "stale")]:
        with pytest.raises(ValueError):
            update_options(store, "a", owner, generation, {"model": "bad"})
    assert "launch_options" not in store.get("a")


@pytest.mark.parametrize("visible", ["shell $", "› draft\n  gpt-6-astra max · /tmp", "›\n  gpt-6-astra max · /tmp\n esc to interrupt", "Allow this command?\n› 1. Yes"])
def test_recovery_never_types_into_drafts_running_turns_or_prompts(visible):
    assert not app._model_recovery_composer_ready(visible)


def test_recovery_requires_an_explicit_empty_codex_composer():
    assert app._model_recovery_composer_ready("Service unavailable\n\n›\n\n  gpt-6-astra max · /tmp")


@pytest.mark.asyncio
async def test_fallback_opt_out_does_not_read_or_mutate_terminal(tmp_path):
    store = SessionLifecycleStore(tmp_path / "state.json")
    store.register_active("a", cwd=str(tmp_path), owner_id="admin", launch_options={"model": "gpt-6-astra", "effort": "max", "no_fallback": True})
    with patch.object(app, "_session_lifecycle", store), patch.object(app, "subprocess") as process:
        await app._model_fallback_once("a")
        process.run.assert_not_called()


@pytest.fixture
def recovery_runtime(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager, nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import time
    store = SessionLifecycleStore(tmp_path / 'state.json')
    opts = {'model': 'gpt-6-astra', 'effort': 'max', 'no_fallback': False}
    row = store.register_active('a', cwd=str(tmp_path), owner_id='admin', launch_options=opts)
    store.checkpoint_active('a', cwd=str(tmp_path), owner_id='admin', resume_uuid='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', expected_generation=row['generation'])
    event = {'type': 'task_complete', 'turn_id': 'failed-turn', 'completed_at': time.time()-30,
             'error': {'codex_error_info': 'serverOverloaded'}}
    state = {'running': True, 'event': event, 'commands': [], 'visible': 'Service error\n\n›\n\n  gpt-6-astra max · /tmp'}
    binding = {'session_id': '$123', 'generation': row['generation']}
    @asynccontextmanager
    async def lock(*args):
        yield
    async def running(*args): return state['running']
    async def ensure(*args, **kwargs):
        state['running'] = True
        state['resume'] = kwargs
        return True
    def command(args, **kwargs):
        if args[1]=='capture-pane': return SimpleNamespace(returncode=0, stdout=state['visible'])
        state['commands'].append(args)
        if args[-1]=='Enter' and state.get('typed')=='/quit': state['running']=False
        if '-l' in args: state['typed']=args[-1]
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(app, '_session_lifecycle', store)
    monkeypatch.setattr(app, '_session_auth_mode', {'a':'subscription'})
    monkeypatch.setattr(app, '_strict_session_owner', lambda *a: ('admin', {'id':'admin'}))
    monkeypatch.setattr(app, '_session_owner_id', lambda *a: 'admin')
    monkeypatch.setattr(app, '_async_is_codex_running', running)
    monkeypatch.setattr(app, '_model_recovery_event', lambda *a: state['event'])
    monkeypatch.setattr(app, '_codex_session_facts', lambda *a: {'model':store.get('a')['launch_options']['model'],'metrics_thread_id':store.get('a')['resume_uuid']})
    monkeypatch.setattr(app, '_load_model_snapshot', lambda *a: ([], {'gpt-5.6-sol':['max'], 'gpt-5.6-terra':['max']}, 1))
    monkeypatch.setattr(app, '_async_tmux_server_mutation_lock', lock)
    monkeypatch.setattr(app, '_session_operation_lock', lambda *a: nullcontext())
    monkeypatch.setattr(app, '_autopush_action_lock', lock)
    monkeypatch.setattr(app, '_autopush_terminal_binding', AsyncMock(return_value=binding))
    monkeypatch.setattr(app, '_terminal_binding_state', lambda *a: 'current')
    monkeypatch.setattr(app, '_ensure_codex_running', ensure)
    monkeypatch.setattr(app.subprocess, 'run', command)
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(app, '_ensure_codex_submitted', AsyncMock(return_value='submitted'))
    return store, state


@pytest.mark.asyncio
async def test_recovery_resumes_exact_thread_with_saved_model_and_only_once(recovery_runtime):
    store, state = recovery_runtime
    assert await app._model_fallback_once('a') is True
    assert store.get('a')['launch_options']['model'] == 'gpt-5.6-sol'
    assert store.get('a')['launch_options']['effort'] == 'max'
    assert state['resume']['resume_uuid'] == 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    assert state['resume']['allow_fresh'] is False
    assert state['resume']['expected_owner_id'] == 'admin'
    assert 'continuing the same session' in store.get('a')['launch_options']['fallback']['status']
    count = len(state['commands'])
    assert await app._model_fallback_once('a') is False
    assert len(state['commands']) == count
    assert not any('C-c' in command for command in state['commands'])


@pytest.mark.asyncio
async def test_recovery_checks_newer_turn_after_restart_and_does_not_submit(recovery_runtime, monkeypatch):
    store, state = recovery_runtime
    original = app._ensure_codex_running
    async def ensure(*a, **k):
        result = await original(*a, **k)
        state['event'] = {'type':'task_started', 'turn_id':'human-turn'}
        return result
    monkeypatch.setattr(app, '_ensure_codex_running', ensure)
    assert await app._model_fallback_once('a') is False
    assert [c[-1] for c in state['commands'] if '-l' in c] == ['/quit']


@pytest.mark.asyncio
async def test_recovery_does_not_continue_until_submission_is_confirmed(recovery_runtime, monkeypatch):
    from unittest.mock import AsyncMock
    store, state = recovery_runtime
    monkeypatch.setattr(app, '_ensure_codex_submitted', AsyncMock(return_value='stranded'))
    assert await app._model_fallback_once('a') is False
    assert 'manually' in store.get('a')['launch_options']['fallback']['status']


@pytest.mark.asyncio
async def test_fallback_stops_after_two_models_and_keeps_policy_blocks_visible(recovery_runtime):
    store, state = recovery_runtime
    assert await app._model_fallback_once('a')
    state['event'] = {**state['event'], 'turn_id':'second-failure'}
    assert await app._model_fallback_once('a')
    state['event'] = {**state['event'], 'turn_id':'third-failure'}
    assert not await app._model_fallback_once('a')
    assert store.get('a')['launch_options']['fallback']['attempts'] == 2
    assert 'stopped' in store.get('a')['launch_options']['fallback']['status']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['draft', 'policy', 'wrong_binding', 'api_auth', 'active_turn'])
async def test_recovery_sends_nothing_when_guards_fail(recovery_runtime, monkeypatch, change):
    store, state = recovery_runtime
    if change=='draft': state['visible']='› my draft\n  gpt-6-astra max · /tmp'
    if change=='policy': state['event']['error']['codex_error_info']='cyberPolicy'
    if change=='wrong_binding': monkeypatch.setattr(app, '_terminal_binding_state', lambda *a:'replaced')
    if change=='api_auth': monkeypatch.setattr(app, '_session_auth_mode', {'a':'api'})
    if change=='active_turn': state['event']={'type':'task_started','turn_id':'new'}
    assert not await app._model_fallback_once('a')
    assert state['commands']==[]
    assert store.get('a')['launch_options']['model']=='gpt-6-astra'


def test_manual_model_change_rejects_a_recycled_session_during_settings_read(tmp_path, monkeypatch):
    store = SessionLifecycleStore(tmp_path / 'state.json')
    options={'model':'gpt-6-astra','effort':'max','no_fallback':False}
    store.register_active('a', cwd=str(tmp_path), owner_id='admin', launch_options=options)
    def recycle(*args):
        store.register_active('a', cwd=str(tmp_path), owner_id='admin', launch_options=options)
        return dict(options)
    monkeypatch.setattr(app, '_session_lifecycle', store)
    monkeypatch.setattr(app, '_session_launch_options', recycle)
    with pytest.raises(app._SessionOwnershipChangedError):
        app._write_validated_session_codex_settings('a',model_efforts={'gpt-5.6-sol':['max']},requested_model='gpt-5.6-sol',expected_owner_id='admin')
    assert store.get('a')['launch_options']['model']=='gpt-6-astra'


@pytest.mark.asyncio
async def test_old_model_failure_cannot_override_a_new_selection(recovery_runtime, monkeypatch):
    store,state=recovery_runtime
    monkeypatch.setattr(app,'_codex_session_facts',lambda *args:{'model':'gpt-5.6-luna','metrics_thread_id':store.get('a')['resume_uuid']})
    assert not await app._model_fallback_once('a')
    assert state['commands']==[]


def test_explicit_session_name_survives_automatic_title_updates(tmp_path, monkeypatch):
    from runtime_control import LockedJsonStore
    store=SessionLifecycleStore(tmp_path/'lifecycle.json')
    row=store.register_active('website-updates',cwd=str(tmp_path),owner_id='admin')
    monkeypatch.setattr(app,'_session_lifecycle',store)
    monkeypatch.setattr(app,'_session_owner_id',lambda *args:'admin')
    labels={'website-updates':{'owner_id':'admin','generation':row['generation'],'manual_label':'Website updates <round 2>','label':'Automatic title'}}
    assert app._session_tab_label('website-updates',labels)=='Website updates <round 2>'
    labels['website-updates']['generation']='old'
    assert app._session_tab_label('website-updates',labels)!='Website updates <round 2>'
