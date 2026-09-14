"""Explicit, connection-local pre-test check-in. No timer or speech is persisted."""
import re
import time

CHECKIN_QUESTION = 'Any other feature tweaks before I test?'
CHECKIN_SECONDS = 180
HOLD = re.compile(r"\b(?:wait|stop|hold|cancel|not yet|do not|don['’]t|no deployment)\b", re.I)
CHECKIN_GUIDANCE = (
    'The user explicitly enabled the attached-session three-minute pre-test check-in for this '
    'voice connection. Before testing the current requested work, ask exactly: '
    + CHECKIN_QUESTION + ' Wait for adjustments or the scoped inactivity continuation. '
    'That continuation permits only already-requested testing and the usual workflow when all '
    'existing project rules and approvals permit it. It grants no deployment or sensitive-action '
    'permission and cannot override any hold, restriction or required approval. '
    'Changed work requires a fresh question. Never apply this check-in to another session.'
)


class FeatureCheckin:
    def __init__(self, connection):
        self.c = connection
        self.pending = None
        self.seen = None
        self.speech = ''
        self.input_revision = 0

    def cancel(self):
        self.pending = None
        self.speech = ''
        self.input_revision += 1

    def activity(self, text=''):
        self.input_revision += 1
        if not self.pending:
            return
        self.pending['deadline'] = time.monotonic() + CHECKIN_SECONDS
        self.speech = (self.speech + text)[-2000:]
        if HOLD.search(self.speech):
            self.cancel()

    async def observe(self, decision):
        c = self.c
        async with c.action_lock:
            if not c.checkin_enabled:
                self.cancel()
                return
            await c.validate()
            row = c.state()
            if (not c.ready or c.ended or c.stopping or not row.get('connected')
                    or row.get('paused') or row.get('nonce') != c.nonce):
                self.cancel()
                return
            key = (row.get('revision'), decision)
            if CHECKIN_QUESTION not in decision.splitlines():
                self.cancel()
                return
            if self.pending and self.pending['key'] != key:
                self.cancel()
            if not self.pending:
                if key == self.seen:
                    return
                self.seen = key
                self.speech = ''
                self.pending = {'key': key, 'deadline': time.monotonic() + CHECKIN_SECONDS}
                await c.browser({'type': 'feature_checkin', 'message':
                    'Pre-test check-in: 3 minutes without input will continue only the already-requested '
                    'work. Existing holds and approvals still apply.'})
                return
            if time.monotonic() < self.pending['deadline']:
                return
            if c.calls or not c.response_queue.empty() or c.response_ids:
                self.pending['deadline'] = time.monotonic() + CHECKIN_SECONDS
                return
            pending = self.pending
            input_revision = self.input_revision
            # Read the current attached conversation again, never a background update.
            page = await c.context()
            latest = [entry for entry in page['entries'] if entry.get('kind') in ('user', 'assistant')][-1:]
            current = c.decision_text(latest)
            activity = await c.host.async_detect_activity(c.name)
            await c.validate()
            if (self.pending is not pending or not c.checkin_enabled or c.ended or c.stopping
                    or c.state().get('revision') != key[0] or c.state().get('paused')
                    or current != decision or activity.get('status') != 'idle'):
                self.cancel()
                return
            if time.monotonic() < pending['deadline']:
                return
            self.pending = None  # Consume without cancelling this authorized attempt.
            self.speech = ''
            c.request.state.voice_target_guard = {
                'nonce': c.nonce, 'active': True, 'revision': key[0], 'paused': False}
            c.request.state.voice_checkin = True
            c.request.state.voice_permission_check = lambda: (
                c.checkin_enabled and not c.ended and not c.stopping and self.input_revision == input_revision)
            try:
                await c.send('[Voice check-in timeout] The user explicitly enabled the three-minute '
                    'pre-test check-in for this voice connection. The exact current question was asked '
                    'and the interval elapsed without input. Continue only the already-requested testing '
                    'and normal workflow within the existing task scope. Fix failures before any release. '
                    'This is not deployment consent and grants no new permission. Preserve explicit holds, '
                    'no-deploy restrictions and every project, sensitive-action and external approval '
                    'requirement. If an approval or restriction prevents continuation, report it and wait.',
                    False, preserve_guard=True)
            finally:
                c.request.state.voice_target_guard = {'nonce': c.nonce, 'active': True}
                c.request.state.voice_permission_check = None
                c.request.state.voice_checkin = False
            await c.browser({'type': 'feature_checkin', 'message':
                'Check-in ended. The existing task may continue only under its original rules and approvals.'})
