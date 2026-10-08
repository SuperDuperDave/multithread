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
        # The managed-settings preflight reads this host's Windows registry under WSL; fixtures never do.
        for name in ("_WSL_REG", "_WSL_CLAUDE_POLICY"):
            isolated = mock.patch.object(peer, name, Path("/nonexistent") / name)
            isolated.start()
            self.addCleanup(isolated.stop)
        # Restricted calls admit only reviewed binaries; these fixtures stand in for the hand-reviewed one.
        from relay_runtime import claude_peer as claude_review
        reviewed = mock.patch.object(claude_review, "reviewed",
                                     return_value={**next(iter(claude_review.BUILT_IN.values())), "source": "built_in"})
        reviewed.start()
        self.addCleanup(reviewed.stop)
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
                 for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")}
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

    def configure_hook(self, hook):
        hooks = {event: [{"hooks": [{"type": "command", "command": hook, "timeout": 3}]}]
                 for event in peer._hook_events("claude")}
        self.native_arguments = ["--settings", json.dumps({"hooks": hooks})]
        plan = {"schema": 1, "provider": "claude", "repo": str(self.repo), "hook_command": hook,
                "native_arguments": self.native_arguments, "launches_provider": False,
                "changes_provider_settings": False, "changes_permissions": False}
        self.executable(self.relay, f"print({json.dumps(plan)!r})\n")

    def test_shell_evaluation_and_ambiguous_syntax_refuse_before_native_or_evidence(self):
        cases = (("semicolon", "checkout;touch${IFS}canary;#", False),
                 ("command-substitution", "checkout-$(touch${IFS}canary)", True),
                 ("parameter", "checkout-${SYNTHETIC}", True),
                 ("backtick", "checkout-`touch${IFS}canary`", True),
                 ("glob-star", "checkout*", False),
                 ("glob-question", "checkout?", False),
                 ("glob-bracket", "checkout[ab]", False),
                 ("brace", "checkout{a,b}", False),
                 ("tilde", "checkout~name", False),
                 ("comment-marker", "checkout#name", False),
                 ("double-quote-escape-mismatch", "checkout-\\$SYNTHETIC", True))
        for label, name, double in cases:
            self.repo = self.base / name
            self.repo.mkdir()
            argument = '"' + str(self.repo) + '"' if double else str(self.repo)
            hook = f"{self.relay} --repo {argument} provider-hook --client claude"
            # These examples passed the former token-only admission check.
            self.assertEqual(peer.hook_argv(self.relay, "claude", self.repo), shlex.split(hook))
            self.configure_hook(hook)
            for dry in (True, False):
                evidence = self.base / f"refused-{label}-{dry}"
                with (self.subTest(case=label, dry=dry),
                      mock.patch.object(peer, "check_native_arguments", side_effect=AssertionError(
                          "unsafe hook reached native argument inspection")) as native,
                      mock.patch.object(peer.user_hooks, "delivery", side_effect=AssertionError(
                          "unsafe hook reached account hook inspection")) as delivery):
                    code, result, _ = self.invoke(*(["--dry-run"] if dry else []), output=evidence)
                self.assertEqual((1, "unavailable", "relay_configuration"),
                                 (code, result["state"], result["unavailable_stage"]))
                self.assertFalse(result["provider_started"])
                # Nothing ran; a real call's refusal is recorded where its caller reads, and nothing else is.
                self.assertEqual(None if dry else str(evidence), result["evidence_directory"])
                self.assertIn("hook command", result["message"])
                native.assert_not_called()
                delivery.assert_not_called()
                if dry:
                    self.assertFalse(evidence.exists())
                else:
                    self.assertEqual(["result.json"], sorted(path.name for path in evidence.iterdir()))
                    self.assertEqual(result, json.loads((evidence / "result.json").read_text()))
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.receipt.exists())
                self.assertFalse((self.base / "canary").exists())

    def test_unsafe_hook_refuses_interactive_launch_before_prompt_or_provider(self):
        hook = f'{self.relay} --repo "{self.repo}" provider-hook --client claude'
        self.configure_hook(hook)
        shown, errors = io.StringIO(), io.StringIO()
        with (redirect_stdout(shown), redirect_stderr(errors),
              mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(peer.sys, "stdin", mock.Mock(isatty=lambda: True)),
              mock.patch("builtins.input", side_effect=AssertionError("unsafe hook prompted")) as prompt,
              mock.patch.object(peer.subprocess, "call", side_effect=AssertionError(
                  "unsafe hook started a provider")) as native,
              mock.patch.object(peer.user_hooks, "delivery", side_effect=AssertionError(
                  "unsafe hook inspected account settings")) as delivery):
            code = peer.launch_main(["claude", "--repo", str(self.repo), "--multithread",
                                     str(self.relay), "--provider", str(self.provider)])
        self.assertEqual(1, code)
        self.assertEqual("", shown.getvalue())
        self.assertIn("quote shell metacharacters", errors.getvalue())
        prompt.assert_not_called()
        native.assert_not_called()
        delivery.assert_not_called()
        self.assertFalse(self.calls.exists())

    def test_literal_hook_formats_match_shell_and_preserve_exact_native_text(self):
        self.repo = self.base / "checkout ' 雪 ;$`*?[]{}~#\\\n\x1b\u202e"
        self.repo.mkdir()
        argv = peer.hook_argv(self.relay, "claude", self.repo)
        canonical = shlex.join(argv)
        double = lambda word: '"' + "".join("\\" + char if char in '$`"\\' else char
                                           for char in word) + '"'
        escaped = lambda word: "".join(char if char.isalnum() or char in "_./-" else "\\" + char
                                        for char in word)
        # The argv recorder witnesses shell semantics independently of the decoder.
        recorder = self.base / "argv-recorder"
        self.executable(recorder, "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        forms = (canonical,
                 " \t" + "\t  ".join(shlex.quote(word) for word in argv) + "\t ",
                 " ".join(double(word) for word in argv),
                 shlex.quote(str(self.relay)) + " --repo " + shlex.quote(str(self.repo))
                 + " pro'vider'\"-hook\" --cli\\ent claude")
        for hook in forms:
            with self.subTest(hook=hook):
                self.assertEqual(argv, peer._literal_hook_argv(hook))
                words = hook.replace(str(self.relay), str(recorder), 1)
                shell = subprocess.run(["/bin/sh", "-c", words], cwd=self.base,
                                       env=self.environment, capture_output=True, text=True, check=True)
                self.assertEqual(argv[1:], json.loads(shell.stdout))
                self.configure_hook(hook)
                code, result, _ = self.invoke("--dry-run")
                self.assertEqual((0, "call_prepared"), (code, result["state"]))
                self.assertEqual(self.native_arguments, result["argv"][1:3])
                self.assertFalse(self.calls.exists())
        # Backslash quoting is safe outside quotes, except line continuation.
        plain = self.base / "escaped ;$`*?[]{}~#雪"
        plain.mkdir()
        self.repo = plain
        argv = peer.hook_argv(self.relay, "claude", self.repo)
        hook = " ".join(escaped(word) for word in argv)
        self.configure_hook(hook)
        code, result, _ = self.invoke()
        self.assertEqual((0, "returned"), (code, result["state"]))
        self.assertEqual(self.native_arguments, json.loads(self.receipt.read_text())["argv"][1:3])

    def test_literal_hook_rejects_separators_continuations_and_nul(self):
        for command in ("a\nb", "a\rb", "a\vb", "a\\\nb", '"a\\\nb"',
                        "a\0b", "'a\0b'", '"a\0b"', "'unfinished", '"unfinished', "a\\"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                peer._literal_hook_argv(command)

    def test_followup_retains_safely_escaped_launcher_entry(self):
        self.relay = self.base / "multithread-$literal`entry`"
        hook = '"' + str(self.relay).replace("$", "\\$").replace("`", "\\`") + '"'
        hook += " --repo " + shlex.quote(str(self.repo)) + " provider-hook --client claude"
        self.configure_hook(hook)
        code, result, _ = self.invoke()
        self.assertEqual((0, "returned"), (code, result["state"]))
        prefix = result["follow_up_preparation"]["argv_prefix"]
        self.assertEqual(str(self.relay), prefix[0])
        self.assertEqual(str(self.relay), prefix[prefix.index("--multithread") + 1])

    def assert_argument_rejected(self, *extra):
        # A failed argument guard must stop the test, never discover an account's
        # provider or run launch preparation. Keep disposable paths and dry-run
        # as independent boundaries if validation regresses.
        with (mock.patch.object(peer, "prepare", side_effect=AssertionError(
                "rejected argument reached launch preparation")) as preparation,
              mock.patch.object(peer.subprocess, "Popen", side_effect=AssertionError(
                "rejected argument attempted a subprocess")) as native,
              self.assertRaises(SystemExit) as raised):
            self.invoke(*extra, "--dry-run")
        self.assertEqual(2, raised.exception.code)
        preparation.assert_not_called()
        native.assert_not_called()

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

    def test_final_json_records_the_one_model_its_usage_names(self):
        usage = {"inputTokens": 3, "outputTokens": 4, "costUSD": 0.01}
        unobserved = {"source": "unavailable", "reported_model": None, "relation": "unknown"}

        def observed(relation):
            return {"source": "claude_model_usage", "reported_model": "claude-opus-5-5", "relation": relation}
        cases = ((("--model", "opus"), {"claude-opus-5-5": usage}, observed("different_name_unverified")),
                 (("--model", "claude-opus-5-5"), {"claude-opus-5-5": usage}, observed("same_literal")),
                 ((), {"claude-opus-5-5": usage}, observed("not_requested")),
                 # Several names do not say which model answered; nothing is inferred.
                 (("--model", "opus"), {"claude-opus-5-5": usage, "claude-haiku-4-5": usage}, unobserved),
                 (("--model", "opus"), {}, unobserved),
                 (("--model", "opus"), {"claude-opus-5-5\n": usage}, unobserved))
        for extra, models, observation in cases:
            with self.subTest(extra=extra, models=list(models)):
                self.configure(native={"modelUsage": models})
                code, result, _ = self.invoke(*extra)
                self.assertEqual((0, "returned"), (code, result["state"]))
                self.assertEqual(observation, result["model_observation"])
                self.assertEqual("unknown", result["effective_effort"])
                # The sanitized report keeps the relation alone, never a model name.
                call = peer._report_projection(result)
                self.assertEqual(observation["relation"], call["model_relation"])
                self.assertNotIn("claude-", json.dumps(call))

    def test_claude_effort_choices_are_unchanged(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                code, dry, _ = self.invoke("--effort", effort, "--dry-run")
                self.assertEqual(0, code)
                self.assertEqual(["--effort", effort], dry["argv"][-4:-2])
        for effort in ("ultra", "HIGH", "--permission-mode"):
            with self.subTest(effort=effort):
                self.assert_argument_rejected("--effort=" + effort)
        self.assertFalse(self.calls.exists())

    def test_model_name_cannot_become_a_native_option(self):
        self.assert_argument_rejected("--model=--permission-mode")
        self.assertFalse(self.calls.exists())

    def test_rejected_argument_tests_fail_before_execution_when_guards_regress(self):
        cases = (("_CLAUDE_EFFORTS", (*peer._CLAUDE_EFFORTS, "ultra"), "--effort=ultra"),
                 ("_model_selection", lambda value: value, "--model=--permission-mode"))
        for guard, replacement, argument in cases:
            with (self.subTest(guard=guard), mock.patch.object(peer, guard, replacement),
                  mock.patch.object(peer.subprocess, "Popen") as native,
                  self.assertRaisesRegex(AssertionError,
                                         "rejected argument reached launch preparation")):
                self.assert_argument_rejected(argument)
            native.assert_not_called()
            self.assertFalse(self.calls.exists())
            self.assertFalse(self.receipt.exists())
            self.assertFalse((self.base / "relay-argv.json").exists())
            self.assertFalse(list(self.base.glob("evidence-*")))

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
        code, result, errors = self.invoke("--dry-run", output=evidence)
        self.assertEqual(0, code)
        self.assertEqual("call_prepared", result["state"])
        self.assertFalse(result["provider_started"])
        self.assertNotIn("stdout_observation", result)
        self.assertNotIn("stdout_observation_error", result)
        self.assertFalse(self.calls.exists())
        self.assertFalse(evidence.exists())
        # A prepared call is not a green light: it says readiness went unchecked, and how to check it.
        check = [str(self.relay), "setup", "--repo", str(self.repo), "--check"]
        self.assertEqual(("not_checked", check), (result["readiness"], result["readiness_check"]))
        self.assertEqual("multithread peer: dry run only: the task and invocation are valid, but readiness was "
                         "not checked. Check it with: " + shlex.join(check) + "\n", errors)
        code, result, _ = self.invoke(output=self.base / "real-evidence")
        self.assertEqual((0, "returned"), (code, result["state"]))
        self.assertNotIn("readiness", result)
        self.assertNotIn("readiness_check", result)

    def test_retired_launcher_flag_still_prepares_without_provider_or_evidence_writes(self):
        evidence = self.base / "legacy-dry-evidence"
        code, result, errors = self.invoke("--dry-run", output=evidence, launcher_flag="--relay")
        self.assertEqual(0, code)
        self.assertTrue(errors.startswith("multithread peer: warning: --relay is deprecated; use --multithread. "
                                          "--relay still works in this release and will be removed in a later "
                                          "one.\n"), errors)
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

        def interrupted_stop(child, **options):
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


    @staticmethod
    def images():
        """Small structurally complete images of each accepted type, built here (artificial content)."""
        import struct, zlib
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
        gif = b"GIF89a" + struct.pack("<HH", 1, 1) + b"\x00\x00\x00" + b"\x2c" + b"\x00" * 9 + b"\x02\x02D\x01\x00\x3b"
        webp_body = b"WEBP" + b"VP8L" + struct.pack("<I", 5) + b"\x2f\x00\x00\x00\x00\x00"
        webp = b"RIFF" + struct.pack("<I", len(webp_body)) + webp_body
        jpeg = (b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 11) + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
                + b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x00\x00" + b"\xff\xd9")
        return {"image/png": png, "image/gif": gif, "image/webp": webp, "image/jpeg": jpeg}

    def test_tools_and_attachments_run_restricted_in_stream_mode_without_hook_settings(self):
        image = self.base / "card.png"
        image.write_bytes(self.images()["image/png"])
        self.configure_hook(shlex.join([str(self.relay), "--repo", str(self.repo), "provider-hook", "--client", "claude"]))
        code, dry, _ = self.invoke("--dry-run")
        self.assertIn("--settings", dry["argv"])
        code, dry, _ = self.invoke("--tools", "none", "--attach", str(image), "--dry-run")
        self.assertEqual(0, code)
        argv = dry["argv"]
        self.assertNotIn("--settings", argv)
        self.assertEqual(["--tools", "", "--restricted", "--strict-mcp-config", "--disable-slash-commands"],
                         argv[argv.index("--tools"):argv.index("--tools") + 5])
        self.assertEqual("stream-json", argv[argv.index("--input-format") + 1])
        self.assertEqual({"requested": [], "reported": None, "mcp_servers": None, "source": "not_observed"},
                         dry["provider_tools"])
        self.assertEqual([{"path": str(image), "media_type": "image/png", "bytes": image.stat().st_size,
                           "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}], dry["attachments"])
        code, dry, _ = self.invoke("--tools", "Read,Glob", "--dry-run")
        self.assertEqual("Glob,Read", dry["argv"][dry["argv"].index("--tools") + 1])
        self.assertFalse(self.calls.exists())

    def test_managed_settings_refuse_a_restricted_call_before_launch(self):
        machine, home, policy = self.base / "machine", self.base / "claude-home", self.base / "ClaudeCode"
        home.mkdir()
        reg = self.base / "reg.exe"
        answers = self.base / "reg-answers.json"
        # An answer is [exit, stdout(, seconds)], or a list of them given one per attempt (the last repeats).
        reg.write_text(f"#!{sys.executable}\nimport json,sys,time\nA={str(answers)!r}\na=json.load(open(A))[sys.argv[2]]\n"
                       "if isinstance(a[0], list):\n"
                       "    c=json.load(open(A+'.n')) if __import__('os').path.exists(A+'.n') else {}\n"
                       "    n=c.get(sys.argv[2], 0); c[sys.argv[2]]=n+1; json.dump(c, open(A+'.n','w')); a=a[min(n, len(a)-1)]\n"
                       "time.sleep(a[2] if len(a) > 2 else 0); sys.stdout.write(a[1]); sys.exit(a[0])\n")
        reg.chmod(0o700)
        hklm, hkcu = peer._WSL_POLICY_PARENTS
        listing = {hklm: [0, "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\Microsoft\r\n"],
                   hkcu: [0, "\r\nHKEY_CURRENT_USER\\SOFTWARE\\Policies\\Microsoft\r\n"]}
        def sources(*, wsl=True, keys=None, environ=None, cwd=None):
            answers.write_text(json.dumps(keys or listing))
            Path(str(answers) + ".n").unlink(missing_ok=True)
            with (mock.patch.object(peer, "_MANAGED_CLAUDE_SETTINGS", {peer.platform.system(): machine}),
                  mock.patch.object(peer, "_WSL_CLAUDE_POLICY", policy), mock.patch.object(peer, "_WSL_REG", reg),
                  mock.patch.object(peer, "_is_wsl", return_value=wsl), mock.patch.object(peer, "_REG_BACKOFF", (0, 0, 0)),
                  mock.patch.object(peer, "_REG_TIMEOUT", 0.5),
                  mock.patch.object(peer, "_POLICY_CACHE", self.base / "policy-read.json"),
                  mock.patch.object(peer, "_POLICY_CACHE_SECONDS", -1),  # every case here reads afresh
                  mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1),
                  mock.patch.object(peer, "_POLICY_STALE_SECONDS", -1),
                  mock.patch.dict(os.environ, environ or {"CLAUDE_CONFIG_DIR": str(home)})):
                return peer._managed_claude_sources(cwd or self.repo)
        self.assertEqual([], sources())
        machine.mkdir()
        self.assertEqual([str(machine)], sources())
        machine.rmdir()
        for name in ("remote-settings.json", "policy-limits.json"):
            (home / name).write_text("{}")
            self.assertEqual([str(home / name)], sources())
            (home / name).unlink()
        # A relative directory is Claude's, resolved from where Claude starts; names are NFC as Claude reads them.
        (self.repo / "cfg\u00e9").mkdir()
        (self.repo / "cfg\u00e9" / "remote-settings.json").write_text("{}")
        self.assertEqual([str(self.repo / "cfg\u00e9" / "remote-settings.json")],
                         sources(environ={"CLAUDE_CONFIG_DIR": "cfge\u0301"}))
        with self.assertRaises(peer.LaunchError):
            sources(environ={"CLAUDE_CONFIG_DIR": ""})
        policy.mkdir()
        self.assertEqual([str(policy)], sources())
        self.assertEqual([], sources(wsl=False), "the Windows chain applies only under WSL")
        policy.rmdir()
        present = dict(listing, **{hklm: [0, "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Policies\\ClaudeCode\r\n"]})
        self.assertEqual([hklm + "\\ClaudeCode"], sources(keys=present))
        lower = dict(listing, **{hkcu: [0, "\r\nHKEY_CURRENT_USER\\Software\\Policies\\claudecode\r\n"]})
        self.assertEqual([hkcu + "\\ClaudeCode"], sources(keys=lower))
        for name, answer, why in (("access denied", [1, "Zugriff verweigert."], "exit 1"),
                                  ("unexpected status", [2, ""], "exit 2"),
                                  ("timed out", [0, "", 2], "timed out")):
            with self.subTest(registry=name):
                self.assertEqual([hkcu + f"\\ClaudeCode (unreadable after 4 attempts: {why})"],
                                 sources(keys=dict(listing, **{hkcu: answer})))
        # WSL's bridge to Windows fails a call now and then (a live training step was refused by one); a read
        # that recovers on a retry decides alone, and a policy key found on the retry still refuses.
        # A success that lists nothing recognizable is a failed read, retried; it refuses if it never recovers.
        for garbled in ([0, "\r\nERROR: something else\r\n"], [0, "\r\nHKEY_LOCAL_MACHINE\\SOFTWARE\\Other\r\n"],
                        [0, "\r\n    garbage\r\n"], [0, "\r\n    HKEY_CURRENT_USER\\SOFTWARE\\Policies\\ClaudeCode\r\n"]):
            with self.subTest(listing=garbled[1]):
                self.assertEqual([hkcu + "\\ClaudeCode (unreadable after 4 attempts: no recognizable listing)"],
                                 sources(keys=dict(listing, **{hkcu: garbled})))
        self.assertEqual([], sources(keys=dict(listing, **{hkcu: [[0, ""], listing[hkcu]]})))
        # The parent's own header (it has values) is a recognizable listing, and an answer of no subkeys given by
        # every attempt is an empty key, not a glitch: neither refuses forever.
        header = [0, "\r\nHKEY_CURRENT_USER\\SOFTWARE\\Policies\r\n    Setting    REG_SZ    1\r\n"]
        self.assertEqual([], sources(keys=dict(listing, **{hkcu: header})))
        self.assertEqual([], sources(keys=dict(listing, **{hkcu: [0, "\r\n"]})))
        transient = [[1, ""], listing[hkcu]]
        self.assertEqual([], sources(keys=dict(listing, **{hkcu: transient})))
        found_late = [[1, ""], [1, ""], [1, ""], [0, "\r\nHKEY_CURRENT_USER\\SOFTWARE\\Policies\\ClaudeCode\r\n"]]
        self.assertEqual([hkcu + "\\ClaudeCode"], sources(keys=dict(listing, **{hkcu: found_late})))
        reg.unlink()
        self.assertEqual([], sources(), "without reg.exe Claude cannot read the registry either")
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}):
            self.assertTrue(peer._is_wsl())
        with mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"]) as found:
            code, result, _ = self.invoke("--tools", "none")
            self.assertNotEqual(0, code)
            self.assertEqual(("unavailable", False), (result["state"], result["provider_started"]))
            self.assertIn("Managed Claude settings were found (/etc/claude-code)", result["message"])
            code, _, _ = self.invoke("--dry-run")
            self.assertEqual(0, code, "an unrestricted call is unaffected")
            self.assertEqual(1, found.call_count)
        self.assertFalse(self.calls.exists())

    def test_a_refusal_record_never_follows_a_directory_swapped_after_creation(self):
        other = self.base / "another-call"
        other.mkdir()
        (other / "result.json").write_text("another call")
        record = self.base / "refused-call"
        real_mkdir = Path.mkdir
        def swap(path, *arguments, **options):
            real_mkdir(path, *arguments, **options)
            if path == record:  # an actor controlling the parent replaces it before the record is written
                path.rmdir()
                path.symlink_to(other, target_is_directory=True)
        with (mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"]),
              mock.patch.object(Path, "mkdir", swap)):
            code, result, _ = self.invoke("--tools", "none", output=record)
        self.assertNotEqual(0, code)
        self.assertEqual("another call", (other / "result.json").read_text())
        self.assertIsNone(result["evidence_directory"])

    def test_a_refusal_record_never_replaces_one_already_there(self):
        record = self.base / "raced-call"
        real_listdir = os.listdir
        def raced(target=None):
            names = real_listdir(target)
            if isinstance(target, int):  # another writer publishes after the emptiness check
                with open(os.path.join(record, "result.json"), "x") as stream:
                    stream.write("the other writer")
                return names
            return names
        with (mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"]),
              mock.patch.object(peer.os, "listdir", raced)):
            self.invoke("--tools", "none", output=record)
        self.assertEqual("the other writer", (record / "result.json").read_text())
        self.assertEqual(["result.json"], sorted(path.name for path in record.iterdir()), "no temporary left behind")

    def test_a_late_fault_drops_a_stored_partial_and_kills_what_outlives_the_leader(self):
        from relay_runtime import claude_peer
        # The leader exits and is reaped while a descendant keeps running in its group: a restricted fault kills the
        # descendant at once, with no shutdown grace.
        process = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; exit 0"], start_new_session=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        descendant = int(process.stdout.readline())
        process.stdout.close()
        process.wait()
        os.killpg(process.pid, 0)  # the descendant still holds the group
        def running():  # a killed process awaiting its reaper is a zombie, not a survivor
            try:
                stat = Path(f"/proc/{descendant}/stat").read_text()
            except FileNotFoundError:
                return False
            return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        envelope = {"partial_result": "words written before the fault"}
        driver = claude_peer._Driver(process, b"task", str(self.repo), None, envelope, 1, None, tools=[])
        driver.results = [{"result_excerpt": "THE ANSWER", "result_excerpt_truncated": False}]
        self.assertIsInstance(driver.fault("outside the registry"), claude_peer.ProtocolError)
        driver.problem("outside the registry")  # every fault of a restricted call ends here
        self.assertNotIn("partial_result", envelope)
        self.assertIsNone(driver.results[0]["result_excerpt"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and running():
            time.sleep(0.02)
        if running():
            os.kill(descendant, signal.SIGKILL)
            self.fail("the descendant outlived the restricted fault")

    def test_parallel_calls_read_the_windows_policy_once_and_one_at_a_time(self):
        # WSL's interop drops some launches made in a burst (each fails after 10 s), so parallel restricted calls
        # once refused together; now one reads while the rest wait, and they reuse its clean read.
        import threading
        reg, log, answer = self.base / "reg.exe", self.base / "reg.log", self.base / "reg-exit"
        answer.write_text("0")
        reg.write_text(f"#!{sys.executable}\nimport os,sys,time\nL={str(log)!r}\n"
                       "def mark(w):\n    fd=os.open(L,os.O_WRONLY|os.O_APPEND|os.O_CREAT);os.write(fd,f'{w} {time.monotonic()}\\n'.encode());os.close(fd)\n"
                       f"mark('start');time.sleep(0.1);mark('end');code=int(open({str(answer)!r}).read())\n"
                       "sys.stdout.write('' if code else '\\r\\n'+{'HKLM':'HKEY_LOCAL_MACHINE','HKCU':'HKEY_CURRENT_USER'}[sys.argv[2][:4]]+sys.argv[2][4:]+'\\\\Microsoft\\r\\n');sys.exit(code)\n")
        reg.chmod(0o700)
        cache = self.base / "state" / "policy-read.json"
        def reads():
            return [line.split()[0] for line in log.read_text().splitlines()].count("start") if log.exists() else 0
        with (mock.patch.object(peer, "_MANAGED_CLAUDE_SETTINGS", {}), mock.patch.object(peer, "_WSL_REG", reg),
              mock.patch.object(peer, "_WSL_CLAUDE_POLICY", self.base / "no-policy"),
              mock.patch.object(peer, "_is_wsl", return_value=True), mock.patch.object(peer, "_REG_BACKOFF", (0, 0, 0)),
              mock.patch.object(peer, "_POLICY_CACHE", cache),
              mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.base / "claude-home")})):
            results = []
            threads = [threading.Thread(target=lambda: results.append(peer._managed_claude_sources(self.repo)))
                       for _ in range(6)]
            [thread.start() for thread in threads]
            [thread.join() for thread in threads]
            self.assertEqual([[]] * 6, results)
            self.assertEqual(2, reads(), "one read of each parent serves every waiting call")
            depth = peak = 0
            for line in sorted(log.read_text().splitlines(), key=lambda line: float(line.split()[1])):
                depth += 1 if line.startswith("start") else -1
                peak = max(peak, depth)
            self.assertEqual(1, peak, "never two launches at once")
            self.assertEqual(0o600, cache.stat().st_mode & 0o777)
            # A stale, foreign-boot or loosely permissioned record is not reused.
            aged = lambda value, by: {**value, "parents": {parent: {**entry, "at": entry["at"] - by, "wall": entry["wall"] - by}
                                                           for parent, entry in value["parents"].items()}}
            for name, change in (("stale", lambda value: aged(value, 31)),
                                 ("another boot", lambda value: {**value, "boot": "another"}),
                                 ("another reg.exe", lambda value: {**value, "reg": "/elsewhere/reg.exe"}),
                                 ("readable by others", None)):
                with self.subTest(cache=name):
                    before = reads()
                    if change is None:
                        cache.chmod(0o644)
                    else:
                        cache.write_text(json.dumps(change(json.loads(cache.read_text()))))
                    self.assertEqual([], peer._managed_claude_sources(self.repo))
                    self.assertEqual(before + 2, reads())
            # The record is dated by when its first query began.
            cache.unlink()
            before = reads()
            boot = peer._clock()[0]
            with mock.patch.object(peer, "_clock", side_effect=lambda: (boot, 100.0 + reads(), 200.0 + reads())):
                self.assertEqual([], peer._managed_claude_sources(self.repo))
            self.assertEqual(100.0 + before, json.loads(cache.read_text())["parents"]["HKLM\\SOFTWARE\\Policies"]["at"])
            # A failed read refuses, and the calls queued behind it refuse with it rather than each retrying in turn;
            # once it is a few seconds old the next call reads again.
            cache.unlink()
            answer.write_text("1")
            before = reads()
            self.assertEqual(2, len(peer._managed_claude_sources(self.repo)))
            answer.write_text("0")
            shared = peer._managed_claude_sources(self.repo)
            self.assertEqual(2, len(shared))
            self.assertIn("read by another call", shared[0])
            self.assertEqual(before + 8, reads())
            with mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1):
                self.assertEqual([], peer._managed_claude_sources(self.repo))
            self.assertEqual(before + 8 + 2, reads())
            # A malformed read is never used, nor a failure that names no failed parent.
            for name, forge in (("lines not a list", lambda value: {**value, "parents": {
                                    **value["parents"], "HKLM\\SOFTWARE\\Policies": {**value["parents"]["HKLM\\SOFTWARE\\Policies"], "lines": None}}}),
                                ("failure without a failed parent", lambda value: {**aged(value, 31), "failed": {
                                    "at": value["parents"]["HKLM\\SOFTWARE\\Policies"]["at"], "wall": value["parents"]["HKLM\\SOFTWARE\\Policies"]["wall"],
                                    "reads": {parent: [["x"], None] for parent in peer._WSL_POLICY_PARENTS}}})):
                with self.subTest(record=name):
                    cache.write_text(json.dumps(forge(json.loads(cache.read_text()))))
                    before = reads()
                    self.assertEqual([], peer._managed_claude_sources(self.repo))
                    self.assertEqual(before + 2, reads())
            # A reader that never finishes leaves the others refusing, not reading alongside it.
            import fcntl
            holder = os.open(cache.with_suffix(".lock"), os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                with mock.patch.object(peer, "_POLICY_LOCK_SECONDS", 0.2):
                    cache.unlink()
                    found = peer._managed_claude_sources(self.repo)
            finally:
                os.close(holder)
            self.assertEqual(2, len(found))
            self.assertIn("another policy read did not finish", found[0])

    def test_a_parents_last_read_stands_in_only_when_interop_failed_it_and_says_so(self):
        # A WSL interop outage fails every fresh read. A parent's last successful read of this boot stands in,
        # bounded by both clocks and visible; it never hides a key, and nothing else that fails is excused.
        reg, answers = self.base / "reg.exe", self.base / "reg-answers.json"
        reg.write_text(f"#!{sys.executable}\nimport json,sys\na=json.load(open({str(answers)!r}))[sys.argv[2][:4]]\n"
                       "full={'HKLM':'HKEY_LOCAL_MACHINE','HKCU':'HKEY_CURRENT_USER'}[sys.argv[2][:4]]+sys.argv[2][4:]\n"
                       "if a=='vsock': sys.stderr.write('<3>WSL (1 - ) ERROR: UtilAcceptVsock:271: accept4 failed 110\\n'); sys.exit(1)\n"
                       "if a=='denied': sys.exit(1)\n"
                       "sys.stdout.write(''.join('\\r\\n'+full+'\\\\'+k for k in a)+'\\r\\n')\n")
        reg.chmod(0o700)
        cache = self.base / "state" / "policy-read.json"
        hklm, hkcu = peer._WSL_POLICY_PARENTS
        real = peer._clock
        offset = {"boot": 0.0, "wall": 0.0}
        def answer(first, second):
            answers.write_text(json.dumps({"HKLM": first, "HKCU": second}))
        def sources(**later):
            offset.update({"boot": 0.0, "wall": 0.0, **later})
            reused = []
            return peer._managed_claude_sources(self.repo, reused), reused
        ok, dropped = ["Microsoft"], "vsock"
        with (mock.patch.object(peer, "_MANAGED_CLAUDE_SETTINGS", {}), mock.patch.object(peer, "_WSL_REG", reg),
              mock.patch.object(peer, "_WSL_CLAUDE_POLICY", self.base / "no-policy"),
              mock.patch.object(peer, "_is_wsl", return_value=True), mock.patch.object(peer, "_REG_BACKOFF", (0, 0, 0)),
              mock.patch.object(peer, "_POLICY_CACHE", cache), mock.patch.object(peer, "_POLICY_CACHE_SECONDS", -1),
              mock.patch.object(peer, "_clock", side_effect=lambda: (lambda b, t, w: (b, t + offset["boot"], w + offset["wall"]))(*real())),
              mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.base / "claude-home")})):
            with mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1):
                answer(dropped, dropped)
                found, reused = sources()
                self.assertEqual((2, []), (len(found), reused), "with no successful read this boot, a failure refuses")
                answer(ok, ok)
                self.assertEqual(([], []), sources())
                answer(dropped, dropped)
                found, reused = sources()
                self.assertEqual([], found)
                self.assertRegex(reused[0]["read_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertIn(f"{hkcu}: interop dropped the launch", reused[0]["because"])
                # Six hours by both clocks: a host that slept stops the boot clock, not the wall clock.
                for name, later, admitted in (("just inside", {"boot": 6 * 3600 - 5, "wall": 6 * 3600 - 5}, True),
                                              ("just past", {"boot": 6 * 3600 + 5, "wall": 6 * 3600 + 5}, False),
                                              ("host slept", {"boot": 10, "wall": 6 * 3600 + 5}, False),
                                              ("clock ran back", {"wall": -60}, False)):
                    with self.subTest(age=name):
                        found, reused = sources(**later)
                        self.assertEqual((admitted, admitted), (found == [], bool(reused)))
                # Only interop is excused: a read reg.exe itself fails refuses, a recent success notwithstanding.
                answer("denied", "denied")
                found, reused = sources()
                self.assertEqual((2, []), (len(found), reused))
                # A key found fresh refuses, and is remembered: a later outage cannot erase it with an older absence.
                answer(["ClaudeCode"], dropped)
                found, reused = sources()
                self.assertEqual([f"{hklm}\\ClaudeCode"], found)
                answer(dropped, dropped)
                found, reused = sources()
                self.assertEqual([f"{hklm}\\ClaudeCode"], found, "the last successful read found the key")
                # An empty listing is too easily a glitch to stand in.
                cache.unlink()
                answer([], [])
                self.assertEqual(([], []), sources())
                answer(dropped, dropped)
                self.assertEqual(2, len(sources()[0]))
            # Successes from different reads never combine past a failure between them into a reused clean read.
            with mock.patch.object(peer, "_POLICY_CACHE_SECONDS", 30), mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1):
                cache.unlink()
                answer(ok, ok)
                sources()
                answer("denied", ok)
                self.assertEqual(1, len(sources(boot=31, wall=31)[0]))
                answer(ok, "denied")
                self.assertEqual(1, len(sources(boot=37, wall=37)[0]))
                found, reused = sources(boot=38, wall=38)
                self.assertEqual((1, []), (len(found), reused), "the latest read failed, so this call reads again")
            # A read that cannot be recorded leaves no older record to stand in for what it found.
            with mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1):
                cache.unlink()
                answer(ok, ok)
                sources()
                answer(["ClaudeCode"], ok)
                with mock.patch.object(peer, "_atomic_record", side_effect=OSError("artificial full disk")):
                    self.assertEqual([f"{hklm}\\ClaudeCode"], sources()[0])
                self.assertFalse(cache.exists())
                answer(dropped, dropped)
                self.assertEqual((2, []), (len(sources()[0]), sources()[1]))
            # A record with a malformed failure is not used at all, its successes included.
            with mock.patch.object(peer, "_POLICY_CACHE_SECONDS", 30):
                cache.unlink()
                answer(ok, ok)
                sources()
                value = json.loads(cache.read_text())
                cache.write_text(json.dumps({**value, "failed": {"at": 1.0, "wall": 1.0, "reads": {hklm: [None, "timed out"]}}}))
                answer("denied", "denied")
                self.assertEqual(2, len(sources()[0]), "the malformed record was not reused")
            # A call that cannot take the lock reads alone and discards the record rather than leave it standing.
            with mock.patch.object(peer, "_POLICY_FAILURE_SECONDS", -1):
                cache.unlink()
                answer(ok, ok)
                sources()
                real_open = os.open
                def no_lock(path, *arguments, **options):
                    if str(path).endswith(".lock"):
                        raise PermissionError("artificial")
                    return real_open(path, *arguments, **options)
                answer(["ClaudeCode"], ok)
                with mock.patch.object(peer.os, "open", side_effect=no_lock):
                    self.assertEqual([f"{hklm}\\ClaudeCode"], sources()[0])
                self.assertFalse(cache.exists())
            # The calls queued behind a failure get the same stand-in.
            cache.unlink(missing_ok=True)
            answer(ok, ok)
            sources()
            answer(dropped, dropped)
            first, second = sources(), sources()
            self.assertEqual([], second[0])
            self.assertIn("read by another call", second[1][0]["because"])
            # A reader that never finishes leaves the others refusing: no read failed, so nothing stands in.
            import fcntl
            holder = os.open(cache.with_suffix(".lock"), os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                with mock.patch.object(peer, "_POLICY_LOCK_SECONDS", 0.2):
                    found, reused = sources()
            finally:
                os.close(holder)
            self.assertEqual((2, []), (len(found), reused))
            self.assertIn("another policy read did not finish", found[0])
        # The call's record and its display name the stand-in.
        stand_in = {"read_at": "2026-10-08T01:12:00Z", "age_seconds": 9600, "because": f"{hkcu}: timed out"}
        def preflight(cwd, reused=None):
            reused.append(stand_in)
            return []
        with mock.patch.object(peer, "_managed_claude_sources", side_effect=preflight):
            code, result, _ = self.invoke("--tools", "none", "--dry-run")
            self.assertEqual((0, stand_in), (code, result["windows_policy_reused"]))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                peer._display_peer({**result, "state": "returned", "result": "ok"})
        self.assertIn("Windows policy: reused the last successful read, from 2026-10-08T01:12:00Z (9600 s old), "
                      "because interop failed", output.getvalue())
        self.assertIsNone(self.invoke("--dry-run")[1]["windows_policy_reused"], "every record carries the field")

    def test_the_cli_keywords_for_every_tool_are_not_tool_names(self):
        for word in ("default", "all", "Read,ALL"):
            with self.subTest(tools=word), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                peer.peer_main(["claude", "--task-file", str(self.task), "--tools", word])
        self.assertFalse(self.calls.exists())

    def test_a_call_refused_before_it_starts_still_leaves_its_record(self):
        record = self.base / "refused-call"
        with mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"]):
            code, result, _ = self.invoke("--tools", "none", output=record)
        self.assertNotEqual(0, code)
        self.assertEqual(result, json.loads((record / "result.json").read_text()))
        self.assertEqual((str(record), False), (result["evidence_directory"], result["provider_started"]))
        self.assertIn(f"This refusal's record is in {record}; a retry needs a new --output-dir.", result["message"])
        self.assertEqual(["result.json"], sorted(path.name for path in record.iterdir()))
        # Another call's directory is never reused, even for a refusal.
        (record / "result.json").write_text("another call")
        with mock.patch.object(peer, "_managed_claude_sources", return_value=["/etc/claude-code"]):
            self.invoke("--tools", "none", output=record)
        self.assertEqual("another call", (record / "result.json").read_text())

    def test_attachments_are_typed_images_within_their_bounds(self):
        for media_type, body in self.images().items():
            with self.subTest(accepted=media_type):
                path = self.base / ("ok." + media_type.split("/")[1])
                path.write_bytes(body)
                code, dry, _ = self.invoke("--attach", str(path), "--dry-run")
                self.assertEqual((0, media_type), (code, dry["attachments"][0]["media_type"]))
        text = self.base / "notes.txt"
        text.write_text("not an image")
        link = self.base / "link.png"
        link.symlink_to(self.base / "ok.png")
        # A type check, not a decoder: legal JPEG fill bytes pass; signature-only or mislabelled files do not.
        padded = self.base / "padded.jpg"
        padded.write_bytes(b"\xff\xd8\xff" + self.images()["image/jpeg"][2:])
        self.assertEqual("image/jpeg", self.invoke("--attach", str(padded), "--dry-run")[1]["attachments"][0]["media_type"])
        forged = {"signature only": b"\x89PNG\r\n\x1a\n", "webp then text": b"RIFF0000WEBP then text"}
        cases = [(text, "is not a PNG, JPEG, GIF or WebP image"), (link, "could not be opened"),
                 (self.base / "absent.png", "could not be opened")]
        for name, body in forged.items():
            path = self.base / (name.replace(" ", "-") + ".img")
            path.write_bytes(body)
            cases.append((path, "is not a PNG, JPEG, GIF or WebP image"))
        for path, why in cases:
            with self.subTest(refused=path.name):
                code, result, _ = self.invoke("--attach", str(path))
                self.assertNotEqual(0, code)
                self.assertEqual(("unavailable", False), (result["state"], result["provider_started"]))
                self.assertIn(why, result["message"])
        png = self.base / "ok.png"
        code, result, _ = self.invoke(*[argument for _ in range(peer._MAX_ATTACHMENTS + 1) for argument in ("--attach", str(png))])
        self.assertIn(f"At most {peer._MAX_ATTACHMENTS} images", result["message"])
        with mock.patch.object(peer, "_MAX_ATTACHMENT", 16):
            self.assertIn("exceeds", self.invoke("--attach", str(png))[1]["message"])
        with mock.patch.object(peer, "_MAX_ATTACHMENTS_TOTAL", png.stat().st_size + 1):
            self.assertIn("in total", self.invoke("--attach", str(png), "--attach", str(png))[1]["message"])
        self.assertFalse(self.calls.exists())
        for tools in ("Read,Read", "Agent", "Read,Task"):
            with self.subTest(tools=tools), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                peer.peer_main(["claude", "--task-file", str(self.task), "--tools", tools])

class RepositoryRoutingTests(unittest.TestCase):
    def test_conflicting_selections_refuse_before_alias_or_native_work(self):
        cases = (
            ["peer", "claude", "--repo", "/srv/other", "--task-file", "task.txt"],
            ["peer", "claude", "--rep=/srv/other", "--task-file", "task.txt"],
            ["peer", "claude", "--repo", "-1", "--task-file", "task.txt"],
            ["peer", "packet", "--repo=/srv/other", "--path", "src/file.py"],
            ["launch", "codex", "--repo", "/srv/other"],
            ["setup", "--repo", "/srv/other", "--apply"],
            ["update", "--enroll-repo=/srv/other", "--yes"],
            ["update", "--enroll", "/srv/other", "--yes"],
            ["bind", "reviewer", "--repo", "/srv/other"],
            ["wake", "reviewer", "--repo", "/srv/other", "--ref", "1"],
            ["agent", "muse", "send", "Buddy", "packet.json", "--r", "/srv/other"],
        )
        for suffix in cases:
            with (self.subTest(suffix=suffix), redirect_stderr(io.StringIO()) as error,
                  mock.patch.object(cli, "Admission") as admission,
                  mock.patch.object(peer, "peer_main") as helper,
                  mock.patch.object(peer.subprocess, "Popen") as native,
                  mock.patch.object(peer.subprocess, "run") as config):
                alias = mock.Mock(side_effect=AssertionError("routing refusal reached alias/helper"))
                code = cli.main(["--repo", "/srv/checkout", *suffix], command_alias_check=alias)
            self.assertEqual(64, code)
            self.assertIn("conflicting repository selections", error.getvalue())
            alias.assert_not_called()
            admission.assert_not_called()
            helper.assert_not_called()
            native.assert_not_called()
            config.assert_not_called()

    def test_repeated_native_conflict_also_refuses_without_global_selection(self):
        with (redirect_stderr(io.StringIO()), mock.patch.object(peer, "peer_main") as helper):
            code = cli.main(["peer", "claude", "--repo", "/srv/checkout", "--repo=/srv/other"])
        self.assertEqual(64, code)
        helper.assert_not_called()

    def test_repeated_global_conflict_refuses_before_it_can_be_collapsed(self):
        for first in (["--repo", "/srv/checkout"], ["--rep=/srv/checkout"], ["--r", "/srv/checkout"],
                      ["--re=/srv/checkout"], ["--j", "--repo", "/srv/checkout"]):
            with (self.subTest(first=first), redirect_stderr(io.StringIO()),
                  mock.patch.object(peer, "peer_main") as helper):
                code = cli.main([*first, "--repo=/srv/other", "peer", "claude"])
            self.assertEqual(64, code)
            helper.assert_not_called()

    def test_equal_or_single_selections_preserve_the_helper_spelling(self):
        absolute = str(Path("checkout").absolute())
        for global_args, selected in ((["--repo", absolute], ["--rep=checkout"]),
                                      (["--repo", "/srv/checkout"], ["--repo=/srv/checkout"]),
                                      ([], ["--repo", "/srv/checkout"])):
            suffix = ["claude", *selected, "--task-file", "task.txt"]
            with (self.subTest(selected=selected), mock.patch.object(peer, "peer_main", return_value=7) as helper,
                  mock.patch.object(Path, "resolve", side_effect=AssertionError("resolved enrollment path"))):
                self.assertEqual(7, cli.main([*global_args, "peer", *suffix]))
            helper.assert_called_once_with(suffix)

    def test_distinct_symlink_or_parent_spellings_do_not_collapse(self):
        for selected in ("/srv/checkout/../checkout", "/srv/checkout-alias"):
            with (self.subTest(selected=selected), redirect_stderr(io.StringIO()),
                  mock.patch.object(Path, "resolve", side_effect=AssertionError("resolved enrollment path")),
                  mock.patch.object(peer, "peer_main") as helper):
                self.assertEqual(64, cli.main(["--repo", "/srv/checkout", "peer", "claude", "--repo", selected]))
            helper.assert_not_called()

    def test_inherited_options_precede_separator_and_do_not_scan_positional_payload(self):
        suffix = ["muse", "prepare", "Buddy", "--", "--repo=/srv/payload"]
        with mock.patch("relay_runtime.agent.agent_main", return_value=7) as helper:
            self.assertEqual(7, cli.main(["--repo", "/srv/checkout", "agent", *suffix]))
        helper.assert_called_once_with(["muse", "prepare", "Buddy", "--repo", "/srv/checkout",
                                      "--", "--repo=/srv/payload"])

    def test_receipt_and_account_helpers_do_not_inherit_checkout_authority(self):
        cases = (("peer", ["report", "--call-dir", "/srv/receipt"], "relay_runtime.provider.peer_main"),
                 ("peer", ["control", "status", "--call-dir", "/srv/receipt"], "relay_runtime.provider.peer_main"),
                 ("agent", ["muse", "list"], "relay_runtime.agent.agent_main"),
                 ("hooks", ["check"], "relay_runtime.hooks.hooks_main"))
        for command, suffix, target in cases:
            with self.subTest(command=command, suffix=suffix), mock.patch(target, return_value=7) as helper:
                self.assertEqual(7, cli.main(["--repo", "/srv/checkout", command, *suffix]))
            helper.assert_called_once_with(suffix)


if __name__ == "__main__":
    unittest.main()
