import pytest

from chat_summaries import clean_prose, compact_summary, latest_turn, recent_turns


def codex(text, role='assistant', timestamp=0, **extra):
    return {'type': 'response_item', 'timestamp': timestamp, 'payload': {
        'type': 'message', 'role': role,
        'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}], **extra}}


def legacy(text, role='assistant', timestamp=0):
    return {'type': 'event_msg', 'timestamp': timestamp, 'payload': {
        'type': 'user_message' if role == 'user' else 'agent_message', 'message': text}}


def claude(text, role='assistant', timestamp=0, **extra):
    return {'type': role, 'timestamp': timestamp, 'message': {
        'role': role, 'content': [{'type': 'text', 'text': text}]}, **extra}


def test_codex_canonical_commentary_and_final():
    result = latest_turn([codex('Fix mobile chat', 'user', '2026-09-13T00:00:00Z'),
                          codex('Found the missing reply.', timestamp=1, channel='commentary'),
                          codex('Chat fixed. Tests pass.', timestamp=2, channel='final')])
    assert result['user_text'] == 'Fix mobile chat'
    assert result['started_at'] == 1789257600
    assert result['has_user'] is True
    assert result['text'] == 'Found the missing reply.\n\nChat fixed. Tests pass.'
    assert result['final_text'] == 'Chat fixed. Tests pass.'
    assert result['last_output_at'] == 2


@pytest.mark.parametrize('field', ['channel', 'phase'])
def test_analysis_is_never_visible(field):
    result = latest_turn([codex('Fix chat', 'user'), codex('Private chain of thought', **{field: 'analysis'}),
                          {'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': 'Secret'}},
                          {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'Secret'}},
                          codex('Not user-facing', role='developer'), codex('Done', channel='final')])
    assert result['text'] == result['final_text'] == 'Done'


def test_phase_final_answer_is_recognized():
    assert latest_turn([codex('Done', phase='final_answer')])['final_text'] == 'Done'


@pytest.mark.parametrize('channel', ['summary', 'notification', 'analysis', 'reasoning', 'thinking'])
def test_non_conversation_channels_are_excluded(channel):
    assert latest_turn([codex('Internal context', channel=channel)])['text'] == ''


def test_split_canonical_final_messages_are_joined():
    result = latest_turn([codex('Checking.', channel='commentary'),
                          codex('Fixed.', channel='final'), codex('Tests pass.', channel='final')])
    assert result['final_text'] == 'Fixed.\n\nTests pass.'


@pytest.mark.parametrize('reverse', [False, True])
def test_canonical_user_and_assistant_win_over_duplicate_notifications(reverse):
    users = [legacy('Fix chat', 'user', 1), codex('Fix chat', 'user', 2)]
    if reverse:
        users.reverse()
    result = latest_turn(users + [legacy('Duplicate legacy reply', timestamp=3),
                                 codex('Canonical reply', timestamp=4, channel='final'),
                                 legacy('Duplicate legacy reply', timestamp=5)])
    assert result['started_at'] == 2
    assert result['text'] == 'Canonical reply'
    assert result['last_output_at'] == 4


def test_legacy_only_turn_is_supported():
    result = latest_turn([legacy('Fix it', 'user', '2.5'), legacy('Fixed', timestamp=4)])
    assert result['user_text'] == 'Fix it'
    assert result['started_at'] == 2.5
    assert result['text'] == result['final_text'] == 'Fixed'
    assert result['explicit_final'] is False


def test_legacy_task_complete_is_an_explicit_completion_signal():
    result = latest_turn([legacy('Fix it', 'user', 1), legacy('Fixed', timestamp=2),
                          {'type': 'event_msg', 'payload': {'type': 'task_complete'}}])
    assert result['explicit_final'] is True


def test_sidechain_task_complete_does_not_complete_root_turn():
    result = latest_turn([legacy('Still working', timestamp=2), {'type': 'event_msg',
                          'isSidechain': True, 'payload': {'type': 'task_complete'}}])
    assert result['explicit_final'] is False


def test_legacy_and_claude_candidates_are_not_explicit_finals():
    assert latest_turn([legacy('Still working', timestamp=2)])['explicit_final'] is False
    assert latest_turn([claude('Still working', timestamp=2)])['explicit_final'] is False
    assert latest_turn([codex('Still working', channel='commentary')])['explicit_final'] is False
    assert latest_turn([codex('Done', channel='final')])['explicit_final'] is True
    assert latest_turn([codex('Done', phase='final_answer')])['explicit_final'] is True


def test_legacy_fallback_is_per_turn_not_entire_file():
    result = latest_turn([codex('Old question', 'user'), codex('Old answer', channel='final'),
                          legacy('Newest question', 'user', 2), legacy('Newest answer', timestamp=3)])
    assert result['user_text'] == 'Newest question'
    assert result['text'] == 'Newest answer'


def test_new_user_starts_a_fresh_turn_even_when_prompt_is_repeated():
    first = latest_turn([codex('Continue', 'user', 1), codex('First answer')])
    second = latest_turn([codex('Continue', 'user', 1), codex('First answer'),
                          codex('Continue', 'user', 2), codex('Second answer')])
    assert second['text'] == 'Second answer'
    assert first['turn_id'] != second['turn_id']


def test_setup_injections_do_not_hide_current_assistant():
    result = latest_turn([codex('Fix chat', 'user', 1), codex('Working on chat.'),
                          codex('# AGENTS.md instructions for /example\n<INSTRUCTIONS>rules</INSTRUCTIONS>', 'user'),
                          codex('<environment_context>workspace</environment_context>', 'user'),
                          codex('<system-reminder>internal notice</system-reminder>', 'user'),
                          codex('Done', channel='final')])
    assert result['user_text'] == 'Fix chat'
    assert result['text'] == 'Working on chat.\n\nDone'


def test_mixed_setup_and_real_request_keeps_only_the_request():
    row = codex('<environment_context>setup</environment_context>\nActual request', 'user')
    row['payload']['content'].insert(0, {'type': 'input_text', 'text': '# AGENTS.md instructions for /example\nrules'})
    assert latest_turn([row])['user_text'] == 'Actual request'


def test_claude_text_ignores_thinking_tool_use_tool_results_and_sidechains():
    assistant = claude('Working on chat.', timestamp=2)
    assistant['message']['content'] += [{'type': 'thinking', 'thinking': 'Private'},
                                        {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'secret'}}]
    result = latest_turn([claude('Fix chat', 'user', 1), assistant,
                          {'type': 'user', 'message': {'role': 'user', 'content': [
                              {'type': 'tool_result', 'content': 'Sensitive shell output'}]}},
                          claude('Sidechain question', 'user', 3, isSidechain=True),
                          claude('Sidechain reply', timestamp=4, isSidechain=True),
                          claude('Fixed. Tests pass.', timestamp=5)])
    assert result['user_text'] == 'Fix chat'
    assert result['text'] == 'Working on chat.\n\nFixed. Tests pass.'
    assert result['final_text'] == 'Fixed. Tests pass.'
    assert result['last_output_at'] == 5


def test_clipped_tail_uses_stable_fallback_identity():
    first = latest_turn([codex('Investigating')], fallback_user='Fix chat', fallback_ts=100)
    second = latest_turn([codex('Fixed', channel='final')], fallback_user='Fix chat', fallback_ts=100)
    assert first['turn_id'] == second['turn_id']
    assert second['has_user'] is False
    assert second['started_at'] == 100
    assert second['user_text'] == 'Fix chat'
    assert second['source'] == 'codex'


def test_clipped_claude_tail_retains_source_for_completion_gating():
    result = latest_turn([claude('Still working.')], fallback_user='Fix chat', fallback_ts=100)
    assert result['source'] == 'claude'
    assert result['has_user'] is False


def test_user_record_identifier_is_stable_across_timestamp_formats():
    a = codex('Fix it', 'user', 1)
    b = codex('Fix it', 'user', '2026-09-13T00:00:00Z')
    a['id'] = b['id'] = 'stable-record-id'
    assert latest_turn([a])['turn_id'] == latest_turn([b])['turn_id']


@pytest.mark.parametrize('value', [None, 'invalid', float('nan'), float('inf'), -1, True])
def test_invalid_timestamps_are_safe(value):
    assert latest_turn([codex('Request', 'user', value)])['started_at'] == 0


def test_non_records_and_empty_unsafe_content_do_not_produce_summary():
    records = [None, [], {'type': 'response_item', 'payload': []},
               codex('```python\nprint("private")\n```'),
               {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                   {'type': 'thinking', 'thinking': 'Private'}]}}]
    result = latest_turn(records)
    assert result['text'] == result['final_text'] == ''
    assert result['last_output_at'] == 0
    assert compact_summary('```sh\ncat secret\n```') == ''


def test_clean_prose_removes_fenced_indented_code_traceback_and_diff():
    raw = '''Fixed the mobile button.
```python
print("not a message")
```
    assert result == 1
Traceback (most recent call last):
File "test_api.py", line 9275
AssertionError: expected 1
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old_code()
+new_code()
Tests pass. [Open dashboard](https://codex.lisa.my).
'''
    assert clean_prose(raw) == 'Fixed the mobile button.\nTests pass. [Open dashboard](https://codex.lisa.my).'


def test_unterminated_fence_stays_hidden():
    assert clean_prose('Useful update.\n~~~python\nprint("secret")') == 'Useful update.'


def test_markdown_headings_lists_links_and_questions_remain_readable():
    assert clean_prose('## Result\n- **Fixed** mobile chat in `app.py`.\n    - Tests pass.\n'
                       'Open [dashboard](https://codex.lisa.my/Nimo/demo).\nWhich layout works?') == (
                           'Result\nFixed mobile chat in app.py.\nTests pass.\n'
                           'Open [dashboard](https://codex.lisa.my/Nimo/demo).\nWhich layout works?')


def test_code_inline_expressions_and_terminal_metadata_are_not_replies():
    assert clean_prose('Use `x = 1` to test.\nBash(pytest -q)\n⎿ raw output\nWall time: 1 second\n'
                       'const token = "secret";\nDone.') == 'Use to test.\nDone.'


def test_json_and_bulleted_tool_status_do_not_become_chat_messages():
    assert clean_prose('• Ran ls\n{"raw": "data"}\n["raw", "data"]\nFixed chat.') == 'Fixed chat.'


def test_compact_summary_prefers_latest_outcome_over_opening_commentary():
    text = 'I will inspect the files. I will trace the bug. I am still reviewing. '
    text += 'Chat fixed. Mobile button restored. Tests pass. Let me know if you need anything.'
    assert compact_summary(text) == 'Chat fixed. Mobile button restored. Tests pass.'


def test_compact_summary_uses_latest_paragraph_after_long_early_commentary():
    early = ' '.join(['Earlier investigation details'] * 100)
    assert compact_summary(early + '\n\nParser fixed. Checking mobile layout now.') == (
        'Parser fixed. Checking mobile layout now.')


def test_later_progress_changes_summary_despite_large_unchanged_prefix():
    early = ' '.join(['Earlier investigation details'] * 100)
    first = compact_summary(early + '\n\nParser fixed. Checking mobile layout now.')
    second = compact_summary(early + '\n\nParser fixed. Checking mobile layout now.'
                             '\n\nMobile layout fixed. Running regression checks now.')
    assert first != second
    assert second.endswith('Mobile layout fixed. Running regression checks now.')


def test_compact_summary_is_bounded_and_does_not_cut_markdown_links():
    text = 'Earlier commentary. The fix is live at [open dashboard](https://codex.lisa.my/Nimo/demo). Tests pass.'
    summary = compact_summary(text, max_words=12, max_sentences=2)
    assert '[open dashboard](https://codex.lisa.my/Nimo/demo)' in summary
    assert summary.endswith('Tests pass.')
    assert len(summary.split()) <= 12


def test_long_sentence_is_shortened_and_fallback_preserves_latest_outcome():
    assert compact_summary(' '.join(['word'] * 100), max_words=5) == 'word word word word word…'
    assert compact_summary(' '.join(['Earlier'] * 100) + '. Fixed.', max_words=5) == 'Fixed.'


@pytest.mark.parametrize('options', [{'max_words': 0}, {'max_sentences': 0}, {'max_words': -2}, {'max_sentences': '3'}])
def test_invalid_summary_limits_return_empty(options):
    assert compact_summary('Fixed.', **options) == ''


def test_recent_turns_returns_history_and_running_latest_turn():
    records = [codex('First question', 'user', 1), codex('First answer', channel='final', timestamp=2),
               codex('Next question', 'user', 3), codex('Working on it', channel='commentary', timestamp=4)]
    turns = recent_turns(records)
    assert [turn['user_text'] for turn in turns] == ['First question', 'Next question']
    assert [turn['text'] for turn in turns] == ['First answer', 'Working on it']
    assert turns[0]['explicit_final'] is True
    assert turns[0]['ended_by_user'] is True
    assert turns[1]['explicit_final'] is False
    assert turns[1]['ended_by_user'] is False
    assert turns[-1] == latest_turn(records)


def test_recent_turns_deduplicates_legacy_user_notices_and_assistant_messages():
    records = [legacy('First question', 'user', 1), codex('First question', 'user', 1),
               codex('First answer', channel='final'), legacy('First answer'),
               codex('Next question', 'user', 2), legacy('Next question', 'user', 2),
               legacy('Legacy duplicate'), codex('Canonical answer', channel='final')]
    turns = recent_turns(records)
    assert len(turns) == 2
    assert [turn['text'] for turn in turns] == ['First answer', 'Canonical answer']


def test_recent_turns_ignores_setup_and_tool_result_user_records():
    records = [claude('First question', 'user', 1), claude('First answer'),
               claude('<system-reminder>internal notice</system-reminder>', 'user'),
               {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'hidden'}]}},
               claude('More progress'), claude('Next question', 'user', 2)]
    turns = recent_turns(records)
    assert len(turns) == 2
    assert turns[0]['text'] == 'First answer\n\nMore progress'
    assert turns[0]['final_text'] == 'More progress'
    assert turns[0]['explicit_final'] is False
    assert turns[1]['text'] == ''


def test_recent_turns_is_bounded_and_consumes_generator_once():
    def records():
        for index in range(50):
            yield codex(f'Question {index}', 'user', index + 1)
            yield codex(f'Answer {index}', channel='final')
    turns = recent_turns(records())
    assert len(turns) == 30
    assert turns[0]['user_text'] == 'Question 20'
    assert turns[-1]['user_text'] == 'Question 49'
    assert len(recent_turns(records(), limit=500)) == 30
    assert [turn['user_text'] for turn in recent_turns(records(), limit=2)] == ['Question 48', 'Question 49']


def test_recent_turns_excludes_orphaned_clipped_output_but_keeps_latest_user():
    turns = recent_turns([codex('Old orphaned reply', channel='final'), codex('New question', 'user', 1)])
    assert len(turns) == 1
    assert turns[0]['user_text'] == 'New question'
    assert turns[0]['text'] == ''
    assert recent_turns([codex('Only orphaned output')]) == []


def test_completion_does_not_leak_into_a_later_turn():
    records = [legacy('First', 'user', 1), legacy('First answer'),
               {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
               legacy('Second', 'user', 2), legacy('Second progress')]
    turns = recent_turns(records)
    assert turns[0]['explicit_final'] is True
    assert turns[1]['explicit_final'] is False


@pytest.mark.parametrize('limit', [0, -1, None, '2', True])
def test_recent_turns_invalid_limit_is_empty(limit):
    assert recent_turns([codex('Question', 'user')], limit=limit) == []
