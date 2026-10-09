"""Native terminal observations survive bounded owned-process cleanup."""

import hashlib
import json
import signal
import unittest
from unittest import mock

import test_codex_protocol as protocol
from relay_runtime import codex_peer, provider as peer
from relay_runtime.peer_control import CallControl


class CodexCleanupTests(unittest.TestCase):
    setUp = protocol.CodexProtocolTests.setUp
    executable = staticmethod(protocol.CodexProtocolTests.executable)
    configure = protocol.CodexProtocolTests.configure
    invoke = protocol.CodexProtocolTests.invoke

    def assert_observation(self, result, directory):
        raw = (directory / "stdout.json").read_bytes()
        self.assertEqual(len(raw), result["stdout_observation"]["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["stdout_observation"]["sha256"])
        self.assertLessEqual(len(raw), 16 * 1024 * 1024 + 1)
        self.assertEqual(result, json.loads((directory / "result.json").read_text()))
        return raw

    def termination_output(self, events=(), extra=""):
        handler = (
            "import signal\n"
            "def terminate(number, frame):\n"
            f"    values = {events!r}\n"
            "    for value in values: print(json.dumps(value), flush=True)\n"
            + ("    " + extra.replace("\n", "\n    ") + "\n" if extra else "")
            + "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, terminate)\n"
        )
        source = self.provider.read_text().replace("while True:\n    message = receive()", handler + "\nwhile True:\n    message = receive()")
        self.executable(self.provider, source.split("\n", 1)[1])

    @staticmethod
    def quick_wait(process, timeout, observer=None, feedback=None):
        return CodexCleanupTests.original_wait(process, min(timeout, 0.2), observer, feedback)

    original_wait = staticmethod(peer._wait)

    def test_completed_turn_survives_stdout_held_open_until_owned_cleanup(self):
        self.configure(sleep=True)
        with mock.patch.object(peer, "_wait", side_effect=self.quick_wait):
            code, result, directory = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertEqual(protocol.ANSWER, result["result"])
        self.assertTrue(result["needs_attention"])
        self.assertIn("server_cleanup", result)
        self.assertEqual("shutdown_timeout", result["caller_stop_reason"])
        self.assert_observation(result, directory)

    def test_mailbox_closure_fault_does_not_erase_answer_or_prevent_owned_cleanup(self):
        self.configure(sleep=True)
        with (mock.patch.object(CallControl, "stop_accepting", side_effect=OSError("synthetic receipt failure")),
              mock.patch.object(peer, "_wait", side_effect=self.quick_wait)):
            code, result, directory = self.invoke()
        self.assertNotEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertEqual(protocol.ANSWER, result["result"])
        self.assertIsNotNone(result['process_exit_code'])
        self.assertTrue(result['needs_attention'])
        self.assertIn('evidence_recording', result)
        self.assert_observation(result, directory)

    def test_exited_provider_with_unavailable_stdout_eof_still_returns_its_answer(self):
        read = codex_peer.Observation.read

        def held_open(observation):
            # Model a descriptor held outside the owned process group without
            # creating an escaped process in the test host.
            result = read(observation)
            if observation.eof:
                observation.eof = False
            return result

        with (mock.patch.object(codex_peer.Observation, 'read', held_open),
              mock.patch.object(peer, '_wait', side_effect=self.quick_wait)):
            code, result, directory = self.invoke()
        self.assertEqual(0, code, result)
        self.assertEqual('returned', result['state'])
        self.assertEqual(protocol.ANSWER, result['result'])
        self.assertTrue(result['needs_attention'])
        self.assertEqual('incomplete after owned cleanup', result['stdout_completion'])
        self.assert_observation(result, directory)

    def test_unread_cancellation_bytes_and_sigterm_text_are_retained(self):
        queued = {"method": "item/agentMessage/delta", "params": {
            "threadId": protocol.THREAD, "turnId": protocol.TURN,
            "itemId": "partial", "delta": "queued before cancellation; "}}
        shutdown = {"method": "item/agentMessage/delta", "params": {
            "threadId": protocol.THREAD, "turnId": protocol.TURN,
            "itemId": "partial", "delta": "SIGTERM shutdown text"}}
        self.configure(events=[queued], sleep=True)
        self.termination_output([shutdown])
        ready = self.base / 'turn-observed'
        source = self.provider.read_text().replace("for event in spec.get('events', []):",
            f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n        for event in spec.get('events', []):")
        self.executable(self.provider, source.split("\n", 1)[1])
        original_read = codex_peer.Observation.read
        interrupted = False

        def interrupt_before_read(observation):
            nonlocal interrupted
            if observation.driver is not None and observation.driver.turn and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            result = original_read(observation)
            if observation.driver is not None and observation.driver.turn:
                ready.touch()
            return result

        with mock.patch.object(codex_peer.Observation, "read", interrupt_before_read):
            code, result, directory = self.invoke()
        self.assertTrue(interrupted)
        self.assertEqual(128 + signal.SIGINT, code)
        self.assertEqual("uncertain", result["state"])
        self.assertEqual("queued before cancellation; SIGTERM shutdown text", result["partial_result"])
        raw = self.assert_observation(result, directory)
        self.assertIn(b"queued before cancellation", raw)
        self.assertIn(b"SIGTERM shutdown text", raw)

    def test_valid_terminal_emitted_during_sigterm_keeps_answer_with_attention(self):
        self.configure(events=[], sleep=True)
        self.termination_output([protocol.item("Final answer during termination"), protocol.completed()])
        code, result, directory = self.invoke("--timeout", "1")
        self.assertEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertEqual("Final answer during termination", result["result"])
        self.assertTrue(result["needs_attention"])
        self.assert_observation(result, directory)

    def test_cleanup_protocol_fault_stops_before_unbounded_termination_output(self):
        self.configure(events=[], sleep=True)
        # The first invalid record disables boundary interpretation, so the owned group
        # is stopped rather than being allowed to continue filling the raw capture.
        self.termination_output(extra="for _ in range(4100):\n    os.write(sys.stdout.fileno(), b'x' * 4095 + b'\\n')")
        code, result, directory = self.invoke("--timeout", "1")
        self.assertNotEqual(0, code)
        self.assertEqual("uncertain", result["state"])
        self.assertFalse(result["stdout_observation"]["truncated"])
        self.assertLess(len(self.assert_observation(result, directory)), 16 * 1024 * 1024)
        self.assertIsNone(result["result"])
        self.assertIsNotNone(result["process_exit_code"])

    def test_active_protocol_fault_cannot_be_repaired_by_shutdown_final(self):
        self.configure(events=[{"broken": "active protocol"}], sleep=True)
        self.termination_output([protocol.item("Unverified later answer"), protocol.completed()])
        with mock.patch.object(peer, "_wait", side_effect=self.quick_wait):
            code, result, directory = self.invoke()
        self.assertNotEqual(0, code)
        self.assertEqual("uncertain", result["state"])
        self.assertIsNone(result["result"])
        # An active protocol fault sends SIGKILL, so the provider cannot use its
        # SIGTERM handler to write a later answer after boundary observation failed.
        self.assertNotIn(b"Unverified later answer", self.assert_observation(result, directory))

    def test_buffered_terminal_stops_acceptance_without_advertising_active_target(self):
        class Control:
            def __init__(self):
                self.targets = []
                self.stopped = False

            def set_target(self, *target):
                self.targets.append(target)

            def stop_accepting(self, reason):
                self.stopped = True

        control = Control()
        driver = codex_peer._Driver(mock.Mock(), b"work", "/fixture", None, {}, 1, control)
        driver.session = protocol.THREAD
        driver.turn_requested = True
        driver.pending[1] = ("turn/start", None)
        driver.buffered = [protocol.item(), protocol.completed()]
        driver.response({"id": 1, "result": {"turn": {
            "id": protocol.TURN, "items": [], "status": "inProgress"}}})
        self.assertTrue(control.stopped)
        self.assertEqual([], control.targets)
        self.assertEqual("returned", driver.envelope["state"])
