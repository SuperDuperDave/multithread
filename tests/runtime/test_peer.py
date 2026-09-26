"""Native peer boundaries using disposable executables; no real provider access."""

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import cli, provider as peer


class PeerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-peer-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.relay, self.provider = self.base / "multithread", self.base / "provider entry"
        self.task = self.base / "task.txt"
        self.task.write_text("Review the scoped change.\n", encoding="utf-8")
        self.receipt, self.calls = self.base / "receipt.json", self.base / "calls.txt"
        self.response = self.base / "response.json"
        self.response.write_text("{}")
        self.environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
                            "HOME": str(self.base / "home"),
                            "CLAUDE_CONFIG_DIR": str(self.base / "normal-profile"),
                            "RELAY_TEST_NATIVE_ENVIRONMENT": "ordinary inherited fixture"}
        hook = shlex.join([str(self.relay), "--repo", str(self.repo), "provider-hook", "--client", "claude"])
        hooks = {event: [{"hooks": [{"type": "command", "command": hook, "timeout": 3}]}]
                 for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}
        self.native_arguments = ["--settings", json.dumps({"hooks": hooks})]
        plan = {"schema": 1, "provider": "claude", "repo": str(self.repo), "hook_command": hook,
                "native_arguments": self.native_arguments, "launches_provider": False,
                "changes_provider_settings": False, "changes_permissions": False}
        self.executable(self.relay, "import json, sys\nfrom pathlib import Path\n"
                        f"Path({str(self.base / 'relay-argv.json')!r}).write_text(json.dumps(sys.argv))\n"
                        f"print({json.dumps(plan)!r})\n")
        self.executable(self.provider, "import json, os, sys, time\nfrom pathlib import Path\n"
                        f"with open({str(self.calls)!r}, 'a') as stream: stream.write('call\\n')\n"
                        f"spec = json.loads(Path({str(self.response)!r}).read_text())\n"
                        "time.sleep(spec.get('read_delay', 0))\n"
                        "if spec.get('close_stdin'): os.close(0); task = b''\n"
                        "elif 'read_bytes' in spec: task = os.read(0, spec['read_bytes']); os.close(0)\n"
                        "else: task = sys.stdin.buffer.read()\n"
                        f"Path({str(self.base / 'received-task.txt')!r}).write_bytes(task)\n"
                        f"receipt = {{'argv': sys.argv, 'cwd': os.getcwd(), 'pid': os.getpid(), 'pgid': os.getpgrp(), 'env': {{key: os.environ.get(key) for key in {list(self.environment)!r}}}}}\n"
                        "session_flag = '--resume' if '--resume' in sys.argv else '--session-id'\n"
                        "native = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
                        "          'session_id': sys.argv[sys.argv.index(session_flag) + 1],\n"
                        "          'result': 'Useful peer answer 雪', 'permission_denials': [],\n"
                        "          'terminal_reason': 'completed'}\n"
                        "native.update(spec.get('native', {}))\n"
                        "for key in spec.get('remove', []): native.pop(key, None)\n"
                        "body = spec['raw'] if 'raw' in spec else json.dumps(native)\n"
                        "print('artificial provider diagnostic', file=sys.stderr, flush=True)\n"
                        "if spec.get('before_sleep'): print(body, flush=True)\n"
                        f"Path({str(self.receipt)!r}).write_text(json.dumps(receipt))\n"
                        "time.sleep(spec.get('sleep_seconds', 20 if spec.get('sleep') else 0))\n"
                        "if not spec.get('before_sleep'): print(body, flush=True)\n"
                        "sys.exit(spec.get('exit', 0))\n")
        self.count = 0

    @staticmethod
    def executable(path, source):
        path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o700)

    def invoke(self, *extra, stdin=None, output=None, launcher_flag="--multithread"):
        self.count += 1
        directory = output or self.base / f"evidence-{self.count}"
        arguments = ["claude", "--repo", str(self.repo), launcher_flag, str(self.relay),
                     "--provider", str(self.provider), "--task-file", "-" if stdin is not None else str(self.task),
                     "--output-dir", str(directory), "--json", *extra]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              mock.patch.object(peer.sys, "stdin", mock.Mock(buffer=io.BytesIO(stdin or b""))),
              mock.patch.dict(os.environ, self.environment, clear=True)):
            code = peer.peer_main(arguments)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def configure(self, **spec):
        self.response.write_text(json.dumps(spec))

    def test_literal_task_environment_hooks_and_private_evidence(self):
        task = "Inspect 雪 and café.\n`touch injected` $(touch injected) ' \\\n".encode()
        code, result, _ = self.invoke(stdin=task)
        self.assertEqual(0, code)
        self.assertEqual(task, (self.base / "received-task.txt").read_bytes())
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(self.environment, receipt["env"])
        self.assertEqual(str(self.repo), receipt["cwd"])
        self.assertEqual([str(self.provider), *self.native_arguments, "--print", "--output-format", "json",
                          "--permission-prompts", "none", "--session-id",
                          result["session_id"]], receipt["argv"])
        self.assertEqual([str(self.relay), "--repo", str(self.repo), "--json", "provider-config",
                          "--client", "claude", "--launcher-name", "multithread"],
                         json.loads((self.base / "relay-argv.json").read_text()))
        self.assertFalse((self.repo / "injected").exists())
        self.assertFalse(result["needs_attention"])
        self.assertEqual("written", result["task_delivery"])
        self.assertEqual(0, result["native_input_unwritten_bytes"])
        self.assertEqual("not_checked", result["workflow_completion"])
        evidence = Path(result["evidence_directory"])
        self.assertEqual(task, (evidence / "task.txt").read_bytes())
        raw_output = (evidence / "stdout.json").read_bytes()
        self.assertEqual({"bytes": len(raw_output), "sha256": hashlib.sha256(raw_output).hexdigest(),
                          "truncated": False, "scope": "bounded_read"}, result["stdout_observation"])
        for path in evidence.iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o700, evidence.stat().st_mode & 0o777)

    def test_followup_resumes_exact_peer_and_reads_task_file(self):
        _, first, _ = self.invoke()
        self.task.write_text("Follow up on the original answer. 雪", encoding="utf-8")
        code, result, _ = self.invoke("--resume", first["session_id"], "--max-turns", "37")
        self.assertEqual(0, code)
        self.assertEqual(first["session_id"], result["session_id"])
        argv = json.loads(self.receipt.read_text())["argv"]
        self.assertEqual(["--resume", first["session_id"]], argv[-2:])
        self.assertEqual(1, argv.count("--max-turns"))
        self.assertEqual("37", argv[argv.index("--max-turns") + 1])
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--session-id", argv)
        self.assertEqual(self.task.read_bytes(), (self.base / "received-task.txt").read_bytes())

    def test_per_call_model_and_effort_are_requested_without_claiming_effective_effort(self):
        code, dry, _ = self.invoke("--model", "opus", "--effort", "high", "--dry-run")
        self.assertEqual(0, code)
        self.assertEqual("opus", dry["requested_model"])
        self.assertEqual("high", dry["requested_effort"])
        self.assertEqual("unknown", dry["effective_effort"])
        self.assertEqual(["--model", "opus", "--effort", "high"], dry["argv"][-6:-2])
        self.assertFalse(self.calls.exists())
        code, result, _ = self.invoke("--model", "opus", "--effort", "high")
        self.assertEqual(0, code)
        self.assertEqual("unknown", result["effective_effort"])
        self.assertEqual("unknown", result["model_observation"]["relation"])
        self.assertEqual(["--model", "opus", "--effort", "high"],
                         json.loads(self.receipt.read_text())["argv"][-6:-2])
        prefix = result["follow_up_preparation"]["argv_prefix"]
        self.assertEqual("opus", prefix[prefix.index("--model") + 1])
        self.assertEqual("high", prefix[prefix.index("--effort") + 1])

    def test_model_name_cannot_become_a_native_option(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            peer.peer_main(["claude", "--task-file", str(self.task), "--model=--permission-mode"])
        self.assertEqual(2, raised.exception.code)
        self.assertFalse(self.calls.exists())

    def test_wait_slices_deliver_partial_stdin_once_and_keep_default_final_json(self):
        task = ("ARTIFICIAL-PRIVATE-TASK 雪 `touch injected`\n" * 1200).encode()
        self.assertLess(len(task), 64 * 1024)
        self.configure(read_delay=0.16, sleep_seconds=0.14,
                       native={"result": "ARTIFICIAL-PRIVATE-ANSWER"})
        call_final_json = peer._call_final_json

        def bounded_pipe(process, task, timeout, feedback, envelope):
            # Force backpressure even on hosts whose ordinary pipe fits 64 KiB.
            fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            return call_final_json(process, task, timeout, feedback, envelope)

        with (mock.patch.object(peer, "_WAIT_FEEDBACK_SECONDS", 0.03),
              mock.patch.object(peer, "_call_final_json", side_effect=bounded_pipe)):
            code, result, diagnostic = self.invoke("--timeout", "3", stdin=task)
        self.assertEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertEqual("written", result["task_delivery"])
        self.assertEqual(0, result["native_input_unwritten_bytes"])
        self.assertEqual("ARTIFICIAL-PRIVATE-ANSWER", result["result"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(task, (self.base / "received-task.txt").read_bytes())
        self.assertFalse((self.repo / "injected").exists())
        argv = json.loads(self.receipt.read_text())["argv"]
        self.assertEqual("json", argv[argv.index("--output-format") + 1])
        self.assertNotIn("--input-format", argv)
        self.assertNotIn("control", result)
        lines = diagnostic.splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertIn("local evidence", lines[0])
        writing = "writing task to provider stdin; consumption unknown"
        waiting = "task written to provider stdin; waiting for result; consumption unknown"
        self.assertTrue(any(writing in line for line in lines[1:]))
        self.assertTrue(any(waiting in line for line in lines[1:]))
        for line in lines[1:]:
            self.assertTrue(writing in line or waiting in line, line)
            self.assertNotIn("waiting for provider return", line)
            self.assertIn("input not enabled for this call", line)
            self.assertIn("provider progress unknown", line)
            self.assertNotIn(result["session_id"], line)
            self.assertNotIn(str(self.repo), line)
            self.assertNotIn(str(self.provider), line)
        self.assertNotIn("ARTIFICIAL-PRIVATE", diagnostic)
        self.assertNotIn("artificial provider diagnostic", diagnostic)
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))

    def test_task_delivery_and_wait_slices_share_one_call_deadline(self):
        task = ("ARTIFICIAL-PRIVATE-TASK 雪\n" * 1800).encode()
        self.configure(read_delay=0.65, sleep_seconds=0.65)
        call_final_json = peer._call_final_json

        def bounded_pipe(process, task, timeout, feedback, envelope):
            fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            return call_final_json(process, task, timeout, feedback, envelope)

        with (mock.patch.object(peer, "_WAIT_FEEDBACK_SECONDS", 0.04),
              mock.patch.object(peer, "_call_final_json", side_effect=bounded_pipe),
              mock.patch.object(peer, "_stop", wraps=peer._stop) as stop):
            code, result, _ = self.invoke("--timeout", "1", stdin=task)
        self.assertEqual(1, code, result)
        self.assertEqual("uncertain", result["state"])
        self.assertEqual("written", result["task_delivery"])
        self.assertEqual(0, result["native_input_unwritten_bytes"])
        self.assertIn("timed out", result["message"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(task, (self.base / "received-task.txt").read_bytes())
        self.assertLess(result["elapsed_seconds"], 5)
        stop.assert_called_once()

    def test_early_native_refusal_survives_a_closed_input_pipe(self):
        self.configure(close_stdin=True, native={"subtype": "error_during_execution", "is_error": True,
                                                "result": None, "errors": ["Artificial native refusal"]})
        call_final_json = peer._call_final_json

        def bounded_pipe(process, task, timeout, feedback, envelope):
            fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
            return call_final_json(process, task, timeout, feedback, envelope)

        with mock.patch.object(peer, "_call_final_json", side_effect=bounded_pipe):
            code, result, _ = self.invoke(stdin=b"Artificial task\n" * 3000)
        self.assertEqual(1, code, result)
        self.assertEqual("provider_error", result["state"])
        self.assertEqual("uncertain", result["task_delivery"])
        self.assertGreater(result["native_input_unwritten_bytes"], 0)
        self.assertEqual(result["requested_session_id"], result["session_id"])
        self.assertEqual(["Artificial native refusal"], result["provider_errors"])
        self.assertEqual(0, result["process_exit_code"])
        self.assertIsNone(result["result"])
        self.assertNotIn("unavailable_stage", result)
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(b"", (self.base / "received-task.txt").read_bytes())
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))

    def test_partial_task_followed_by_native_success_retains_an_uncertain_answer(self):
        task = b"Artificial partial task\n" * 2200
        self.configure(read_bytes=1)
        call_final_json = peer._call_final_json
        capacity = []

        def bounded_pipe(process, task, timeout, feedback, envelope):
            capacity.append(fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096))
            return call_final_json(process, task, timeout, feedback, envelope)

        with mock.patch.object(peer, "_call_final_json", side_effect=bounded_pipe):
            code, result, _ = self.invoke(stdin=task)
        self.assertEqual(1, code, result)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual("uncertain", result["task_delivery"])
        self.assertEqual(len(task) - capacity[0], result["native_input_unwritten_bytes"])
        self.assertEqual(result["requested_session_id"], result["session_id"])
        self.assertEqual(0, result["process_exit_code"])
        self.assertEqual("success", result["provider_subtype"])
        self.assertFalse(result["provider_is_error"])
        self.assertIsNone(result["result"])
        self.assertEqual("Useful peer answer 雪", result["partial_result"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(task[:1], (self.base / "received-task.txt").read_bytes())
        evidence = Path(result["evidence_directory"])
        self.assertEqual(task, (evidence / "task.txt").read_bytes())
        native = json.loads((evidence / "stdout.json").read_text())
        self.assertEqual("Useful peer answer 雪", native["result"])
        self.assertEqual(result, json.loads((evidence / "result.json").read_text()))

    def test_timeout_during_partial_delivery_retains_the_unwritten_count(self):
        task = b"Artificial partial task\n" * 2200
        self.configure(read_delay=20)
        call_final_json = peer._call_final_json
        capacity = []

        def bounded_pipe(process, task, timeout, feedback, envelope):
            capacity.append(fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096))
            return call_final_json(process, task, timeout, feedback, envelope)

        with (mock.patch.object(peer, "_WAIT_FEEDBACK_SECONDS", 0.04),
              mock.patch.object(peer, "_call_final_json", side_effect=bounded_pipe),
              mock.patch.object(peer, "_stop", wraps=peer._stop) as stop):
            code, result, _ = self.invoke("--timeout", "1", stdin=task)
        self.assertEqual(1, code, result)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual("uncertain", result["task_delivery"])
        self.assertEqual(len(task) - capacity[0], result["native_input_unwritten_bytes"])
        self.assertIsNone(result["result"])
        self.assertIn("timed out", result["message"])
        self.assertEqual(-signal.SIGTERM, result["process_exit_code"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(task, (Path(result["evidence_directory"]) / "task.txt").read_bytes())
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))
        stop.assert_called_once()

    def test_cancellation_during_partial_delivery_retains_the_unwritten_count(self):
        task = b"Artificial partial task\n" * 2200
        self.configure(read_delay=20)
        call_final_json, write = peer._call_final_json, peer.os.write
        written = []

        def interrupting_call(process, task, timeout, feedback, envelope):
            fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)

            def record_write(fd, body):
                count = write(fd, body)
                written.append(count)
                return count

            def interrupt_after_write():
                if written:
                    raise KeyboardInterrupt()
                feedback()

            with mock.patch.object(peer.os, "write", side_effect=record_write):
                return call_final_json(process, task, timeout, interrupt_after_write, envelope)

        with (mock.patch.object(peer, "_call_final_json", side_effect=interrupting_call) as call,
              mock.patch.object(peer, "_stop", wraps=peer._stop) as stop):
            code, result, _ = self.invoke(stdin=task)
        self.assertEqual(130, code, result)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["provider_started"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual("uncertain", result["task_delivery"])
        self.assertGreater(sum(written), 0)
        self.assertLess(sum(written), len(task))
        self.assertEqual(len(task) - sum(written), result["native_input_unwritten_bytes"])
        self.assertIsNone(result["result"])
        self.assertIn("interrupted", result["message"])
        self.assertIsNotNone(result["process_exit_code"])
        self.assertEqual(task, (Path(result["evidence_directory"]) / "task.txt").read_bytes())
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))
        call.assert_called_once()
        stop.assert_called_once()

    def test_failed_waiting_diagnostic_does_not_stop_or_retry_provider(self):
        self.configure(sleep_seconds=0.14)
        original_print = print

        def disconnected_diagnostic(*args, **kwargs):
            if kwargs.get("file") is sys.stderr and "provider progress unknown" in str(args[0]):
                raise BrokenPipeError("ARTIFICIAL-PRIVATE-DIAGNOSTIC")
            return original_print(*args, **kwargs)

        with (mock.patch.object(peer, "_WAIT_FEEDBACK_SECONDS", 0.02),
              mock.patch("builtins.print", side_effect=disconnected_diagnostic),
              mock.patch.object(peer, "_stop", wraps=peer._stop) as stop):
            code, result, diagnostic = self.invoke("--timeout", "3")
        self.assertEqual(0, code, result)
        self.assertEqual("returned", result["state"])
        self.assertFalse(result["needs_attention"])
        self.assertEqual("call\n", self.calls.read_text())
        stop.assert_not_called()
        self.assertEqual(1, len(diagnostic.splitlines()))
        self.assertNotIn("ARTIFICIAL-PRIVATE-DIAGNOSTIC", json.dumps(result))

    def test_initial_evidence_breadcrumb_escapes_controls_without_changing_path(self):
        directory = self.base / "evidence\n\x1b[31m\r\u202ename"
        code, result, diagnostic = self.invoke(output=directory)
        self.assertEqual(0, code, result)
        self.assertEqual(str(directory), result["evidence_directory"])
        self.assertTrue((directory / "result.json").is_file())
        self.assertEqual(1, len(diagnostic.splitlines()))
        self.assertNotIn("\x1b", diagnostic)
        self.assertNotIn("\r", diagnostic)
        self.assertNotIn("\u202e", diagnostic)
        self.assertIn("evidence\\n\\u001b[31m\\r\\u202ename", diagnostic)

    def test_source_peer_does_not_attribute_selected_launcher_runtime(self):
        hook = shlex.join([str(self.relay), "--repo", str(self.repo), "provider-hook", "--client", "claude"])
        unrelated_runtime = {"status": "recorded", "runtime_manifest_sha256": "a" * 64}
        plan = {"schema": 1, "provider": "claude", "repo": str(self.repo), "hook_command": hook,
                "native_arguments": self.native_arguments, "launches_provider": False,
                "changes_provider_settings": False, "changes_permissions": False,
                "version": "99.0.0-unrelated-fixture", "producer_runtime": unrelated_runtime}
        self.executable(self.relay, "import json, sys\n"
                        "assert 'runtime' not in sys.argv, 'unexpected installation query'\n"
                        f"print({json.dumps(plan)!r})\n")
        code, result, _ = self.invoke()
        self.assertEqual(0, code)
        expected = {"status": "unavailable", "runtime_manifest_sha256": None}
        self.assertEqual(expected, result["producer_runtime"])
        evidence = Path(result["evidence_directory"])
        self.assertEqual(expected, json.loads((evidence / "request.json").read_text())["producer_runtime"])
        self.assertEqual(expected, json.loads((evidence / "result.json").read_text())["producer_runtime"])

    def test_dry_run_has_no_provider_or_evidence_writes(self):
        evidence = self.base / "dry-evidence"
        code, result, _ = self.invoke("--dry-run", output=evidence)
        self.assertEqual(0, code)
        self.assertEqual("call_prepared", result["state"])
        self.assertFalse(result["provider_started"])
        self.assertNotIn("stdout_observation", result)
        self.assertNotIn("stdout_observation_error", result)
        self.assertFalse(self.calls.exists())
        self.assertFalse(evidence.exists())

    def test_legacy_launcher_flag_still_prepares_without_provider_or_evidence_writes(self):
        evidence = self.base / "legacy-dry-evidence"
        code, result, _ = self.invoke("--dry-run", output=evidence, launcher_flag="--relay")
        self.assertEqual(0, code)
        self.assertEqual("call_prepared", result["state"])
        self.assertFalse(result["provider_started"])
        self.assertFalse(self.calls.exists())
        self.assertFalse(evidence.exists())

    def test_existing_evidence_directory_is_untouched(self):
        evidence = self.base / "existing"
        evidence.mkdir()
        (evidence / "keep.txt").write_bytes(b"original evidence")
        before = {path.name: path.read_bytes() for path in evidence.iterdir()}
        code, result, _ = self.invoke(output=evidence)
        self.assertEqual(1, code)
        self.assertFalse(result["provider_started"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in evidence.iterdir()})
        self.assertFalse(self.calls.exists())
        self.assertIn("--output-dir already exists", result["message"])
        self.assertIn("redirect command output outside it", result["message"])

    def test_bad_tasks_refuse_before_config_or_provider(self):
        for body in (b" \n", b"bad\0task", b"\xff", b"x" * (64 * 1024 + 1)):
            with self.subTest(body=body[:20]):
                code, result, _ = self.invoke(stdin=body)
                self.assertEqual(1, code)
                self.assertFalse(result["provider_started"])
                self.assertFalse((self.base / "relay-argv.json").exists())
                self.assertFalse(self.calls.exists())

    def test_timeout_retains_side_effects_and_never_retries(self):
        self.configure(sleep=True)
        with (mock.patch.object(peer, "_WAIT_FEEDBACK_SECONDS", 0.04),
              mock.patch.object(peer, "_stop", wraps=peer._stop) as stop):
            code, result, diagnostic = self.invoke("--timeout", "1")
        stop.assert_called_once()
        owned = stop.call_args.args[0]
        receipt = json.loads(self.receipt.read_text())
        self.assertEqual(receipt["pid"], owned.pid)
        self.assertEqual(receipt["pid"], receipt["pgid"])
        self.assertEqual(-signal.SIGTERM, result["process_exit_code"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(receipt["pgid"], 0)
        self.assertGreater(len(diagnostic.splitlines()), 2)
        self.assertLess(result["elapsed_seconds"], 5)
        self.assertEqual(1, code)
        self.assertEqual("uncertain", result["state"])
        self.assertEqual("timeout", result["caller_stop_reason"])
        self.assertIn("Call timed out", result["message"])
        self.assertTrue(result["provider_started"])
        self.assertIsNone(result["session_id"])
        self.assertIsNone(result["result"])
        self.assertEqual("unknown", result["provider_tools"])
        self.assertEqual("unknown", result["hook_delivery"])
        self.assertEqual("not_checked", result["workflow_completion"])
        self.assertEqual("call\n", self.calls.read_text())
        self.assertEqual(self.task.read_bytes(), (self.base / "received-task.txt").read_bytes())
        evidence = Path(result["evidence_directory"])
        self.assertEqual(b"", (evidence / "stdout.json").read_bytes())
        self.assertEqual({"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                          "truncated": False, "scope": "bounded_read"}, result["stdout_observation"])
        self.assertIn("artificial provider diagnostic", (evidence / "stderr.txt").read_text())
        self.assertEqual(result["requested_session_id"], json.loads((evidence / "request.json").read_text())["requested_session_id"])
        self.assertEqual(result, json.loads((evidence / "result.json").read_text()))

    def test_timeout_retains_partial_or_valid_stdout_without_validating_or_exposing_it(self):
        requested = "00000000-0000-4000-8000-000000000001"
        secret = "artificial-private-output-secret"
        for output_spec in ({"raw": '{"unfinished": "' + secret},
                            {"native": {"result": secret}}):
            with self.subTest(output_spec=output_spec):
                self.calls.unlink(missing_ok=True)
                self.configure(sleep=True, before_sleep=True, **output_spec)
                code, result, _ = self.invoke("--timeout", "1", "--resume", requested)
                self.assertEqual(1, code)
                self.assertEqual("uncertain", result["state"])
                self.assertTrue(result["needs_attention"])
                self.assertIsNone(result["session_id"])
                self.assertIsNone(result["result"])
                self.assertNotIn("observed_session_id", result)
                self.assertNotIn("provider_subtype", result)
                self.assertEqual(requested, result["requested_session_id"])
                self.assertIn("timed out", result["message"])
                self.assertEqual("call\n", self.calls.read_text())
                evidence = Path(result["evidence_directory"])
                body = (evidence / "stdout.json").read_bytes()
                self.assertIn(secret.encode(), body)
                self.assertEqual({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                  "truncated": False, "scope": "bounded_read"}, result["stdout_observation"])
                self.assertEqual(result, json.loads((evidence / "result.json").read_text()))
                output = io.StringIO()
                with redirect_stdout(output):
                    peer._display_peer(result)
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn(secret, json.dumps(result))
                self.assertIn("Requested session (unverified): " + requested, output.getvalue())
                self.assertNotIn("Peer session:", output.getvalue())
                self.assertNotIn("--resume", output.getvalue())

    def test_timeout_bounded_observation_preserves_complete_stdout(self):
        body = ("artificial-private-output-" * 20 + "\n").encode()
        self.configure(sleep=True, before_sleep=True, raw=body.decode().rstrip("\n"))
        read_cap = 32
        with mock.patch.object(peer, "_MAX_RESULT", read_cap):
            code, result, _ = self.invoke("--timeout", "1")
        self.assertEqual(1, code)
        self.assertEqual("uncertain", result["state"])
        self.assertIn("timed out", result["message"])
        prefix = body[:read_cap + 1]
        self.assertEqual({"bytes": len(prefix), "sha256": hashlib.sha256(prefix).hexdigest(),
                          "truncated": True, "scope": "bounded_read"}, result["stdout_observation"])
        self.assertEqual(body, (Path(result["evidence_directory"]) / "stdout.json").read_bytes())
        self.assertEqual("call\n", self.calls.read_text())

    def test_timeout_unreadable_stdout_keeps_original_outcome_and_unknown_observation(self):
        body = "artificial-private-output-secret"
        self.configure(sleep=True, before_sleep=True, raw=body)
        original_open = Path.open

        def unavailable_stdout(path, mode="r", *args, **kwargs):
            if path.name == "stdout.json" and mode == "rb":
                raise OSError("artificial-private-read-error")
            return original_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", unavailable_stdout):
            code, result, _ = self.invoke("--timeout", "1")
        self.assertEqual(1, code)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertIn("timed out", result["message"])
        self.assertIsNotNone(result["process_exit_code"])
        self.assertIsNone(result["session_id"])
        self.assertIsNone(result["result"])
        self.assertNotIn("stdout_observation", result)
        self.assertEqual("unavailable; byte count and digest are unknown", result["stdout_observation_error"])
        self.assertNotIn("artificial-private-read-error", json.dumps(result))
        evidence = Path(result["evidence_directory"])
        self.assertEqual((body + "\n").encode(), (evidence / "stdout.json").read_bytes())
        self.assertEqual(result, json.loads((evidence / "result.json").read_text()))
        self.assertEqual("call\n", self.calls.read_text())

    def test_provider_spawn_failure_does_not_observe_unstarted_stdout(self):
        original_open = Path.open

        def reject_stdout_read(path, mode="r", *args, **kwargs):
            if path.name == "stdout.json" and mode == "rb":
                self.fail("Provider never started; stdout must not be inspected")
            return original_open(path, mode, *args, **kwargs)

        with (mock.patch.object(peer.subprocess, "Popen", side_effect=OSError("artificial spawn failure")),
              mock.patch.object(peer, "prepare", return_value={"argv": [str(self.provider)], "repo": str(self.repo)}),
              mock.patch.object(Path, "open", reject_stdout_read)):
            code, result, _ = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("unavailable", result["state"])
        self.assertEqual("provider_spawn", result["unavailable_stage"])
        self.assertFalse(result["provider_started"])
        self.assertNotIn("stdout_observation", result)
        self.assertNotIn("stdout_observation_error", result)
        self.assertFalse(self.calls.exists())
        self.assertEqual(b"", (Path(result["evidence_directory"]) / "stdout.json").read_bytes())

    def test_termination_signals_stop_native_group_and_retain_uncertain_result(self):
        body = b"artificial-private-signal-output\n"
        self.configure(sleep=True, before_sleep=True, raw=body.decode().rstrip("\n"))
        # This cancellation fixture explicitly opts into termination signals;
        # the invoking shell or CI runner may have ignored them on entry.
        wrapper_entry = ("import runpy, signal, sys\n"
                         "for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):\n"
                         "    signal.signal(number, signal.SIG_DFL)\n"
                         "sys.argv = sys.argv[1:]\n"
                         "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                self.receipt.unlink(missing_ok=True)
                self.calls.unlink(missing_ok=True)
                evidence = self.base / f"signal-{signum.name}"
                wrapper = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-B", "-c", wrapper_entry,
                     str(ROOT / "examples" / "call_peer.py"),
                     "claude", "--repo", str(self.repo), "--multithread", str(self.relay),
                     "--provider", str(self.provider), "--task-file", str(self.task),
                     "--output-dir", str(evidence), "--timeout", "15", "--json"],
                    cwd=ROOT, env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, start_new_session=True)
                receipt = None
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            receipt = json.loads(self.receipt.read_text())
                            break
                        except (FileNotFoundError, json.JSONDecodeError):
                            if wrapper.poll() is not None:
                                break
                            time.sleep(0.01)
                    self.assertIsNotNone(receipt, "fake provider did not report startup")
                    self.assertEqual(receipt["pid"], receipt["pgid"])
                    self.assertNotEqual(wrapper.pid, receipt["pgid"])
                    wrapper.send_signal(signum)
                    stdout, stderr = wrapper.communicate(timeout=10)
                    self.assertEqual(128 + signum, wrapper.returncode, stderr)
                    result = json.loads(stdout)
                    self.assertEqual("uncertain", result["state"])
                    self.assertEqual("interrupted", result["caller_stop_reason"])
                    self.assertIn("Call interrupted", result["message"])
                    self.assertTrue(result["provider_started"])
                    self.assertIsNotNone(result["process_exit_code"])
                    self.assertTrue(result["needs_attention"])
                    self.assertIsNone(result["session_id"])
                    self.assertIsNone(result["result"])
                    self.assertIn("interrupted", result["message"])
                    self.assertEqual(body, (evidence / "stdout.json").read_bytes())
                    self.assertEqual({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                      "truncated": False, "scope": "bounded_read"}, result["stdout_observation"])
                    self.assertEqual(result, json.loads((evidence / "result.json").read_text()))
                    self.assertEqual("call\n", self.calls.read_text())
                    self.assertEqual(self.task.read_bytes(), (evidence / "task.txt").read_bytes())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(receipt["pid"], 0)
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(receipt["pgid"], 0)
                finally:
                    if wrapper.poll() is None:
                        wrapper.kill()
                    wrapper.communicate(timeout=5)
                    # Cleanup remains bounded even when an assertion exposes an orphan.
                    if receipt is None:
                        try:
                            receipt = json.loads(self.receipt.read_text())
                        except (FileNotFoundError, json.JSONDecodeError):
                            pass
                    if receipt is not None:
                        try:
                            os.killpg(receipt["pgid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_interrupted_cleanup_records_final_provider_exit(self):
        handlers = {number: signal.getsignal(number)
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        process = mock.Mock(returncode=None)
        stops = 0

        def interrupted_stop(child):
            nonlocal stops
            self.assertIs(process, child)
            stops += 1
            if stops == 1:
                raise KeyboardInterrupt()
            child.returncode = -signal.SIGTERM

        plan = {"argv": [str(self.provider), *self.native_arguments], "repo": str(self.repo)}
        with (mock.patch.object(peer, "prepare", return_value=plan),
              mock.patch.object(peer.subprocess, "Popen", return_value=process),
              mock.patch.object(peer, "_call_final_json", side_effect=KeyboardInterrupt()),
              mock.patch.object(peer, "_stop", side_effect=interrupted_stop)):
            code, result, _ = self.invoke()
        self.assertEqual(130, code)
        self.assertEqual("uncertain", result["state"])
        self.assertTrue(result["provider_started"])
        self.assertEqual(-signal.SIGTERM, result["process_exit_code"])
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))
        self.assertEqual(handlers, {number: signal.getsignal(number) for number in handlers})

    def test_sigkill_leaves_an_honest_recovery_checkpoint_without_terminal_receipt(self):
        self.configure(sleep=True)
        evidence = self.base / "killed-caller"
        wrapper = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(ROOT / "examples" / "call_peer.py"),
             "claude", "--repo", str(self.repo), "--multithread", str(self.relay),
             "--provider", str(self.provider), "--task-file", str(self.task),
             "--output-dir", str(evidence), "--timeout", "30", "--json"],
            cwd=ROOT, env=self.environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        native = None
        checkpoint = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    native = json.loads(self.receipt.read_text())
                    checkpoint = json.loads((evidence / "checkpoint.json").read_text())
                    if checkpoint["phase"] == "spawned":
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    if wrapper.poll() is not None:
                        break
                time.sleep(0.01)
            self.assertIsNotNone(native, "fixture provider did not reach its sleep")
            self.assertIsNotNone(checkpoint, "provider checkpoint was never observed")
            self.assertEqual("spawned", checkpoint["phase"])
            wrapper.kill()
            self.assertEqual(-signal.SIGKILL, wrapper.wait(timeout=5))
            self.assertFalse((evidence / "result.json").exists())
            report = peer._read_report(evidence)
            self.assertEqual("incomplete", report["report_state"])
            self.assertEqual("missing", report["receipt_status"])
            self.assertEqual("spawned", report["checkpoint"]["phase"])
            self.assertEqual("unknown", report["checkpoint"]["outcome"])
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)
            if native is not None:
                try:
                    os.killpg(native["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_inherited_ignored_hangup_stays_ignored_through_call(self):
        original = peer.prepare

        def check_ignored_hangup(*args, **kwargs):
            self.assertEqual(signal.SIG_IGN, signal.getsignal(signal.SIGHUP))
            os.kill(os.getpid(), signal.SIGHUP)
            return original(*args, **kwargs)

        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            with mock.patch.object(peer, "prepare", side_effect=check_ignored_hangup):
                code, result, _ = self.invoke()
            self.assertEqual(signal.SIG_IGN, signal.getsignal(signal.SIGHUP))
            self.assertEqual(0, code)
            self.assertEqual("returned", result["state"])
            self.assertEqual("call\n", self.calls.read_text())
        finally:
            signal.signal(signal.SIGHUP, previous)

    def test_termination_after_provider_exit_preserves_returned_result(self):
        handlers = {number: signal.getsignal(number)
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        original = peer._interpret

        def terminate_during_interpretation(directory, envelope):
            self.assertEqual(0, envelope["process_exit_code"])
            os.kill(os.getpid(), signal.SIGTERM)
            return original(directory, envelope)

        with mock.patch.object(peer, "_interpret", side_effect=terminate_during_interpretation):
            code, result, _ = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual("returned", result["state"])
        self.assertEqual("Useful peer answer 雪", result["result"])
        self.assertFalse(result["needs_attention"])
        self.assertEqual(result["requested_session_id"], result["session_id"])
        self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))
        self.assertEqual(handlers, {number: signal.getsignal(number) for number in handlers})
        self.assertEqual("call\n", self.calls.read_text())

    def test_termination_during_spawn_preserves_handle_for_cleanup(self):
        handlers = {number: signal.getsignal(number)
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        original = subprocess.Popen
        children = []

        def interrupted_spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            if kwargs.get("start_new_session"):
                children.append(child)
                os.kill(os.getpid(), signal.SIGTERM)
            return child

        try:
            with mock.patch.object(peer.subprocess, "Popen", side_effect=interrupted_spawn):
                code, result, _ = self.invoke()
            self.assertEqual(143, code)
            self.assertEqual(1, len(children))
            self.assertIsNotNone(children[0].poll())
            self.assertEqual("uncertain", result["state"])
            self.assertTrue(result["provider_started"])
            self.assertEqual(children[0].returncode, result["process_exit_code"])
            self.assertEqual(result, json.loads((Path(result["evidence_directory"]) / "result.json").read_text()))
            self.assertEqual(handlers, {number: signal.getsignal(number) for number in handlers})
            with self.assertRaises(ProcessLookupError):
                os.killpg(children[0].pid, 0)
        finally:
            for child in children:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait(timeout=5)
                if child.stdin is not None:
                    child.stdin.close()

    def test_unreadable_missing_and_mismatched_results_remain_uncertain(self):
        cases = ({"raw": ""}, {"raw": "not JSON"}, {"raw": "[]"},
                 {"remove": ["result"]}, {"remove": ["session_id"]},
                 {"native": {"session_id": "00000000-0000-4000-8000-000000000001"}},
                 {"native": {"is_error": "false"}}, {"native": {"permission_denials": "denied"}},
                 {"native": {"errors": [False]}}, {"native": {"result": "\ud800"}})
        for spec in cases:
            with self.subTest(spec=spec):
                self.configure(**spec)
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("uncertain", result["state"])
                self.assertTrue(result["needs_attention"])
                if spec.get("native", {}).get("session_id"):
                    self.assertEqual(spec["native"]["session_id"], result["observed_session_id"])
                    self.assertIsNone(result["session_id"])
                    self.assertIsNone(result["result"])
        self.assertEqual(len(cases), len(self.calls.read_text().splitlines()))

    def test_denials_retain_useful_answer_without_claiming_completion(self):
        self.configure(native={"permission_denials": [{"tool_name": "Bash", "tool_use_id": "fixture-call",
                                                        "tool_input": {"command": "artificial private command"}}]})
        code, result, _ = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual("Useful peer answer 雪", result["result"])
        self.assertTrue(result["needs_attention"])
        self.assertEqual("not_checked", result["workflow_completion"])
        self.assertEqual([{"tool_name": "Bash", "tool_use_id": "fixture-call"}], result["permission_denials"])
        self.assertNotIn("artificial private command", json.dumps(result))
        self.assertIn("artificial private command", (Path(result["evidence_directory"]) / "stdout.json").read_text())

    def test_nonzero_exit_and_provider_errors_preserve_answer(self):
        for spec in ({"exit": 4}, {"native": {"is_error": True, "subtype": "error_during_execution"}}):
            with self.subTest(spec=spec):
                self.configure(**spec)
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("provider_error", result["state"])
                self.assertEqual("Useful peer answer 雪", result["result"])

    def test_resultless_native_errors_are_returned_with_explicit_bounds(self):
        for errors in (["Artificial provider failure"], ["Artificial failure " * 200] * 9):
            with self.subTest(count=len(errors)):
                self.configure(remove=["result"], native={"is_error": True,
                               "subtype": "error_during_execution", "errors": errors})
                code, result, _ = self.invoke()
                self.assertEqual(1, code)
                self.assertEqual("provider_error", result["state"])
                self.assertIsNone(result["result"])
                self.assertTrue(result["needs_attention"])
                self.assertTrue(result["provider_errors"][0].startswith("Artificial"))
                self.assertLessEqual(len(result["provider_errors"]), 8)
                self.assertTrue(all(len(error) <= 2000 for error in result["provider_errors"]))
                self.assertEqual(len(errors) > 1, result["provider_errors_truncated"])
                raw = json.loads((Path(result["evidence_directory"]) / "stdout.json").read_text())
                self.assertEqual(errors, raw["errors"])

    def test_result_record_failure_retains_returned_text(self):
        original = peer._atomic_record
        def record(directory, name, value, **kwargs):
            if name == "result.json":
                raise OSError("artificial full disk")
            return original(directory, name, value, **kwargs)
        with mock.patch.object(peer, "_atomic_record", side_effect=record):
            code, result, _ = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("returned", result["state"])
        self.assertEqual("Useful peer answer 雪", result["result"])
        self.assertIn("unavailable", result["evidence_recording"])
        self.assertEqual("call\n", self.calls.read_text())

    def test_atomic_result_publish_preserves_old_receipt_on_replace_failure(self):
        directory = self.base / "atomic-result"
        directory.mkdir(mode=0o700)
        result = directory / "result.json"
        result.write_bytes(b'{"old":true}')
        with mock.patch.object(peer.os, "replace", side_effect=OSError("artificial rename failure")):
            with self.assertRaises(OSError):
                peer._atomic_record(directory, "result.json", {"new": True})
        self.assertEqual(b'{"old":true}', result.read_bytes())
        self.assertEqual(["result.json"], sorted(path.name for path in directory.iterdir()))

    def test_spawned_checkpoint_failure_keeps_terminal_attention_and_excludes_cost(self):
        original = peer._atomic_record

        def fail_checkpoint(directory, name, value, **kwargs):
            if name == "checkpoint.json":
                raise OSError("artificial checkpoint failure")
            return original(directory, name, value, **kwargs)

        with mock.patch.object(peer, "_atomic_record", side_effect=fail_checkpoint):
            code, result, _ = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("returned", result["state"])
        self.assertTrue(result["needs_attention"])
        self.assertNotIn("follow_up_preparation", result)
        self.assertIn("durability could not be confirmed", result["evidence_recording"])
        directory = Path(result["evidence_directory"])
        self.assertTrue(json.loads((directory / "result.json").read_text())["needs_attention"])
        self.assertEqual("reported", peer._read_report(directory)["call"]["faults"]["recording"])
        self.assertIsNone(peer._comparable_cost_receipt(directory))

    def test_installed_dispatch_preserves_native_environment_boundary(self):
        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(cli, "_closed_environment", side_effect=AssertionError("confined native call")),
              mock.patch.object(cli, "abi_version", side_effect=AssertionError("worker admission")),
              mock.patch.object(peer, "peer_main", return_value=7) as call):
            code = cli.main(["--repo", str(self.repo), "--json", "peer", "claude", "--task-file", str(self.task)])
        self.assertEqual(7, code)
        call.assert_called_once_with(["claude", "--task-file", str(self.task), "--repo", str(self.repo), "--json"])

    def test_installed_peer_help_exits_without_native_execution(self):
        output = io.StringIO()
        with (redirect_stdout(output), mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(peer.subprocess, "Popen") as native,
              mock.patch.object(peer.subprocess, "run") as config,
              self.assertRaises(SystemExit) as stopped):
            cli.main(["peer", "--help"])
        self.assertEqual(0, stopped.exception.code)
        self.assertIn("--task-file", output.getvalue())
        native.assert_not_called()
        config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
