"""Transport capture with disposable byte pipes, synthetic executables and faulted files."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import native_io, provider
import test_codex_protocol as protocol_fixture

ANSWER = protocol_fixture.ANSWER
item = protocol_fixture.item
completed = protocol_fixture.completed


class CaptureFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def capture(self, context=None):
        return native_io.TransportCapture(self.root, context or {"synthetic_only": True})

    @staticmethod
    def complete(capture):
        capture.bytes("stdin", b'{"id":1}\n')
        capture.bytes("stdout", b'{"id":1,"result":{}}\n')
        capture.event("stdin_closed")
        capture.event("stdout_eof")

    def assert_incomplete(self, report):
        self.assertEqual("incomplete", report["status"])
        for name in ("capture_authenticated", "review_accepted", "release_approved"):
            self.assertIs(False, report[name])
        self.assertNotIn("inventory_sha256", report)

    def test_exact_inventory_and_idempotent_finish(self):
        capture = self.capture()
        self.complete(capture)
        report = capture.finish(0, clean=True)
        self.assertEqual("complete", report["status"])
        self.assertIs(False, report["capture_authenticated"])
        self.assertEqual(report, capture.finish(0, clean=True))
        inventory = json.loads((self.root / report["path"]).read_bytes())
        self.assertEqual("inventory_only", inventory["status"])
        self.assertEqual(4, len(inventory["members"]))
        for member in inventory["members"]:
            path = self.root / "transport" / member["path"]
            body = path.read_bytes()
            self.assertEqual((len(body), hashlib.sha256(body).hexdigest()),
                             (member["bytes"], member["sha256"]))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE((self.root / "transport").stat().st_mode))

    def test_missing_eof_exit_pending_or_forced_end_cannot_seal(self):
        for missing, exit_code, clean in (("stdout_eof", 0, True), ("stdin_closed", 0, True),
                                          (None, None, True), (None, 1, True),
                                          (None, False, True), (None, 0, False)):
            with self.subTest(missing=missing, exit_code=exit_code, clean=clean), tempfile.TemporaryDirectory() as folder:
                c = native_io.TransportCapture(Path(folder), {})
                c.bytes("stdin", b"x")
                c.bytes("stdout", b"y")
                for kind in ("stdin_closed", "stdout_eof"):
                    if kind != missing:
                        c.event(kind)
                self.assert_incomplete(c.finish(exit_code, clean=clean))

    def test_bounds_and_unknown_events_remain_incomplete(self):
        for fault in ("stream", "chunk", "journal", "duplicate", "unknown", "explicit"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                c = native_io.TransportCapture(Path(folder), {})
                if fault == "stream":
                    c.MAX_BYTES = 1
                    c.bytes("stdin", b"xx")
                elif fault == "chunk":
                    c.bytes("stdin", b"x" * (c.MAX_CHUNK + 1))
                elif fault == "journal":
                    c.MAX_EVENTS = 0
                    c.bytes("stdin", b"x")
                elif fault == "duplicate":
                    c.event("stdout_eof")
                    c.event("stdout_eof")
                elif fault == "unknown":
                    c.event("process_exit_is_not_eof")
                else:
                    c.fault()
                self.complete(c)
                self.assert_incomplete(c.finish(0, clean=True))

    def test_file_replacement_added_file_and_same_length_tampering_refuse(self):
        for action in ("added", "changed", "mode", "link"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                c = native_io.TransportCapture(Path(folder), {})
                self.complete(c)
                directory = Path(folder) / "transport"
                path = directory / "stdin.bin"
                if action == "added":
                    (directory / "foreign").write_bytes(b"x")
                elif action == "changed":
                    path.write_bytes(b"x" * path.stat().st_size)
                elif action == "mode":
                    path.chmod(0o644)
                else:
                    os.link(path, Path(folder) / "alias")
                self.assert_incomplete(c.finish(0, clean=True))

    def test_private_directory_and_exclusive_creation(self):
        self.root.chmod(0o755)
        c = self.capture()
        self.assert_incomplete(c.finish(0, clean=True))
        self.assertFalse((self.root / "transport").exists())
        self.root.chmod(0o700)
        target = self.root / "target"
        target.mkdir()
        (self.root / "transport").symlink_to(target, target_is_directory=True)
        c = self.capture()
        self.assert_incomplete(c.finish(0, clean=True))
        self.assertEqual([], list(target.iterdir()))

    def test_each_initial_recording_operation_failure_refuses_without_throwing(self):
        for operation in ("open", "mkdir", "write", "fsync"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                with mock.patch.object(native_io.os, operation, side_effect=OSError("synthetic fault")):
                    c = native_io.TransportCapture(Path(folder), {})
                self.complete(c)
                self.assert_incomplete(c.finish(0, clean=True))

    def test_each_seal_operation_failure_is_not_complete(self):
        for operation in ("write", "fsync", "pread", "listdir", "fstat", "link", "unlink"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as folder:
                c = native_io.TransportCapture(Path(folder), {})
                self.complete(c)
                with mock.patch.object(native_io.os, operation, side_effect=OSError("synthetic fault")):
                    report = c.finish(0, clean=True)
                self.assert_incomplete(report)
                if (Path(folder) / "transport/inventory.json").exists():
                    self.assertEqual("inventory_only", json.loads(
                        (Path(folder) / "transport/inventory.json").read_text())["status"])

    def test_partial_artifact_file_writes_are_not_native_input_retries(self):
        original = os.write
        def partial(fd, body):
            return original(fd, body[:3])
        with mock.patch.object(native_io.os, "write", side_effect=partial):
            c = self.capture()
            self.complete(c)
            report = c.finish(0, clean=True)
        self.assertEqual("complete", report["status"])
        self.assertEqual(b'{"id":1}\n', (self.root / "transport/stdin.bin").read_bytes())

    def test_close_failure_cannot_leave_complete_receipt(self):
        c = self.capture()
        self.complete(c)
        original = os.close
        def close(fd):
            original(fd)
            raise OSError("synthetic close fault")
        with mock.patch.object(native_io.os, "close", side_effect=close):
            report = c.finish(0, clean=True)
        self.assert_incomplete(report)

    def test_final_directory_sync_failure_leaves_inventory_insufficient(self):
        c = self.capture()
        self.complete(c)
        original = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic final directory sync fault")
            return original(fd)
        with mock.patch.object(native_io.os, "fsync", side_effect=sync):
            self.assert_incomplete(c.finish(0, clean=True))
        inventory = json.loads((self.root / "transport/inventory.json").read_text())
        self.assertEqual("inventory_only", inventory["status"])
        self.assertIs(False, inventory["review_accepted"])

    def test_later_fault_revokes_transport_completion_not_raw_inventory(self):
        c = self.capture()
        self.complete(c)
        self.assertEqual("complete", c.finish(0, clean=True)["status"])
        c.fault()
        self.assert_incomplete(c.finish(0, clean=True))

    def test_raw_read_precedes_retained_form_and_eof_is_empty_read(self):
        c = self.capture()
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        process = SimpleNamespace(stdout=stream, stdin=None)
        observation = native_io.Observation(process, self.root, {})
        observation.capture = c
        driver = SimpleNamespace(session=None, message=lambda line: None,
                                 preserve=lambda **kwargs: None, observation_only=False)
        observation.driver = driver
        observation.retain = lambda line: b'{"reference":true}'
        raw = b'{"synthetic_payload":"unchanged"}\n'
        os.write(write_fd, raw)
        observation.read()
        self.assertFalse(c.eof)
        self.assertEqual(raw, (self.root / "transport/stdout.bin").read_bytes())
        self.assertEqual(b'{"reference":true}\n', (self.root / "stdout.json").read_bytes())
        os.close(write_fd)
        observation.read()
        self.assertTrue(c.eof)
        c.bytes("stdin", b"x")
        c.event("stdin_closed")
        observation.close()
        self.assertEqual("complete", c.finish(0, clean=True)["status"])

    def test_read_fault_and_incomplete_protocol_record_refuse(self):
        for kind in ("read", "protocol"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                read_fd, write_fd = os.pipe()
                process = SimpleNamespace(stdout=os.fdopen(read_fd, "rb"), stdin=None)
                observation = native_io.Observation(process, Path(folder), {})
                c = native_io.TransportCapture(Path(folder), {})
                observation.capture = c
                if kind == "read":
                    with mock.patch.object(native_io.os, "read", side_effect=OSError("synthetic read fault")):
                        with self.assertRaises(OSError):
                            observation.read()
                else:
                    observation.fault("synthetic protocol fault")
                os.close(write_fd)
                observation.close()
                self.assert_incomplete(c.finish(0, clean=True))

    def test_entry_hash_is_observation_only(self):
        path = self.root / "synthetic-entry"
        path.write_bytes(b"synthetic executable bytes")
        observed = native_io.capture_entry(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), observed["sha256"])
        self.assertIs(False, observed["execution_identity_verified"])
        self.assertEqual("unavailable", native_io.capture_entry(self.root)["status"])


class CaptureTransportTests(unittest.TestCase):
    # Reuse only the existing disposable fixture, never its inherited tests.
    setUp = protocol_fixture.CodexProtocolTests.setUp
    executable = staticmethod(protocol_fixture.CodexProtocolTests.executable)
    configure = protocol_fixture.CodexProtocolTests.configure
    invoke = protocol_fixture.CodexProtocolTests.invoke
    recorded_requests = protocol_fixture.CodexProtocolTests.recorded_requests

    def test_default_produces_no_capture_and_opt_in_records_exact_wire(self):
        code, answer, directory = self.invoke()
        self.assertEqual((0, ANSWER), (code, answer["result"]))
        self.assertNotIn("transport_capture", answer)
        self.assertFalse((directory / "transport").exists())
        code, answer, directory = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, answer["result"]))
        self.assertEqual("complete", answer["transport_capture"]["status"])
        raw = (directory / "transport/stdout.bin").read_bytes()
        self.assertEqual((directory / "stdout.json").read_bytes(), raw)
        sent = [json.loads(line) for line in (directory / "transport/stdin.bin").read_bytes().splitlines()]
        self.assertEqual(self.recorded_requests(), sent)
        context = json.loads((directory / "transport/context.json").read_bytes())
        self.assertEqual("e" * 64, context["packet_manifest_sha256"])
        self.assertEqual(answer["producer_runtime"], context["producer_runtime"])
        self.assertEqual(answer["control"]["call_id"], context["call_id"])
        self.assertIsNone(context["source_oid"])
        self.assertIs(False, answer["transport_capture"]["review_accepted"])

    def test_partial_successful_native_writes_and_eagain_preserve_wire(self):
        original = os.write
        calls = 0
        def partial(fd, body):
            nonlocal calls
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                calls += 1
                if calls % 3 == 0:
                    raise BlockingIOError()
                return original(fd, body[:31])
            return original(fd, body)
        with mock.patch.object(native_io.os, "write", side_effect=partial):
            code, result, directory = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("complete", result["transport_capture"]["status"])
        rows = [json.loads(line) for line in (directory / "transport/stdin.bin").read_bytes().splitlines()]
        self.assertEqual(self.recorded_requests(), rows)
        journal = [json.loads(line) for line in (directory / "transport/journal.jsonl").read_bytes().splitlines()]
        self.assertGreater(sum(row["kind"] == "stdin" for row in journal), len(rows))

    def test_recording_fault_after_native_write_preserves_answer_and_never_resends(self):
        original = native_io.TransportCapture._append
        def fail(capture, kind, data):
            if kind == "stdin":
                raise OSError("synthetic recording fault after native write")
            return original(capture, kind, data)
        with mock.patch.object(native_io.TransportCapture, "_append", new=fail):
            code, result, directory = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("incomplete", result["transport_capture"]["status"])
        requests = self.recorded_requests()
        ids = [row["id"] for row in requests if "id" in row]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual("call\n", self.calls.read_text())
        self.assertFalse((directory / "transport/inventory.json").exists())

    def test_protocol_fault_or_missing_ack_does_not_qualify(self):
        for spec in ({"events": [item(), completed(), []]},
                     {"wrong_response_id": "turn/start"}):
            self.configure(**spec)
            code, result, _ = self.invoke("--capture-transport", "e" * 64)
            self.assertNotEqual(0, code)
            self.assertEqual("incomplete", result["transport_capture"]["status"])

    def test_cli_refuses_invalid_pin_or_other_provider_before_execution(self):
        for argv in (["codex", "--task-file", "-", "--capture-transport", "bad"],
                     ["claude", "--task-file", "-", "--capture-transport", "e" * 64]):
            with self.subTest(argv=argv), mock.patch.object(provider, "_run_peer") as run:
                with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    provider.peer_main(argv)
                run.assert_not_called()

    def test_cleanup_suffix_is_captured_and_capture_cap_preserves_answer(self):
        suffix = {"method": "thread/tokenUsage/updated", "params": {
            "threadId": protocol_fixture.THREAD, "turnId": protocol_fixture.TURN,
            "tokenUsage": {"last": protocol_fixture.COUNTS, "total": protocol_fixture.COUNTS}}}
        self.configure(later_events=[suffix], exit_after_events=False)
        code, result, directory = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("complete", result["transport_capture"]["status"])
        raw = [json.loads(line) for line in (directory / "transport/stdout.bin").read_bytes().splitlines()]
        self.assertIn(suffix, raw)
        self.configure()
        with mock.patch.object(native_io.TransportCapture, "MAX_BYTES", 1):
            code, result, _ = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("incomplete", result["transport_capture"]["status"])

    def test_nonzero_exit_and_missing_pipe_eof_keep_answer_but_refuse_capture(self):
        self.configure(exit=7)
        code, result, _ = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("incomplete", result["transport_capture"]["status"])
        self.configure()
        original_read = native_io.Observation.read
        original_sysread = os.read
        original_wait = provider._wait
        def held_open(observation):
            def read(fd, size):
                data = original_sysread(fd, size)
                if not data:
                    raise BlockingIOError()
                return data
            with mock.patch.object(native_io.os, "read", side_effect=read):
                return original_read(observation)
        def quick_wait(process, timeout, observer=None, feedback=None):
            return original_wait(process, min(timeout, 0.2), observer, feedback)
        # Model an unavailable pipe EOF without escaping any owned process.
        with (mock.patch.object(native_io.Observation, "read", new=held_open),
              mock.patch.object(provider, "_wait", side_effect=quick_wait)):
            code, result, _ = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("incomplete", result["transport_capture"]["status"])

    def test_entry_changed_after_call_refuses_capture_without_withdrawing_answer(self):
        original = native_io.capture_entry
        calls = 0
        def entry(path):
            nonlocal calls
            calls += 1
            result = original(path)
            if calls == 4:
                result["sha256"] = "f" * 64
            return result
        with mock.patch.object(native_io, "capture_entry", side_effect=entry):
            code, result, _ = self.invoke("--capture-transport", "e" * 64)
        self.assertEqual((0, ANSWER), (code, result["result"]))
        self.assertEqual("incomplete", result["transport_capture"]["status"])


if __name__ == "__main__":
    unittest.main()
