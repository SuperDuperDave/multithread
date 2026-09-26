"""Claude stream selection, durable input receipts and owned cleanup together."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import shlex
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import test_claude_protocol as protocol
import test_codex_protocol as fixture
from relay_runtime import peer_control as control, provider as peer


class ClaudeIntegrationTests(unittest.TestCase):
    setUp = fixture.CodexProtocolTests.setUp
    executable = staticmethod(fixture.CodexProtocolTests.executable)
    configure = fixture.CodexProtocolTests.configure

    def invoke(self, steps, *, update=False, closure_fault=False, progress_only=False):
        hook = shlex.join([str(self.relay), '--repo', str(self.repo), 'provider-hook', '--client', 'claude'])
        hooks = {event: [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 3}]}]
                 for event in ('SessionStart', 'UserPromptSubmit', 'Stop', 'SessionEnd')}
        plan = {'schema': 1, 'provider': 'claude', 'repo': str(self.repo), 'hook_command': hook,
                'native_arguments': ['--settings', json.dumps({'hooks': hooks})],
                'launches_provider': False, 'changes_provider_settings': False, 'changes_permissions': False}
        self.executable(self.relay, f'print({json.dumps(plan)!r})\n')
        source = protocol.NATIVE.replace("    if value == '$cwd':", "    if value == '" + protocol.SESSION + "':\n        return sys.argv[sys.argv.index('--session-id') + 1]\n    if value == '$cwd':")
        constants = {'SPEC_PATH': str(self.specification), 'RECEIPT_PATH': str(self.receipt),
                     'REQUESTS_PATH': str(self.requests)}
        self.executable(self.provider, ''.join(f'{key} = {value!r}\n' for key, value in constants.items()) + source)
        self.specification.write_text(json.dumps(steps))
        directory = self.base / 'stream-evidence'
        message = self.base / 'message.txt'
        message.write_text('Additional scoped review input')
        identifier = str(uuid.uuid4())
        set_target = control.CallControl.set_target

        def advertise(owner, session, turn):
            set_target(owner, session, turn)
            if update:
                files = control._Files(directory)
                try:
                    with mock.patch.object(control, '_WAIT_SECONDS', 0):
                        result = control._send(files, files.call(), SimpleNamespace(
                            request_id=identifier, session=session, turn=None, message_file=str(message)))
                        self.assertEqual('pending', result['state'])
                finally:
                    files.release()

        arguments = ['claude', '--stream-progress' if progress_only else '--live-input',
                     '--repo', str(self.repo), '--relay', str(self.relay),
                     '--provider', str(self.provider), '--task-file', str(self.task),
                     '--output-dir', str(directory), '--timeout', '2', '--json']
        output, errors = io.StringIO(), io.StringIO()
        from contextlib import nullcontext
        fault = (mock.patch.object(control.CallControl, 'stop_accepting', side_effect=OSError('synthetic receipt failure'))
                 if closure_fault else nullcontext())
        with (redirect_stdout(output), redirect_stderr(errors), fault,
              mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(control.CallControl, 'set_target', advertise)):
            code = peer.peer_main(arguments)
        value = json.loads(output.getvalue())
        self.assertEqual(value, json.loads((directory/'result.json').read_text()))
        self.assertIsNotNone(value['process_exit_code'])
        self.assertEqual('not_checked', value['workflow_completion'])
        receipt = directory/'control/receipts'/(identifier+'.json')
        return code, value, json.loads(receipt.read_text()) if receipt.exists() else None

    def test_fresh_call_uses_native_stream_flags_and_exact_task(self):
        code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()}, {'emit':protocol.result()}])
        self.assertEqual(0, code, value)
        self.assertEqual('returned', value['state'])
        self.assertEqual(value['requested_session_id'], value['session_id'])
        prefix = value['follow_up_preparation']['argv_prefix']
        self.assertIn('--live-input', prefix)
        self.assertIn('--resume=' + value['session_id'], prefix)
        self.assertEqual(['--dry-run', '--json', '--task-file'], prefix[-3:])
        argv = json.loads(self.receipt.read_text())['argv']
        self.assertIn('--replay-user-messages', argv)
        self.assertIn('--verbose', argv)
        self.assertEqual('stream-json', argv[argv.index('--input-format')+1])
        self.assertEqual('stream-json', argv[argv.index('--output-format')+1])
        submitted = [json.loads(row) for row in self.requests.read_text().splitlines()]
        self.assertEqual(self.task.read_text(), submitted[0]['message']['content'])

    def test_progress_stream_returns_without_creating_a_live_input_mailbox(self):
        code, value, receipt = self.invoke(
            [{'read': 1}, {'emit': protocol.init()}, {'emit': protocol.result()}],
            progress_only=True)
        self.assertEqual(0, code, value)
        self.assertIsNone(receipt)
        self.assertNotIn('control', value)
        self.assertEqual('native_result', value['native_progress']['last_event'])
        self.assertIn('--stream-progress', value['follow_up_preparation']['argv_prefix'])
        self.assertNotIn('--live-input', value['follow_up_preparation']['argv_prefix'])
        self.assertEqual('stream-json', json.loads(self.receipt.read_text())['argv'][
            json.loads(self.receipt.read_text())['argv'].index('--output-format') + 1])

    def test_live_update_can_be_consumed_in_a_later_native_result(self):
        code, value, receipt = self.invoke([
            {'read':1}, {'emit':protocol.init()}, {'read':1}, {'emit':protocol.result()},
            {'emit':protocol.result(text=protocol.LATER, uuids=['$uuid:1'])}], update=True)
        self.assertEqual(0, code, value)
        self.assertEqual(protocol.LATER, value['result'])
        self.assertEqual('consumed', receipt['state'])
        self.assertEqual(2, len(value['native_results']))

    def test_missing_result_identity_does_not_prove_a_matching_answer(self):
        final = protocol.result()
        del final['session_id']
        code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()}, {'emit':final}])
        self.assertNotEqual(0, code, value)
        self.assertIsNone(value['result'])

    def test_missing_consumption_does_not_settle_or_cancel_an_update(self):
        code, value, receipt = self.invoke([
            {'read':1}, {'emit':protocol.init()}, {'read':1}, {'emit':protocol.result(uuids=None)},
            {'sleep':.05}, {'emit':protocol.result(text=protocol.LATER, uuids=['$uuid:0', '$uuid:1'])}], update=True)
        self.assertEqual(0, code, value)
        self.assertEqual(protocol.LATER, value['result'])
        self.assertEqual('consumed', receipt['state'])
        self.assertEqual(2, len(value['native_results']))

    def test_receipt_closure_fault_preserves_a_valid_answer(self):
        code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()}, {'emit':protocol.result()},
                                     {'sleep':.2}], closure_fault=True)
        self.assertNotEqual(0, code)
        self.assertEqual('returned', value['state'])
        self.assertEqual(protocol.ANSWER, value['result'])
        self.assertTrue(value['needs_attention'])
        self.assertIn('evidence_recording', value)
        self.assertNotIn('follow_up_preparation', value)

    def test_broken_mailbox_does_not_terminate_the_original_task(self):
        with mock.patch.object(control.CallControl, 'pending', side_effect=control.ControlError('synthetic mailbox fault')):
            code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()}, {'sleep':.05},
                                         {'emit':protocol.result()}])
        self.assertNotEqual(0, code)
        self.assertEqual('returned', value['state'])
        self.assertEqual(protocol.ANSWER, value['result'])
        self.assertEqual(0, value['process_exit_code'])
        self.assertEqual('pending', value['control_fault']['operation'])
        self.assertNotIn('unavailable_stage', value)

    def test_background_cleanup_gets_remaining_call_time_and_retains_separate_result(self):
        wait = peer._wait

        def scaled_wait(process, timeout, observer=None, feedback=None):
            # Make the old fixed shutdown deadline fail quickly, while leaving
            # the two-second overall fixture allowance intact.
            return wait(process, .05 if timeout == 5 else timeout, observer, feedback)

        with mock.patch.object(peer, '_wait', scaled_wait):
            code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()},
                {'emit':protocol.result()}, {'sleep':.2},
                {'emit':protocol.init()},
                {'emit':protocol.result(text='Background observation', uuids=None,
                    origin={'kind':'task-notification'}, total_cost_usd=.75)}])
        self.assertEqual(0, code, value)
        self.assertEqual(0, value['process_exit_code'])
        self.assertEqual(protocol.ANSWER, value['result'])
        self.assertEqual([True, False], [row['related'] for row in value['native_results']])
        self.assertEqual('task-notification', value['native_results'][-1]['origin_kind'])
        self.assertEqual(2, value['native_initialization_count'])
        self.assertEqual(.75, value['estimated_cost_usd'])
        self.assertFalse(value['needs_attention'])
        self.assertNotIn('server_cleanup', value)

    def test_protocol_fault_does_not_grant_the_background_work_allowance(self):
        wait = peer._wait
        allowances = []

        def observe_wait(process, timeout, observer=None, feedback=None):
            allowances.append(timeout)
            return wait(process, .05, observer, feedback)

        with mock.patch.object(peer, '_wait', observe_wait):
            code, value, _ = self.invoke([{'read':1}, {'emit':protocol.init()},
                {'raw':b'{"type":"result","type":"user"}\n'.hex()}, {'sleep':1}])
        self.assertEqual(5, allowances[0])
        self.assertNotEqual(0, code)
        self.assertEqual('uncertain', value['state'])
        self.assertTrue(value['needs_attention'])
        self.assertIn('server_cleanup', value)
        self.assertEqual('shutdown_timeout', value['caller_stop_reason'])
