"""Exercise the public no-account demo and a real broken-fix negative control."""

import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = "/usr/bin/python3"
PAYLOAD = (
    "relay_core/__init__.py", "relay_core/cli.py", "relay_core/protocol.py",
    "relay_core/store.py", "relay_runtime/__init__.py", "relay_runtime/admission.py",
    "relay_runtime/cli.py", "relay_runtime/confinement.py", "relay_runtime/enrollment.py",
    "relay_runtime/provider.py",
    "relay_runtime/codex_peer.py", "relay_runtime/claude_peer.py",
    "relay_runtime/native_io.py", "relay_runtime/peer_control.py", "relay_runtime/agent.py",
    "relay_runtime/setup.py", "relay_runtime/update.py",
)
CLOSED_FILES = (
    "src/relay.py", "src/relay_bootstrap.py", *("src/" + name for name in PAYLOAD),
    "examples/no_account_demo.py", "examples/demo_scenario.py",
)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def snapshot(directory):
    result = {}
    for path in (directory, *sorted(directory.rglob("*"))):
        info = path.lstat()
        payload = os.readlink(path) if stat.S_ISLNK(info.st_mode) else (
            digest(path.read_bytes()) if stat.S_ISREG(info.st_mode) else None)
        result[str(path.relative_to(directory))] = (info.st_mode, info.st_mtime_ns, payload)
    return result


class DemoTests(unittest.TestCase):
    def setUp(self):
        if (sys.platform != "linux" or platform.machine() != "x86_64"
                or os.getuid() == 0 or not Path("/usr/bin/bwrap").is_file()):
            self.skipTest("public demo requires ordinary-user x86-64 Linux and bubblewrap")
        temporary = tempfile.TemporaryDirectory(prefix="relay-demo-test-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.profiles = 0

    def public_process(self, arguments):
        self.profiles += 1
        profile = self.base / ("profile-" + str(self.profiles))
        account = profile / "ambient-home"
        work = profile / "ambient-project"
        account.mkdir(parents=True, mode=0o700)
        work.mkdir(mode=0o700)
        installation = account / ".local/share/relay/installation"
        installation.mkdir(parents=True)
        (installation / "preserve-fixture").write_bytes(b"unrelated synthetic installation")
        hooks = work / ".git/hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_bytes(b"synthetic unrelated hook; preserve")
        before = snapshot(profile)
        env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(account),
            "XDG_CONFIG_HOME": str(account / "config"), "XDG_DATA_HOME": str(account / "data"),
            "XDG_CACHE_HOME": str(account / "cache"), "PYTHONPATH": str(work),
            "RELAY_HOME": str(work / "must-not-use"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            [PYTHON, "-I", "-S", "-B", *map(str, arguments)], cwd=work,
            env=env, text=True, capture_output=True, timeout=120)
        self.assertEqual(before, snapshot(profile), "public command changed ambient account/project")
        return result

    def checked_json(self, arguments):
        result = self.public_process(arguments)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual("", result.stderr)
        return json.loads(result.stdout)

    def test_public_demo_repeats_exactly_and_binds_result_to_source_release(self):
        bundle = self.base / "expected-bundle"
        built = self.checked_json([
            ROOT / "src/relay_bootstrap.py", "build-release", "--output", bundle,
            "--version", "0.0.0-demo",
        ])
        self.assertFalse(built["approved"])
        record_bytes = (bundle / "release.json").read_bytes()
        self.assertEqual(digest(record_bytes), built["release_id"])
        record = json.loads(record_bytes)
        bootstrap = (ROOT / "src/relay_bootstrap.py").read_bytes()
        self.assertEqual(bootstrap, (bundle / "bootstrap.py").read_bytes())
        self.assertEqual({"sha256": digest(bootstrap), "size": len(bootstrap)}, record["bootstrap"])
        self.assertEqual(set(PAYLOAD), set(record["runtime"]["members"]))
        for name in PAYLOAD:
            body = (ROOT / "src" / name).read_bytes()
            self.assertEqual(body, (bundle / "payload" / name).read_bytes())
            self.assertEqual({"sha256": digest(body), "size": len(body)},
                             record["runtime"]["members"][name])
        first, second = [self.checked_json([ROOT / "examples/no_account_demo.py", "--json"])
                         for _ in range(2)]
        self.assertEqual(first, second, "fresh profiles produced different demo evidence")
        self.assertEqual("pass", first["result"])
        self.assertEqual(1, first["schema"])
        self.assertEqual(built["release_id"], first["release_sha256"])
        self.assertEqual(digest((ROOT / "examples/demo_scenario.py").read_bytes()),
                         first["scenario_sha256"])
        self.assertEqual("d8c304e092db7313e142bb57d18f7152b847d748", first["artifact"])
        self.assertEqual("42ac5b6a38d5ce05637827e54feac1c3550483d773ba02b5de6a1842bfd182e7",
                         first["artifact_file_sha256"])
        self.assertEqual({"run": 4, "failures": 2}, first["baseline_tests"])
        self.assertEqual({"run": 4, "failures": 0}, first["review_tests"])
        for field, expected in {
            "events": 6, "handoff_events": 1, "ack_events": 1,
            "producer_exit_before_notification": 75, "simulated_duplicate_wakes": 2,
            "pending_reads_appended_events": 0, "provider_calls": 0,
        }.items():
            self.assertEqual(expected, first[field], field)
        self.assertEqual("ok", first["sqlite_integrity"])
        self.assertEqual("scripted codex/claude labels", first["actors"])
        self.assertEqual("separate namespace; loopback only", first["network"])
        self.assertIs(False, first["host_installation_used"])
        for field in (
            "source_absent_during_workflow", "fresh_process_recovery",
            "competing_claim_refused", "wrong_session_release_refused",
            "conflicting_handoff_retry_refused", "ack_link_verified_in_sqlite",
            "explicit_owner_release", "linked_worktree_shared", "fixture_hooks_preserved",
        ):
            self.assertIs(True, first[field], field)

    def test_baseline_retained_as_fix_cannot_report_pass_or_acknowledgement(self):
        fixture = self.base / "closed-broken-fixture"
        original = {}
        for name in CLOSED_FILES:
            source = ROOT / name
            self.assertTrue(stat.S_ISREG(source.lstat().st_mode), name)
            original[name] = source.read_bytes()
            destination = fixture / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original[name])
        self.assertEqual(set(CLOSED_FILES),
                         {str(path.relative_to(fixture)) for path in fixture.rglob("*") if path.is_file()})
        scenario = fixture / "examples/demo_scenario.py"
        text = scenario.read_text()
        assignments = [node for node in ast.parse(text).body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "FIX"
                               for target in node.targets)]
        self.assertEqual(1, len(assignments))
        assignment = assignments[0]
        lines = text.splitlines(keepends=True)
        scenario.write_text("".join(lines[:assignment.lineno - 1]) + "FIX = BASELINE\n"
                            + "".join(lines[assignment.end_lineno:]))
        self.assertEqual(["examples/demo_scenario.py"],
                         [name for name in CLOSED_FILES if (fixture / name).read_bytes() != original[name]])
        result = self.public_process([fixture / "examples/no_account_demo.py", "--json"])
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout, "broken fixture emitted a pass/ACK receipt")
        self.assertIn("Multithread demo did not pass:", result.stderr)
        self.assertIn("exited 1, expected 0", result.stderr)
        self.assertIn("FAILED (failures=2)", result.stderr)
        self.assertNotIn('"ack_events"', result.stderr)
        self.assertEqual(original["examples/demo_scenario.py"],
                         (ROOT / "examples/demo_scenario.py").read_bytes())
