"""User-level provider hooks in synthetic homes; no real provider, account config or trust."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay_runtime import codex_peer, hooks, provider

LAUNCHER = Path("/home/fixture/.local/bin/multithread")
CODEX_COMMAND = "/home/fixture/.local/bin/multithread provider-hook --client codex"
CLAUDE_COMMAND = '/home/fixture/.local/bin/multithread --repo "$CLAUDE_PROJECT_DIR" provider-hook --client claude'
# currentHash that Codex 0.159.2's own hooks/list reported for CODEX_COMMAND,
# observed in a disposable CODEX_HOME. The derivation here must reproduce it.
NATIVE_HASHES = {
    "SessionStart": "sha256:3aef92f5b034e63a1aff9d3d7e51ddcc50146684680d03f412330e2cc2dfced2",
    "SessionEnd": "sha256:b36882c47399aafe66cd1848067f986fe2fa12cb4420cf06185d6fcde23f4b47",
    "UserPromptSubmit": "sha256:4b24b9f9e605f79133f4345bfa540d540952a5cf166a57719d4871dd5b96b9b6",
    "Stop": "sha256:bd68ef73b3e588c6ae2733720d156774dfcad9260eca9e141cf92ce58501002d",
    "Interrupt": "sha256:76c2ccc10a289e9917897d14ddd848e8dc8fc8acfcd8ac8e4e77cf9d046fd12d",
}
FOREIGN = {  # Someone else's hooks, shaped like an existing dispatcher: never ours to edit.
    "description": "Another tool's user-level hooks.",
    "hooks": {
        "PreToolUse": [{"matcher": "^Bash$", "hooks": [
            {"type": "command", "command": "/usr/bin/python3 /opt/guard.py", "timeout": 3,
             "statusMessage": "Checking"}]}],
        "SessionStart": [{"hooks": [
            {"type": "command", "command": "/usr/bin/env -i /bin/bash /opt/dispatch.sh lifecycle", "timeout": 5},
            {"type": "command", "command": "/usr/bin/env -i /bin/bash /opt/dispatch.sh brief", "timeout": 5,
             "additionalContextLimit": 5000}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/env -i /bin/bash /opt/dispatch.sh lifecycle",
                             "timeout": 5}]}],
    },
}


class HomeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-user-hooks-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.codex_home = self.base / "codex-home"
        self.claude_home = self.base / "claude-home"
        self.codex_home.mkdir()
        self.claude_home.mkdir()
        self.environ = {"HOME": str(self.base / "home"), "CODEX_HOME": str(self.codex_home),
                        "CLAUDE_CONFIG_DIR": str(self.claude_home)}
        patcher = mock.patch.object(hooks, "account_launcher", return_value=LAUNCHER)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, self.environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file(self, client):
        return (self.codex_home / "hooks.json") if client == "codex" else (self.claude_home / "settings.json")

    def write(self, client, value, mode=0o640):
        path = self.file(client)
        path.write_text(json.dumps(value, indent=4) + "\n")
        path.chmod(mode)
        return path.read_bytes()

    def apply(self, action, clients=("codex", "claude")):
        plan = hooks.change_files(action, clients)
        return hooks.change_files(action, clients, expected=plan["plan_sha256"])


class DefinitionTests(HomeCase):
    def test_commands_name_no_checkout_and_codex_matches_invocation_hooks(self):
        self.assertEqual(CODEX_COMMAND, hooks.command("codex"))
        self.assertEqual(CLAUDE_COMMAND, hooks.command("claude"))
        # The invocation hook's exact text, so one trust review keeps covering every checkout.
        with mock.patch("relay_runtime.account_launcher", return_value=LAUNCHER):
            from relay_runtime import hook_argv
            self.assertEqual(CODEX_COMMAND, shlex.join(hook_argv(LAUNCHER, "codex", "/any/checkout")))
        self.assertTrue(codex_peer._multithread_hook(CLAUDE_COMMAND, str(LAUNCHER)))

    def test_codex_hash_reproduces_codex_own_listing(self):
        self.assertEqual(NATIVE_HASHES, {event: hooks.codex_hash(event) for event in hooks.EVENTS["codex"]})

    def test_provider_homes_follow_the_provider_own_variables(self):
        self.assertEqual(self.codex_home / "hooks.json", hooks.hook_file("codex"))
        self.assertEqual(self.claude_home / "settings.json", hooks.hook_file("claude"))
        self.assertEqual(Path("/h/.codex/hooks.json"), hooks.hook_file("codex", {"HOME": "/h"}))
        self.assertEqual(Path("/h/.claude/settings.json"), hooks.hook_file("claude", {"HOME": "/h", "CLAUDE_CONFIG_DIR": "relative"}))


class FileTests(HomeCase):
    def test_install_appends_after_other_hooks_and_leaves_them_exactly(self):
        original = self.write("codex", FOREIGN)
        plan = hooks.change_files("install", ("codex",))
        self.assertEqual("planned", plan["state"])
        self.assertEqual(original, self.file("codex").read_bytes(), "a plan writes nothing")
        result = hooks.change_files("install", ("codex",), expected=plan["plan_sha256"])
        self.assertEqual("applied", result["state"])
        value = json.loads(self.file("codex").read_text())
        self.assertEqual(FOREIGN["description"], value["description"])
        for event, groups in FOREIGN["hooks"].items():
            # Existing positions are unchanged, so Codex's trust keys still match.
            self.assertEqual(groups, value["hooks"][event][:len(groups)])
        for event in hooks.EVENTS["codex"]:
            ours = value["hooks"][event][-1]
            self.assertEqual({"hooks": [{"type": "command", "command": CODEX_COMMAND, "timeout": 3}]}, ours)
        self.assertEqual({event: [[len(FOREIGN["hooks"].get(event, [])), 0]] for event in hooks.EVENTS["codex"]},
                         {event: entry["slots"] for event, entry in hooks.inspect("codex")["events"].items()})
        self.assertEqual(0o640, stat.S_IMODE(self.file("codex").stat().st_mode))
        backup = Path(result["plans"][0]["backup"])
        self.assertEqual(original, backup.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(backup.stat().st_mode))
        # The file keeps its own layout: only Multithread's entries are new.
        self.assertFalse(result["plans"][0]["reformatted"])
        self.assertIn('\n    "hooks": {', self.file("codex").read_text())
        self.assertEqual("installed", hooks.inspect("codex")["state"])
        self.assertEqual({"PreToolUse": 1, "SessionStart": 2, "Stop": 1}, hooks.inspect("codex")["other_hooks"])
        self.assertEqual("unchanged", self.apply("install", ("codex",))["state"])

    def test_install_creates_both_files_and_remove_restores_their_content(self):
        self.write("claude", {"model": "fixture", "hooks": {"UserPromptSubmit": FOREIGN["hooks"]["Stop"]}})
        before = json.loads(self.file("claude").read_text())
        result = self.apply("install")
        self.assertEqual("applied", result["state"])
        self.assertIsNone(result["plans"][0]["backup"], "a new Codex file has nothing to keep")
        self.assertEqual(0o600, stat.S_IMODE(self.file("codex").stat().st_mode))
        claude = json.loads(self.file("claude").read_text())
        self.assertEqual(set(hooks.EVENTS["claude"]), {event for event in claude["hooks"]
                                                        if any(hooks._ours(item) for group in claude["hooks"][event]
                                                               for item in group["hooks"])})
        self.assertEqual({"type": "command", "command": CLAUDE_COMMAND, "timeout": 3},
                         claude["hooks"]["SessionStart"][0]["hooks"][0])
        self.assertEqual("installed", hooks.inspect("claude")["state"])
        removed = self.apply("remove")
        self.assertEqual("applied", removed["state"])
        self.assertEqual(before, json.loads(self.file("claude").read_text()))
        self.assertEqual({"hooks": {}}, json.loads(self.file("codex").read_text()))
        self.assertEqual("absent", hooks.inspect("claude")["state"])

    def test_remove_names_every_other_hook_that_moves(self):
        self.write("codex", FOREIGN)
        self.apply("install", ("codex",))
        value = json.loads(self.file("codex").read_text())
        value["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/opt/later.sh", "timeout": 2}]})
        self.file("codex").write_text(json.dumps(value))
        plan = hooks.change_files("remove", ("codex",))
        self.assertEqual([{"event": "Stop", "from": [2, 0], "to": [1, 0]}], plan["plans"][0]["moved_hooks"])
        result = hooks.change_files("remove", ("codex",), expected=plan["plan_sha256"])
        after = json.loads(self.file("codex").read_text())
        self.assertEqual(FOREIGN["hooks"]["Stop"] + [value["hooks"]["Stop"][2]], after["hooks"]["Stop"])
        self.assertEqual(FOREIGN["hooks"]["SessionStart"], after["hooks"]["SessionStart"])
        self.assertNotIn("Interrupt", after["hooks"])
        self.assertTrue(Path(result["plans"][0]["backup"]).exists())

    def test_a_compact_or_escaped_file_reports_its_normalized_layout(self):
        self.file("claude").write_text(json.dumps({"note": "caf\u00e9", "hooks": {}}))
        plan = hooks.change_files("install", ("claude",))
        self.assertTrue(plan["plans"][0]["reformatted"])
        hooks.change_files("install", ("claude",), expected=plan["plan_sha256"])
        self.assertIn('"note": "caf\\u00e9"', self.file("claude").read_text(), "ASCII escapes are kept")

    def test_our_handler_under_a_matcher_is_repaired(self):
        self.write("codex", {"hooks": {"SessionStart": [{"matcher": "^startup$", "hooks": [hooks.handler("codex")]}]}})
        state = hooks.inspect("codex")
        self.assertEqual("mismatched", state["events"]["SessionStart"]["state"])
        self.apply("install", ("codex",))
        value = json.loads(self.file("codex").read_text())
        self.assertEqual([{"hooks": [hooks.handler("codex")]}], value["hooks"]["SessionStart"])
        self.assertEqual("installed", hooks.inspect("codex")["state"])

    def test_older_multithread_hook_is_replaced_in_place(self):
        older = {"hooks": {**FOREIGN["hooks"], "Stop": FOREIGN["hooks"]["Stop"] + [{"hooks": [
            {"type": "command", "command": "/home/fixture/.local/bin/relay --repo /alpha provider-hook --client codex",
             "timeout": 3}]}]}}
        self.write("codex", older)
        state = hooks.inspect("codex")
        self.assertEqual("needs_repair", state["state"])
        self.assertEqual("mismatched", state["events"]["Stop"]["state"])
        with self.assertRaises(hooks.HooksError) as refused:
            hooks.delivery("codex")
        self.assertIn(shlex.join([str(LAUNCHER), "hooks", "install", "--client", "codex"]), str(refused.exception))
        plan = hooks.change_files("install", ("codex",))
        self.assertIn({"event": "Stop", "change": "replace_older_multithread_hook", "slot": [1, 0]},
                      plan["plans"][0]["changes"])
        hooks.change_files("install", ("codex",), expected=plan["plan_sha256"])
        value = json.loads(self.file("codex").read_text())
        self.assertEqual(2, len(value["hooks"]["Stop"]))
        self.assertEqual(CODEX_COMMAND, value["hooks"]["Stop"][1]["hooks"][0]["command"])

    def test_refusals_write_nothing_and_name_their_recovery(self):
        duplicate = {"hooks": {"Stop": [{"hooks": [hooks.handler("codex")]}, {"hooks": [hooks.handler("codex")]}]}}
        cases = [
            ("duplicate", json.dumps(duplicate), "hooks remove --client codex"),
            ("invalid", "{not json", "Fix it with the provider's own settings"),
            ("repeated key", '{"hooks": {}, "hooks": {}}', "repeats a key"),
            ("not an object", "[]", "does not hold a JSON object"),
        ]
        for name, body, recovery in cases:
            with self.subTest(name):
                self.file("codex").write_text(body)
                with self.assertRaises(hooks.HooksError) as refused:
                    hooks.change_files("install", ("codex",))
                self.assertIn(recovery, str(refused.exception))
                self.assertEqual(body, self.file("codex").read_text())
        self.file("codex").unlink()
        target = self.base / "dotfiles-hooks.json"
        target.write_text("{}")
        self.file("codex").symlink_to(target)
        with self.assertRaises(hooks.HooksError) as refused:
            hooks.change_files("install", ("codex",))
        self.assertIn("symbolic link", str(refused.exception))
        self.assertEqual("{}", target.read_text())

    def test_a_stale_plan_or_a_concurrent_edit_writes_nothing(self):
        original = self.write("codex", FOREIGN)
        plan = hooks.change_files("install", ("codex",))
        with self.assertRaises(hooks.HooksError) as stale:
            hooks.change_files("install", ("codex",), expected="0" * 64)
        self.assertIn("hooks install --json", str(stale.exception))
        self.assertEqual(original, self.file("codex").read_bytes())
        real_read = hooks._read

        def edited_meanwhile(path):
            result = real_read(path)
            Path(path).write_text(json.dumps({"hooks": {}, "edited": True}))
            return result
        with mock.patch.object(hooks, "_read", side_effect=edited_meanwhile):
            with self.assertRaises(hooks.HooksError) as raced:
                hooks.change_files("install", ("codex",), expected=plan["plan_sha256"])
        self.assertIn("changed after the plan was made", str(raced.exception))
        self.assertEqual({"hooks": {}, "edited": True}, json.loads(self.file("codex").read_text()))
        self.assertEqual([], list(self.codex_home.glob("*.bak")))

    def test_missing_provider_directory_is_skipped_not_created(self):
        self.claude_home.rmdir()
        result = self.apply("install")
        self.assertEqual("provider_not_found", result["plans"][1]["state"])
        self.assertFalse(self.claude_home.exists())
        self.assertEqual({"source": "session_flags", "file": None, "command": None}, hooks.delivery("claude"))


class DeliveryTests(HomeCase):
    """Launch and peer pass invocation hooks only when no user-level copy exists."""

    def prepare(self, client):
        repo = self.base / "checkout"
        repo.mkdir(exist_ok=True)
        entry = self.base / "provider"
        entry.write_text("#!/bin/sh\n")
        entry.chmod(0o700)
        launcher = self.base / "multithread"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o700)
        command = shlex.join([str(launcher), *([] if client == "codex" else ["--repo", str(repo)]),
                              "provider-hook", "--client", client])
        events = provider._hook_events(client)
        hooks_value = {event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}] for event in events}
        native = (["--settings", json.dumps({"hooks": hooks_value}, separators=(",", ":"))] if client == "claude"
                  else [part for event in events for part in (
                      "-c", "hooks." + event + "=[{hooks=[{type=\"command\",command=" + json.dumps(command) + ",timeout=3}]}]")])
        plan = {"schema": 1, "provider": client, "repo": str(repo), "hook_command": command, "events": events,
                "native_arguments": native, "launches_provider": False, "changes_provider_settings": False,
                "changes_permissions": False}
        with (mock.patch.object(provider, "account_launcher", return_value=launcher),
              mock.patch.object(hooks, "account_launcher", return_value=launcher),
              mock.patch.object(provider.platform, "system", return_value="Linux"),
              mock.patch.object(provider.platform, "machine", return_value="x86_64"),
              mock.patch.object(provider.subprocess, "run", return_value=subprocess.CompletedProcess(
                  [], 0, json.dumps(plan), ""))):
            return provider.prepare(client, repo, launcher, entry), native, launcher

    def test_each_event_runs_once_whichever_source_delivers_it(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client, installed=False):
                plan, native, _ = self.prepare(client)
                self.assertEqual("session_flags", plan["hooks"]["source"])
                self.assertEqual(native, plan["argv"][1:])
        with mock.patch.object(hooks, "account_launcher", return_value=self.base / "multithread"):
            self.apply("install")
        for client in ("codex", "claude"):
            with self.subTest(client=client, installed=True):
                plan, _, launcher = self.prepare(client)
                self.assertEqual([str(self.base / "provider")], plan["argv"], "no invocation copy beside user hooks")
                self.assertEqual({"source": "user", "file": str(self.file(client)),
                                  "command": shlex.join([str(launcher), "provider-hook", "--client", "codex"])
                                  if client == "codex" else
                                  shlex.quote(str(launcher)) + ' --repo "$CLAUDE_PROJECT_DIR" provider-hook --client claude'},
                                 plan["hooks"])
                output = io.StringIO()
                with redirect_stdout(output):
                    provider._display_launch(plan)
                self.assertIn("Hooks: your user-level hooks in " + str(self.file(client)), output.getvalue())
                self.assertNotIn("Invocation hooks", output.getvalue())

    def test_a_partial_installation_refuses_launch_with_its_fix(self):
        with mock.patch.object(hooks, "account_launcher", return_value=self.base / "multithread"):
            self.apply("install", ("codex",))
        value = json.loads(self.file("codex").read_text())
        del value["hooks"]["Interrupt"]
        self.file("codex").write_text(json.dumps(value))
        with self.assertRaises(provider.LaunchError) as refused:
            self.prepare("codex")
        self.assertIn("incomplete (Interrupt: missing)", str(refused.exception))
        self.assertIn("hooks install --client codex", str(refused.exception))


def listed(event, key, *, command=CODEX_COMMAND, source="user", path=None, status="untrusted", **updates):
    hook = {"key": key, "eventName": hooks._LISTED[event], "matcher": None, "timeoutSec": 3,
            "statusMessage": None, "additionalContextLimit": None, "sourcePath": path, "source": source,
            "pluginId": None, "displayOrder": 0, "enabled": True, "isManaged": False,
            "currentHash": hooks.codex_hash(event) if command == CODEX_COMMAND else "sha256:" + "f" * 64,
            "trustStatus": status, "handlerType": "command", "command": command, "async": False}
    hook.update(updates)
    return hook


FAKE_CODEX = r'''#!/usr/bin/python3
import json, sys
from pathlib import Path
spec_path = Path(SPEC)
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    spec = json.loads(spec_path.read_text())
    with open(LOG, "a") as stream:
        stream.write(json.dumps(message) + "\n")
    if "id" not in message:
        continue
    method, identifier, params = message["method"], message["id"], message.get("params", {})
    if method == "initialize":
        emit({"id": identifier, "result": {"userAgent": "fixture/0.0.1"}})
    elif method == "config/read":
        emit({"id": identifier, "result": {"config": {}, "origins": {}, "layers": [
            {"name": {"type": "user", "file": spec["config_file"], "profile": None},
             "version": spec["version"], "config": {}, "disabledReason": None}]}})
    elif method == "hooks/list":
        emit({"id": identifier, "result": {"data": [
            {"cwd": params["cwds"][0], "hooks": spec["hooks"], "warnings": [], "errors": []}]}})
    elif method == "config/batchWrite":
        if params.get("expectedVersion") != spec["version"]:
            emit({"id": identifier, "error": {"code": -32600, "message": "Configuration was modified since last read."}})
            continue
        for edit in params["edits"]:
            if edit["keyPath"] == "hooks.state" and edit["mergeStrategy"] == "upsert":
                for key, record in edit["value"].items():
                    for hook in spec["hooks"]:
                        if hook["key"] == key:
                            hook["trustStatus"] = "trusted" if record["trusted_hash"] == hook["currentHash"] else "modified"
            elif edit["value"] is None:
                path = edit["keyPath"][len("hooks.state."):]
                key = json.loads(path[:-len(".trusted_hash")] if path.endswith(".trusted_hash") else path)
                for hook in spec["hooks"]:
                    if hook["key"] == key:
                        hook["trustStatus"] = "untrusted"
        spec["version"] = "sha256:" + "1" * 64
        spec_path.write_text(json.dumps(spec))
        emit({"id": identifier, "result": {"status": "ok", "version": spec["version"],
                                           "filePath": spec["config_file"], "overriddenMetadata": None}})
    else:
        emit({"id": identifier, "error": {"code": -32601, "message": "unexpected method"}})
'''


class TrustTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.write("codex", FOREIGN)
        self.apply("install", ("codex",))
        self.path = str(self.file("codex"))
        slots = {event: entry["slots"][0] for event, entry in hooks.inspect("codex")["events"].items()}
        self.keys = {event: f"{self.path}:{hooks._LABELS[event]}:{slots[event][0]}:{slots[event][1]}"
                     for event in hooks.EVENTS["codex"]}
        self.foreign = [listed("SessionStart", self.path + ":session_start:0:0", command="/opt/dispatch.sh lifecycle",
                               path=self.path, status="modified")]
        self.spec = self.base / "codex-spec.json"
        self.log = self.base / "codex-requests.jsonl"
        self.codex = self.base / "codex"
        self.codex.write_text(FAKE_CODEX.replace("SPEC", repr(str(self.spec))).replace("LOG", repr(str(self.log))))
        self.codex.chmod(0o700)
        self.config(self.ours())

    def ours(self, **updates):
        return [listed(event, self.keys[event], path=self.path, **updates.get(event, {}))
                for event in hooks.EVENTS["codex"]]

    def config(self, hooks_listed):
        self.spec.write_text(json.dumps({"config_file": str(self.codex_home / "config.toml"),
                                         "version": "sha256:" + "0" * 64, "hooks": hooks_listed + self.foreign}))
        self.log.write_text("")

    def requests(self):
        return [json.loads(line)["method"] for line in self.log.read_text().splitlines()]

    def test_plan_then_apply_records_exactly_the_five_hooks_codex_listed(self):
        plan = hooks.trust(self.codex)
        self.assertEqual("planned", plan["state"])
        self.assertNotIn("config/batchWrite", self.requests())
        self.assertEqual([{"keyPath": "hooks.state", "mergeStrategy": "upsert", "value": {
            self.keys[event]: {"trusted_hash": NATIVE_HASHES[event]} for event in hooks.EVENTS["codex"]}}],
            plan["edits"])
        self.assertEqual({"needing_review": 1, "statuses": ["modified"]}, plan["other_hooks"])
        self.assertEqual(["--yes", "--expected-plan", plan["plan_sha256"], "--json"], plan["apply_argv"][-4:])
        self.log.write_text("")
        applied = hooks.trust(self.codex, expected=plan["plan_sha256"])
        self.assertEqual("trusted", applied["state"])
        self.assertTrue(applied["changes_provider_settings"])
        self.assertEqual(["initialize", "initialized", "config/read", "hooks/list", "config/batchWrite", "hooks/list"],
                         self.requests())
        write = [json.loads(line) for line in self.log.read_text().splitlines()][4]["params"]
        self.assertEqual({"edits": plan["edits"], "expectedVersion": "sha256:" + "0" * 64,
                          "reloadUserConfig": True}, write)
        self.assertEqual({"trusted"}, {item["status_after"] for item in applied["hooks"]})
        self.assertEqual(shlex.join([str(LAUNCHER), "hooks", "trust", "--revoke"]), applied["revoke_command"])
        self.assertEqual("modified", json.loads(self.spec.read_text())["hooks"][-1]["trustStatus"],
                         "another tool's hook is left for its person")
        self.config(self.ours(**{event: {"status": "trusted"} for event in hooks.EVENTS["codex"]}))
        self.assertEqual("already_trusted", hooks.trust(self.codex)["state"])
        self.assertNotIn("config/batchWrite", self.requests())

    def test_any_difference_from_what_multithread_installed_refuses_everything(self):
        stop = self.keys["Stop"]
        cases = {
            "command": ({"Stop": {"command": "/home/fixture/.local/bin/multithread provider-hook --client  codex"}},
                        "differs from the one Multithread installed"),
            "hash": ({"Stop": {"currentHash": "sha256:" + "a" * 64}}, "Codex's hash differs"),
            "source": ({"Stop": {"source": "project"}}, "does not come from"),
            "file": ({"Stop": {"sourcePath": str(self.base / "elsewhere.json")}}, "does not come from"),
            "slot": ({"Stop": {"key": stop[:-3] + "9:0"}}, "listed position differs"),
            "timeout": ({"Stop": {"timeoutSec": 5}}, "timeout, matcher or execution mode"),
            "matcher": ({"Stop": {"matcher": ".*"}}, "timeout, matcher or execution mode"),
            "managed": ({"Stop": {"isManaged": True}}, "timeout, matcher or execution mode"),
            "disabled": ({"Stop": {"enabled": False}}, "re-enable it there first"),
            "status": ({"Stop": {"trustStatus": "futureStatus"}}, "unrecognized trust status"),
        }
        for name, (updates, reason) in cases.items():
            with self.subTest(name):
                listing = self.ours()
                for hook in listing:
                    event = next(e for e, label in hooks._LISTED.items() if label == hook["eventName"])
                    hook.update(updates.get(event, {}))
                self.config(listing)
                result = hooks.trust(self.codex)
                self.assertEqual("refused", result["state"])
                self.assertRegex(result["message"], r"Nothing was trusted: Stop: [^.]*" + re.escape(reason))
                self.assertIn("Nothing was trusted", result["message"])
                self.assertNotIn("edits", result)
                self.assertNotIn("config/batchWrite", self.requests())
        # A second Multithread copy (an invocation flag) beside the user hook.
        self.config(self.ours() + [listed("Stop", "/<session-flags>/config.toml:stop:0:0", source="sessionFlags")])
        result = hooks.trust(self.codex)
        self.assertEqual("refused", result["state"])
        self.assertIn("Stop: Codex lists 2 Multithread hooks for this event, not one", result["message"])

    def test_a_changed_plan_or_configuration_writes_nothing(self):
        plan = hooks.trust(self.codex)
        with self.assertRaises(hooks.HooksError) as refused:
            hooks.trust(self.codex, expected="0" * 64)
        self.assertIn("changed since the plan was shown", str(refused.exception))
        spec = json.loads(self.spec.read_text())
        spec["version"] = "sha256:" + "2" * 64
        self.spec.write_text(json.dumps(spec))
        with self.assertRaises(hooks.HooksError):
            hooks.trust(self.codex, expected=plan["plan_sha256"])
        self.assertNotIn("config/batchWrite", self.requests())

    def test_revoke_removes_only_records_holding_multithread_hashes(self):
        records = {key: NATIVE_HASHES[event] for event, key in self.keys.items()}
        records[self.path + ":session_start:0:0"] = "sha256:" + "e" * 64
        records["/<session-flags>/config.toml:stop:0:0"] = NATIVE_HASHES["Stop"]
        (self.codex_home / "config.toml").write_text("".join(
            f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{value}"\n'
            + ("enabled = true\n" if key == self.keys["Stop"] else "") for key, value in records.items()))
        self.config(self.ours(**{event: {"status": "trusted"} for event in hooks.EVENTS["codex"]}))
        plan = hooks.trust(self.codex, revoke=True)
        self.assertEqual(sorted(self.keys.values()), [item["key"] for item in plan["hooks"]])
        self.assertEqual({"replace"}, {edit["mergeStrategy"] for edit in plan["edits"]})
        self.assertEqual({None}, {edit["value"] for edit in plan["edits"]})
        # A record holding only trust goes; the person's own setting beside it stays.
        self.assertEqual(sorted(['hooks.state.' + json.dumps(key) + ('.trusted_hash' if event == "Stop" else '')
                                 for event, key in self.keys.items()]),
                         sorted(edit["keyPath"] for edit in plan["edits"]))
        result = hooks.trust(self.codex, revoke=True, expected=plan["plan_sha256"])
        self.assertEqual("revoked", result["state"])
        self.assertEqual({"untrusted"}, {item["status_after"] for item in result["hooks"]})

    def test_trust_needs_the_installed_hooks_first(self):
        self.apply("remove", ("codex",))
        with self.assertRaises(hooks.HooksError) as refused:
            hooks.trust(self.codex)
        self.assertIn(shlex.join([str(LAUNCHER), "hooks", "install", "--client", "codex"]), str(refused.exception))
        self.assertEqual([], self.requests())

    def test_offline_estimate_reads_recorded_hashes_only(self):
        self.assertEqual("not_trusted", hooks.trust_estimate())
        (self.codex_home / "config.toml").write_text("".join(
            f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{NATIVE_HASHES[event]}"\n'
            for event, key in self.keys.items()))
        self.assertEqual("trusted", hooks.trust_estimate())
        (self.codex_home / "config.toml").write_text(
            (self.codex_home / "config.toml").read_text() + '[hooks.state.' + json.dumps(self.keys["Stop"])[:-1]
            + 'x"]\nenabled = false\n')
        self.assertEqual("trusted", hooks.trust_estimate(), "a different key does not disable ours")

    def test_status_gives_the_person_both_trust_routes(self):
        result = hooks.status(("codex",), codex=self.codex)
        self.assertEqual("needs_attention", result["state"])
        self.assertEqual("codex_listing", result["providers"]["codex"]["trust"]["source"])
        self.assertEqual("not_trusted", result["providers"]["codex"]["trust"]["state"])
        options = result["next_actions"][0]["options"]
        self.assertEqual(["manual", "agent_assisted"], [option["mode"] for option in options])
        self.assertEqual("person", options[0]["actor"])
        self.assertIn("/hooks", options[0]["steps"][2])
        self.assertIn(CODEX_COMMAND, options[0]["steps"][2])
        self.assertIn("only after the person chose this mode", options[1]["actor"])
        self.assertNotIn("config/batchWrite", self.requests())


class CommandLineTests(HomeCase):
    def run_hooks(self, *argv, tty=False, answer=""):
        output, errors = io.StringIO(), io.StringIO()
        terminal = mock.Mock()
        terminal.isatty.return_value = tty
        with (redirect_stdout(output), redirect_stderr(errors), mock.patch.object(hooks.sys, "stdin", terminal),
              mock.patch("builtins.input", return_value=answer)):
            code = hooks.hooks_main(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def test_agents_apply_only_the_reviewed_plan(self):
        code, output, _ = self.run_hooks("install", "--json")
        plan = json.loads(output)
        self.assertEqual((0, "planned"), (code, plan["state"]))
        self.assertFalse(self.file("codex").exists())
        code, output, _ = self.run_hooks("install", "--yes", "--json")
        self.assertEqual(1, code)
        self.assertIn("--expected-plan", json.loads(output)["message"])
        code, output, _ = self.run_hooks("install", "--yes", "--expected-plan", plan["plan_sha256"], "--json")
        self.assertEqual((0, "applied"), (code, json.loads(output)["state"]))
        self.assertEqual("installed", hooks.inspect("codex")["state"])

    def test_people_approve_by_typing_and_nothing_happens_otherwise(self):
        code, output, errors = self.run_hooks("install")
        self.assertEqual(1, code)
        self.assertIn("Nothing was written", errors)
        self.assertIn("append SessionStart at position 0", output)
        self.assertFalse(self.file("codex").exists())
        code, output, _ = self.run_hooks("install", tty=True, answer="no")
        self.assertEqual((0, True), (code, "Nothing was written." in output))
        self.assertFalse(self.file("codex").exists())
        code, output, _ = self.run_hooks("install", tty=True, answer="install")
        self.assertEqual(0, code)
        self.assertIn("Done. Check with: " + shlex.join([str(LAUNCHER), "hooks", "status"]), output)
        code, output, _ = self.run_hooks("status", "--client", "claude")
        self.assertEqual(0, code, output)
        self.assertIn("Claude Code: installed (" + str(self.file("claude")) + ")", output)


class CoverageTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.base / "project"
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "--allow-empty", "-qm", "Fixture"], check=True)
        self.linked = self.base / "project-linked"
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "worktree", "add", "-qb", "linked",
                        str(self.linked)], check=True)
        self.now = time.time()

    def codex_index(self, rows):
        database = sqlite3.connect(self.codex_home / "state_5.sqlite")
        database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, "
                         "updated_at INTEGER, source TEXT, cwd TEXT, title TEXT, first_user_message TEXT)")
        for identifier, cwd, age_hours, source in rows:
            stamp = int(self.now - age_hours * 3600)
            database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (identifier, "/private/rollout.jsonl", stamp - 60, stamp, source, str(cwd),
                              "PRIVATE_TITLE_SENTINEL", "PRIVATE_MESSAGE_SENTINEL"))
        database.commit()
        database.close()

    def transcript(self, directory, session, age_hours):
        folder = self.claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", directory)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (session + ".jsonl")
        path.write_text("PRIVATE_TRANSCRIPT_SENTINEL\n")
        path.chmod(0)  # Names and times only: an unreadable transcript still counts.
        stamp = self.now - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_evidence_is_metadata_for_this_repository_and_recent_sessions_only(self):
        here, linked, sub, old, other, agent = (str(uuid.uuid4()) for _ in range(6))
        self.codex_index([(here, self.repo, 1, "vscode"), (linked, self.linked, 2, "exec"),
                          (sub, self.repo / "src", 3, "cli"), (old, self.repo, 30, "vscode"),
                          (other, self.base / "project-other", 1, "vscode"),
                          (agent, self.repo, 1, '{"subagent":{"other":"guardian"}}')])
        claude = str(uuid.uuid4())
        self.transcript(str(self.repo), claude, 1)
        self.transcript(str(self.repo), str(uuid.uuid4()), 48)
        self.transcript(str(self.base / "project-other"), str(uuid.uuid4()), 1)
        (self.claude_home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
         / "not-a-session.jsonl").write_text("")
        evidence = hooks.coverage_evidence(self.repo)
        text = json.dumps(evidence)
        for sentinel in ("PRIVATE_TITLE_SENTINEL", "PRIVATE_MESSAGE_SENTINEL", "PRIVATE_TRANSCRIPT_SENTINEL",
                         "/private/rollout.jsonl"):
            self.assertNotIn(sentinel, text)
        self.assertEqual([here, linked, sub], [item["session"] for item in evidence["providers"]["codex"]["sessions"]])
        self.assertEqual([claude], [item["session"] for item in evidence["providers"]["claude"]["sessions"]])
        self.assertEqual({"absent"}, {entry["hooks"] for entry in evidence["providers"].values()})
        report = hooks.coverage_report(evidence, {"codex": {linked}, "claude": set()})
        self.assertEqual("gap", report["state"])
        self.assertEqual([here, sub], report["providers"]["codex"]["silent"])
        self.assertEqual(
            "2 Codex conversations worked here in the last 24 hours with no ledger events: Multithread's user-level "
            "Codex hooks aren't installed. Fix: " + shlex.join([str(LAUNCHER), "hooks", "install", "--client", "codex"])
            + ".", report["messages"][0])
        self.assertEqual(
            "A Claude Code session worked here in the last 24 hours with no ledger events: Multithread's user-level "
            "Claude Code hooks aren't installed. Fix: " + shlex.join([str(LAUNCHER), "hooks", "install", "--client",
                                                                      "claude"]) + ".", report["messages"][1])

    def test_each_cause_names_its_own_fix(self):
        base = {"window_hours": 24, "repo": "/r", "launcher": str(LAUNCHER)}
        session = [{"session": "s1", "last_activity": 1}]
        cases = [
            ({"hooks": "needs_repair"}, "Multithread's user-level Codex hooks are altered or duplicated",
             str(LAUNCHER) + " hooks install --client codex"),
            ({"hooks": "installed", "trust": "not_trusted"}, "Codex hasn't trusted Multithread's user-level hooks",
             str(LAUNCHER) + " hooks trust (agent-assisted, with your go) or /hooks in a Codex terminal"),
            ({"hooks": "installed", "trust": "trusted"}, "the hooks are installed and trusted, so it likely started "
             "before that, or its hook could not reach the ledger (look for a MULTITHREAD WARNING in it)",
             "start a new conversation; if the gap remains, run " + str(LAUNCHER)
             + " setup --repo /r --check"),
        ]
        for extra, cause, fix in cases:
            with self.subTest(cause):
                report = hooks.coverage_report({**base, "providers": {"codex": {
                    "evidence": "codex_thread_index", "sessions": session, **extra}}}, {"codex": set()})
                self.assertIn(": " + cause, report["messages"][0])
                self.assertTrue(report["messages"][0].endswith("Fix: " + fix + "."), report["messages"][0])
        covered = hooks.coverage_report({**base, "providers": {"codex": {
            "evidence": "codex_thread_index", "sessions": session, "hooks": "absent"}}}, {"codex": {"s1"}})
        self.assertEqual(("covered", []), (covered["state"], covered["messages"]))
        unknown = hooks.coverage_report({**base, "providers": {"codex": {"evidence": "unavailable", "sessions": []}}}, {})
        self.assertEqual("unavailable", unknown["state"], "unavailable evidence is never reported as covered")

    def test_unrecognized_or_missing_indexes_are_not_absence(self):
        database = sqlite3.connect(self.codex_home / "state_9.sqlite")
        database.execute("CREATE TABLE threads (identifier TEXT)")
        database.close()
        evidence = hooks.coverage_evidence(self.repo)
        self.assertEqual("unrecognized", evidence["providers"]["codex"]["evidence"])
        self.assertEqual("not_found", evidence["providers"]["claude"]["evidence"])
        self.assertEqual("unavailable", hooks.coverage_report(evidence, {})["state"])


class InstalledCoverageTests(unittest.TestCase):
    """doctor names the gap and status warns, through the real retained runtime."""

    def setUp(self):
        import test_installed as installed
        self.fixture = installed.InstalledTests("test_actual_lifecycle_hook_records_once")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.codex_home = self.fixture.base / "codex-home"
        self.claude_home = self.fixture.base / "claude-home"
        self.codex_home.mkdir()
        self.claude_home.mkdir()
        self.env = {"CODEX_HOME": str(self.codex_home), "CLAUDE_CONFIG_DIR": str(self.claude_home)}
        self.session = str(uuid.uuid4())
        database = sqlite3.connect(self.codex_home / "state_5.sqlite")
        database.execute("CREATE TABLE threads (id TEXT, cwd TEXT, updated_at INTEGER, source TEXT, title TEXT)")
        database.execute("INSERT INTO threads VALUES (?, ?, ?, 'vscode', 'PRIVATE_TITLE_SENTINEL')",
                         (self.session, str(self.fixture.repo), int(time.time())))
        database.commit()
        database.close()

    def test_doctor_and_status_make_a_silent_session_visible_until_it_reaches_the_ledger(self):
        self.fixture.initialize()
        doctor = self.fixture.success("doctor", extra_env=self.env)
        self.assertTrue(doctor["ok"], "coverage never changes ledger health")
        coverage = doctor["hook_coverage"]
        self.assertEqual("gap", coverage["state"])
        self.assertEqual([self.session], coverage["providers"]["codex"]["silent"])
        self.assertIn("A Codex conversation worked here in the last 24 hours with no ledger events: Multithread's "
                      "user-level Codex hooks aren't installed. Fix: ", coverage["messages"][0])
        self.assertNotIn("PRIVATE_TITLE_SENTINEL", json.dumps(doctor))
        status = self.fixture.command("status", extra_env=self.env)
        self.assertEqual(0, status.returncode, status.stderr)
        json.loads(status.stdout)
        self.assertEqual(["multithread: warning: " + coverage["messages"][0]], status.stderr.splitlines())
        started = self.fixture.command("provider-hook", "--client", "codex", extra_env=self.env,
                                       stdin=json.dumps({"hook_event_name": "SessionStart", "session_id": self.session}))
        self.assertIn("MULTITHREAD BRIEF", started.stdout)
        doctor = self.fixture.success("doctor", extra_env=self.env)
        self.assertEqual("covered", doctor["hook_coverage"]["state"])
        status = self.fixture.command("brief", "--agent", "codex", extra_env=self.env)
        self.assertEqual("", status.stderr)

    def test_the_installed_command_reaches_hook_management(self):
        result = self.fixture.command("hooks", "status", extra_env=self.env)
        self.assertEqual(1, result.returncode, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual("needs_attention", value["state"])
        self.assertEqual({"absent"}, {entry["state"] for entry in value["providers"].values()})
        self.assertEqual(str(self.claude_home / "settings.json"), value["providers"]["claude"]["file"])
        self.assertEqual([["hooks", "install", "--client", "codex"], ["hooks", "install", "--client", "claude"]],
                         [action["command"][1:] for action in value["next_actions"]])
        self.assertEqual([], sorted(path.name for path in self.claude_home.iterdir()))

    def test_unavailable_evidence_never_fails_the_ledger_command(self):
        self.fixture.initialize()
        (self.codex_home / "state_5.sqlite").write_bytes(b"not a database")
        doctor = self.fixture.success("doctor", extra_env=self.env)
        self.assertTrue(doctor["ok"])
        self.assertEqual("unavailable", doctor["hook_coverage"]["state"])
        self.assertEqual("unavailable", doctor["hook_coverage"]["providers"]["codex"]["evidence"])


if __name__ == "__main__":
    unittest.main()
