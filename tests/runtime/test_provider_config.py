"""Readonly native provider argument generation in disposable installed fixtures."""

import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import tomllib
import unittest

import test_installed as installed


def snapshot(root):
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        content = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                   else hashlib.sha256(path.read_bytes()).hexdigest()
                   if stat.S_ISREG(info.st_mode) else None)
        result[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, content)
    return result


_NO_PROVIDER = """
import shutil
def no_provider_lookup(*args, **kwargs):
    raise AssertionError("provider-config attempted executable PATH lookup")
shutil.which = no_provider_lookup
def guard_provider_process(event, args):
    if event in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        if os.fsdecode(args[0]) not in {"/usr/bin/git", "git"}:
            raise AssertionError("provider-config attempted non-Git process execution")
sys.addaudithook(guard_provider_process)
"""


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.fixture = installed.InstalledTests("test_real_verified_runtime_initializes_writes_reopens_and_diagnoses")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def config(self, client, *, launcher_name=None, **kwargs):
        selected = ["--launcher-name", launcher_name] if launcher_name is not None else []
        result = self.fixture.command("provider-config", "--client", client, *selected, **kwargs)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({
            "schema", "provider", "repo", "hook_command", "events", "native_arguments",
            "launches_provider", "changes_provider_settings", "changes_permissions",
        }, set(value))
        self.assertEqual(1, value["schema"])
        self.assertIs(type(value["schema"]), int)
        self.assertEqual(client, value["provider"])
        self.assertEqual(str(self.fixture.repo), value["repo"])
        launcher = str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin" / (launcher_name or "relay"))
        self.assertEqual(
            [launcher, *([] if client == "codex" else ["--repo", str(self.fixture.repo)]),
             "provider-hook", "--client", client],
            shlex.split(value["hook_command"]))
        events = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]
        if client == "codex":
            events.append("Interrupt")
        self.assertEqual(events, value["events"])
        for flag in ("launches_provider", "changes_provider_settings", "changes_permissions"):
            self.assertIs(value[flag], False)
        arguments = value["native_arguments"]
        self.assertIsInstance(arguments, list)
        self.assertTrue(all(isinstance(part, str) for part in arguments))
        expected = {event: [{"hooks": [{
            "type": "command", "command": value["hook_command"], "timeout": 3,
        }]}] for event in events}
        if client == "codex":
            self.assertEqual(len(events) * 2, len(arguments))
            parsed = {}
            for index in range(0, len(arguments), 2):
                self.assertEqual("-c", arguments[index])
                value_part = tomllib.loads(arguments[index + 1])
                self.assertEqual({"hooks"}, set(value_part))
                self.assertEqual(1, len(value_part["hooks"]))
                self.assertFalse(set(parsed) & set(value_part["hooks"]), "duplicate event override")
                parsed.update(value_part["hooks"])
            self.assertEqual(expected, parsed)
        else:
            self.assertEqual(2, len(arguments))
            self.assertEqual("--settings", arguments[0])
            self.assertEqual({"hooks": expected}, json.loads(arguments[1]))
        return value

    def assert_readonly(self, before):
        after = snapshot(self.fixture.base)
        key = str((self.fixture.state / "relay.sqlite3-shm").relative_to(self.fixture.base))
        if key in before:
            # SQL readonly still permits bookkeeping in this pinned sidecar.
            self.assertEqual(before[key][:4], after[key][:4])
            before = dict(before)
            after = dict(after)
            before.pop(key)
            after.pop(key)
        self.assertEqual(before, after)

    def test_exact_native_shapes_without_provider_lookup_or_execution(self):
        self.fixture.initialize()
        for client in ("codex", "claude"):
            for name in (None, "relay", "multithread"):
                with self.subTest(client=client, launcher=name):
                    before = snapshot(self.fixture.base)
                    self.config(client, launcher_name=name, before=_NO_PROVIDER)
                    self.assert_readonly(before)

    def test_command_name_cannot_select_an_arbitrary_executable(self):
        self.fixture.initialize()
        before = snapshot(self.fixture.base)
        for name in ("../multithread", "/tmp/foreign-command", "foreign"):
            result = self.fixture.command("provider-config", "--client", "claude", "--launcher-name", name)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual(before, snapshot(self.fixture.base))

    def test_quoted_unicode_and_shell_metacharacter_repository_round_trip(self):
        # DEL is legal in a Git pathname but must be escaped in TOML strings.
        renamed = self.fixture.repo.with_name("space 'quote' 雪 🧪 ;$(not-a-command)&\x7f")
        self.fixture.repo.rename(renamed)
        self.fixture.repo = renamed
        self.fixture.initialize()
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                before = snapshot(self.fixture.base)
                self.config(client, before=_NO_PROVIDER)
                self.assert_readonly(before)

    def test_newline_repository_is_exactly_round_tripped_or_refused_without_writes(self):
        # Git's line-oriented identity output currently rejects this pathname.
        # If admission gains explicit support later, require exact argument data.
        renamed = self.fixture.repo.with_name("line\nbreak")
        self.fixture.repo.rename(renamed)
        self.fixture.repo = renamed
        before = snapshot(self.fixture.base)
        initialized = self.fixture.command("init")
        if initialized.returncode != 0:
            self.assertEqual("", initialized.stdout)
            self.assertEqual(before, snapshot(self.fixture.base))
            for client in ("codex", "claude"):
                result = self.fixture.command("provider-config", "--client", client, before=_NO_PROVIDER)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual(before, snapshot(self.fixture.base))
        else:
            for client in ("codex", "claude"):
                before = snapshot(self.fixture.base)
                self.config(client, before=_NO_PROVIDER)
                self.assert_readonly(before)

    def test_generation_preserves_settings_git_ledger_and_ambient_home(self):
        self.fixture.initialize()
        self.fixture.event()
        for relative in ("project/.claude", "project/.codex", "account/provider-settings", "foreign-home"):
            folder = self.fixture.base / relative
            folder.mkdir(parents=True)
            (folder / "settings.json").write_text('{"unrelated":{"enabled":true},"permissions":["fixture-only"]}\n')
        hook = self.fixture.repo / ".git/hooks/provider-config-canary"
        hook.parent.mkdir(exist_ok=True)
        hook.write_bytes(b"unrelated hook must remain\n")
        foreign = self.fixture.base / "foreign-home"
        env = {"HOME": str(foreign), "XDG_CONFIG_HOME": str(foreign),
               "XDG_DATA_HOME": str(foreign), "PYTHONPATH": str(self.fixture.repo),
               "CLAUDE_PROJECT_DIR": str(foreign)}
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                before = snapshot(self.fixture.base)
                self.config(client, before=_NO_PROVIDER, extra_env=env,
                            stdin="not JSON and deliberately not consumed")
                self.assert_readonly(before)

    def test_unenrolled_missing_and_unhealthy_state_refuse_without_output_or_repair(self):
        before = snapshot(self.fixture.base)
        for client in ("codex", "claude"):
            result = self.fixture.command("provider-config", "--client", client)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual(before, snapshot(self.fixture.base))
        self.fixture.initialize()
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm", "enrollment.json"):
            with self.subTest(missing=name):
                original = self.fixture.state / name
                held = self.fixture.base / ("held-" + name)
                original.rename(held)
                before = snapshot(self.fixture.base)
                for client in ("codex", "claude"):
                    result = self.fixture.command("provider-config", "--client", client)
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual("", result.stdout)
                    self.assertEqual(before, snapshot(self.fixture.base))
                held.rename(original)
        # Preserve the inodes while corrupting only this disposable DB fixture.
        # Clear its WAL too: otherwise valid page-one WAL contents can mask the
        # intentionally invalid main-file header and still yield readable state.
        database = self.fixture.state / "relay.sqlite3"
        database.write_bytes(b"not a SQLite database")
        (self.fixture.state / "relay.sqlite3-wal").write_bytes(b"")
        before = snapshot(self.fixture.base)
        for client in ("codex", "claude"):
            result = self.fixture.command("provider-config", "--client", client)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual(before, snapshot(self.fixture.base))

    def test_final_custody_failure_suppresses_generated_arguments(self):
        self.fixture.initialize()
        held = self.fixture.state.with_name("held-provider-config-state")
        result = self.fixture.command("provider-config", "--client", "codex", before=f"""
def replace(stage):
    if stage == "worker-exited":
        state = pathlib.Path({str(self.fixture.state)!r})
        state.rename(pathlib.Path({str(held)!r}))
        state.mkdir(mode=0o700)
cli._checkpoint = replace
""")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual([], list(self.fixture.state.iterdir()))
        self.assertEqual({"enrollment.json", "relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"},
                         {path.name for path in held.iterdir()})
