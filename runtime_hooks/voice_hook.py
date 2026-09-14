#!/usr/bin/python3
"""Trusted managed hook. Session ownership comes from controller peer ancestry."""
import json
import os
import socket
import sys
from pathlib import Path


def pause_fallback(payload, environment):
    """Never recreate the obsolete regex-based release gate."""
    path = Path(environment.get('TMUX_DASH_HOST_HOME', '/home/nimrod_rotem')) / '.tmux-dashboard' / 'voice-guards.json'
    try:
        row = json.loads(path.read_text()).get(environment['TMUX_DASH_VOICE_SESSION'], {})
        if not isinstance(row, dict):
            raise ValueError('Invalid voice guard')
        generation = environment.get('TMUX_DASH_VOICE_GENERATION')
        identity = row.get('root') == payload.get('session_id') and (
            not generation or generation == row.get('generation'))
        if not identity:
            return {'ok': False, 'reason': 'Voice pause identity changed. Reconnect the controls before continuing.'}
        paused = bool(identity and row.get('paused') and
                      (row.get('supervised') or row.get('active')))
        return {'ok': not paused, 'reason': 'The user explicitly paused coding. Resume or release the hold first.'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # A managed account unable to verify its durable hold must fail closed.
        return {'ok': False, 'reason': 'Voice supervision is temporarily unavailable. Wait for the controller or explicitly disable supervision.'}


def check(payload, environment):
    socket_path = environment.get('TMUX_DASH_CONTROLLER_SOCKET') or str(
        Path(environment.get('TMUX_DASH_HOST_HOME', '/home/nimrod_rotem')) / '.tmux-dashboard' / 'controller.sock')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(4)
            connection.connect(socket_path)
            connection.sendall((json.dumps({'op': 'voice_hook', 'payload': payload,
                'generation': environment.get('TMUX_DASH_VOICE_GENERATION', '')}) + '\n').encode())
            response = bytearray()
            while not response.endswith(b'\n'):
                part = connection.recv(65536)
                if not part or len(response) + len(part) > 65536:
                    raise ValueError('Invalid controller response')
                response.extend(part)
            result = json.loads(response)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('Invalid controller response')
            return result
    except (OSError, ValueError):
        return pause_fallback(payload, environment)


def main():
    if not os.environ.get('TMUX_DASH_VOICE_SESSION'):
        return
    try:
        payload = json.loads(sys.stdin.read(1_000_001))
        if not isinstance(payload, dict):
            raise ValueError('Invalid payload')
    except (ValueError, TypeError):
        print('Voice hook payload was invalid.', file=sys.stderr)
        raise SystemExit(2)
    result = check(payload, os.environ)
    if not result.get('ok') and payload.get('hook_event_name') == 'PreToolUse':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny', 'permissionDecisionReason': result.get('reason', 'Voice pause could not be verified.')}}))


if __name__ == '__main__':
    main()
