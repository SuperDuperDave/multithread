"""Public control commands reach only their owned synthetic native turn."""

import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

import test_codex_protocol as protocol


class PeerControlIntegrationTests(unittest.TestCase):
    setUp = protocol.CodexProtocolTests.setUp
    executable = staticmethod(protocol.CodexProtocolTests.executable)
    configure = protocol.CodexProtocolTests.configure
    invoke = protocol.CodexProtocolTests.invoke
    recorded_requests = protocol.CodexProtocolTests.recorded_requests

    def exercise(self, mode, extra_events=()):
        handler = '''
    elif method == 'turn/steer':
        assert message['params']['threadId'] == THREAD_ID
        assert message['params']['expectedTurnId'] == TURN_ID
        assert message['params']['input'] == [{'type': 'text', 'text': 'Additional scoped input'}]
        mode = spec['steering_mode']
        if mode == 'terminal_before_ack':
            for event in spec['terminal_events']: emit(event)
        if mode == 'reject':
            emit({'id': message['id'], 'error': {'code': -32000, 'message': 'turn ended'}})
        elif mode != 'lost_ack':
            send_result(message, {'turnId': 'unrelated-turn' if mode == 'wrong_turn' else TURN_ID})
        if mode != 'terminal_before_ack':
            for event in spec['terminal_events']: emit(event)
        raise SystemExit(0)
'''
        source = self.provider.read_text().replace("    elif method == 'turn/interrupt':", handler + "    elif method == 'turn/interrupt':")
        self.executable(self.provider, source.split("\n", 1)[1])
        self.configure(events=[], exit_after_events=False, steering_mode=mode,
                       terminal_events=[protocol.item(), protocol.completed(), *extra_events])
        directory = self.base / "evidence-1"
        message = self.base / "update.txt"
        message.write_text("Additional scoped input")
        reply, failures = [], []

        def publish():
            try:
                deadline = time.monotonic() + 5
                target_path = directory / "control/target.json"
                while time.monotonic() < deadline:
                    if target_path.exists():
                        target = json.loads(target_path.read_text())
                        if target['turn_id'] is not None:
                            break
                    time.sleep(0.01)
                else:
                    raise AssertionError("Synthetic peer never advertised its target")
                result = subprocess.run([
                    sys.executable, "-I", "-S", "-B", str(protocol.ROOT / "examples/call_peer.py"),
                    "control", "send", "--call-dir", str(directory),
                    "--session", target['session_id'], "--turn", target['turn_id'],
                    "--message-file", str(message), "--json"],
                    capture_output=True, text=True, timeout=8)
                reply.append((result.returncode, json.loads(result.stdout)))
            except BaseException as exc:
                failures.append(exc)

        sender = threading.Thread(target=publish)
        sender.start()
        try:
            result = self.invoke("--timeout", "5")
        finally:
            sender.join(timeout=10)
        self.assertFalse(sender.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(1, len(reply))
        self.assertEqual(1, sum(row.get('method') == 'turn/steer' for row in self.recorded_requests()))
        code, receipt = reply[0]
        final = json.loads((directory / "control/receipts" / (receipt['request_id'] + '.json')).read_text())
        self.assertEqual(receipt['state'], final['state'])
        self.assertTrue(json.loads((directory / "control/target.json").read_text())['closed'])
        self.assertEqual("not_checked", result[1]['workflow_completion'])
        return result, code, receipt

    def test_exact_acceptance_does_not_assert_consumption(self):
        (code, result, _), sent, receipt = self.exercise('accept')
        self.assertEqual(0, code, result)
        self.assertEqual(0, sent, receipt)
        self.assertEqual('accepted', receipt['state'])
        self.assertNotIn('consumed', receipt)
        self.assertEqual(protocol.ANSWER, result['result'])

    def test_late_ack_survives_terminal_notification(self):
        (code, result, _), sent, receipt = self.exercise('terminal_before_ack')
        self.assertEqual(0, code, result)
        self.assertEqual(0, sent, receipt)
        self.assertEqual('accepted', receipt['state'])

    def test_lost_ack_preserves_answer_and_uncertain_input(self):
        (code, result, _), sent, receipt = self.exercise('lost_ack')
        self.assertEqual(0, code, result)
        self.assertTrue(result['needs_attention'])
        self.assertEqual(protocol.ANSWER, result['result'])
        self.assertEqual(1, sent, receipt)
        self.assertEqual('uncertain', receipt['state'])

    def test_lost_ack_does_not_preserve_answer_after_protocol_fault(self):
        (code, result, _), sent, receipt = self.exercise('lost_ack', [{"broken": "after completion"}])
        self.assertNotEqual(0, code, result)
        self.assertEqual('uncertain', result['state'])
        self.assertIsNone(result['result'])
        self.assertEqual(1, sent, receipt)
        self.assertEqual('uncertain', receipt['state'])

    def test_wrong_native_target_cannot_prove_delivery(self):
        (_, result, _), sent, receipt = self.exercise('wrong_turn')
        self.assertTrue(result['needs_attention'])
        self.assertEqual(1, sent, receipt)
        self.assertEqual('uncertain', receipt['state'])

    def test_native_refusal_is_distinct_from_successful_task_return(self):
        (code, result, _), sent, receipt = self.exercise('reject')
        self.assertEqual(0, code, result)
        self.assertEqual(protocol.ANSWER, result['result'])
        self.assertEqual(1, sent, receipt)
        self.assertEqual('rejected', receipt['state'])
