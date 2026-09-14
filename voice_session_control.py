"""Explicit, one-use cross-session actions; no microphone or attachment transfer."""
import asyncio
import contextlib
import copy
import json
import re
import secrets
import time

from fastapi import Request


class SessionControl:
    def __init__(self, connection):
        self.c = connection
        self.offer = None

    @property
    def voice(self):
        import voice_mode
        return voice_mode

    def row(self, binding):
        row = self.voice.store(self.c.host.MESSAGES_DIR).read().get(binding['session_name'], {})
        if not isinstance(row, dict):
            raise self.voice.VoiceError('Target supervision state is unavailable.')
        if (row.get('active') or row.get('connected') or row.get('paused') or self.voice.supervised_row(row)) and (
                    row.get('owner_id') != binding['owner_id'] or
                    row.get('generation') != binding['generation'] or
                    row.get('root') != binding['resume_uuid']):
            raise self.voice.VoiceError('Target supervision identity changed. Find the session again.')
        return row

    def snapshot(self, binding):
        row = self.row(binding)
        return self.guard(row)

    @staticmethod
    def guard(row):
        return {k: copy.deepcopy(row.get(k)) for k in
                ('active', 'connected', 'nonce', 'revision', 'paused', 'hook_ready',
                 'supervised', 'owner_id', 'generation', 'root')}

    def durable(self, row):
        return bool(self.supervised_guard(row) and row.get('hook_ready'))

    def supervised_guard(self, row):
        # Snapshots represent absent legacy keys with None; retain legacy holds.
        return self.voice.supervised_row({key: value for key, value in row.items() if value is not None})

    def status(self, binding):
        row = self.row(binding)
        return {'active': bool(row.get('active')), 'hook_ready': bool(row.get('hook_ready')),
                'supervised': self.voice.supervised_row(row),
                'paused': bool(row.get('paused')), 'revision': row.get('revision'),
                'deployment_workflow': 'existing_project_rules'}

    async def mutate(self, binding, expected, change, *, source_revision, require_supervision=False):
        """Fence both incarnations, then atomically compare source and target guards."""
        c, voice = self.c, self.voice
        await c.validate()
        request = self.request(binding, expected=expected, source_revision=source_revision,
                               require_supervision=require_supervision)
        with contextlib.ExitStack() as locks:
            for name in sorted({c.name, binding['session_name']}):
                locks.enter_context(c.host._session_operation_lock(name))
            if not voice.request_target_matches(c.host, request, binding['session_name']):
                raise voice.VoiceError('Source or target conversation changed.')
            def update(rows):
                source = rows.get(c.name, {})
                if (c.ended or c.stopping or not source.get('active')
                        or source.get('nonce') != c.nonce or source.get('owner_id') != c.user['id']
                        or source.get('generation') != c.binding['generation']
                        or source.get('root') != c.binding['resume_uuid']
                        or source.get('revision') != source_revision
                        or (require_supervision and (not c.supervision_enabled
                            or source.get('supervision_nonce') != c.nonce))):
                    raise voice.VoiceError('The supervising voice connection changed.')
                row = rows.setdefault(binding['session_name'], {})
                if not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items()):
                    raise voice.VoiceError('Target work or approval changed. Ask for fresh confirmation.')
                change(row)
                return self.guard(row)
            return voice.store(c.host.MESSAGES_DIR).update(update)[1]

    def request(self, binding, *, expected=None, source_revision=None, require_supervision=False):
        source = {'session_name': self.c.name, 'owner_id': self.c.user['id'],
                  'generation': self.c.binding['generation'], 'root': self.c.binding['resume_uuid'],
                  'nonce': self.c.nonce}
        if source_revision is not None:
            source['revision'] = source_revision
        if require_supervision:
            source['supervision_nonce'] = self.c.nonce
        return Request({**self.c.request.scope, 'state': {
            **self.c.request.scope.get('state', {}), 'voice_approval': False,
            'voice_target_binding': binding, 'voice_idle_only': False,
            'voice_target_guard': expected, 'voice_permission_check': None,
            'voice_source_guard': source}})

    def require_supervision_request(self, binding, args):
        """A model-selected target or paraphrased quote cannot authorize a restart."""
        c, voice = self.c, self.voice
        if not c.supervision_enabled:
            raise voice.VoiceError('Explicitly enable supervision in the attached voice controls first.')
        turns = c.speech_turns(time.time() - 120)
        latest = next((turn['text'] for turn in reversed(turns) if turn['role'] == 'user'), '')
        if not latest or voice.spoken_text(latest) != voice.spoken_text(str(args.get('request_quote', ''))):
            raise voice.VoiceError('Quote the complete latest request naming the target and supervision or restart.')
        if re.match(r'^(?:what|how|why|when|where|is|are|did|has|have|can i|should)\b', voice.spoken_text(latest)):
            raise voice.VoiceError('A status question cannot authorize restarting another worker.')
        labels = [voice.spoken_text(binding['session_name']), voice.spoken_text(c.spoken_name(binding['session_name']))]
        clauses = re.split(r'[.!?;,\n]|\b(?:but|however|instead)\b', latest.casefold())
        if any(re.search(r'\b(?:not|never|no|don t|dont|without)\b(?:\s+\w+){0,4}\s+'
                         r'(?:restart(?:ing)?|relaunch(?:ing)?|supervis(?:e|ion|ing))\b',
                         voice.spoken_text(clause)) for clause in clauses):
            raise voice.VoiceError('A restart restriction needs clarification before enabling supervision.')
        for clause in clauses:
            text = voice.spoken_text(clause)
            action = re.search(r'\b(?:restart|relaunch|supervise|enable\s+(?:managed\s+|voice\s+)*supervision)\b', text)
            if (action and not re.search(r'\b(?:not|never|no|don t|dont|without)\b', text[:action.end()])
                    and any(label and (' ' + label + ' ') in (' ' + text + ' ') for label in labels)):
                return
        raise voice.VoiceError('Ask explicitly to supervise or restart the named target before confirming.')

    async def send(self, binding, text, *, expected=None, source_revision=None):
        response = await self.c.host.api_send_command(
            self.request(binding, expected=expected, source_revision=source_revision), binding['session_name'],
            self.c.host.SendCommand(command=text))
        data = json.loads(response.body)
        if response.status_code >= 400 or not data.get('ok'):
            raise self.voice.VoiceError(data.get('error', 'Target message was not delivered.'))
        return data

    async def prepare(self, args):
        c, voice = self.c, self.voice
        # Discovery/reading requires actual user speech. Passive updates grant nothing.
        if c.requested_other_session(args):
            raise voice.VoiceError('Idle assignment consent cannot authorize another action.')
        binding = await c.other_binding(str(args.get('target_id', '')))
        action = args.get('action')
        summary = str(args.get('summary', '')).strip()
        if action not in ('instruct', 'pause', 'resume', 'confirm', 'supervise', 'deploy'):
            raise voice.VoiceError('Unknown target action.')
        if not summary or len(summary) > 3000:
            raise voice.VoiceError('Name the concrete action and its scope.')
        snapshot = self.snapshot(binding)
        if action == 'supervise':
            self.require_supervision_request(binding, args)
            if snapshot['active'] or snapshot['connected'] or self.supervised_guard(snapshot):
                raise voice.VoiceError('That target already has voice controls. Read its state; do not replace them.')
            if not c.host._voice_supervision_available(binding['session_name']):
                raise voice.VoiceError('Managed voice hooks are unavailable for that target.')
        if snapshot['paused'] and action not in ('resume', 'pause', 'supervise'):
            raise voice.VoiceError('Explicitly resume the held target before sending other directions.')
        page = await asyncio.to_thread(c.host._read_terminal_history, binding, '', False)
        # A non-release exception must be an actual current question from the target.
        question = str(args.get('question', '')).strip()
        if action == 'confirm':
            assistant = next((e.get('text', '') for e in reversed(page['entries'])
                              if e.get('kind') == 'assistant'), '')
            if (not question.startswith('Can I ') or not question.endswith('?') or
                    question not in assistant or summary != question[6:-1]):
                raise voice.VoiceError('Quote the target’s current exact Can I question and its full scope.')
        await c.other_binding(args['target_id'])
        await c.validate()
        if snapshot != self.snapshot(binding):
            raise voice.VoiceError('Target work changed while preparing confirmation.')
        label = c.spoken_name(binding['session_name'])
        verbs = {'instruct': 'send these instructions to', 'pause': 'request an interruption of',
                 'resume': 'resume', 'confirm': 'approve this specific action in',
                 'supervise': 'restart the coding worker and enable managed voice supervision for',
                 'deploy': 'send this deployment direction to'}
        if action == 'pause' and self.durable(snapshot):
            verbs['pause'] = 'interrupt and hold future tool calls in'
        if action == 'resume' and snapshot['paused']:
            verbs['resume'] = 'release the explicit tool hold and resume'
        confirmation = voice.confirmation_question(f'{verbs[action]} {label}: {summary}')
        now = time.time()
        c.idle_offer = None
        self.offer = {'id': secrets.token_hex(16), 'target_id': args['target_id'],
                      'binding': copy.deepcopy(binding), 'action': action, 'summary': summary,
                      'question': question, 'snapshot': snapshot, 'history': voice.fingerprint(page['entries']),
                      'confirmation': confirmation, 'created': now, 'expires': now + 120,
                      'source_revision': c.state().get('revision')}
        await c.browser({'type': 'target_confirmation', 'session': binding['session_name'],
                         'spoken_name': label, 'message': confirmation})
        return {'confirmation_id': self.offer['id'], 'confirmation': confirmation,
                'session': binding['session_name'], 'spoken_name': label}

    async def confirm(self, args):
        c, voice = self.c, self.voice
        offer = c.confirmed_offer(self.offer, str(args.get('approval_quote', '')))
        if args.get('confirmation_id') != offer['id'] or c.state().get('revision') != offer['source_revision']:
            raise voice.VoiceError('The action confirmation changed.')
        binding = await c.other_binding(offer['target_id'])
        if binding != offer['binding'] or self.snapshot(binding) != offer['snapshot']:
            raise voice.VoiceError('Target work or approval changed. Ask for fresh confirmation.')
        page = await asyncio.to_thread(c.host._read_terminal_history, binding, '', False)
        if voice.fingerprint(page['entries']) != offer['history']:
            raise voice.VoiceError('Target history changed. Read it and ask for fresh confirmation.')
        await c.other_binding(offer['target_id'])
        await c.validate()
        c.confirmed_offer(offer, str(args.get('approval_quote', '')))
        if c.state().get('revision') != offer['source_revision']:
            raise voice.VoiceError('Source work changed while confirming the action.')
        self.offer = None  # Consume before I/O, including uncertain delivery and cancellation.
        action, summary, expected = offer['action'], offer['summary'], offer['snapshot']
        source_revision = offer['source_revision']
        if action == 'supervise':
            await self.supervise(binding, expected, offer['target_id'], source_revision)
        elif action == 'pause':
            held = self.durable(expected)
            if held:
                expected = await self.mutate(binding, expected, lambda row: row.update(
                    paused=True, revision=(row.get('revision') or 0) + 1, approved=None, pending=None),
                    source_revision=source_revision)
            response = await c.host.api_interrupt_session(self.request(binding, expected=expected,
                source_revision=source_revision), binding['session_name'])
            if response.status_code >= 400:
                raise voice.VoiceError('Target interruption failed.' + (' Future tool calls remain held.' if held else ''))
            status = await c.host.async_detect_activity(binding['session_name'])
            await c.other_binding(offer['target_id'])
            await c.validate()
            current = self.row(binding)
            if any(current.get(key) != value for key, value in expected.items()):
                raise voice.VoiceError('Target controls changed after interruption. Read its current state.')
            return await self.feedback(binding, ('Future tool calls held. ' if held else '') +
                ('Coding work stopped.' if status.get('status') == 'idle'
                 else 'Interruption requested; completion has not been confirmed.'))
        else:
            released = action == 'resume' and bool(expected['paused'])
            if released:
                if not self.durable(expected):
                    raise voice.VoiceError('The held target has no verified hook. Reconnect its controls before releasing it.')
                expected = await self.mutate(binding, expected, lambda row: row.update(
                    paused=False, revision=(row.get('revision') or 0) + 1, approved=None, pending=None),
                    source_revision=source_revision)
            marker = '[Voice confirmation]' if action == 'confirm' else '[Voice instructions]'
            try:
                await self.send(binding, marker + ' For ' + c.spoken_name(binding['session_name']) + ': ' + summary
                    + '\n' + ('The user confirmed only this exact action: ' + offer['question'] + '\n'
                              if action == 'confirm' else '')
                    + voice.OTHER_SESSION_GUIDANCE + ' '
                    + voice.CONSENT_GUIDANCE, expected=expected, source_revision=source_revision)
            except Exception as exc:
                if released:
                    raise voice.VoiceError('The confirmed hold was released, but resume instructions were not delivered.') from exc
                raise
        return await self.feedback(binding, {'supervise': 'Managed pause controls enabled. Existing project rules remain unchanged.',
            'deploy': 'Deployment direction delivered.',
            'confirm': 'Scoped confirmation delivered.', 'instruct': 'Instructions delivered.',
            'resume': 'Resume instructions delivered.'}[action])

    async def feedback(self, binding, message):
        label = self.c.spoken_name(binding['session_name'])
        await self.c.browser({'type': 'target_action', 'session': binding['session_name'],
                             'spoken_name': label, 'message': label + ': ' + message})
        return {'ok': True, 'session': binding['session_name'], 'spoken_name': label, 'message': message}

    async def supervise(self, binding, expected, target_id, source_revision):
        c, voice = self.c, self.voice
        if (not c.supervision_enabled or expected['active'] or expected['connected']
                or self.supervised_guard(expected) or not c.host._voice_supervision_available(binding['session_name'])):
            raise voice.VoiceError('Supervision consent or target availability changed.')
        nonce = secrets.token_hex(16)
        expected = await self.mutate(binding, expected, lambda row: row.update(
            active=False, connected=False, supervised=True, hook_ready=False,
            owner_id=binding['owner_id'], generation=binding['generation'], root=binding['resume_uuid'],
            nonce=nonce, revision=(row.get('revision') or 0) + 1, paused=bool(row.get('paused')),
            approved=None, pending=None), source_revision=source_revision, require_supervision=True)
        # The trusted hook may acknowledge the launch while the restart helper is
        # finishing. Only that monotonic readiness transition is allowed here.
        guard = {key: value for key, value in expected.items() if key != 'hook_ready'}
        request = self.request(binding, expected=guard, source_revision=source_revision, require_supervision=True)
        if not await c.host._voice_enable_supervision(request, binding['session_name']):
            raise voice.VoiceError('Target restart was not confirmed. Supervision is not ready; existing holds remain unchanged.')
        for _ in range(80):
            await c.validate()
            await c.other_binding(target_id)
            row = self.row(binding)
            if (not c.supervision_enabled or c.state().get('revision') != source_revision
                    or any(row.get(key) != value for key, value in guard.items())):
                raise voice.VoiceError('Target or supervising connection changed during restart.')
            if row.get('hook_ready'):
                return
            await asyncio.sleep(.25)
        raise voice.VoiceError('Target hook did not acknowledge readiness. Reconnect its controls; existing holds remain unchanged.')

    def disconnect(self):
        # Losing voice neither pauses target work nor releases an explicit user pause.
        self.offer = None
