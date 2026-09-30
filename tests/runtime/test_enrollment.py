"""Enrollment witnesses use disposable repositories and internal test registries."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from relay_runtime import enrollment as subject


def snapshot(root):
    """Read-only byte/mode/mtime witness, excluding directory atime."""
    result = {}
    if root.exists():
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            payload = os.readlink(path) if path.is_symlink() else (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            )
            result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, payload)
    return result


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-enrollment-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        self.git(self.repo, "init", "--template=", "-q")
        self.registry = subject.Registry(self.base / "account" / "enrollments")

    def git(self, directory, *args):
        env = dict(subject._GIT_ENV, GIT_AUTHOR_NAME="Test Fixture",
                   GIT_COMMITTER_NAME="Test Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_EMAIL="fixture@example.invalid")
        return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
                              env=env, input="fixture\n", text=True, capture_output=True, check=True)

    def second_repo(self, name="other"):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "--template=", "-q")
        return repo

    def assert_refusal_unchanged(self, operation):
        before = snapshot(self.base)
        with self.assertRaises(subject.EnrollmentError):
            operation()
        self.assertEqual(before, snapshot(self.base))

    def record_path(self):
        return next(self.registry.root.glob("*.json"))

    def test_unknown_lookup_does_not_create_anything(self):
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        self.assertFalse(self.registry.root.exists())
        self.assertFalse((self.repo / ".relay").exists())

    def test_explicit_enrollment_reserves_markers_without_database(self):
        entry = self.registry.enroll(self.repo)
        self.assertEqual(entry.generation, 1)
        self.assertEqual(entry.workspace.common, self.repo / ".git")
        self.assertEqual(entry.workspace.state, self.repo / ".relay")
        self.assertEqual({p.name for p in entry.workspace.state.iterdir()}, {"enrollment.json"})
        self.assertEqual(0o700, entry.workspace.state.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.record_path().stat().st_mode & 0o777)
        self.assertFalse(list(self.base.rglob("*.sqlite3")))

    def test_reenrollment_and_lookup_are_exact_noops_with_populated_state(self):
        first = self.registry.enroll(self.repo)
        (first.workspace.state / "relay.sqlite3").write_bytes(b"synthetic populated ledger, never opened")
        before = snapshot(self.base)
        self.assertEqual(first, self.registry.lookup(self.repo))
        self.assertEqual(first, self.registry.enroll(self.repo))
        self.assertEqual(before, snapshot(self.base))

    def test_existing_unregistered_state_is_never_adopted(self):
        state = self.repo / ".relay"
        state.mkdir()
        (state / "relay.sqlite3").write_bytes(b"existing unrelated ledger")
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))
        self.assertFalse(self.registry.root.exists())

    def test_empty_state_is_not_implicitly_enrolled(self):
        (self.repo / ".relay").mkdir()
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_existing_git_marker_is_not_adopted(self):
        (self.repo / ".git" / "relay-enrollment.json").write_text("{}")
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_foreign_clone_with_identical_head_and_origin_remains_unenrolled(self):
        self.git(self.repo, "commit", "--allow-empty", "-q", "-F", "-")
        self.git(self.repo, "remote", "add", "origin", "https://example.invalid/example/repo.git")
        self.registry.enroll(self.repo)
        clone = self.base / "clone"
        self.git(self.base, "clone", "--no-local", "--template=", "-q", str(self.repo), str(clone))
        self.git(clone, "remote", "set-url", "origin", "https://example.invalid/example/repo.git")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD").stdout, self.git(self.repo, "rev-parse", "HEAD").stdout)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(clone))

    def test_linked_worktree_shares_enrollment(self):
        self.git(self.repo, "commit", "--allow-empty", "-q", "-F", "-")
        first = self.registry.enroll(self.repo)
        linked = self.base / "linked"
        self.git(self.repo, "worktree", "add", "--detach", "-q", str(linked))
        second = self.registry.lookup(linked)
        self.assertEqual(first.enrollment_id, second.enrollment_id)
        self.assertEqual(first.workspace.state, second.workspace.state)
        self.assertEqual(second.workspace.root, linked)
        before = snapshot(self.base)
        self.assertEqual(second, self.registry.enroll(linked))
        self.assertEqual(before, snapshot(self.base))

    def test_forged_git_file_alias_is_not_a_linked_worktree(self):
        self.registry.enroll(self.repo)
        alias = self.base / "alias"
        alias.mkdir()
        (alias / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        self.assert_refusal_unchanged(lambda: self.registry.lookup(alias))

    def test_replaced_git_directory_refuses(self):
        self.registry.enroll(self.repo)
        (self.repo / ".git").rename(self.repo / ".git-old")
        self.git(self.repo, "init", "--template=", "-q")
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_recreation_nonce_is_required_even_if_fingerprint_were_reused(self):
        self.registry.enroll(self.repo)
        (self.repo / ".git" / "relay-enrollment.json").unlink()
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_replaced_state_directory_refuses(self):
        self.registry.enroll(self.repo)
        state = self.repo / ".relay"
        state.rename(self.repo / ".relay-old")
        state.mkdir(mode=0o700)
        (state / "enrollment.json").write_bytes((self.repo / ".relay-old" / "enrollment.json").read_bytes())
        (state / "enrollment.json").chmod(0o600)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_moved_checkout_requires_explicit_recovery(self):
        self.registry.enroll(self.repo)
        moved = self.base / "moved"
        self.repo.rename(moved)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(moved))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(moved))

    def test_bare_repository_refuses(self):
        bare = self.base / "bare.git"
        self.git(self.base, "init", "--bare", "--template=", "-q", str(bare))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(bare))

    def test_separate_git_repositories_sharing_parent_refuse(self):
        for name in ("a", "b"):
            work = self.base / f"work-{name}"
            self.git(self.base, "init", "--template=", "-q", "--separate-git-dir", str(self.base / f"meta-{name}"), str(work))
            self.assert_refusal_unchanged(lambda: self.registry.enroll(work))
        self.assertFalse((self.base / ".relay").exists())

    def test_requested_symlink_is_rejected_before_normalization(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assert_refusal_unchanged(lambda: self.registry.enroll(alias))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(alias / ".." / "project"))

    def test_state_symlink_is_not_followed(self):
        foreign = self.base / "private"
        foreign.mkdir()
        (foreign / "relay.sqlite3").write_bytes(b"unrelated state")
        (self.repo / ".relay").symlink_to(foreign, target_is_directory=True)
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_unsafe_registry_permissions_are_not_repaired(self):
        self.registry.enroll(self.repo)
        self.registry.root.chmod(0o755)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_unsafe_git_permissions_refuse_before_state_reservation(self):
        (self.repo / ".git").chmod(0o775)
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))
        self.assertFalse((self.repo / ".relay").exists())

    def test_permission_refusal_names_observed_directory_mode_and_requirement(self):
        for private in (False, True):
            with self.subTest(private=private):
                path = self.base / ("private\n\x1b[2J" if private else "shared 雪")
                path.mkdir()
                path.chmod(0o750 if private else 0o775)
                before = snapshot(self.base)
                with subject._Custody() as custody:
                    with self.assertRaises(subject.EnrollmentError) as refused:
                        custody.get(path, private=private)
                message = str(refused.exception)
                self.assertIn("enrollment directory permissions are unsafe", message)
                self.assertIn(json.dumps(str(path), ensure_ascii=True), message)
                self.assertIn("observed mode 0750" if private else "observed mode 0775", message)
                self.assertIn("group/other access is not allowed for a private directory"
                              if private else "group/other write access is not allowed", message)
                self.assertNotIn("\x1b", message)
                self.assertNotIn("\n", message)
                self.assertEqual(before, snapshot(self.base))

    def complete_refusal(self, operation, repo=None):
        """One unsafe-directory refusal, which wrote nothing, completed with every fix."""
        before = snapshot(self.base)
        with self.assertRaises(subject.UnsafeDirectory) as refused:
            operation()
        self.assertEqual(before, snapshot(self.base))
        return str(subject.permission_refusal(repo or self.repo, self.registry.root, refused.exception))

    @staticmethod
    def apply_fixes(message):
        """Run exactly the commands the refusal lists, and return the directories they name."""
        fixes = [shlex.split(line.strip().split("   (")[0]) for line in message.splitlines()
                 if line.startswith("  chmod ")]
        for fix in fixes:
            subprocess.run(["/usr/bin/chmod", *fix[1:]], check=True)
        return [Path(fix[-1]) for fix in fixes]

    def test_one_refusal_lists_every_unsafe_directory_and_its_fixes_suffice(self):
        # A clone made under umask 002: every directory it created is group-writable.
        account = self.registry.root.parent
        account.mkdir()
        (self.repo / "src").mkdir()
        for path in (self.repo, self.repo / ".git", self.repo / ".git" / "objects", self.repo / "src", account):
            path.chmod(0o775)
        message = self.complete_refusal(lambda: self.registry.enroll(self.repo))
        self.assertTrue(message.startswith("enrollment directory permissions are unsafe: other users could "
                                           "change 3 directories this checkout's enrollment relies on"), message)
        self.assertIn("\n  " + shlex.join(["chmod", "g-w,o-w", str(self.repo)])
                      + "   (observed mode 0775; group/other write access is not allowed)\n", message)
        self.assertTrue(message.endswith("\nA umask of 002 creates directories with group write, "
                                         "so a fresh clone can start this way."))
        # Only the directories enrollment checks, in its order; each fix is non-recursive.
        self.assertEqual([self.repo, self.repo / ".git", account], self.apply_fixes(message))
        self.assertEqual(1, self.registry.enroll(self.repo).generation)
        self.assertEqual(0o775, (self.repo / "src").stat().st_mode & 0o777)

    def test_linked_worktree_refusal_names_the_shared_git_directories(self):
        self.git(self.repo, "commit", "--allow-empty", "-q", "-F", "-")
        linked = self.base / "linked"
        self.git(self.repo, "worktree", "add", "--detach", "-q", str(linked))
        shared = self.repo / ".git" / "worktrees"
        for path in (linked, self.repo, shared, shared / "linked"):
            path.chmod(0o775)
        message = self.complete_refusal(lambda: self.registry.enroll(linked), linked)
        self.assertEqual([linked, self.repo, shared, shared / "linked"], self.apply_fixes(message))
        self.assertEqual(self.repo / ".relay", self.registry.enroll(linked).workspace.state)

    def test_private_directories_get_the_stricter_fix(self):
        self.registry.enroll(self.repo)
        for path in (self.repo / ".relay", self.registry.root):
            path.chmod(0o750)
        message = self.complete_refusal(lambda: self.registry.lookup(self.repo))
        self.assertIn("\n  " + shlex.join(["chmod", "g-rwx,o-rwx", str(self.repo / ".relay")])
                      + "   (observed mode 0750; group/other access is not allowed for a private directory)", message)
        self.assertNotIn("umask", message)
        self.assertEqual([self.repo / ".relay", self.registry.root], self.apply_fixes(message))
        self.registry.lookup(self.repo)

    def test_scan_follows_no_symlink_and_escapes_unprintable_paths(self):
        target = self.base / "elsewhere"
        (target / "inner").mkdir(parents=True)
        (target / "inner").chmod(0o777)
        (self.base / "alias").symlink_to(target, target_is_directory=True)
        self.assertEqual([], subject.unsafe_directories(self.base / "alias" / "inner", self.registry.root))
        odd = self.base / "odd\n\x1b[2J"
        odd.mkdir()
        odd.chmod(0o775)
        message = str(subject.permission_refusal(odd, self.registry.root, subject.UnsafeDirectory(odd, 0o775, False)))
        self.assertIn(json.dumps(["chmod", "g-w,o-w", str(odd)], ensure_ascii=True) + " (JSON argument list)", message)
        self.assertNotIn("\x1b", message)
        self.assertEqual(3, len(message.splitlines()))

    def test_unavailable_scan_keeps_the_directory_actually_refused(self):
        refused = subject.UnsafeDirectory(self.repo, 0o770, False)
        with mock.patch.object(subject, "unsafe_directories", side_effect=OSError("artificial")):
            message = str(subject.permission_refusal(self.repo, self.registry.root, refused))
        self.assertIn("change 1 directory", message)
        self.assertIn(shlex.join(["chmod", "g-w,o-w", str(self.repo)]) + "   (observed mode 0770;", message)

    def test_private_registry_leaf_below_writable_parent_refuses(self):
        self.registry.enroll(self.repo)
        self.registry.root.parent.chmod(0o775)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_unsafe_registry_parent_refuses_before_any_creation(self):
        self.registry.root.parent.mkdir(mode=0o775)
        self.registry.root.parent.chmod(0o775)
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))
        self.assertFalse(self.registry.root.exists())
        self.assertFalse((self.repo / ".relay").exists())

    def test_record_symlink_hardlink_and_permissions_refuse(self):
        self.registry.enroll(self.repo)
        record = self.record_path()
        record.chmod(0o644)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        record.chmod(0o600)
        extra = self.base / "extra-record"
        os.link(record, extra)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        extra.unlink()
        record.rename(extra)
        record.symlink_to(extra)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_invalid_record_variants_refuse_without_repair(self):
        self.registry.enroll(self.repo)
        path = self.record_path()
        original = path.read_text()
        values = ["{", "[]", original[:-2] + ',"v":1}\n',
                  json.dumps(dict(json.loads(original), extra="unexpected")),
                  json.dumps(dict(json.loads(original), generation=True)), "x" * 8193]
        for value in values:
            with self.subTest(value=value[:20]):
                path.write_text(value)
                self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_registry_cannot_redirect_state_to_another_enrolled_project(self):
        self.registry.enroll(self.repo)
        record = self.registry._record_path(subject.resolve_workspace(self.repo))
        other = self.second_repo()
        other_entry = self.registry.enroll(other)
        (other_entry.workspace.state / "relay.sqlite3").write_bytes(b"must not be opened")
        value = json.loads(record.read_text())
        value["state"] = str(other_entry.workspace.state)
        record.write_text(json.dumps(value))
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_git_and_home_environment_cannot_redirect_identity(self):
        first = self.registry.enroll(self.repo)
        other = self.second_repo()
        self.registry.enroll(other)
        forged = {"HOME": str(other), "XDG_CONFIG_HOME": str(other), "XDG_DATA_HOME": str(other),
                  "GIT_DIR": str(other / ".git"), "GIT_COMMON_DIR": str(other / ".git"),
                  "GIT_WORK_TREE": str(other), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.worktree",
                  "GIT_CONFIG_VALUE_0": str(other), "RELAY_HOME": str(other / ".relay"), "PATH": str(other)}
        expected_root = subject.default_registry_root()
        before = snapshot(self.base)
        with mock.patch.dict(os.environ, forged):
            self.assertEqual(first, self.registry.lookup(self.repo))
            self.assertEqual(expected_root, subject.default_registry_root())
        self.assertEqual(before, snapshot(self.base))

    def test_failure_before_account_publication_leaves_non_authoritative_orphan(self):
        original = subject._publish_new
        def interrupt(directory, name, value, **kwargs):
            if directory.path == self.registry.root:
                raise OSError("simulated publication failure")
            return original(directory, name, value, **kwargs)
        with mock.patch.object(subject, "_publish_new", side_effect=interrupt):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertFalse(list(self.registry.root.glob("*.json")))
        self.assertTrue((self.repo / ".relay" / "enrollment.json").exists())
        self.assertFalse(list(self.base.rglob("*.sqlite3")))
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_concurrent_enrollment_never_replaces_the_winner(self):
        def attempt(_):
            try:
                return self.registry.enroll(self.repo).enrollment_id
            except subject.EnrollmentError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        identities = {value for value in results if value is not None}
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities, {self.registry.lookup(self.repo).enrollment_id})
        self.assertEqual(len(list(self.registry.root.glob("*.json"))), 1)

    def test_new_registry_directory_links_are_fsynced_before_record_publication(self):
        synchronized = []
        original = subject._fsync_directory
        def synchronize(directory):
            synchronized.append(directory.path)
            original(directory)
        with mock.patch.object(subject, "_fsync_directory", side_effect=synchronize):
            self.registry.enroll(self.repo)
        self.assertEqual(synchronized[:2], [self.base, self.registry.root.parent])
        self.assertEqual(synchronized[-1], self.registry.root)
        with mock.patch.object(subject, "_fsync_directory") as synchronization:
            self.registry.enroll(self.repo)
            synchronization.assert_not_called()



    def test_workspace_replacement_at_state_mkdir_cannot_redirect_creation(self):
        foreign = self.second_repo("foreign")
        (foreign / "sentinel").write_bytes(b"foreign workspace")
        before = snapshot(foreign)
        held = self.base / "held-project"
        original = os.mkdir
        fired = False

        def substitute(name, *args, **kwargs):
            nonlocal fired
            if name == ".relay" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                self.repo.rename(held)
                self.repo.symlink_to(foreign, target_is_directory=True)
            return original(name, *args, **kwargs)

        with mock.patch.object(subject.os, "mkdir", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertTrue(fired)
        self.assertEqual(before, snapshot(foreign))
        self.assertTrue((held / ".relay").is_dir())
        self.assertFalse(list(self.registry.root.glob("*.json")))
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_state_replacement_at_pending_open_cannot_redirect_marker_write(self):
        foreign = self.base / "foreign-state"
        foreign.mkdir()
        (foreign / "relay.sqlite3").write_bytes(b"populated foreign ledger")
        before = snapshot(foreign)
        state = self.repo / ".relay"
        held = self.repo / ".held-state"
        original = os.open
        fired = False

        def substitute(name, *args, **kwargs):
            nonlocal fired
            descriptor = kwargs.get("dir_fd")
            if (isinstance(name, str) and name.startswith(".pending-")
                    and descriptor is not None and not fired
                    and subject._fingerprint(os.fstat(descriptor)) == subject._fingerprint(state.stat())):
                fired = True
                state.rename(held)
                state.symlink_to(foreign, target_is_directory=True)
            return original(name, *args, **kwargs)

        with mock.patch.object(subject.os, "open", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertTrue(fired)
        self.assertEqual(before, snapshot(foreign))
        self.assertEqual(list(held.iterdir()), [])
        self.assertFalse(list(self.registry.root.glob("*.json")))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_git_replacement_at_pending_open_cannot_redirect_nonce_write(self):
        foreign = self.second_repo("foreign")
        foreign_git = foreign / ".git"
        (foreign_git / "sentinel").write_bytes(b"foreign Git metadata")
        before = snapshot(foreign)
        common = self.repo / ".git"
        held = self.repo / ".held-git"
        original = os.open
        fired = False

        def substitute(name, *args, **kwargs):
            nonlocal fired
            descriptor = kwargs.get("dir_fd")
            if (isinstance(name, str) and name.startswith(".pending-")
                    and descriptor is not None and not fired
                    and subject._fingerprint(os.fstat(descriptor)) == subject._fingerprint(common.stat())):
                fired = True
                common.rename(held)
                common.symlink_to(foreign_git, target_is_directory=True)
            return original(name, *args, **kwargs)

        with mock.patch.object(subject.os, "open", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertTrue(fired)
        self.assertEqual(before, snapshot(foreign))
        self.assertFalse((held / "relay-enrollment.json").exists())
        self.assertFalse(list(self.registry.root.glob("*.json")))


    def test_registry_ancestor_replacement_at_publication_is_confined_to_held_directory(self):
        for replacement_kind in ("symlink", "directory"):
            with self.subTest(replacement_kind=replacement_kind):
                repo = self.second_repo(f"project-{replacement_kind}")
                registry = subject.Registry(self.base / f"account-{replacement_kind}" / "enrollments")
                foreign = self.base / f"foreign-{replacement_kind}"
                foreign.mkdir()
                (foreign / "enrollments").mkdir()
                (foreign / "enrollments" / "sentinel").write_bytes(b"foreign registry")
                held = self.base / f"held-account-{replacement_kind}"
                original = os.link
                fired = False
                replacement_snapshot = None

                def substitute(source, destination, **kwargs):
                    nonlocal fired, replacement_snapshot
                    if destination.endswith(".json") and destination != "enrollment.json" and destination != "relay-enrollment.json":
                        fired = True
                        registry.root.parent.rename(held)
                        if replacement_kind == "symlink":
                            registry.root.parent.symlink_to(foreign, target_is_directory=True)
                        else:
                            foreign.rename(registry.root.parent)
                        replacement_snapshot = snapshot(registry.root.parent)
                    return original(source, destination, **kwargs)

                with mock.patch.object(subject.os, "link", side_effect=substitute):
                    with self.assertRaises(subject.EnrollmentError):
                        registry.enroll(repo)
                self.assertTrue(fired)
                self.assertEqual(replacement_snapshot, snapshot(registry.root.parent))
                self.assertEqual(len(list((held / "enrollments").glob("*.json"))), 1)
                self.assert_refusal_unchanged(lambda: registry.lookup(repo))
                self.assert_refusal_unchanged(lambda: registry.enroll(repo))

    def test_registry_replacement_during_fsync_is_detected_before_return(self):
        foreign = self.base / "foreign-registry"
        foreign.mkdir()
        (foreign / "sentinel").write_bytes(b"foreign registry")
        before = snapshot(foreign)
        held = self.base / "held-enrollments"
        original = os.fsync
        fired = False

        def substitute(fd):
            nonlocal fired
            if (not fired and self.registry.root.exists()
                    and subject._fingerprint(os.fstat(fd)) == subject._fingerprint(self.registry.root.stat())):
                fired = True
                self.registry.root.rename(held)
                self.registry.root.symlink_to(foreign, target_is_directory=True)
            return original(fd)

        with mock.patch.object(subject.os, "fsync", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertTrue(fired)
        self.assertEqual(before, snapshot(foreign))
        self.assertEqual(len(list(held.glob("*.json"))), 1)
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))

    def test_identical_record_replacement_during_read_is_not_returned_as_valid(self):
        self.registry.enroll(self.repo)
        record = self.record_path()
        expected = subject._fingerprint(record.stat())
        original = os.read
        after_substitution = None

        def substitute(fd, size):
            nonlocal after_substitution
            payload = original(fd, size)
            if after_substitution is None and subject._fingerprint(os.fstat(fd)) == expected:
                identical = record.with_suffix(".replacement")
                identical.write_bytes(record.read_bytes())
                identical.replace(record)
                after_substitution = snapshot(self.base)
            return payload

        with mock.patch.object(subject.os, "read", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.lookup(self.repo)
        self.assertIsNotNone(after_substitution)
        self.assertEqual(after_substitution, snapshot(self.base))

    def test_workspace_unsafe_ancestor_refuses_before_registry_or_state_writes(self):
        self.repo.chmod(0o777)
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))
        self.assertFalse(self.registry.root.exists())

    def test_lookup_and_enroll_close_custody_descriptors_on_success_and_refusal(self):
        descriptors = Path("/proc/self/fd")
        before = len(list(descriptors.iterdir()))
        self.registry.enroll(self.repo)
        for _ in range(3):
            self.registry.lookup(self.repo)
            self.registry.enroll(self.repo)
            missing = self.second_repo(f"missing-{_}")
            self.assert_refusal_unchanged(lambda: self.registry.lookup(missing))
        self.assertEqual(before, len(list(descriptors.iterdir())))


    def crash_enrollment(self, registry, repo, stage):
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from relay_runtime import enrollment\n"
            "def cut(stage):\n"
            "    if stage == sys.argv[4]: os._exit(73)\n"
            "enrollment._checkpoint = cut\n"
            "enrollment.Registry(Path(sys.argv[2])).enroll(sys.argv[3])\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script,
             str(Path(subject.__file__).resolve().parents[1]), str(registry.root), str(repo), stage],
            env=subject._GIT_ENV, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 73, result.stderr)

    def test_abrupt_publication_cuts_leave_orphans_that_lookup_and_enroll_refuse(self):
        stages = ("state-directory-created", "state-marker-linked", "state-marker-unlinked", "state-marker-published",
                  "git-marker-linked", "git-marker-unlinked", "git-marker-published", "account-record-linked")
        for stage in stages:
            with self.subTest(stage=stage):
                repo = self.second_repo(f"project-{stage}")
                registry = subject.Registry(self.base / f"account-{stage}" / "enrollments")
                self.crash_enrollment(registry, repo, stage)
                self.assertTrue((repo / ".relay").is_dir())
                self.assertFalse(list(self.base.rglob("*.sqlite3")))
                self.assert_refusal_unchanged(lambda: registry.lookup(repo))
                self.assert_refusal_unchanged(lambda: registry.enroll(repo))
                records = list(registry.root.glob("*.json"))
                self.assertEqual(len(records), 1 if stage == "account-record-linked" else 0)
                if records:
                    self.assertEqual(records[0].stat().st_nlink, 2)

    def test_abrupt_cut_after_durable_account_publication_is_an_exact_retry(self):
        self.crash_enrollment(self.registry, self.repo, "account-record-published")
        (self.repo / ".relay" / "relay.sqlite3").write_bytes(b"synthetic populated state, never opened")
        before = snapshot(self.base)
        recovered = self.registry.lookup(self.repo)
        self.assertEqual(recovered, self.registry.enroll(self.repo))
        self.assertEqual(before, snapshot(self.base))
        self.assertFalse(list(self.base.rglob(".pending-*")))

    def test_abrupt_registry_mkdir_cut_grants_no_workspace_enrollment(self):
        self.crash_enrollment(self.registry, self.repo, "registry-directory-created")
        self.assertFalse((self.repo / ".relay").exists())
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        # Account scaffolding alone is not a workspace reservation. A new
        # explicit enrollment may create the still-absent project state.
        entry = self.registry.enroll(self.repo)
        self.assertEqual(entry, self.registry.lookup(self.repo))



    def test_populated_state_substitution_before_first_open_is_not_adopted(self):
        foreign = self.base / "foreign-populated-state"
        foreign.mkdir()
        (foreign / "relay.sqlite3").write_bytes(b"preexisting populated ledger")
        before = snapshot(foreign)
        state = self.repo / ".relay"
        held = self.repo / ".held-state"
        original = os.open
        fired = False

        def substitute(name, *args, **kwargs):
            nonlocal fired
            if name == ".relay" and kwargs.get("dir_fd") is not None and not fired:
                fired = True
                state.rename(held)
                foreign.rename(state)
            return original(name, *args, **kwargs)

        with mock.patch.object(subject.os, "open", side_effect=substitute):
            with self.assertRaises(subject.EnrollmentError):
                self.registry.enroll(self.repo)
        self.assertTrue(fired)
        self.assertEqual(before, snapshot(state))
        self.assertEqual(list(held.iterdir()), [])
        self.assertFalse(list(self.registry.root.glob("*.json")))
        self.assertFalse((self.repo / ".git" / "relay-enrollment.json").exists())
        self.assert_refusal_unchanged(lambda: self.registry.lookup(self.repo))
        self.assert_refusal_unchanged(lambda: self.registry.enroll(self.repo))

    def test_abrupt_cut_before_account_directory_sync_can_leave_visible_complete_record(self):
        self.crash_enrollment(self.registry, self.repo, "account-record-unlinked")
        before = snapshot(self.base)
        entry = self.registry.lookup(self.repo)
        self.assertEqual(entry, self.registry.enroll(self.repo))
        self.assertEqual(before, snapshot(self.base))
        self.assertEqual(self.record_path().stat().st_nlink, 1)
        # Process death leaves the current kernel namespace observable. This
        # does not claim that a not-yet-synced directory entry survives power loss.
        self.assertFalse(list(self.base.rglob(".pending-*")))


if __name__ == "__main__":
    unittest.main()
