"""Collapsed paste chips must submit once without pressing unrelated pickers."""
from unittest.mock import AsyncMock, Mock

import pytest
import app


def pane(composer):
    return f"• Previous reply.\n› {composer}\n\n  gpt-6-astra xhigh · ~/project · Main [default]\n"


@pytest.mark.parametrize("composer,text", [
    ("[Pasted Content 4096 chars]", "x" * 4096),
    ("[Pasted Content 1024 chars][Pasted Content 1024 chars] #2 rest of message", "x" * 2048 + " rest of message"),
    ("Please check [Pasted Content 2048 chars]", "Please check " + "x" * 2048),
    ("[Pasted text +20 lines]", "long\n" * 20),
    ("This prompt wraps across terminal rows", "This prompt wraps\n across terminal rows"),
])
@pytest.mark.asyncio
async def test_collapsed_or_wrapped_paste_gets_submit_and_confirmation(monkeypatch, composer, text):
    capture = Mock(side_effect=[pane(composer), pane("")])
    run = Mock()
    monkeypatch.setattr(app, 'capture_pane_recent', capture)
    monkeypatch.setattr(app.subprocess, 'run', run)
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    assert await app._ensure_codex_submitted('demo', text, '$99') == 'submitted'
    assert [call.args[0][-1] for call in run.call_args_list] == ['C-m']


@pytest.mark.asyncio
async def test_collapsed_paste_uses_codex_queue_when_busy(monkeypatch):
    monkeypatch.setattr(app, 'capture_pane_recent', Mock(return_value=pane('[Pasted Content 2048 chars]') + 'Press Tab to queue message'))
    run = Mock()
    monkeypatch.setattr(app.subprocess, 'run', run)
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    assert await app._ensure_codex_submitted('demo', 'x' * 2048, '$99') == 'queued'
    assert [call.args[0][-1] for call in run.call_args_list] == ['Tab']


@pytest.mark.parametrize('visible', [
    '› [Pasted Content 4096 chars]\nSelect one\n❯ 1. Yes\n  2. No',
    pane('A different draft'),
    pane('A different draft [Pasted Content 1024 chars]'),
])
@pytest.mark.asyncio
async def test_paste_retry_does_not_touch_history_picker_or_another_draft(monkeypatch, visible):
    run = Mock()
    monkeypatch.setattr(app, 'capture_pane_recent', Mock(return_value=visible))
    monkeypatch.setattr(app.subprocess, 'run', run)
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    assert await app._ensure_codex_submitted('demo', 'x' * 4096, '$99') == 'submitted'
    run.assert_not_called()


@pytest.mark.asyncio
async def test_last_retry_is_verified(monkeypatch):
    monkeypatch.setattr(app, 'capture_pane_recent', Mock(side_effect=[pane('[Pasted Content 2048 chars]')] * 3 + [pane('')]))
    monkeypatch.setattr(app.subprocess, 'run', Mock())
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    assert await app._ensure_codex_submitted('demo', 'x' * 2048, '$99') == 'submitted'


@pytest.mark.asyncio
async def test_paste_that_never_leaves_is_reported_stranded(monkeypatch):
    monkeypatch.setattr(app, 'capture_pane_recent', Mock(return_value=pane('[Pasted Content 2048 chars]')))
    monkeypatch.setattr(app.subprocess, 'run', Mock())
    monkeypatch.setattr(app.asyncio, 'sleep', AsyncMock())
    assert await app._ensure_codex_submitted('demo', 'x' * 2048, '$99') == 'stranded'
