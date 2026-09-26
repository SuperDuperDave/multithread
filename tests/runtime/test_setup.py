"""Readiness composition and partial-success boundaries; no provider execution."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from relay_runtime import setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-setup-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 'quote' 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.account = self.base / "account"
        self.launcher = str(self.account / ".local/bin/multithread")
        self.commands = []
        self.responses = {}
        self.runtime = {"installed": True, "launcher": self.launcher, "preferred_command_available": True,
                        "activation": {"activation_id": "a" * 32, "release_id": "b" * 64}}
        self.doctor = {"ok": True, "integrity": "ok", "repo_root": str(self.repo),
                       "git_common_dir": str(self.repo / ".git"), "database": str(self.repo / "ledger.db")}
        self.status = {"database": self.doctor["database"], "last_seq": 7,
                       "active_claims": [{"resource": "fixture", "owner": "preserve"}],
                       "recent_signals": [{"seq": 7, "kind": "work.handoff"}]}
        for target, value in (("runtime", self.runtime), ("doctor", self.doctor),
                              ("status", self.status), ("init", {"ok": True, "initialized": True})):
            self.responses[target] = (0, json.dumps(value).encode(), b"")
        for patcher in (
            mock.patch.object(setup.pwd, "getpwuid", return_value=mock.Mock(pw_dir=str(self.account))),
            mock.patch.object(setup.subprocess, "run", side_effect=self.run_command),
            mock.patch.object(setup.shutil, "which", return_value=None),
            mock.patch.object(setup.codex_peer, "list_hooks", side_effect=self.list_hooks),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.listings = []
        self.hook_statuses = {}
        self.listing_error = None

    def run_command(self, command, **options):
        self.commands.append(command)
        self.assertEqual(subprocess.DEVNULL, options["stdin"])
        self.assertNotIn("shell", options)
        self.assertNotIn("env", options)
        self.assertNotIn("input", options)
        key = "runtime" if command[1:3] == ["runtime", "status"] else command[-1]
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        code, output, errors = response
        options["stdout"].write(output)
        options["stderr"].write(errors)
        return subprocess.CompletedProcess(command, code)

    def codex_plan(self, path):
        hook = shlex.join([self.launcher, "provider-hook", "--client", "codex"])
        return {"argv": [str(path), "-c", "hooks.fixture=[]"], "repo": str(self.repo),
                "relay_plan": {"hook_command": hook}, "provider_started": False}

    def list_hooks(self, argv, repo, *, on_start=None, timeout=15):
        self.listings.append((argv, repo))
        if isinstance(self.listing_error, OSError) and self.listing_error.errno is None:
            raise self.listing_error  # The executable itself could not start.
        on_start()
        if self.listing_error is not None:
            raise self.listing_error
        hook = shlex.join([self.launcher, "provider-hook", "--client", "codex"])
        return {"data": [{"cwd": repo, "hooks": [
            {"eventName": event, "command": hook, "handlerType": "command", "source": "sessionFlags",
             "enabled": True, "trustStatus": self.hook_statuses.get(event, "trusted"),
             "timeoutSec": 3, "matcher": None, "async": False}
            for event in ("sessionStart", "userPromptSubmit", "stop", "sessionEnd", "interrupt")]}]}

    def invoke(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            code = setup.setup_main(["--repo", str(self.repo), "--json", *extra])
        return code, json.loads(output.getvalue())

    def display(self, report):
        before = deepcopy(report)
        output = io.StringIO()
        with redirect_stdout(output):
            setup._display(report)
        self.assertEqual(before, report, "Human rendering changed the structured report")
        return output.getvalue()

    def test_default_and_check_read_existing_work_without_init_or_provider_execution(self):
        for extra in ((), ("--check",)):
            self.commands.clear()
            with mock.patch.object(setup.provider, "prepare") as prepare:
                code, result = self.invoke(*extra)
            self.assertEqual(0, code)
            self.assertEqual("ready", result["state"])
            self.assertEqual(self.status, result["repository"]["status"]["data"])
            self.assertEqual(["status", "doctor", "status"], [command[-1] for command in self.commands])
            self.assertTrue(all(command[0] == self.launcher for command in self.commands))
            self.assertEqual("missing", result["providers"]["claude"]["state"])
            self.assertFalse(result["provider_started"])
            self.assertEqual("unknown", result["hook_delivery"])
            self.assertEqual("unknown", result["provider_tools"])
            self.assertEqual("unknown", result["provider_authentication"])
            prepare.assert_not_called()

    def test_apply_explicitly_initializes_once_then_reads_and_preserves_work(self):
        code, result = self.invoke("--apply")
        self.assertEqual(0, code)
        self.assertEqual(["status", "init", "doctor", "status"], [command[-1] for command in self.commands])
        self.assertEqual("verified", result["repository"]["enrollment"]["state"])
        self.assertEqual(self.status, result["repository"]["status"]["data"])

    def test_unhealthy_runtime_stops_before_enrollment_or_repository(self):
        for value in ({}, {"installed": False, "launcher": self.launcher, "activation": None},
                      {**self.runtime, "launcher": "/unrelated/multithread"},
                      {**self.runtime, "activation": {"release_id": "invalid", "activation_id": "a" * 32}}):
            self.commands.clear()
            self.responses["runtime"] = (0, json.dumps(value).encode(), b"")
            code, result = self.invoke("--apply")
            self.assertEqual(1, code)
            self.assertEqual("not_ready", result["runtime"]["state"])
            self.assertEqual("not_checked", result["repository"]["state"])
            self.assertEqual(1, len(self.commands))

    def test_missing_preferred_command_stops_before_enrollment_or_provider_preparation(self):
        observed = {**self.runtime, "preferred_command_available": False}
        self.responses["runtime"] = (0, json.dumps(observed).encode(), b"")
        for extra in (("--check",), ("--apply",)):
            self.commands.clear()
            with mock.patch.object(setup.provider, "prepare") as prepare:
                code, result = self.invoke(*extra)
            self.assertEqual(1, code)
            self.assertEqual("not_ready", result["runtime"]["state"])
            self.assertEqual(observed, result["runtime"]["data"])
            self.assertEqual("not_checked", result["repository"]["state"])
            self.assertEqual([[self.launcher, "runtime", "status"]], self.commands)
            prepare.assert_not_called()

    def test_failed_command_keeps_diagnostic_and_exact_command_separate_from_unavailability(self):
        self.responses["doctor"] = (1, b"", b"synthetic enrollment refusal\n")
        code, result = self.invoke()
        self.assertEqual(1, code)
        doctor = result["repository"]["doctor"]
        self.assertEqual("failed", doctor["state"])
        self.assertEqual("synthetic enrollment refusal\n", doctor["stderr"])
        self.assertEqual([self.launcher, "--repo", str(self.repo), "--json", "doctor"], doctor["command"])
        self.responses["doctor"] = OSError("artificial unavailable command")
        code, result = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("unavailable", result["repository"]["doctor"]["state"])

    def test_apply_failed_command_retains_verified_runtime_and_independent_healthy_reads(self):
        self.responses["init"] = (1, b"", b"synthetic refused enrollment\n")
        code, result = self.invoke("--apply")
        self.assertEqual(1, code)
        self.assertEqual("verified", result["runtime"]["state"])
        self.assertEqual("failed", result["repository"]["enrollment"]["state"])
        self.assertEqual("verified", result["repository"]["doctor"]["state"])
        self.assertEqual("verified", result["repository"]["status"]["state"])
        self.assertEqual(1, sum(command[-1] == "init" for command in self.commands))
        self.assertIn("--check", result["next_actions"][-1]["command"])

    def test_nonzero_exit_does_not_claim_a_specific_refusal_cause(self):
        for exit_code, diagnostic in ((1, b"synthetic partial initialization\n"),
                                      (2, b"synthetic malformed request\n"),
                                      (3, b"synthetic coordination refusal\n")):
            with self.subTest(exit_code=exit_code):
                self.responses["init"] = (exit_code, b"", diagnostic)
                code, result = self.invoke("--apply")
                enrollment = result["repository"]["enrollment"]
                self.assertEqual(1, code)
                self.assertEqual("failed", enrollment["state"])
                self.assertEqual(exit_code, enrollment["exit_code"])
                self.assertEqual(diagnostic.decode(), enrollment["stderr"])

    def test_unverified_zero_exit_initialization_is_uncertain_with_independent_reads_preserved(self):
        for output in (b"not json", b"[]", b"{}", b'{"ok":true}',
                       b'{"ok":false,"initialized":true}',
                       b" " * (setup._MAX_OUTPUT + 1)):
            with self.subTest(output=output[:40]):
                self.commands.clear()
                self.responses["init"] = (0, output, b"")
                code, result = self.invoke("--apply")
                self.assertEqual(1, code)
                self.assertEqual("uncertain", result["repository"]["enrollment"]["state"])
                self.assertEqual("uncertain", result["repository"]["state"])
                self.assertEqual("verified", result["repository"]["doctor"]["state"])
                self.assertEqual("verified", result["repository"]["status"]["state"])
                self.assertEqual(1, sum(command[-1] == "init" for command in self.commands))
                self.assertIn("--check", result["next_actions"][-1]["command"])

    def test_timed_out_enrollment_is_uncertain_and_never_retried(self):
        self.responses["init"] = subprocess.TimeoutExpired([self.launcher], 30)
        code, result = self.invoke("--apply")
        self.assertEqual(1, code)
        self.assertEqual("uncertain", result["repository"]["enrollment"]["state"])
        self.assertEqual(1, sum(command[-1] == "init" for command in self.commands))
        self.assertEqual("verified", result["repository"]["doctor"]["state"])

    def test_zero_exit_does_not_certify_failed_integrity_or_unreadable_output(self):
        for output, state in ((json.dumps({**self.doctor, "ok": False}).encode(), "not_ready"),
                              (json.dumps({**self.doctor, "integrity": "bad"}).encode(), "not_ready"),
                              (b"[]", "unavailable"), (b"not json", "unavailable")):
            self.responses["doctor"] = (0, output, b"")
            code, result = self.invoke()
            self.assertEqual(1, code)
            self.assertEqual(state, result["repository"]["doctor"]["state"])

    def test_invalid_utf8_in_otherwise_valid_json_is_diagnostic_not_verified_evidence(self):
        for command, payload, state in (("doctor", self.doctor, "unavailable"),
                                         ("init", {"ok": True, "initialized": True}, "uncertain")):
            with self.subTest(command=command):
                before = self.responses[command]
                output = json.dumps(payload).encode()[:-1] + b', "synthetic_text": "\xff"}'
                self.responses[command] = (0, output, b"")
                code, result = self.invoke("--apply")
                self.responses[command] = before
                observed = result["repository"]["enrollment" if command == "init" else "doctor"]
                self.assertEqual(1, code)
                self.assertEqual(state, observed["state"])
                self.assertTrue(observed["stdout_decoding_loss"])
                self.assertIn("\ufffd", observed["stdout"])
                self.assertNotIn("data", observed)
                if command == "init":
                    self.assertEqual("verified", result["repository"]["doctor"]["state"])
                    self.assertEqual("verified", result["repository"]["status"]["state"])

    def test_mismatched_status_database_is_not_repository_readiness(self):
        self.responses["status"] = (0, json.dumps({**self.status, "database": "/another/ledger"}).encode(), b"")
        code, result = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual("not_ready", result["repository"]["status"]["state"])

    def test_bounded_raw_output_is_marked_and_never_parsed_as_complete(self):
        self.responses["doctor"] = (0, b" " * (setup._MAX_OUTPUT + 1), b"x" * (setup._MAX_OUTPUT + 1))
        code, result = self.invoke()
        doctor = result["repository"]["doctor"]
        self.assertEqual(1, code)
        self.assertEqual("unavailable", doctor["state"])
        self.assertTrue(doctor["stdout_truncated"])
        self.assertTrue(doctor["stderr_truncated"])
        self.assertEqual(setup._MAX_OUTPUT, len(doctor["stdout"]))
        self.assertEqual(setup._MAX_OUTPUT, len(doctor["stderr"]))

    def test_observation_storage_failure_is_unavailable_without_running_command(self):
        with mock.patch.object(setup.tempfile, "TemporaryFile", side_effect=OSError("synthetic disk unavailable")):
            code, result = self.invoke("--apply")
        self.assertEqual(1, code)
        self.assertEqual("unavailable", result["runtime"]["state"])
        self.assertEqual([], self.commands)

    def test_provider_paths_only_prepare_plans_and_launch_command_is_quoted(self):
        selected = self.base / "provider ;$(touch injected)"
        def prepare(client, repo, launcher, path):
            self.assertEqual(self.repo, repo)
            self.assertEqual(Path(self.launcher), launcher)
            self.assertEqual(selected, path)
            return {"argv": [str(selected), "--settings", "{}"], "provider_started": False}
        with mock.patch.object(setup.provider, "prepare", side_effect=prepare) as prepare_call:
            code, result = self.invoke("--claude", str(selected))
        self.assertEqual(0, code)
        prepare_call.assert_called_once()
        command = result["providers"]["claude"]["launch_command"]
        self.assertEqual(str(selected), command[-1])
        self.assertEqual(command, shlex.split(shlex.join(command)))
        self.assertTrue(all(row[0] == self.launcher for row in self.commands))
        self.assertFalse(result["changes_provider_settings"])
        self.assertFalse(result["changes_permissions"])
        output = self.display(result)
        launch_line = "  Command: " + shlex.join(command)
        self.assertIn(launch_line, output)
        self.assertLess(output.index(launch_line), output.index("First collaboration,"))

    def test_human_provider_error_is_visible_beside_state_without_erasing_readiness(self):
        def prepare(client, repo, launcher, path):
            if client == "claude":
                raise setup.provider.LaunchError("synthetic hook plan does not match this checkout")
            return self.codex_plan(path)
        with mock.patch.object(setup.provider, "prepare", side_effect=prepare):
            code, result = self.invoke("--codex", "/fixture/codex", "--claude", "/fixture/claude")
        output = self.display(result)
        self.assertEqual(0, code)
        self.assertIn("Multithread is ready for this repository.", output)
        self.assertIn("Runtime: verified", output)
        self.assertIn("Repository: verified", output)
        self.assertIn("codex: prepared", output)
        self.assertIn("claude: unavailable; synthetic hook plan does not match this checkout", output)
        self.assertIn("Provider sign-in, hook delivery and tool execution: not checked.", output)
        self.assertIn("--provider /fixture/claude --json", output)
        self.assertEqual("unknown", result["provider_authentication"])
        self.assertTrue(result["provider_started"], "Codex's hook listing started its app server")
        self.assertEqual([(self.codex_plan("/fixture/codex")["argv"], str(self.repo))], self.listings)

    def test_codex_is_prepared_only_when_a_peer_hook_gate_would_pass(self):
        with mock.patch.object(setup.provider, "prepare", side_effect=lambda client, repo, launcher, path: self.codex_plan(path)):
            code, result = self.invoke("--codex", "/fixture/codex")
            ready = self.display(result)
            self.hook_statuses = {"stop": "modified", "sessionStart": "untrusted"}
            review_code, review = self.invoke("--codex", "/fixture/codex")
        self.assertEqual(0, code)
        self.assertEqual("prepared", result["providers"]["codex"]["state"])
        self.assertEqual("ready", result["providers"]["codex"]["hook_trust"]["state"])
        self.assertIn("Multithread is ready for this repository.\n", ready)
        # Runtime and repository readiness still decide the top-level state and
        # exit code; the unready Codex peer route is stated beside it.
        self.assertEqual(0, review_code)
        self.assertEqual("ready", review["state"])
        codex = review["providers"]["codex"]
        self.assertEqual("needs_hook_review", codex["state"])
        self.assertEqual({"sessionStart": "untrusted", "userPromptSubmit": "trusted", "stop": "modified",
                          "sessionEnd": "trusted", "interrupt": "trusted"}, codex["hook_trust"]["events"])
        self.assertEqual("Codex hooks are not ready (modified: stop; untrusted: sessionStart); Codex peer calls refuse until then",
                         codex["message"])
        output = self.display(review)
        self.assertIn("Multithread is ready for this repository; Codex peer calls need one hook review first.", output)
        self.assertIn("codex: needs_hook_review; Codex hooks are not ready (modified: stop; untrusted: sessionStart)", output)
        action = next(entry for entry in review["next_actions"] if entry["stage"] == "codex")
        self.assertIn("open /hooks and trust the five Multithread hooks running "
                      + shlex.join([self.launcher, "provider-hook", "--client", "codex"]), action["action"])
        self.assertIn("covers every enrolled checkout", action["action"])
        self.assertEqual(codex["launch_command"], action["command"])

    def test_unavailable_codex_listing_is_not_readiness_or_absence(self):
        cases = ((OSError("synthetic exec failure"), False), (subprocess.TimeoutExpired(["codex"], 15), True),
                 (setup.ProtocolError("synthetic listing fault"), True))
        for error, started in cases:
            with (self.subTest(error=type(error).__name__),
                  mock.patch.object(setup.provider, "prepare", side_effect=lambda client, repo, launcher, path: self.codex_plan(path))):
                self.listing_error = error
                code, result = self.invoke("--codex", "/fixture/codex")
                self.assertEqual(0, code)
                self.assertEqual("ready", result["state"])
                codex = result["providers"]["codex"]
                self.assertEqual("unavailable", codex["state"])
                self.assertEqual({"state": "unavailable"}, codex["hook_trust"])
                self.assertIn("a Codex peer call checks the same listing", codex["message"])
                self.assertIs(started, result["provider_started"])
                self.assertIn("Multithread is ready for this repository.\n", self.display(result))

    def test_human_version_uses_observed_activation_and_does_not_require_new_json_fields(self):
        for version in ("0.4.0", None):
            with self.subTest(version=version):
                activation = {**self.runtime["activation"]}
                if version is not None:
                    activation["version"] = version
                self.responses["runtime"] = (0, json.dumps({**self.runtime, "activation": activation}).encode(), b"")
                code, result = self.invoke()
                output = self.display(result)
                self.assertEqual(0, code)
                self.assertIn("Version: " + (version or "not reported"), output)
                self.assertIn("Release: " + activation["release_id"], output)
                self.assertEqual(activation, result["runtime"]["data"]["activation"])
                self.assertIn("claude: missing", output)
                self.assertNotIn("claude: unavailable", output)
        self.responses["runtime"] = (1, b"", b"synthetic unavailable runtime\n")
        code, result = self.invoke()
        output = self.display(result)
        self.assertEqual(1, code)
        self.assertIn("Multithread setup needs attention.", output)
        self.assertNotIn("Version:", output)
        self.assertNotIn("Release:", output)

    def test_human_paths_and_errors_escape_controls_without_changing_json_or_command_arguments(self):
        selected = "/fixture/provider\x1b[31m\nFAKE: ready\r\u202e"
        message = "Cannot prepare " + selected
        with mock.patch.object(setup.provider, "prepare", side_effect=setup.provider.LaunchError(message)):
            code, result = self.invoke("--claude", selected)
        self.assertEqual(0, code)
        self.assertEqual(message, result["providers"]["claude"]["message"])
        self.assertEqual(selected, result["providers"]["claude"]["executable"])
        # Exercise the same display boundary for paths and retained diagnostics.
        result["repo"] += "\x1b[2J"
        result["repository"]["identity"]["git_common_dir"] += "\u202e"
        result["launcher"] += "\rFAKE: ready"
        output = self.display(result)
        for control in ("\x1b", "\r", "\u202e"):
            self.assertNotIn(control, output)
        self.assertNotIn("\nFAKE: ready", output)
        self.assertIn("雪", output)
        self.assertIn("claude: unavailable; Cannot prepare", output)
        command_line = next(line for line in output.splitlines() if line.startswith("  Command (JSON argv): "))
        argv = json.loads(command_line.split(": ", 1)[1])
        self.assertEqual(selected, argv[argv.index("--provider") + 1])
        self.assertEqual(result["next_actions"][-1]["command"], argv)

    def test_multiline_stderr_keeps_each_line_attributed_and_json_unchanged(self):
        forged_heading = "Multithread is ready for this repository."
        diagnostic = "Synthetic enrollment error\n\n" + forged_heading + "\nContext: \x1b[2J\t\u202e雪\n"
        self.responses["doctor"] = (1, b"", diagnostic.encode("utf-8"))
        code, result = self.invoke()
        self.assertEqual(1, code)
        self.assertEqual(diagnostic, result["repository"]["doctor"]["stderr"])
        output = self.display(result)
        lines = output.splitlines()
        self.assertEqual([
            "Diagnostic: Synthetic enrollment error",
            "Diagnostic: ",
            "Diagnostic: " + forged_heading,
            r"Diagnostic: Context: \u001b[2J\t\u202e" + "雪",
        ], [line for line in lines if line.startswith("Diagnostic: ")])
        self.assertNotIn(forged_heading, lines)
        self.assertIn("Multithread setup needs attention.", lines)
        for control in ("\x1b", "\t", "\u202e"):
            self.assertNotIn(control, output)

    def test_unavailable_provider_does_not_erase_runtime_and_repository_readiness(self):
        with mock.patch.object(setup.provider, "prepare", side_effect=setup.provider.LaunchError("synthetic plan refusal")):
            code, result = self.invoke("--claude", "/fixture/provider")
        self.assertEqual(0, code)
        self.assertEqual("ready", result["state"])
        self.assertEqual("unavailable", result["providers"]["claude"]["state"])
        self.assertEqual("synthetic plan refusal", result["providers"]["claude"]["message"])

    def test_account_launcher_ignores_environment_home(self):
        with mock.patch.dict(os.environ, {"HOME": "/unrelated-home", "XDG_DATA_HOME": "/unrelated-data"}):
            code, result = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual(self.launcher, result["launcher"])

    def test_repo_symlink_components_reach_installed_boundary_unchanged(self):
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo)
        with redirect_stdout(io.StringIO()):
            setup.setup_main(["--repo", str(alias), "--json"])
        self.assertEqual(str(alias), self.commands[1][2])

    def test_human_output_gives_readiness_and_exact_next_actions(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = setup.setup_main(["--repo", str(self.repo)])
        self.assertEqual(0, code)
        self.assertIn("Runtime: verified", output.getvalue())
        self.assertIn("Repository: verified", output.getvalue())
        self.assertIn("does not edit PATH", output.getvalue())
        self.assertIn("Launcher: " + self.launcher, output.getvalue())
        self.assertIn("Git common directory: " + self.doctor["git_common_dir"], output.getvalue())
        self.assertIn("not checked", output.getvalue())
        collaboration = output.getvalue().index("First collaboration,")
        for client in ("codex", "claude"):
            self.assertLess(output.getvalue().index(client + ": Install or locate the provider"), collaboration)


if __name__ == "__main__":
    unittest.main()
