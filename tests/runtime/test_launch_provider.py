"""Launch-example contracts with disposable executables; no real provider use."""

from contextlib import redirect_stderr, redirect_stdout
import copy
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
from relay_runtime import provider as launch


class LaunchProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-launch-example-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "checkout 'quote' 雪 ;$(touch injected)"
        self.repo.mkdir()
        self.relay = self.base / "multithread"
        self.provider = self.base / "provider entry"
        self.plan_file = self.base / "configuration.json"
        self.receipt = self.base / "provider-receipt.json"
        self.environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
        for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CODEX_HOME"):
            directory = self.base / name.lower()
            directory.mkdir()
            self.environment[name] = str(directory)
        self.environment["RELAY_TEST_NATIVE_ENVIRONMENT"] = "inherited fixture value"
        self.make_executable(self.relay, (
            "from pathlib import Path\n"
            f"print(Path({str(self.plan_file)!r}).read_text())\n"))
        self.make_executable(self.provider, (
            "import json, os, sys\nfrom pathlib import Path\n"
            f"keys = {list(self.environment)!r}\n"
            "receipt = {'argv': sys.argv, 'cwd': os.getcwd(), "
            "'env': {key: os.environ.get(key) for key in keys}}\n"
            f"Path({str(self.receipt)!r}).write_text(json.dumps(receipt))\n"))
        self.plan = self.configuration("codex")
        self.plan_file.write_text(json.dumps(self.plan))
        for attribute, value in (("system", "Linux"), ("machine", "x86_64")):
            patcher = mock.patch.object(launch.platform, attribute, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def make_executable(path, source):
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)

    def configuration(self, client):
        selector = [] if client == "codex" else ["--repo", str(self.repo)]
        hook = shlex.join([str(self.relay), *selector, "provider-hook", "--client", client])
        events = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]
        if client == "codex":
            events.append("Interrupt")
        hooks = {event: [{"hooks": [{"type": "command", "command": hook, "timeout": 3}]}]
                 for event in events}
        if client == "claude":
            arguments = ["--settings", json.dumps({"hooks": hooks}, ensure_ascii=False, separators=(",", ":"))]
        else:
            arguments = []
            for event in events:
                arguments.extend(["-c", "hooks." + event + "=[{hooks=[{type=\"command\",command="
                                  + json.dumps(hook, ensure_ascii=False) + ",timeout=3}]}]"])
        return {"schema": 1, "provider": client, "repo": str(self.repo), "hook_command": hook,
                "events": events, "native_arguments": arguments, "launches_provider": False,
                "changes_provider_settings": False, "changes_permissions": False}

    def invoke(self, *, client="codex", json_output=True, interactive=False, answer="launch",
               provider=None, extra_arguments=(), input_error=None, launcher_flag="--multithread"):
        arguments = [client, "--repo", str(self.repo), launcher_flag, str(self.relay),
                     "--provider", str(provider or self.provider), *extra_arguments]
        if json_output:
            arguments.append("--json")
        output, errors = io.StringIO(), io.StringIO()
        terminal = mock.Mock()
        terminal.isatty.return_value = interactive
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(launch.sys, "stdin", terminal),
              mock.patch("builtins.input", return_value=answer, side_effect=input_error) as prompt,
              mock.patch.dict(os.environ, self.environment, clear=True)):
            status = launch.launch_main(arguments)
        return status, output.getvalue(), errors.getvalue(), prompt

    def mocked_result(self, plan=None, *, stdout=None, returncode=0):
        return subprocess.CompletedProcess([], returncode,
                                           json.dumps(self.plan if plan is None else plan)
                                           if stdout is None else stdout,
                                           "artificial private diagnostic")

    def assert_unavailable(self, result):
        status, output, errors, prompt = result
        self.assertEqual(1, status)
        self.assertEqual("", errors)
        value = json.loads(output)
        self.assertEqual("unavailable", value["state"])
        self.assertIs(value["provider_started"], False)
        self.assertEqual("unknown", value["hook_delivery"])
        self.assertEqual("unknown", value["provider_tools"])
        self.assertNotIn("artificial private diagnostic", output)
        prompt.assert_not_called()
        self.assertFalse(self.receipt.exists())
        return value

    def test_json_prepares_each_client_without_prompt_or_provider_execution(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                self.plan_file.write_text(json.dumps(self.configuration(client)))
                with mock.patch.object(launch.subprocess, "call") as provider_call:
                    status, output, errors, prompt = self.invoke(client=client)
                self.assertEqual(0, status, errors)
                value = json.loads(output)
                self.assertEqual("launch_prepared", value["state"])
                self.assertEqual(client, value["provider"])
                self.assertIs(value["provider_started"], False)
                self.assertEqual("unknown", value["hook_delivery"])
                self.assertEqual("unknown", value["provider_tools"])
                self.assertEqual([str(self.provider), *self.configuration(client)["native_arguments"]], value["argv"])
                prompt.assert_not_called()
                provider_call.assert_not_called()
                self.assertFalse(self.receipt.exists())

    def test_legacy_launcher_flag_keeps_the_same_prepared_invocation(self):
        code, output, errors, prompt = self.invoke(launcher_flag="--relay")
        self.assertEqual(0, code, errors)
        value = json.loads(output)
        self.assertEqual("launch_prepared", value["state"])
        self.assertEqual([str(self.provider), *self.plan["native_arguments"]], value["argv"])
        self.assertFalse(value["provider_started"])
        self.assertFalse(self.receipt.exists())
        prompt.assert_not_called()

    def test_preparation_uses_exact_multithread_argv_and_inherited_environment(self):
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()) as run,
              mock.patch.object(launch.subprocess, "call") as provider_call):
            self.assertEqual(0, self.invoke()[0])
        run.assert_called_once_with(
            [str(self.relay), "--repo", str(self.repo), "--json", "provider-config", "--client", "codex",
             "--launcher-name", "multithread"],
            cwd=self.repo, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
        provider_call.assert_not_called()

    def test_repo_alias_components_reach_relay_unchanged_and_refusal_prevents_launch(self):
        alias = self.base / "checkout alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        received = self.base / "relay-request.json"
        self.make_executable(self.relay, (
            "import json, sys\nfrom pathlib import Path\n"
            f"Path({str(received)!r}).write_text(json.dumps(sys.argv))\n"
            "print('artificial private diagnostic', file=sys.stderr)\n"
            "raise SystemExit(9)\n"))
        for spelling in (alias, alias / ".." / self.repo.name):
            with (self.subTest(spelling=str(spelling)),
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                value = self.assert_unavailable(self.invoke(extra_arguments=("--repo", str(spelling))))
                self.assertIn("Multithread refused launch preparation (exit 9)", value["message"])
                self.assertEqual(
                    [str(self.relay), "--repo", str(spelling), "--json", "provider-config", "--client", "codex",
                     "--launcher-name", "multithread"],
                    json.loads(received.read_text()))
                provider_call.assert_not_called()

    def test_relative_repo_components_expand_from_cwd_without_normalization(self):
        alias = self.base / "relative alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        spelling = Path(alias.name) / ".." / self.repo.name
        received = self.base / "relay-request.json"
        self.make_executable(self.relay, (
            "import json, sys\nfrom pathlib import Path\n"
            f"Path({str(received)!r}).write_text(json.dumps(sys.argv))\n"
            "raise SystemExit(9)\n"))
        with (mock.patch.object(launch.Path, "cwd", return_value=self.base),
              mock.patch.object(launch.subprocess, "call") as provider_call):
            value = self.assert_unavailable(self.invoke(extra_arguments=("--repo", str(spelling))))
        self.assertIn("Multithread refused launch preparation (exit 9)", value["message"])
        self.assertEqual(
            [str(self.relay), "--repo", str(self.base / spelling), "--json", "provider-config", "--client", "codex",
             "--launcher-name", "multithread"],
            json.loads(received.read_text()))
        provider_call.assert_not_called()

    def test_malformed_json_and_nonobject_plans_refuse(self):
        for output in ("", "{broken", "[]", "null", "true", "1", '"text"'):
            with (self.subTest(output=output),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(stdout=output)),
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                self.assert_unavailable(self.invoke())
                provider_call.assert_not_called()

    def test_schema_identity_and_invocation_only_flags_are_required(self):
        mutations = [("schema", value) for value in (None, True, 1.0, "1", 2)]
        mutations += [("provider", "claude"), ("repo", str(self.base))]
        mutations += [(key, value) for key in ("launches_provider", "changes_provider_settings", "changes_permissions")
                      for value in (None, True, 0, "false")]
        for key, value in mutations:
            plan = {**self.plan, key: value}
            with (self.subTest(key=key, value=value),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan)),
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                self.assert_unavailable(self.invoke())
                provider_call.assert_not_called()

    def test_missing_plan_fields_refuse(self):
        for key in ("schema", "provider", "repo", "hook_command", "native_arguments",
                    "launches_provider", "changes_provider_settings", "changes_permissions"):
            plan = copy.deepcopy(self.plan)
            del plan[key]
            with (self.subTest(key=key),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan))):
                self.assert_unavailable(self.invoke())

    def test_malformed_hook_command_types_and_quotes_refuse_cleanly(self):
        for value in (None, False, 17, [], {}, "'unfinished", ""):
            plan = {**self.plan, "hook_command": value}
            with (self.subTest(value=value),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan))):
                self.assert_unavailable(self.invoke())

    def test_mismatched_hook_executable_repo_client_or_action_refuses(self):
        relay, repo = str(self.relay), str(self.repo)
        wrong = {
            # Codex's command must stay checkout-free: one trust review covers
            # every checkout only while the reviewed text is identical.
            "codex": ([str(self.provider), "provider-hook", "--client", "codex"],
                      [relay, "--repo", repo, "provider-hook", "--client", "codex"],
                      [relay, "--repo", str(self.base), "provider-hook", "--client", "codex"],
                      [relay, "provider-config", "--client", "codex"],
                      [relay, "provider-hook", "--client", "claude"]),
            "claude": ([str(self.provider), "--repo", repo, "provider-hook", "--client", "claude"],
                       [relay, "provider-hook", "--client", "claude"],
                       [relay, "--repo", str(self.base), "provider-hook", "--client", "claude"],
                       [relay, "--repo", repo, "provider-config", "--client", "claude"],
                       [relay, "--repo", repo, "provider-hook", "--client", "codex"]),
        }
        for client, commands in wrong.items():
            for command in commands:
                plan = {**self.configuration(client), "hook_command": shlex.join(command)}
                with (self.subTest(client=client, command=command),
                      mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan))):
                    value = self.assert_unavailable(self.invoke(client=client))
                    self.assertEqual("The hook command does not match the selected Multithread and checkout.",
                                     value["message"])

    def test_invalid_native_arguments_refuse(self):
        for arguments in (None, [], "-c setting", {}, [1], [None], ["contains\0nul"]):
            plan = {**self.plan, "native_arguments": arguments}
            with (self.subTest(arguments=arguments),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan))):
                self.assert_unavailable(self.invoke())

    def test_native_permission_flags_and_contradictory_embedded_commands_refuse(self):
        for client in ("codex", "claude"):
            plan = self.configuration(client)
            native = plan["native_arguments"]
            permission_flag = "--dangerously-bypass-approvals-and-sandbox" if client == "codex" else "--dangerously-skip-permissions"
            cases = [[permission_flag, *native], [permission_flag],
                     [value.replace("provider-hook", "provider-config") for value in native]]
            if client == "codex":
                cases += [["-c", "sandbox_mode=\"danger-full-access\"", *native[2:]],
                          [*native[:2], *native[:2], *native[4:]],
                          [*native[:-1], "hooks.Stop = [broken"],
                          [value.replace("timeout=3", "timeout=30") for value in native]]
            else:
                settings = json.loads(native[1])
                settings["permissions"] = {"defaultMode": "bypassPermissions"}
                cases += [["--settings", json.dumps(settings)], ["--settings", "[]"],
                          ["--settings", "{broken"],
                          [native[0], native[1].replace('"timeout":3', '"timeout":30')]]
            for arguments in cases:
                with (self.subTest(client=client, arguments=arguments),
                      mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(
                          {**plan, "native_arguments": arguments})),
                      mock.patch.object(launch.subprocess, "call") as provider_call):
                    self.assert_unavailable(self.invoke(client=client))
                    provider_call.assert_not_called()

    def test_equivalent_native_formatting_preserves_exact_argv(self):
        for client in ("codex", "claude"):
            plan = self.configuration(client)
            if client == "codex":
                plan["native_arguments"] = [value.replace("timeout=3", "timeout = 3")
                                            for value in plan["native_arguments"]]
            else:
                plan["native_arguments"][1] = json.dumps(json.loads(plan["native_arguments"][1]), indent=2)
            with (self.subTest(client=client),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan))):
                status, output, errors, _ = self.invoke(client=client)
                self.assertEqual(0, status, errors)
                self.assertEqual([str(self.provider), *plan["native_arguments"]], json.loads(output)["argv"])

    def test_noninteractive_terminal_never_prompts_or_launches(self):
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
              mock.patch.object(launch.subprocess, "call") as provider_call):
            status, _, errors, prompt = self.invoke(json_output=False)
        self.assertEqual(1, status)
        self.assertIn("interactive terminal", errors)
        prompt.assert_not_called()
        provider_call.assert_not_called()

    def test_cancellation_never_launches(self):
        for answer in ("", "no", "Launch", "launch later"):
            with (self.subTest(answer=answer),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                status, output, errors, prompt = self.invoke(json_output=False, interactive=True, answer=answer)
                self.assertEqual(0, status, errors)
                self.assertIn("Provider was not started", output)
                prompt.assert_called_once()
                provider_call.assert_not_called()

    def test_eof_or_keyboard_interrupt_at_confirmation_does_not_launch(self):
        for error in (EOFError(), KeyboardInterrupt()):
            with (self.subTest(error=type(error).__name__),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                status, _, errors, _ = self.invoke(json_output=False, interactive=True, input_error=error)
                self.assertEqual(130, status)
                self.assertIn("interrupted", errors)
                provider_call.assert_not_called()

    def test_confirmed_launch_passes_exact_argv_without_environment_or_privilege_overrides(self):
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
              mock.patch.object(launch.subprocess, "call", return_value=23) as provider_call):
            status, _, errors, _ = self.invoke(json_output=False, interactive=True)
        self.assertEqual(23, status, errors)
        provider_call.assert_called_once_with(
            [str(self.provider), *self.plan["native_arguments"]], cwd=str(self.repo))

    def test_real_stub_receives_literal_arguments_and_preserved_symlink_entry(self):
        entry = self.base / "provider symlink 'literal' ; &"
        entry.symlink_to(self.provider)
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                configuration = self.configuration(client)
                self.plan_file.write_text(json.dumps(configuration))
                status, _, errors, _ = self.invoke(client=client, json_output=False, interactive=True, provider=entry)
                self.assertEqual(0, status, errors)
                observed = json.loads(self.receipt.read_text())
                self.assertEqual([str(entry), *configuration["native_arguments"]], observed["argv"])
                self.assertEqual(str(self.repo), observed["cwd"])
                self.assertEqual(self.environment, observed["env"])
                self.assertFalse((self.repo / "injected").exists())

    def test_human_review_shows_validated_hooks_without_nested_settings(self):
        for client in ("codex", "claude"):
            plan = self.configuration(client)
            # Descriptive metadata does not determine native behavior.
            plan["events"] = ["incorrect-description"]
            with (self.subTest(client=client),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan)),
                  mock.patch.object(launch.subprocess, "call") as call):
                status, output, errors, _ = self.invoke(
                    client=client, json_output=False, interactive=True, answer="no")
            self.assertEqual(0, status, errors)
            self.assertIn("Provider: " + client, output)
            self.assertIn("Executable: " + str(self.provider), output)
            self.assertIn("Checkout: " + str(self.repo), output)
            self.assertIn("Hook command: " + plan["hook_command"], output)
            self.assertIn("3-second timeout", output)
            self.assertIn("SessionStart, UserPromptSubmit, Stop, SessionEnd", output)
            self.assertEqual(client == "codex", "Interrupt" in output)
            self.assertNotIn("incorrect-description", output)
            self.assertNotIn("native_arguments", output)
            self.assertNotIn("--settings", output)
            self.assertIn("launch --json", output)
            self.assertIn("unknown until observed", output)
            call.assert_not_called()

    def test_control_bearing_review_uses_exact_json_argv_and_safe_display(self):
        self.repo = self.base / "checkout\n\x1b[2J\u202e"
        self.repo.mkdir()
        plan = self.configuration("claude")
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan)),
              mock.patch.object(launch.subprocess, "call", return_value=0) as call):
            status, output, errors, _ = self.invoke(client="claude", json_output=False, interactive=True)
        self.assertEqual(0, status, errors)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)
        self.assertIn("checkout\\n\\u001b[2J\\u202e", output)
        line = next(line for line in output.splitlines() if line.startswith("Hook command (JSON string): "))
        self.assertEqual(plan["hook_command"], json.loads(line.split(": ", 1)[1]))
        call.assert_called_once_with([str(self.provider), *plan["native_arguments"]], cwd=str(self.repo))

    def test_review_preserves_accepted_literal_hook_spacing_for_native_trust(self):
        for client in ("codex", "claude"):
            plan = self.configuration(client)
            original = plan["hook_command"]
            hook = original.replace(" --repo ", "  --repo  ")
            plan["hook_command"] = hook
            plan["native_arguments"] = [
                argument.replace(json.dumps(original, ensure_ascii=False),
                                 json.dumps(hook, ensure_ascii=False))
                for argument in plan["native_arguments"]]
            with (self.subTest(client=client),
                  mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result(plan)),
                  mock.patch.object(launch.subprocess, "call", return_value=0) as call):
                status, output, errors, _ = self.invoke(client=client, json_output=False, interactive=True)
            self.assertEqual(0, status, errors)
            line = next(line for line in output.splitlines() if line.startswith("Hook command: "))
            self.assertEqual("Hook command: " + hook, line)
            call.assert_called_once_with([str(self.provider), *plan["native_arguments"]], cwd=str(self.repo))

    def test_relay_failure_and_timeout_keep_observation_unknown_and_do_not_retry(self):
        cases = (self.mocked_result(returncode=7),
                 subprocess.TimeoutExpired("fixture relay", 15, output="artificial private diagnostic"),
                 OSError("artificial private diagnostic"),
                 UnicodeDecodeError("utf-8", b"\xff", 0, 1, "artificial private diagnostic"))
        for result in cases:
            with (self.subTest(result=type(result).__name__),
                  mock.patch.object(launch.subprocess, "run") as run,
                  mock.patch.object(launch.subprocess, "call") as provider_call):
                if isinstance(result, BaseException):
                    run.side_effect = result
                else:
                    run.return_value = result
                self.assert_unavailable(self.invoke())
                run.assert_called_once()
                provider_call.assert_not_called()

    def test_provider_spawn_error_returns_failure_without_retry(self):
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
              mock.patch.object(launch.subprocess, "call", side_effect=OSError("artificial private diagnostic")) as call):
            status, _, errors, _ = self.invoke(json_output=False, interactive=True)
        self.assertEqual(1, status)
        self.assertNotIn("artificial private diagnostic", errors)
        call.assert_called_once()

    def test_interrupt_after_launch_reports_uncertainty_without_retry(self):
        with (mock.patch.object(launch.subprocess, "run", return_value=self.mocked_result()),
              mock.patch.object(launch.subprocess, "call", side_effect=KeyboardInterrupt()) as call):
            status, output, errors, _ = self.invoke(json_output=False, interactive=True)
        self.assertEqual(130, status)
        self.assertIn("inspect the provider if it had already started", errors)
        self.assertIn("Hook delivery and provider tools: unknown until observed", output)
        call.assert_called_once()

    def test_relative_missing_and_nonexecutable_provider_paths_refuse_before_relay(self):
        nonexecutable = self.base / "not executable"
        nonexecutable.write_text("fixture")
        for provider in (Path("relative-entry"), self.base / "missing", nonexecutable, self.repo):
            with (self.subTest(provider=str(provider)),
                  mock.patch.object(launch.subprocess, "run") as run):
                self.assert_unavailable(self.invoke(provider=provider))
                run.assert_not_called()

    def test_unsupported_platform_refuses_before_any_subprocess(self):
        for system, machine in (("Darwin", "x86_64"), ("Linux", "aarch64")):
            with (self.subTest(system=system, machine=machine),
                  mock.patch.object(launch.platform, "system", return_value=system),
                  mock.patch.object(launch.platform, "machine", return_value=machine),
                  mock.patch.object(launch.subprocess, "run") as run):
                self.assert_unavailable(self.invoke())
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
