"""Real retained-runtime commands; every registry, ledger and Git root is disposable."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SOURCE))
import relay_bootstrap as bootstrap


class InstalledTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-installed-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        subprocess.run(["/usr/bin/git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-q",
                        "-F", "-"], input="Fixture baseline\n", text=True, check=True)
        self.registry = self.base / "account" / "enrollments"
        self.install = self.base / "account" / "runtime"
        payload = {name: (SOURCE / name).read_bytes() for name in bootstrap.PAYLOAD_FILES}
        manager = bootstrap.Installation(self.install)
        digest = manager.install(SOURCE, bootstrap.manifest_for(payload))
        manager.activate(digest, expected_activation=None)

    def command(self, *args, repo=None, before="", extra_env=None, stdin="", cwd=None):
        # cwd runs the command as a provider does: from a directory, without --repo.
        argv = [*([] if cwd is not None else ["--repo", str(repo or self.repo)]), "--json", *args]
        script = f"""
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("trusted_test_bootstrap", {str(SOURCE / 'relay_bootstrap.py')!r})
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)
runtime = bootstrap.Installation(pathlib.Path({str(self.install)!r})).load_active()
runtime.install_importer()
from relay_runtime import cli
from relay_runtime.enrollment import Registry
registry = Registry(pathlib.Path({str(self.registry)!r}))
"""
        script += textwrap.dedent(before)
        script += f"\nraise SystemExit(cli.main({argv!r}, registry=registry))\n"
        env = dict(os.environ)
        env.update(extra_env or {})
        return subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", textwrap.dedent(script)],
            env=env, input=stdin, text=True, capture_output=True, timeout=20, cwd=cwd)

    def success(self, *args, **kwargs):
        result = self.command(*args, **kwargs)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def initialize(self):
        result = self.success("init")
        self.assertTrue(result["initialized"])
        return result

    def event(self, summary="ready"):
        return self.success("signal", "work.intent", "--agent", "codex", "--session",
                            "test-session", "--work-id", "demo", "--summary", summary)

    @property
    def state(self):
        return self.repo / ".relay"

    def test_real_verified_runtime_initializes_writes_reopens_and_diagnoses(self):
        self.initialize()
        event = self.event()
        self.assertGreater(event["event"]["seq"], 0)
        self.assertEqual(event["event"]["seq"], self.event()["event"]["seq"])
        self.assertTrue(self.success("doctor")["ok"])
        events = self.success("events")
        self.assertTrue(any(row["summary"] == "ready" for row in events))
        self.assertEqual({"enrollment.json", "relay.sqlite3", "relay.sqlite3-wal",
                          "relay.sqlite3-shm"}, {p.name for p in self.state.iterdir()})
        self.assertEqual(1, len(list(self.registry.glob("ledger-*.json"))))

    def test_init_is_idempotent_and_readonly_after_first_success(self):
        first = self.initialize()
        event = self.event()
        second = self.success("init")
        self.assertEqual(first, second)
        self.assertEqual(event["event"]["seq"], self.event()["event"]["seq"])

    def test_unenrolled_observation_and_claim_never_create_state(self):
        for args in (("status",), ("claim", "code:sample", "--agent", "codex",
                                     "--session", "one", "--purpose", "test")):
            result = self.command(*args)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.registry.exists())

    def test_state_overrides_refuse_before_enrollment(self):
        for args, env in ((("--home", str(self.base / "elsewhere"), "init"), {}),
                          (("init",), {"RELAY_HOME": ""})):
            result = self.command(*args, extra_env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse(self.state.exists())
            self.assertFalse(self.registry.exists())

    def test_unsupported_confinement_refuses_before_enrollment(self):
        result = self.command("init", before="""
from relay_runtime.confinement import ConfinementError
def unavailable():
    raise ConfinementError("synthetic unsupported kernel")
cli.abi_version = unavailable
""")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.state.exists())

    def test_claim_conflict_and_exact_owner_survive_installed_boundary(self):
        self.initialize()
        acquired = self.success("claim", "code:sample", "--agent", "codex", "--session",
                                "one", "--purpose", "bounded test")
        conflict = self.command("claim", "code:sample", "--agent", "claude",
                                "--session", "two", "--purpose", "conflict")
        self.assertNotEqual(0, conflict.returncode)
        refused = self.command("release", acquired["claim"]["claim_id"], "--agent", "codex", "--session", "two")
        self.assertNotEqual(0, refused.returncode)
        self.success("release", acquired["claim"]["claim_id"], "--agent", "codex", "--session", "one")

    def test_missing_each_object_is_not_recreated_or_adopted(self):
        self.initialize()
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
            with self.subTest(name=name):
                original = self.state / name
                held = self.base / name
                original.rename(held)
                result = self.command("status")
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(original.exists())
                held.rename(original)

    def test_replacement_and_unsafe_modes_refuse_without_repair(self):
        self.initialize()
        self.event()
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
            with self.subTest(name=name):
                path = self.state / name
                held = self.base / name
                path.rename(held)
                path.write_bytes(b"foreign object")
                path.chmod(0o644)
                before = (path.read_bytes(), path.stat())
                result = self.command("status")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(before[0], path.read_bytes())
                after = path.stat()
                self.assertEqual((before[1].st_mode, before[1].st_mtime_ns),
                                 (after.st_mode, after.st_mtime_ns))
                path.unlink()
                held.rename(path)

    def test_no_success_receipt_after_controller_namespace_check_fails(self):
        self.initialize()
        result = self.command("status", before=f"""
def replace(stage):
    if stage == "worker-exited":
        state = pathlib.Path({str(self.state)!r})
        state.rename(state.with_name("held-state"))
        state.mkdir(mode=0o700)
cli._checkpoint = replace
""")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual([], list(self.state.iterdir()))

    def test_worker_exit_after_commit_before_anchor_preserves_orphan(self):
        result = self.command("init", before="""
original = cli._worker
def die_after_close(*args):
    original(*args)
    os._exit(93)
cli._worker = die_after_close
""")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual([], list(self.registry.glob("ledger-*.json")))
        before = {path.name: path.read_bytes() for path in self.state.iterdir()}
        self.assertNotEqual(0, self.command("init").returncode)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.state.iterdir()})

    def test_parent_exit_before_anchor_never_reports_initialized(self):
        result = self.command("init", before="""
def die(stage):
    if stage == "worker-exited":
        os._exit(94)
cli._checkpoint = die
""")
        self.assertEqual(94, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual([], list(self.registry.glob("ledger-*.json")))
        self.assertNotEqual(0, self.command("status").returncode)

    def test_installed_decision_response_and_ack_are_atomic_and_idempotent(self):
        self.initialize()
        self.success("signal", "work.intent", "--agent", "claude", "--session",
                     "author", "--work-id", "decision-demo", "--summary", "bounded review")
        oid = subprocess.check_output(["/usr/bin/git", "-C", str(self.repo),
                                       "rev-parse", "HEAD"], text=True).strip()
        request = self.success(
            "decision", "request", "--agent", "claude", "--session", "author",
            "--decision-id", "fixture-choice", "--work-id", "decision-demo",
            "--scope", "code:fixture", "--summary", "Choose the implementation",
            "--artifact", f"git:{oid}", "--authority-hint", "engineering",
            "--option", "keep", "--option", "change", "--rollout-fence", "active-clients-refreshed")
        response_args = (
            "decision", "respond", str(request["event"]["seq"]), "--agent", "codex",
            "--session", "reviewer", "--judgment", "Keep the tested implementation",
            "--resolution", "choice", "--authority-class", "engineering", "--choice", "keep",
            "--rollout-fence", "active-clients-refreshed")
        interrupted = self.command(*response_args, before="""
from relay_runtime import admission
original = admission._GuardedConnection.execute
commits = 0
def crash(self, sql, *args, **kwargs):
    global commits
    if sql == "COMMIT":
        commits += 1
        if commits == 2:
            os._exit(97)
    return original(self, sql, *args, **kwargs)
admission._GuardedConnection.execute = crash
""")
        self.assertEqual(97, interrupted.returncode)
        self.assertEqual("", interrupted.stdout)
        kinds = [row["kind"] for row in self.success("events")]
        self.assertNotIn("decision.responded", kinds)
        self.assertNotIn("delivery.acknowledged", kinds)
        first = self.success(*response_args)
        second = self.success(*response_args)
        self.assertEqual(first["event"]["seq"], second["event"]["seq"])
        kinds = [row["kind"] for row in self.success("events")]
        self.assertEqual(1, kinds.count("decision.responded"))
        self.assertEqual(1, kinds.count("delivery.acknowledged"))


    def test_existing_partial_zero_files_are_never_fresh_init(self):
        result = self.command("init", before=f"""
from relay_runtime.enrollment import Registry
registry.enroll({str(self.repo)!r})
pathlib.Path({str(self.state / 'relay.sqlite3')!r}).touch(mode=0o600)
""")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(0, (self.state / "relay.sqlite3").stat().st_size)
        self.assertFalse((self.state / "relay.sqlite3-wal").exists())
        self.assertEqual([], list(self.registry.glob("ledger-*.json")))

    def test_hook_unavailable_fails_open_but_never_claims_empty_success(self):
        result = self.command("hook", "--client", "codex",
                              stdin='{"hook_event_name":"SessionStart","session_id":"fixture"}')
        self.assertEqual(0, result.returncode)
        self.assertIn("unavailable", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse(self.state.exists())

    def test_actual_lifecycle_hook_records_once(self):
        self.initialize()
        payload = '{"hook_event_name":"SessionStart","session_id":"fixture"}'
        for _ in range(2):
            result = self.command("hook", "--client", "codex", stdin=payload)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
        events = self.success("events")
        self.assertEqual(1, len([row for row in events if row["kind"] == "session.started"]))

    def test_extraneous_parent_writable_fd_is_closed_in_worker(self):
        self.initialize()
        sentinel = self.base / "unrelated"
        sentinel.write_bytes(b"unchanged")
        result = self.command("status", before=f"""
from relay_runtime import admission
fd = os.open({str(sentinel)!r}, os.O_WRONLY)
original = admission.Admission.confine_worker
def probe(self, **kwargs):
    original(self, **kwargs)
    try:
        os.write(fd, b"CORRUPTED")
    except OSError:
        return
    raise AssertionError("unrelated writable fd survived")
admission.Admission.confine_worker = probe
""")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"unchanged", sentinel.read_bytes())

    def test_foreign_file_write_and_namespace_creation_denied_in_worker(self):
        self.initialize()
        sentinel = self.base / "foreign"
        sentinel.write_bytes(b"unchanged")
        result = self.command("status", before=f"""
from relay_runtime import admission
original = admission.Admission.confine_worker
def probe(self, **kwargs):
    original(self, **kwargs)
    for path in ({str(sentinel)!r}, {str(self.base / 'unexpected')!r}):
        try:
            with open(path, "wb") as target:
                target.write(b"CORRUPTED")
        except PermissionError:
            continue
        raise AssertionError("write escaped file-object policy")
admission.Admission.confine_worker = probe
""")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"unchanged", sentinel.read_bytes())
        self.assertFalse((self.base / "unexpected").exists())

    def test_linked_worktree_resolves_same_enrolled_ledger(self):
        self.initialize()
        self.event()
        linked = self.base / "linked"
        subprocess.run(["/usr/bin/git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null",
                        "worktree", "add", "--detach", "-q", str(linked)], check=True)
        events = self.success("events", repo=linked)
        self.assertTrue(any(row["summary"] == "ready" for row in events))
        self.assertFalse((linked / ".relay").exists())
        self.assertEqual(1, len(list(self.registry.glob("ledger-*.json"))))

    def test_foreign_repository_cannot_reuse_enrollment(self):
        self.initialize()
        foreign = self.base / "foreign-project"
        subprocess.run(["/usr/bin/git", "init", "-q", str(foreign)], check=True)
        result = self.command("status", repo=foreign)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertFalse((foreign / ".relay").exists())

    def test_wal_commit_survives_worker_abrupt_exit_before_close(self):
        self.initialize()
        result = self.command("signal", "work.intent", "--agent", "codex", "--session",
                              "crash-session", "--work-id", "crash-demo", "--summary", "committed",
                              before="""
from relay_core.store import RelayStore
RelayStore.close = lambda self: os._exit(91)
""")
        self.assertEqual(91, result.returncode)
        self.assertEqual("", result.stdout)
        events = self.success("events")
        self.assertTrue(any(row["summary"] == "committed" for row in events))
        self.assertTrue(self.success("doctor")["ok"])

    def test_uncommitted_claim_disappears_after_worker_crash(self):
        self.initialize()
        result = self.command("claim", "code:crash", "--agent", "codex", "--session",
                              "crash-session", "--purpose", "uncommitted", before="""
from relay_runtime import admission
original = admission._GuardedConnection.execute
commits = 0
def crash(self, sql, *args, **kwargs):
    global commits
    if sql == "COMMIT":
        commits += 1
        # One schema-hardening transaction precedes the claim transaction.
        if commits == 2:
            os._exit(92)
    return original(self, sql, *args, **kwargs)
admission._GuardedConnection.execute = crash
""")
        self.assertEqual(92, result.returncode)
        self.assertEqual("", result.stdout)
        self.success("claim", "code:crash", "--agent", "claude", "--session",
                     "recovered", "--purpose", "claim is available")

    def test_parallel_claim_workers_have_exactly_one_owner(self):
        self.initialize()
        from concurrent.futures import ThreadPoolExecutor
        def attempt(index):
            return self.command("claim", "code:shared", "--agent", "codex", "--session",
                                f"worker-{index}", "--purpose", "race")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(1, sum(result.returncode == 0 for result in results),
                         [result.stderr for result in results])
        self.assertTrue(self.success("doctor")["ok"])

    def test_birth_witness_rejects_same_inode_with_different_creation_evidence(self):
        self.initialize()
        anchor_path = next(self.registry.glob("ledger-*.json"))
        anchor = json.loads(anchor_path.read_text())
        anchor["files"]["relay.sqlite3"]["birth_nanoseconds"] ^= 1
        anchor_path.write_text(json.dumps(anchor, sort_keys=True, separators=(",", ":")) + "\n")
        before = (self.state / "relay.sqlite3").read_bytes()
        result = self.command("status")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(before, (self.state / "relay.sqlite3").read_bytes())

    def test_missing_statx_birth_time_refuses_without_fallback(self):
        self.initialize()
        result = self.command("status", before="""
from relay_runtime import admission
from relay_runtime.confinement import ConfinementError
def unavailable(fd):
    raise ConfinementError("synthetic unavailable birth time")
admission.birth_time = unavailable
""")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_existing_unknown_schema_is_never_auto_initialized(self):
        self.initialize()
        # Mutate only this test fixture using a confined worker so sidecar
        # identities are preserved; then prove a subsequent writer refuses.
        result = self.command("status", before="""
from relay_runtime import admission
original = admission.Admission.connect
def corrupt(self, paths, *, read_only, timeout):
    connection = original(self, paths, read_only=False, timeout=timeout)
    connection.execute("PRAGMA user_version = 0")
    connection.close()
    os._exit(95)
admission.Admission.connect = corrupt
cli._readonly = lambda args: False
""")
        self.assertEqual(95, result.returncode)
        result = self.command("claim", "code:invalid", "--agent", "codex", "--session",
                              "schema", "--purpose", "must refuse")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("schema", result.stderr)

    def test_failed_confinement_hook_still_fails_open_without_receipt(self):
        self.initialize()
        result = self.command("hook", "--client", "codex", before="""
from relay_runtime import admission
from relay_runtime.confinement import ConfinementError
def fail(*args, **kwargs):
    raise ConfinementError("synthetic restriction failure")
admission.restrict_file_writes = fail
""")
        self.assertEqual(0, result.returncode)
        self.assertIn("unavailable", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse((self.state / "hook-errors.log").exists())

    def test_abrupt_file_reservations_never_gain_ledger_anchor(self):
        for cut in ("reserved-relay.sqlite3", "reserved-relay.sqlite3-wal",
                    "reserved-relay.sqlite3-shm"):
            with self.subTest(cut=cut):
                repo = self.base / cut
                subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
                result = self.command("init", repo=repo, before=f"""
from relay_runtime import admission
def die(stage):
    if stage == {cut!r}:
        os._exit(96)
admission._checkpoint = die
""")
                self.assertEqual(96, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertNotEqual(0, self.command("init", repo=repo).returncode)
        self.assertEqual([], list(self.registry.glob("ledger-*.json")))
