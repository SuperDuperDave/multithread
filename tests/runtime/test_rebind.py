"""Same-object repository rebind witnesses; all accounts and ledgers are disposable.

Compose the installed fixture as a module, never subclass its TestCase. These
tests open real initialized SQLite ledgers, not synthetic database byte strings.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_installed as installed
import test_recovery as recovery
from relay_runtime import enrollment as subject
from relay_runtime.confinement import birth_time

snapshot = recovery.snapshot

_CUT = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from relay_runtime import enrollment
def cut(stage):
    if stage == sys.argv[5]:
        os._exit(87)
enrollment._checkpoint = cut
registry = enrollment.Registry(pathlib.Path(sys.argv[2]))
registry.rebind(sys.argv[3], sys.argv[4], expected_binding=sys.argv[6], confirm_quiescent=True)
raise AssertionError('requested rebind checkpoint was not reached')
"""


class RebindTests(unittest.TestCase):
    def setUp(self):
        self.case = self.make_case()

    def make_case(self):
        fixture = installed.InstalledTests("test_real_verified_runtime_initializes_writes_reopens_and_diagnoses")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        parent = fixture.base / "parent"
        parent.mkdir(mode=0o700)
        nested = parent / "project"
        fixture.repo.rename(nested)
        fixture.repo = nested
        initialized = fixture.initialize()
        fixture.event("retain this real work intent across a repository move")
        claim = fixture.success("claim", "code:rebind", "--agent", "codex", "--session",
                                "holder", "--purpose", "retain ownership across a move")["claim"]
        events = fixture.success("events")
        claims = fixture.success("status")["active_claims"]
        self.assertEqual(2, len(events))
        self.assertEqual([claim["claim_id"]], [row["claim_id"] for row in claims])
        registry = subject.Registry(fixture.registry)
        entry = registry.lookup(nested)
        authority = registry._record_path(entry.workspace)
        anchor = fixture.registry / ("ledger-" + entry.enrollment_id + ".json")
        settings = fixture.base / "account/unrelated-settings"
        settings.mkdir()
        (settings / "hooks.fixture").write_bytes(b"synthetic unrelated settings; preserve\n")
        protected = {path: snapshot(path) for path in (fixture.install, settings, anchor)}
        return SimpleNamespace(fixture=fixture, base=fixture.base, registry=registry,
                               original=nested, repo=nested, entry=entry, authority=authority,
                               anchor=anchor, initialized=initialized, events=events,
                               claims=claims, claim=claim, protected=protected)

    def move(self, case=None, *, target=None):
        case = case or self.case
        old = case.repo
        target = target or case.base / "moved"
        old.rename(target)
        case.repo = target
        return old

    def plan(self, case, old):
        before = snapshot(case.base)
        plan = case.registry.rebind_plan(case.repo, old)
        self.assertEqual(before, snapshot(case.base), "rebind plan modified the fixture")
        self.assertEqual(case.entry.enrollment_id, plan["enrollment_id"])
        self.assertEqual(str(old), plan["from_repo"])
        self.assertEqual(str(case.repo), plan["repo"])
        self.assertTrue(plan["requires_quiescence"])
        self.assertFalse(plan["ledger_changed"])
        self.assertFalse(plan["enrollment_id_changed"])
        self.assertRegex(plan["expected_binding"], r"^[0-9a-f]{64}$")
        return plan

    def apply(self, case, old, token):
        return case.registry.rebind(case.repo, old, expected_binding=token, confirm_quiescent=True)

    def assert_refusal_unchanged(self, case, operation):
        before = snapshot(case.base)
        with self.assertRaises(subject.EnrollmentError):
            operation()
        self.assertEqual(before, snapshot(case.base))

    def assert_unavailable(self, case):
        self.assert_refusal_unchanged(case, lambda: case.registry.lookup(case.repo))
        self.assert_refusal_unchanged(case, lambda: case.registry.enroll(case.repo))
        before = snapshot(case.base)
        refused = case.fixture.command("status", repo=case.repo)
        self.assertNotEqual(0, refused.returncode)
        self.assertEqual("", refused.stdout)
        self.assertEqual(before, snapshot(case.base))

    def ledger_identities(self, repo):
        result = {}
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
            fd = os.open(repo / ".relay" / name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                result[name] = {"device": info.st_dev, "inode": info.st_ino, **birth_time(fd)}
            finally:
                os.close(fd)
        return result

    def cut_rebind(self, case, old, token, stage):
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", "-c", _CUT, str(installed.SOURCE),
             str(case.registry.root), str(case.repo), str(old), stage, token],
            env=subject._GIT_ENV, stdin=subprocess.DEVNULL, text=True,
            capture_output=True, timeout=20)
        self.assertEqual(87, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)

    def guards(self, case):
        return [(path, json.loads(path.read_text())) for path in sorted(case.registry.root.glob(
            "retired-binding-" + case.entry.enrollment_id + "-*.json"))]

    def assert_retained_objects(self, case, expected_objects):
        for expected in expected_objects:
            matches = [path for path in (case.base / "account").rglob("*")
                       if not path.is_symlink() and path.is_file()
                       and (path.stat().st_dev, path.stat().st_ino) == expected[:2]]
            self.assertEqual(1, len(matches))
            self.assertEqual(expected, snapshot(matches[0])["."])

    def assert_lookup_and_installed_events_refuse(self, case):
        before = snapshot(case.base)
        try:
            admitted = case.registry.lookup(case.repo)
        except subject.EnrollmentError:
            admitted = None
        with self.subTest(boundary="ordinary lookup", repository=case.repo.name):
            self.assertIsNone(admitted, "uncertain publication admitted a stale or unapproved head")
        result = case.fixture.command("events", repo=case.repo)
        with self.subTest(boundary="installed events", repository=case.repo.name):
            self.assertNotEqual(0, result.returncode, "uncertain publication reopened the real ledger")
            self.assertEqual("", result.stdout)
        self.assertEqual(before, snapshot(case.base))

    def assert_preserved(self, case, before_project, identities):
        self.assertEqual(before_project, snapshot(case.repo), "rebind changed project or ledger bytes")
        self.assertEqual(identities, self.ledger_identities(case.repo))
        for path, before in case.protected.items():
            self.assertEqual(before, snapshot(path), path.name)

    def assert_real_ledger(self, case, generation):
        entry = case.registry.lookup(case.repo)
        self.assertEqual(case.entry.enrollment_id, entry.enrollment_id)
        self.assertEqual(generation, entry.generation)
        self.assertEqual(case.events, case.fixture.success("events", repo=case.repo))
        self.assertEqual(case.claims, case.fixture.success("status", repo=case.repo)["active_claims"])
        self.assertTrue(case.fixture.success("doctor", repo=case.repo)["ok"])
        with sqlite3.connect((case.repo / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
            ledger.execute("PRAGMA query_only=ON")
            self.assertEqual("ok", ledger.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(len(case.events), ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_main_rename_plan_and_apply_preserve_real_ledger_and_active_claim(self):
        case = self.case
        identities = self.ledger_identities(case.repo)
        old = self.move()
        self.assert_unavailable(case)
        plan = self.plan(case, old)
        self.assertEqual((1, 2), (plan["generation"], plan["next_generation"]))
        before_project = snapshot(case.repo)
        result = self.apply(case, old, plan["expected_binding"])
        self.assertTrue(result["rebound"])
        self.assertEqual(2, result["generation"])
        self.assert_preserved(case, before_project, identities)
        self.assert_real_ledger(case, 2)
        for arguments in (
            ("claim", "code:rebind", "--agent", "claude", "--session", "contender", "--purpose", "must refuse"),
            ("release", case.claim["claim_id"], "--agent", "codex", "--session", "wrong-holder"),
        ):
            refused = case.fixture.command(*arguments, repo=case.repo)
            self.assertNotEqual(0, refused.returncode)
            self.assertEqual("", refused.stdout)
        self.assertEqual(case.events, case.fixture.success("events", repo=case.repo))
        self.assertEqual(case.claims, case.fixture.success("status", repo=case.repo)["active_claims"])

    def test_parent_rename_is_same_object_rebind_not_a_new_enrollment(self):
        case = self.case
        old = case.repo
        identities = self.ledger_identities(old)
        new_parent = case.base / "renamed-parent"
        old.parent.rename(new_parent)
        case.repo = new_parent / old.name
        self.assert_unavailable(case)
        plan = self.plan(case, old)
        before = snapshot(case.repo)
        self.apply(case, old, plan["expected_binding"])
        self.assert_preserved(case, before, identities)
        self.assert_real_ledger(case, 2)
        # The original authority and the new path's alias name one checkout, listed once at its new place.
        found, unreadable = case.registry.checkouts()
        self.assertEqual(([case.repo.resolve()], []), ([path.resolve() for path in found], unreadable))

    def test_explicit_git_repair_restores_peer_topology_before_main_rebind(self):
        case = self.case
        peer = case.base / "linked-peer"
        def git(repo, *arguments):
            return subprocess.run(
                ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *arguments],
                env=subject._GIT_ENV, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=True, timeout=15)
        git(case.repo, "worktree", "add", "--detach", "-q", str(peer))
        self.assertEqual(case.entry.enrollment_id, case.registry.lookup(peer).enrollment_id)
        self.assertEqual(case.events, case.fixture.success("events", repo=peer))
        identities = self.ledger_identities(case.repo)
        old = self.move()
        self.assert_refusal_unchanged(case, lambda: case.registry.lookup(peer))
        before = snapshot(case.base)
        refused = case.fixture.command("status", repo=peer)
        self.assertNotEqual(0, refused.returncode)
        self.assertEqual("", refused.stdout)
        self.assertEqual(before, snapshot(case.base))
        # This explicit Git command repairs Git's backlinks; Relay does not.
        git(case.repo, "worktree", "repair", str(peer))
        self.assert_refusal_unchanged(case, lambda: case.registry.lookup(peer))
        plan = self.plan(case, old)
        before_project, before_peer = snapshot(case.repo), snapshot(peer)
        self.apply(case, old, plan["expected_binding"])
        self.assert_preserved(case, before_project, identities)
        self.assertEqual(before_peer, snapshot(peer))
        self.assert_real_ledger(case, 2)
        peer_entry = case.registry.lookup(peer)
        self.assertEqual((case.entry.enrollment_id, 2), (peer_entry.enrollment_id, peer_entry.generation))
        self.assertEqual(case.repo / ".relay", peer_entry.workspace.state)
        self.assertEqual(case.events, case.fixture.success("events", repo=peer))
        self.assertEqual(case.claims, case.fixture.success("status", repo=peer)["active_claims"])
        self.assertTrue(case.fixture.success("doctor", repo=peer)["ok"])
        self.assertFalse((peer / ".relay").exists())
        self.assertEqual(1, len(list(case.registry.root.glob("ledger-*.json"))))

    def test_move_back_and_old_alias_reuse_require_fresh_generations(self):
        case = self.case
        first = case.repo
        second = case.base / "moved"
        tokens = []
        for generation, target in ((2, second), (3, first), (4, second)):
            old = self.move(target=target)
            self.assert_unavailable(case)  # An old exact route must not resurrect its old binding.
            plan = self.plan(case, old)
            self.assertEqual((generation - 1, generation), (plan["generation"], plan["next_generation"]))
            for token in tokens:
                self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
            before = snapshot(case.repo)
            identities = self.ledger_identities(case.repo)
            result = self.apply(case, old, plan["expected_binding"])
            self.assertEqual(generation, result["generation"])
            self.assert_preserved(case, before, identities)
            self.assert_real_ledger(case, generation)
            tokens.append(plan["expected_binding"])
        self.assertEqual(1, len(list(case.registry.root.glob("ledger-*.json"))))

    def test_invalid_approval_and_nonliteral_quiescence_refuse_without_writes(self):
        case = self.case
        old = self.move()
        plan = self.plan(case, old)
        for token in (None, "short", "0" * 64, True):
            self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
        for confirmation in (False, None, 1, "yes"):
            with self.subTest(confirmation=confirmation):
                self.assert_refusal_unchanged(case, lambda: case.registry.rebind(
                    case.repo, old, expected_binding=plan["expected_binding"], confirm_quiescent=confirmation))
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind(
            case.repo, old, expected_binding=plan["expected_binding"]))
        # A prepublished route must satisfy exact types, not Python's 2.0 == 2.
        alias = case.registry._record_path(subject.resolve_workspace(case.repo))
        alias.write_text(json.dumps({"v": 2.0, "kind": "binding-alias",
                                     "authority": case.authority.name,
                                     "enrollment_id": case.entry.enrollment_id}))
        alias.chmod(0o600)
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, plan["expected_binding"]))

    def test_actual_copy_with_identical_markers_and_ledger_bytes_is_not_adopted(self):
        case = self.case
        copy = case.base / "copy"
        identities = self.ledger_identities(case.repo)
        shutil.copytree(case.repo, copy)
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
            self.assertEqual((case.repo / ".relay" / name).read_bytes(), (copy / ".relay" / name).read_bytes())
        self.assertNotEqual(identities, self.ledger_identities(copy))
        old = self.move(target=case.base / "retained-original")
        case.repo = copy
        self.assert_unavailable(case)
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(copy, old))
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, "0" * 64))

    def test_same_bytes_replacement_of_each_anchored_file_refuses(self):
        case = self.case
        old = self.move()
        token = self.plan(case, old)["expected_binding"]
        for name in ("relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"):
            with self.subTest(name=name):
                path = case.repo / ".relay" / name
                held = case.base / ("held-" + name)
                path.rename(held)
                path.write_bytes(held.read_bytes())
                path.chmod(0o600)
                self.assertNotEqual(held.stat().st_ino, path.stat().st_ino)
                self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
                self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
                path.unlink()  # Only this test's freshly created replacement object.
                held.rename(path)

    def test_occupied_old_path_and_missing_ledger_anchor_refuse_without_writes(self):
        case = self.case
        old = self.move()
        token = self.plan(case, old)["expected_binding"]
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    old.write_bytes(b"foreign object at old name")
                elif kind == "directory":
                    old.mkdir(mode=0o700)
                    (old / "sentinel").write_bytes(b"foreign directory at old name")
                else:
                    old.symlink_to(case.repo, target_is_directory=True)
                self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
                self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
                if kind == "directory":
                    (old / "sentinel").unlink()
                    old.rmdir()
                else:
                    old.unlink()
        case.anchor.rename(case.base / "retained-ledger-anchor.json")
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))

    def test_unsafe_container_and_authority_modes_are_never_repaired(self):
        case = self.case
        old = self.move()
        token = self.plan(case, old)["expected_binding"]
        for path, unsafe in ((case.registry.root, 0o755), (case.repo / ".relay", 0o755),
                             (case.repo / ".git", 0o775), (case.authority, 0o644)):
            with self.subTest(path=path.name):
                original_mode = path.stat().st_mode & 0o777
                path.chmod(unsafe)
                self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
                self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
                path.chmod(original_mode)
        state = case.repo / ".relay"
        held = case.base / "retained-state"
        state.rename(held)
        state.symlink_to(held, target_is_directory=True)
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))

    def test_abrupt_publication_cuts_have_stateful_retry_and_no_ledger_writes(self):
        stages = ("rebind-alias-prepared", "rebind-alias-published", "rebind-head-prepared",
                  "rebind-guard-prepared", "rebind-guard-published", "rebind-guard-synced",
                  "rebind-before-exchange", "rebind-head-exchanged", "rebind-head-synced")
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                case = self.case if index == 0 else self.make_case()
                old = self.move(case)
                token = self.plan(case, old)["expected_binding"]
                before = snapshot(case.repo)
                identities = self.ledger_identities(case.repo)
                result = subprocess.run(
                    ["/usr/bin/python3", "-I", "-S", "-B", "-c", _CUT, str(installed.SOURCE),
                     str(case.registry.root), str(case.repo), str(old), stage, token],
                    env=subject._GIT_ENV, stdin=subprocess.DEVNULL, text=True,
                    capture_output=True, timeout=20)
                self.assertEqual(87, result.returncode, result.stdout + result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
                self.assert_preserved(case, before, identities)
                if stage in ("rebind-head-exchanged", "rebind-head-synced"):
                    self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
                    self.assert_real_ledger(case, 2)
                else:
                    self.assert_unavailable(case)
                    retry = self.plan(case, old)
                    self.assertEqual(token, retry["expected_binding"])
                    self.apply(case, old, token)
                    self.assert_preserved(case, before, identities)
                    self.assert_real_ledger(case, 2)

    def test_unknown_head_or_alias_replacement_is_preserved_without_success(self):
        stages = ("rebind-alias-prepared", "rebind-alias-published",
                  "rebind-before-exchange", "rebind-head-exchanged")
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                case = self.case if index == 0 else self.make_case()
                old = self.move(case)
                token = self.plan(case, old)["expected_binding"]
                alias = case.registry._record_path(subject.resolve_workspace(case.repo))
                target = alias if stage.startswith("rebind-alias") else case.authority
                witness = {}
                before_project = snapshot(case.repo)
                def substitute(position):
                    if position != stage:
                        return
                    if target.exists():
                        target.rename(case.base / "retained-route.json")
                    target.write_bytes(b'{"foreign":"must remain recoverable"}\n')
                    target.chmod(0o600)
                    witness["info"] = target.stat()
                with mock.patch.object(subject, "_checkpoint", side_effect=substitute):
                    with self.assertRaises(subject.EnrollmentError):
                        self.apply(case, old, token)
                self.assertIn("info", witness)
                before = witness["info"]
                matches = [path for path in case.registry.root.iterdir()
                           if not path.is_symlink() and path.is_file()
                           and (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)]
                self.assertEqual(1, len(matches))
                after = matches[0].stat()
                self.assertEqual((before.st_dev, before.st_ino, before.st_mode, before.st_size,
                                  before.st_mtime_ns, before.st_nlink),
                                 (after.st_dev, after.st_ino, after.st_mode, after.st_size,
                                  after.st_mtime_ns, after.st_nlink))
                self.assertEqual(b'{"foreign":"must remain recoverable"}\n', matches[0].read_bytes())
                self.assertEqual(before_project, snapshot(case.repo))

    def test_syscall_edge_source_substitution_cannot_resurrect_retained_v1_authority(self):
        case = self.case
        original_repo = case.repo
        old = self.move(target=case.base / "generation-two")
        first = self.apply(case, old, self.plan(case, old)["expected_binding"])
        self.assertEqual(2, first["generation"])
        stale = Path(first["retained_binding"])
        stale_record = json.loads(stale.read_text())
        self.assertEqual((1, 1), (stale_record["v"], stale_record["generation"]))
        stale_witness = snapshot(stale)["."]
        current_witness = snapshot(case.authority)["."]
        old = self.move(target=case.base / "generation-three")
        token = self.plan(case, old)["expected_binding"]
        before_project = snapshot(case.repo)
        identities = self.ledger_identities(case.repo)
        saved_prepared = case.base / "account/saved-prepared-generation-three.json"
        original_rename = subject._rename_record
        witness = {}

        def substitute_source_at_syscall(directory, source, destination, flags):
            if flags == 2 and not witness:
                prepared = directory.path / source
                record = json.loads(prepared.read_text())
                self.assertEqual(3, record["generation"])
                witness["prepared"] = snapshot(prepared)["."]
                prepared.rename(saved_prepared)
                stale.rename(prepared)
                # Invoke the real renameat2 exchange after the last verification.
                # No record bytes are forged and authority is never directly written.
                result = original_rename(directory, source, destination, flags)
                witness["exchanged"] = True
                return result
            return original_rename(directory, source, destination, flags)

        with mock.patch.object(subject, "_rename_record", side_effect=substitute_source_at_syscall):
            with self.assertRaises(subject.EnrollmentError):
                self.apply(case, old, token)
        self.assertTrue(witness.get("exchanged"), "the actual syscall-edge substitution did not execute")
        self.assert_preserved(case, before_project, identities)
        # Preserve each displaced/prepared object irrespective of its eventual
        # retained name; throwing an exception is not itself rollback protection.
        for expected in (stale_witness, current_witness, witness["prepared"]):
            matches = [path for path in (case.base / "account").rglob("*")
                       if not path.is_symlink() and path.is_file()
                       and (path.stat().st_dev, path.stat().st_ino) == expected[:2]]
            self.assertEqual(1, len(matches))
            self.assertEqual(expected, snapshot(matches[0])["."])

        self.move(target=original_repo)
        self.assert_preserved(case, before_project, identities)
        before_lookup = snapshot(case.base)
        try:
            resurrected = case.registry.lookup(case.repo)
        except subject.EnrollmentError:
            resurrected = None
        self.assertEqual(before_lookup, snapshot(case.base))
        with self.subTest(boundary="ordinary lookup"):
            self.assertIsNone(resurrected, "stale retained head authorized generation "
                              + str(resurrected.generation if resurrected else None))
        result = case.fixture.command("events", repo=case.repo)
        reopened = json.loads(result.stdout) if result.returncode == 0 else None
        if reopened is not None:
            self.assertEqual(case.events, reopened, "the regression must exercise the original real ledger")
        with self.subTest(boundary="installed command"):
            self.assertNotEqual(0, result.returncode, "stale authority reopened "
                                + str(len(reopened) if reopened is not None else 0) + " actual ledger events")
            self.assertEqual("", result.stdout)

    def test_published_retirement_pins_target_and_exact_retry_preserves_successor(self):
        case = self.case
        original_repo = case.repo
        old = self.move(target=case.base / "generation-two")
        self.apply(case, old, self.plan(case, old)["expected_binding"])
        old = self.move(target=case.base / "pinned-generation-three")
        pinned_target = case.repo
        token = self.plan(case, old)["expected_binding"]
        before_project = snapshot(case.repo)
        identities = self.ledger_identities(case.repo)
        self.cut_rebind(case, old, token, "rebind-guard-published")
        guards = self.guards(case)
        self.assertEqual([1, 2], sorted(row["prior"]["generation"] for _, row in guards))
        guard_path, guard = next(pair for pair in guards if pair[1]["prior"]["generation"] == 2)
        successor = guard["successor"]
        self.assertEqual((3, str(pinned_target / ".git")), (successor["generation"], successor["common"]))
        guard_before = snapshot(guard_path)
        pending = self.plan(case, old)
        self.assertTrue(pending["retirement_pending"])
        self.assertEqual(successor["binding_id"], pending["next_binding_id"])
        self.assertEqual(token, pending["expected_binding"])
        self.assert_preserved(case, before_project, identities)

        self.move(target=case.base / "unapproved-generation-three")
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))
        self.assert_lookup_and_installed_events_refuse(case)
        # Even matching old physical locations cannot reactivate either prior.
        for prior_path in (old, original_repo):
            self.move(target=prior_path)
            self.assert_lookup_and_installed_events_refuse(case)
        self.move(target=pinned_target)
        self.assert_lookup_and_installed_events_refuse(case)
        result = self.apply(case, old, token)
        self.assertEqual((3, successor["binding_id"]), (result["generation"], result["binding_id"]))
        self.assertEqual(successor, json.loads(case.authority.read_text()))
        self.assertEqual(guard_before, snapshot(guard_path))
        self.assertEqual(2, len(self.guards(case)), "same-target retry created another retirement authority")
        self.assert_preserved(case, before_project, identities)
        self.assert_real_ledger(case, 3)
        self.assert_refusal_unchanged(case, lambda: self.apply(case, old, token))

    def test_genuine_unapproved_same_generation_source_cannot_override_pinned_target(self):
        case = self.case
        old = self.move(target=case.base / "generation-two")
        self.apply(case, old, self.plan(case, old)["expected_binding"])
        old = self.move(target=case.base / "unapproved-candidate")
        candidate_repo = case.repo
        candidate_token = self.plan(case, old)["expected_binding"]
        self.cut_rebind(case, old, candidate_token, "rebind-head-prepared")
        self.assertEqual([1], [row["prior"]["generation"] for _, row in self.guards(case)])
        candidates = [path for path in case.registry.root.glob(".retained-rebind-*.json")
                      if json.loads(path.read_text()).get("generation") == 3]
        self.assertEqual(1, len(candidates))
        unapproved = candidates[0]
        old_record = json.loads(unapproved.read_text())
        self.assertEqual(str(candidate_repo / ".git"), old_record["common"])
        unapproved_witness = snapshot(unapproved)["."]
        current_witness = snapshot(case.authority)["."]
        self.move(target=case.base / "approved-candidate")
        approved_repo = case.repo
        plan = self.plan(case, old)
        self.assertFalse(plan["retirement_pending"])
        before_project = snapshot(case.repo)
        identities = self.ledger_identities(case.repo)
        saved = case.base / "account/saved-approved-head.json"
        original_rename = subject._rename_record
        witness = {}
        def substitute_source(directory, source, destination, flags):
            if flags == 2 and not witness:
                prepared = directory.path / source
                approved = json.loads(prepared.read_text())
                self.assertEqual((3, str(approved_repo / ".git")), (approved["generation"], approved["common"]))
                self.assertNotEqual(old_record["binding_id"], approved["binding_id"])
                witness["approved"] = approved
                witness["prepared"] = snapshot(prepared)["."]
                prepared.rename(saved)
                unapproved.rename(prepared)
                result = original_rename(directory, source, destination, flags)
                witness["exchanged"] = True
                return result
            return original_rename(directory, source, destination, flags)
        with mock.patch.object(subject, "_rename_record", side_effect=substitute_source):
            with self.assertRaises(subject.EnrollmentError):
                self.apply(case, old, plan["expected_binding"])
        self.assertTrue(witness.get("exchanged"))
        highest = max((row for _, row in self.guards(case)), key=lambda row: row["prior"]["generation"])
        self.assertEqual(witness["approved"], highest["successor"])
        self.assert_retained_objects(case, (unapproved_witness, current_witness, witness["prepared"]))
        self.assert_preserved(case, before_project, identities)
        self.assert_lookup_and_installed_events_refuse(case)
        self.move(target=candidate_repo)
        self.assert_preserved(case, before_project, identities)
        self.assert_lookup_and_installed_events_refuse(case)
        self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, old))

    def test_guard_source_substitution_at_real_noreplace_boundary_fails_closed(self):
        case = self.case
        old = self.move(target=case.base / "generation-two")
        self.apply(case, old, self.plan(case, old)["expected_binding"])
        first_guards = self.guards(case)
        self.assertEqual(1, len(first_guards))
        prior_guard = first_guards[0][0]
        prior_witness = snapshot(prior_guard)["."]
        old = self.move(target=case.base / "generation-three")
        token = self.plan(case, old)["expected_binding"]
        before_project = snapshot(case.repo)
        identities = self.ledger_identities(case.repo)
        saved = case.base / "account/saved-intended-retirement.json"
        original_rename = subject._rename_record
        witness = {}
        def substitute_guard(directory, source, destination, flags):
            if flags == 1 and destination.startswith("retired-binding-") and not witness:
                prepared = directory.path / source
                intended = json.loads(prepared.read_text())
                self.assertEqual(2, intended["prior"]["generation"])
                witness["prepared"] = snapshot(prepared)["."]
                prepared.rename(saved)
                prior_guard.rename(prepared)
                result = original_rename(directory, source, destination, flags)
                witness["published"] = True
                return result
            return original_rename(directory, source, destination, flags)
        with mock.patch.object(subject, "_rename_record", side_effect=substitute_guard):
            with self.assertRaises(subject.EnrollmentError):
                self.apply(case, old, token)
        self.assertTrue(witness.get("published"), "real guard no-replace publication was not exercised")
        self.assert_retained_objects(case, (prior_witness, witness["prepared"]))
        self.assert_preserved(case, before_project, identities)
        self.assert_lookup_and_installed_events_refuse(case)
        self.move(target=old)
        self.assert_preserved(case, before_project, identities)
        self.assert_lookup_and_installed_events_refuse(case)

    def test_malformed_duplicate_or_incomplete_retirement_sequence_never_authorizes(self):
        case = self.case
        for target in (case.base / "generation-two", case.base / "generation-three"):
            old = self.move(target=target)
            self.apply(case, old, self.plan(case, old)["expected_binding"])
        current = case.repo
        guards = sorted(self.guards(case), key=lambda pair: pair[1]["prior"]["generation"])
        self.assertEqual([1, 2], [row["prior"]["generation"] for _, row in guards])
        next_target = case.base / "generation-four"
        self.move(target=next_target)
        next_token = self.plan(case, current)["expected_binding"]
        self.move(target=current)
        for variant in ("malformed", "hash-mismatch", "duplicate-generation", "hole"):
            with self.subTest(variant=variant):
                first_path, first_record = guards[0]
                last_path = guards[1][0]
                original_last = last_path.read_bytes()
                extra = None
                held = case.base / "account/held-first-retirement.json"
                if variant == "malformed":
                    last_path.write_bytes(b"{")
                elif variant == "hash-mismatch":
                    modified = json.loads(original_last)
                    modified["successor"]["binding_id"] = "f" * 32
                    last_path.write_bytes(subject._canonical(modified))
                elif variant == "duplicate-generation":
                    modified = json.loads(json.dumps(first_record))
                    modified["successor"]["binding_id"] = "f" * 32
                    body = subject._canonical(modified)
                    extra = case.registry.root / ("retired-binding-" + case.entry.enrollment_id
                                                   + "-1-" + hashlib.sha256(body).hexdigest() + ".json")
                    self.assertFalse(extra.exists())
                    extra.write_bytes(body)
                    extra.chmod(0o600)
                else:
                    first_path.rename(held)
                self.assert_lookup_and_installed_events_refuse(case)
                self.assert_refusal_unchanged(case, lambda: case.registry.enroll(case.repo))
                self.move(target=next_target)
                self.assert_refusal_unchanged(case, lambda: case.registry.rebind_plan(case.repo, current))
                self.assert_refusal_unchanged(case, lambda: self.apply(case, current, next_token))
                self.move(target=current)
                if variant in ("malformed", "hash-mismatch"):
                    last_path.write_bytes(original_last)
                elif extra is not None:
                    extra.unlink()  # Only the malformed fixture record created above.
                else:
                    held.rename(first_path)
        self.assert_real_ledger(case, 3)

    def test_workspace_substitution_during_head_preparation_preserves_foreign_namespace(self):
        case = self.case
        old = self.move()
        token = self.plan(case, old)["expected_binding"]
        foreign = case.base / "foreign-directory"
        foreign.mkdir(mode=0o700)
        (foreign / "sentinel").write_bytes(b"foreign namespace must not be touched")
        before_foreign = snapshot(foreign)
        before_project = snapshot(case.repo)
        held = case.base / "held-project"
        fired = []
        def substitute(stage):
            if stage == "rebind-head-prepared":
                case.repo.rename(held)
                case.repo.symlink_to(foreign, target_is_directory=True)
                fired.append(stage)
        with mock.patch.object(subject, "_checkpoint", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.apply(case, old, token)
        self.assertEqual(["rebind-head-prepared"], fired)
        self.assertEqual(before_foreign, snapshot(foreign))
        self.assertEqual(before_project, snapshot(held))
        self.assert_refusal_unchanged(case, lambda: case.registry.lookup(case.repo))


if __name__ == "__main__":
    unittest.main()
