"""Synthetic byte sinks and owned process groups for observation-loss invariants."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import codex_peer, native_io, provider
from relay_runtime.peer_control import ObservedControl
import test_codex_protocol as protocol


class ObservationLossTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def driver(self, process=None):
        process = process or SimpleNamespace(pid=123456, returncode=None)
        envelope = {}
        driver = codex_peer._Driver(process, b"synthetic task", "/fixture", None, envelope, 2, None)
        driver.session, driver.turn = protocol.THREAD, protocol.TURN
        driver.message(json.dumps(protocol.item()).encode())
        driver.message(json.dumps(protocol.completed()).encode())
        self.assertEqual("returned", envelope["state"])
        return driver, envelope

    def observation(self, driver, envelope):
        read_fd, write_fd = os.pipe()
        process = SimpleNamespace(stdout=os.fdopen(read_fd, "rb"), stdin=None)
        observer = native_io.Observation(process, self.root, envelope)
        observer.driver = driver
        observer.retain = driver.retain
        self.addCleanup(observer.output.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(os.close, write_fd)
        return observer

    def test_revocation_precedes_persistent_deferred_sink_failure(self):
        for operation in ("write", "flush"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                driver, envelope = self.driver()
                saved_root, self.root = self.root, Path(folder)
                observer = self.observation(driver, envelope)
                self.root = saved_root
                observer.buffer.extend(b"synthetic unverified suffix\n")
                observer.deferred = True
                sink = mock.Mock(wraps=observer.output)
                getattr(sink, operation).side_effect = OSError("synthetic persistent sink failure")
                observer.output = sink
                with mock.patch.object(codex_peer.os, "killpg") as kill:
                    with self.assertRaises(OSError):
                        observer.fault("synthetic observation loss")
                    self.assertEqual("uncertain", envelope["state"])
                    self.assertIsNone(envelope["result"])
                    self.assertFalse(observer.interpret)
                    self.assertEqual(bytearray(), observer.buffer)
                    kill.assert_called_once_with(driver.process.pid, signal.SIGKILL)

    def test_cleanup_fallback_revokes_returned_answer_despite_sink_failure(self):
        driver, envelope = self.driver()
        observer = self.observation(driver, envelope)
        observer.buffer.extend(b"synthetic deferred suffix\n")
        observer.deferred = True
        sink = mock.Mock(wraps=observer.output)
        sink.write.side_effect = OSError("synthetic persistent sink failure")
        observer.output = sink
        with (mock.patch.object(observer, "read", side_effect=OSError("synthetic read loss")),
              mock.patch.object(codex_peer.os, "killpg") as kill):
            self.assertFalse(provider._drain(observer))
            self.assertEqual("uncertain", envelope["state"])
            self.assertIsNone(envelope["result"])
            self.assertFalse(observer.interpret)
            self.assertIn("evidence_recording", envelope)
            self.assertIn(mock.call(driver.process.pid, signal.SIGKILL), kill.call_args_list)

    def test_observation_loss_cannot_be_repaired_by_queued_completion(self):
        driver, envelope = self.driver()
        with mock.patch.object(codex_peer.os, "killpg"):
            driver.problem("synthetic active I/O observation loss")
        driver.message(json.dumps(protocol.item("Unverified queued answer")).encode())
        driver.finish()
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])
        self.assertTrue(envelope["needs_attention"])

    def test_checked_interruption_still_accepts_shutdown_answer(self):
        driver, envelope = self.driver()
        with mock.patch.object(codex_peer.os, "killpg") as kill:
            driver.problem("synthetic checked interruption", observation_lost=False)
            driver.message(json.dumps(protocol.item("Checked shutdown answer")).encode())
        self.assertEqual("returned", envelope["state"])
        self.assertTrue(envelope["result"].endswith("Checked shutdown answer"))
        self.assertTrue(envelope["needs_attention"])
        kill.assert_not_called()

    @staticmethod
    def alive(pid):
        try:
            # Process state only; never argv/environment/account contents.
            return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def test_receipt_fault_preserves_observation_until_later_native_loss(self):
        driver, envelope = self.driver()
        owner = mock.Mock()
        owner.stop_accepting.side_effect = OSError("synthetic receipt persistence failure")
        driver.control = ObservedControl(owner, envelope)
        observer = self.observation(driver, envelope)
        driver.control.stop_accepting("synthetic completed turn")
        self.assertEqual("returned", envelope["state"])
        self.assertTrue(observer.interpret)
        self.assertIn("control_fault", envelope)
        with mock.patch.object(codex_peer.os, "killpg"):
            observer.fault("synthetic later native boundary loss")
        driver.finish()
        self.assertFalse(observer.interpret)
        self.assertEqual("uncertain", envelope["state"])
        self.assertIsNone(envelope["result"])

    def test_snapshot_failure_preserves_valid_answer_and_native_observation(self):
        driver, envelope = self.driver()
        observer = self.observation(driver, envelope)
        with mock.patch.object(observer, "snapshot", side_effect=OSError("synthetic receipt failure")):
            self.assertFalse(provider._drain(observer))
        self.assertEqual("returned", envelope["state"])
        self.assertTrue(observer.interpret)
        self.assertIn("evidence_recording", envelope)

    def test_exited_unreaped_leader_fault_kills_owned_descendant_without_shutdown_grace(self):
        for fault in ("protocol", "overflow", "boundary"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                pid_file, trigger = root / "child.pid", root / "trigger"
                raw = (b"not-json\n" if fault == "protocol" else
                       b'{"bad":"' + b"x" * 131072 + b'"}\n' if fault == "overflow" else
                       json.dumps({"method": "mcpServer/startupStatus/updated", "params": {
                           "name": "unadmitted-synthetic", "status": "starting"}}).encode() + b"\n")
                script = (
                    "import os,sys,time\nfrom pathlib import Path\n"
                    "child=os.fork()\n"
                    "if child: Path(sys.argv[1]).write_text(str(child)); os._exit(0)\n"
                    "while not Path(sys.argv[2]).exists(): time.sleep(0.01)\n" +
                    ("body=b'{\"bad\":\"' + b'x'*131072 + b'\"}\\n'\n" if fault == "overflow" else f"body={raw!r}\n") +
                    "while body:\n count=os.write(1,body); body=body[count:]\n"
                    "while True: time.sleep(10)\n")
                process = subprocess.Popen([sys.executable, "-c", script, str(pid_file), str(trigger)],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, start_new_session=True)
                def cleanup():
                    provider._retire_owned_group(process)
                    process.stdin.close()
                    process.stdout.close()
                try:
                    self.assertEqual(0, provider._wait(process, 2))
                    child = int(pid_file.read_text())
                    self.assertTrue(self.alive(child))
                    driver, envelope = self.driver(process)
                    observer = native_io.Observation(process, root, envelope)
                    observer.driver, observer.retain = driver, driver.retain
                    self.addCleanup(observer.output.close)
                    trigger.touch()
                    deadline = time.monotonic() + 1
                    with mock.patch.object(native_io, "MAX_OUTPUT", 65536):
                        while observer.interpret and time.monotonic() < deadline:
                            provider._drain(observer)
                            time.sleep(0.005)
                    self.assertFalse(observer.interpret)
                    self.assertEqual("uncertain", envelope["state"])
                    self.assertIsNone(envelope["result"])
                    deadline = time.monotonic() + 1
                    while self.alive(child) and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertFalse(self.alive(child), "owned descendant survived observation loss after leader reap")
                finally:
                    cleanup()



    def test_known_reaped_or_lost_group_revokes_answer_without_signalling(self):
        for state in ('reaped', 'retired', 'lost'):
            with self.subTest(ownership=state):
                process = SimpleNamespace(pid=123456, returncode=0 if state == 'reaped' else None,
                                          _owned_group_retired=state == 'retired',
                                          _owned_child_lost=state == 'lost')
                driver, envelope = self.driver(process)
                with mock.patch.object(codex_peer.os, 'killpg') as kill:
                    driver.problem('synthetic post-ownership observation fault')
                self.assertEqual('uncertain', envelope['state'])
                self.assertIsNone(envelope['result'])
                kill.assert_not_called()

class ActiveObservationLossTests(unittest.TestCase):
    setUp = protocol.CodexProtocolTests.setUp
    executable = staticmethod(protocol.CodexProtocolTests.executable)
    configure = protocol.CodexProtocolTests.configure
    invoke = protocol.CodexProtocolTests.invoke

    def test_active_oserror_freezes_observation_before_cleanup(self):
        original = native_io.Observation.read
        failed_observers = []
        def read(observer):
            result = original(observer)
            if observer.driver.terminal is not None and not failed_observers:
                failed_observers.append(observer)
                raise OSError("synthetic active read failure after terminal bytes")
            return result
        with mock.patch.object(native_io.Observation, "read", new=read):
            code, result, _ = self.invoke()
        self.assertEqual(1, len(failed_observers))
        self.assertNotEqual(0, code)
        self.assertEqual("uncertain", result["state"])
        self.assertIsNone(result["result"])
        self.assertFalse(failed_observers[0].interpret)
