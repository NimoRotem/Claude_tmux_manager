"""Explicit, one-use cross-session actions; no microphone or attachment transfer."""
import asyncio
import copy
import json
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
        if row.get('active') and (row.get('owner_id') != binding['owner_id'] or
                    row.get('generation') != binding['generation'] or
                    row.get('root') != binding['resume_uuid']):
            raise self.voice.VoiceError('Target supervision identity changed. Find the session again.')
        return row

    def snapshot(self, binding):
        row = self.row(binding)
        return {k: copy.deepcopy(row.get(k)) for k in
                ('active', 'nonce', 'revision', 'paused', 'hook_ready')}

    def status(self, binding):
        row = self.row(binding)
        return {'active': bool(row.get('active')), 'hook_ready': bool(row.get('hook_ready')),
                'paused': bool(row.get('paused')), 'revision': row.get('revision'),
                'deployment_workflow': 'existing_project_rules'}

    def mutate(self, binding, expected, change):
        def update(rows):
            row = rows.setdefault(binding['session_name'], {})
            if any(row.get(k) != v for k, v in expected.items()):
                raise self.voice.VoiceError('Target work or approval changed. Ask for fresh confirmation.')
            if row.get('active') and (row.get('owner_id') != binding['owner_id'] or
                        row.get('generation') != binding['generation'] or
                        row.get('root') != binding['resume_uuid']):
                raise self.voice.VoiceError('Target supervision changed.')
            return change(row)
        return self.voice.store(self.c.host.MESSAGES_DIR).update(update)[1]

    def request(self, binding, *, expected=None):
        return Request({**self.c.request.scope, 'state': {
            **self.c.request.scope.get('state', {}), 'voice_approval': False,
            'voice_target_binding': binding, 'voice_idle_only': False,
            'voice_target_guard': expected}})

    async def send(self, binding, text, *, expected=None):
        response = await self.c.host.api_send_command(
            self.request(binding, expected=expected), binding['session_name'],
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
        if action == 'supervise':
            raise voice.VoiceError('Durable supervision is unavailable. Voice mode does not restart coding workers.')
        if action not in ('instruct', 'pause', 'resume', 'confirm', 'deploy'):
            raise voice.VoiceError('Unknown target action.')
        if not summary or len(summary) > 3000:
            raise voice.VoiceError('Name the concrete action and its scope.')
        snapshot = self.snapshot(binding)
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
                 'deploy': 'send this deployment direction to'}
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
        self.offer = None  # Consume before I/O, including uncertain delivery and cancellation.
        action, summary, expected = offer['action'], offer['summary'], offer['snapshot']
        if action == 'pause':
            response = await c.host.api_interrupt_session(self.request(binding, expected=expected), binding['session_name'])
            if response.status_code >= 400:
                raise voice.VoiceError('Target interruption failed.')
            status = await c.host.async_detect_activity(binding['session_name'])
            await c.other_binding(offer['target_id'])
            await c.validate()
            return await self.feedback(binding, 'Coding work stopped.' if status.get('status') == 'idle'
                else 'Interruption requested; completion has not been confirmed.')
        else:
            marker = '[Voice confirmation]' if action == 'confirm' else '[Voice instructions]'
            await self.send(binding, marker + ' For ' + c.spoken_name(binding['session_name']) + ': ' + summary
                + '\n' + ('The user confirmed only this exact action: ' + offer['question'] + '\n'
                          if action == 'confirm' else '')
                + voice.OTHER_SESSION_GUIDANCE + ' '
                + voice.CONSENT_GUIDANCE, expected=expected)
        return await self.feedback(binding, {'deploy': 'Deployment direction delivered.',
            'confirm': 'Scoped confirmation delivered.', 'instruct': 'Instructions delivered.',
            'resume': 'Resume instructions delivered.'}[action])

    async def feedback(self, binding, message):
        label = self.c.spoken_name(binding['session_name'])
        await self.c.browser({'type': 'target_action', 'session': binding['session_name'],
                             'spoken_name': label, 'message': label + ': ' + message})
        return {'ok': True, 'session': binding['session_name'], 'spoken_name': label, 'message': message}

    def disconnect(self):
        # Losing voice neither pauses target work nor releases an explicit user pause.
        self.offer = None
