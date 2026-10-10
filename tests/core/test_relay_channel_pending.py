#!/usr/bin/env python3
"""Privacy and fail-closed tests for the Relay Claude Channel projection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import Any


SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from relay_core.store import RelayStore  # noqa: E402


CLI = SOURCE_DIR / "relay.py"


#: Environment names that could silently redirect a child Relay away from the
#: workspace under test.  Cleared for every subprocess; PATH is preserved so Git
#: still resolves (an emptied environment would fail for the wrong reason).
_SANITIZED_ENV_NAMES = (
    "RELAY_HOME",
    "RELAY_WORKSPACE",
    "CLAUDE_PROJECT_DIR",
    "PYTHONPATH",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

#: The candidate package is a CLOSED set: exactly these files, nothing else.
#: An exclusion list can only refuse what it anticipated; naming the members
#: lets a test assert the destination *equals* the set.  Duplicated per suite on
#: purpose — the authorized fence forbids a shared helper outside it.
_CANDIDATE_PACKAGE_FILES = ("__init__.py", "cli.py", "protocol.py", "store.py")


def sanitized_env(**overrides: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SANITIZED_ENV_NAMES}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env.update(overrides)
    return env


def _lstat_mode(path: Path) -> int:
    """`lstat` mode for `path`, or 0 when it is missing or unreadable.

    `lstat`, never `stat`: a symlink must be observed AS a symlink rather than
    dereferenced into whatever regular file it points at.
    """

    try:
        return path.lstat().st_mode
    except OSError:
        return 0


def install_candidate_runtime(destination_root: Path, *, source: Path = SOURCE_DIR) -> Path:
    """Copy the candidate entrypoint + the closed package set under `destination_root`.

    A parent-process patch of the binding resolver does not cross
    `subprocess.run` or `Popen`, so a child - including the persistent Channel
    process - can only bind to the disposable repository by executing a copy
    that physically lives inside it.  The copy is a sealed set of exactly
    `_CANDIDATE_PACKAGE_FILES`: every source is lstat-verified AND fully read
    before anything is written — the entrypoint payload included, so no read
    ever follows a write — and a missing, symlinked or non-directory root, or a
    missing, symlinked or non-regular member or entrypoint, refuses loudly and
    leaves no partial destination behind.
    """

    entry_source = source / "relay.py"
    if not stat.S_ISREG(_lstat_mode(entry_source)):
        raise AssertionError(
            f"refusing a missing, symlinked, or non-regular entrypoint: {entry_source}"
        )

    package_source = source / "relay_core"
    if not stat.S_ISDIR(_lstat_mode(package_source)):
        raise AssertionError(
            f"refusing a missing, symlinked, or non-directory package root: {package_source}"
        )

    entry_payload = entry_source.read_bytes()
    members = []
    for name in _CANDIDATE_PACKAGE_FILES:
        member = package_source / name
        if not stat.S_ISREG(_lstat_mode(member)):
            raise AssertionError(
                f"refusing a missing, symlinked, or non-regular package file: {member}"
            )
        members.append((name, member.read_bytes()))

    tools = destination_root / "src"
    package = tools / "relay_core"
    package.mkdir(parents=True, exist_ok=True)
    entry = tools / "relay.py"
    entry.write_bytes(entry_payload)
    entry.chmod(0o700)
    for name, payload in members:
        (package / name).write_bytes(payload)
    return entry


def git_common_dir(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return Path(result.stdout.strip()).resolve()


class ChannelPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="relay-channel-test-"
        )
        self.root = Path(self._temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Relay Test"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "user.email",
                "relay-test@example.invalid",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        (self.repo / "README.md").write_text("channel fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "README.md"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-q", "-m", "seed"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        self.home = self.root / "state"

        # Bind BOTH halves to this disposable repository before anything opens a
        # store: a copied candidate for every subprocess and for the persistent
        # Channel Popen, and the private resolver patched at its definition
        # lookup site for in-process calls.  Cleanup is registered before the
        # first open so a failure here cannot leak the patch into another test.
        self.repo_common = git_common_dir(self.repo)
        self.cli = install_candidate_runtime(self.repo)
        self._binding_patch = mock.patch(
            "relay_core.store._expected_workspace_binding",
            return_value=self.repo_common,
        )
        self._binding_patch.start()
        self.addCleanup(self._binding_patch.stop)

        with RelayStore.open(repo=self.repo, state_home=self.home):
            pass

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_channel(
        self,
        *,
        agent: str = "claude",
        source_agent: str = "codex",
        work_id: str = "opaque-import",
        limit: str = "1",
        home: Path | None = None,
        timeout: float = 5,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                str(self.cli),
                "--repo",
                str(self.repo),
                "--home",
                str(home or self.home),
                "channel-pending",
                "--agent",
                agent,
                "--source-agent",
                source_agent,
                "--work-id",
                work_id,
                "--limit",
                limit,
            ],
            capture_output=True,
            timeout=timeout,
            env=sanitized_env(),
        )

    @staticmethod
    def event(event_id: str, **overrides: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "v": 1,
            "id": event_id,
            "kind": "work.blocked",
            "agent": "codex",
            "session": "codex-channel-session",
            "work_id": "opaque-import",
            "target": "claude",
            "summary": "Typed coordination fact",
        }
        value.update(overrides)
        return value

    def event_count(self) -> int:
        connection = sqlite3.connect(self.home / "relay.sqlite3")
        try:
            return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            connection.close()

    def test_projection_is_exact_private_oldest_first_and_ack_aware(self) -> None:
        prompt_canary = "RAW_PROMPT_TEXT_MUST_NEVER_LEAVE_RELAY_8f5a"
        secret_canary = "PRIVATE_CONTEXT_MUST_NEVER_LEAVE_RELAY_2d91"
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            store.emit(
                self.event(
                    "evt:channel-broadcast-0001",
                    target=None,
                    summary=prompt_canary,
                )
            )
            store.emit(
                self.event(
                    "evt:channel-wrong-source-0001",
                    agent="claude",
                    summary=prompt_canary,
                )
            )
            store.emit(
                self.event(
                    "evt:channel-wrong-target-0001",
                    target="codex",
                    summary=prompt_canary,
                )
            )
            store.emit(
                self.event(
                    "evt:channel-wrong-work-0001",
                    work_id="different-work",
                    summary=prompt_canary,
                )
            )
            store.emit(
                self.event(
                    "evt:channel-null-work-0001",
                    work_id=None,
                    summary=prompt_canary,
                )
            )
            store.emit(
                self.event(
                    "evt:channel-intent-0001",
                    kind="work.intent",
                    summary=prompt_canary,
                )
            )
            first = store.emit(
                self.event(
                    "evt:channel-first-0001",
                    summary=prompt_canary,
                    meta={"reason": secret_canary},
                )
            )["event"]
            second = store.emit(
                self.event(
                    "evt:channel-second-0001",
                    kind="review.requested",
                    summary=prompt_canary,
                    artifact=f"git:{'a' * 40}",
                    meta={"evidence_sha256": "b" * 64},
                )
            )["event"]

        before = self.event_count()
        result = self.run_channel()
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(
            {
                "v": 1,
                "pending": {"seq": first["seq"], "kind": "work.blocked"},
                "more": True,
            },
            json.loads(result.stdout),
        )
        self.assertEqual(before, self.event_count(), "projection must not mutate Relay")
        self.assertNotIn(prompt_canary.encode(), result.stdout)
        self.assertNotIn(secret_canary.encode(), result.stdout)
        self.assertEqual(
            {"v", "pending", "more"}, set(json.loads(result.stdout))
        )
        self.assertEqual(
            {"seq", "kind"}, set(json.loads(result.stdout)["pending"])
        )

        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            store.acknowledge(
                first["seq"],
                agent="claude",
                session="claude-channel-consumer",
            )
        after_ack = self.event_count()
        replay = self.run_channel()
        self.assertEqual(0, replay.returncode, replay.stderr.decode())
        self.assertEqual(
            {
                "v": 1,
                "pending": {
                    "seq": second["seq"],
                    "kind": "review.requested",
                },
                "more": False,
            },
            json.loads(replay.stdout),
        )
        self.assertEqual(after_ack, self.event_count())

    def test_empty_projection_has_only_the_closed_schema(self) -> None:
        result = self.run_channel()
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(
            {"v": 1, "pending": None, "more": False},
            json.loads(result.stdout),
        )

    def test_route_work_id_and_limit_are_strict(self) -> None:
        cases = (
            {"agent": "codex"},
            {"source_agent": "claude"},
            {"agent": "Claude"},
            {"source_agent": "Codex"},
            {"work_id": " opaque-import "},
            {"work_id": ""},
            {"limit": "0"},
            {"limit": "2"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self.run_channel(**overrides)
                self.assertEqual(64, result.returncode, result.stderr.decode())
                self.assertEqual(b"", result.stdout)

    def test_missing_newer_corrupt_and_semantically_tampered_state_fail_closed(
        self,
    ) -> None:
        missing = self.root / "missing"
        absent = self.run_channel(home=missing)
        self.assertEqual(74, absent.returncode, absent.stderr.decode())
        self.assertFalse(missing.exists(), "read-only projection cannot initialize state")

        connection = sqlite3.connect(self.home / "relay.sqlite3")
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        newer = self.run_channel()
        self.assertEqual(74, newer.returncode, newer.stderr.decode())
        check = sqlite3.connect(self.home / "relay.sqlite3")
        try:
            self.assertEqual(99, check.execute("PRAGMA user_version").fetchone()[0])
        finally:
            check.close()

        corrupt_home = self.root / "corrupt"
        corrupt_home.mkdir(mode=0o700)
        corrupt_bytes = b"CORRUPT_RELAY_CHANNEL_CANARY"
        (corrupt_home / "relay.sqlite3").write_bytes(corrupt_bytes)
        corrupt = self.run_channel(home=corrupt_home)
        self.assertEqual(74, corrupt.returncode, corrupt.stderr.decode())
        self.assertEqual(corrupt_bytes, (corrupt_home / "relay.sqlite3").read_bytes())

        tampered_home = self.root / "tampered"
        with RelayStore.open(repo=self.repo, state_home=tampered_home) as store:
            store.emit(self.event("evt:channel-tampered-0001"))
        tampered_db = tampered_home / "relay.sqlite3"
        tamper = sqlite3.connect(tampered_db)
        tamper.execute("DROP TRIGGER events_are_append_only_update")
        tamper.execute(
            "UPDATE events SET summary = 'column drift' "
            "WHERE event_id = 'evt:channel-tampered-0001'"
        )
        tamper.commit()
        tamper.close()
        invalid = self.run_channel(home=tampered_home)
        self.assertEqual(74, invalid.returncode, invalid.stderr.decode())
        self.assertEqual(b"", invalid.stdout)

    def test_locked_state_fails_closed_without_waiting_or_writing(self) -> None:
        database = self.home / "relay.sqlite3"
        blocker = sqlite3.connect(database, isolation_level=None, timeout=1)
        try:
            blocker.execute("PRAGMA journal_mode = DELETE")
            blocker.execute("PRAGMA locking_mode = EXCLUSIVE")
            blocker.execute("BEGIN EXCLUSIVE")
            before = int(
                blocker.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            )
            result = self.run_channel(timeout=5)
            self.assertEqual(75, result.returncode, result.stderr.decode())
            self.assertEqual(b"", result.stdout)
            self.assertEqual(
                before,
                int(blocker.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
            )
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()


class ChannelSourceProfileTests(unittest.TestCase):
    """Proves the copied source-profile fixture actually binds what it claims.

    Without these, a repaired suite could be green because the fixture disabled
    the guard rather than because it bound the guard to the right workspace.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="relay-profile-")
        self.root = Path(self._temporary.name)
        self.owner = self.root / "owner"
        self.other = self.root / "other"
        for repo in (self.owner, self.other):
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main", str(repo)],
                check=True, capture_output=True, timeout=15,
            )
        self.cli = install_candidate_runtime(self.owner)
        self._staged = 0

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def stage_source(self) -> Path:
        """A disposable copy of the real source tree, safe to make hostile.

        An adversary case must never write into the shipping tree, so each one
        corrupts its own staging directory and passes it through `source=`.
        """

        self._staged += 1
        staging = self.root / f"staging-{self._staged}"
        (staging / "relay_core").mkdir(parents=True)
        (staging / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        for name in _CANDIDATE_PACKAGE_FILES:
            (staging / "relay_core" / name).write_bytes(
                (SOURCE_DIR / "relay_core" / name).read_bytes()
            )
        return staging

    def installed_names(self, destination_root: Path) -> list[str]:
        package = destination_root / "src" / "relay_core"
        return sorted(str(p.relative_to(package)) for p in package.rglob("*"))

    def _run(self, cli: Path, repo: Path):
        return subprocess.run(
            [sys.executable, str(cli), "--repo", str(repo), "--json",
             "events", "--limit", "1"],
            capture_output=True, timeout=15, env=sanitized_env(),
        )

    def test_original_source_cli_refuses_the_disposable_repository(self) -> None:
        result = self._run(CLI, self.owner)
        self.assertEqual(74, result.returncode, msg=result.stderr.decode())
        self.assertFalse((self.owner / ".relay").exists())
        self.assertEqual(b"", result.stdout)
        # A checkout is bound to its own Git directory; an exported source tree
        # has no binding. Both must refuse the disposable owner before writes.
        source_git = subprocess.run(
            ["git", "-C", str(SOURCE_DIR), "rev-parse", "--git-common-dir"],
            capture_output=True, timeout=15, env=sanitized_env(),
        )
        if source_git.returncode == 0:
            self.assertIn(b"refuses a foreign workspace", result.stderr)
        else:
            self.assertEqual(128, source_git.returncode, source_git.stderr.decode())
            self.assertIn(b"not a git repository", source_git.stderr.lower())
            self.assertIn(b"refuses an unbound workspace", result.stderr)

    def test_copied_candidate_succeeds_only_for_its_owner_repository(self) -> None:
        ok = self._run(self.cli, self.owner)
        self.assertEqual(0, ok.returncode, msg=ok.stderr.decode())

    def test_copied_candidate_refuses_an_unrelated_repository(self) -> None:
        result = self._run(self.cli, self.other)
        self.assertEqual(74, result.returncode, msg=result.stderr.decode())
        self.assertIn(b"refuses a foreign workspace", result.stderr)
        self.assertFalse((self.other / ".relay").exists())

    def stage_non_regular(self, path: Path, kind: str) -> None:
        """Replace `path` with a non-regular object of `kind`.

        The FIFO is deliberately mode 0o000.  A named pipe is the sharpest
        non-regular shape, but under a regression that reads BEFORE it lstats,
        `read_bytes()` on a writer-less FIFO blocks forever and unittest has no
        per-test timeout — the law would hang CI instead of going red.  With no
        permission bits the same regression fails immediately with EACCES.
        """

        path.unlink()
        if kind == "fifo":
            os.mkfifo(path)
            path.chmod(0o000)
        elif kind == "directory":
            path.mkdir()
        else:
            raise AssertionError(f"unknown non-regular kind: {kind}")

    def test_candidate_package_is_exactly_the_closed_file_set(self) -> None:
        # A private destination, never executed: "sealed" describes what the
        # copier WRITES, and running a candidate lets CPython drop its own
        # __pycache__ beside it.
        self.assertTrue((self.cli.parent / "relay_core" / "store.py").is_file())
        destination = self.root / "closed-set"
        install_candidate_runtime(destination)
        self.assertEqual(
            sorted(_CANDIDATE_PACKAGE_FILES), self.installed_names(destination)
        )

    def test_closed_set_matches_the_shipping_package_membership(self) -> None:
        """Bind the allowlist to reality instead of to itself.

        The law above compares the copier's output with the copier's own
        constant.  This is the law that fails the day a module joins
        `relay_core` and the candidate would otherwise ship a silently
        incomplete package.
        """

        shipping = sorted(
            entry.name
            for entry in (SOURCE_DIR / "relay_core").iterdir()
            if entry.is_file() and entry.suffix == ".py"
        )
        self.assertEqual(sorted(_CANDIDATE_PACKAGE_FILES), shipping)

    def test_candidate_carries_the_current_source_bytes(self) -> None:
        """The copy must be of what `source` holds RIGHT NOW.

        A mutation run copies MUTATED bytes; a run that copies restored bytes
        reports green while proving nothing.  Filename and exclusion assertions
        cannot see the difference.
        """

        staging = self.stage_source()
        sentinel = b"\n# candidate-freshness-sentinel\n"
        for name in _CANDIDATE_PACKAGE_FILES:
            member = staging / "relay_core" / name
            member.write_bytes(member.read_bytes() + sentinel)
        entry_source = staging / "relay.py"
        entry_source.write_bytes(entry_source.read_bytes() + sentinel)

        destination = self.root / "current-bytes"
        entry = install_candidate_runtime(destination, source=staging)
        package = destination / "src" / "relay_core"
        self.assertEqual(entry_source.read_bytes(), entry.read_bytes())
        for name in _CANDIDATE_PACKAGE_FILES:
            self.assertEqual(
                (staging / "relay_core" / name).read_bytes(),
                (package / name).read_bytes(),
                msg=f"{name} was not copied from the current source bytes",
            )

    def test_temp_cache_and_unrelated_sources_never_reach_the_candidate(self) -> None:
        staging = self.stage_source()
        package_source = staging / "relay_core"
        (package_source / "__pycache__").mkdir()
        (package_source / "__pycache__" / "store.cpython-313.pyc").write_bytes(b"\x00")
        (package_source / "store.py.swp").write_bytes(b"editor swap")
        (package_source / "scratch.tmp").write_bytes(b"temp")
        (package_source / "NOTES.md").write_text("unrelated\n", encoding="utf-8")
        (package_source / "vendor").mkdir()
        (package_source / "vendor" / "extra.py").write_text("x = 1\n", encoding="utf-8")
        (package_source / "linked.py").symlink_to(package_source / "store.py")

        destination = self.root / "from-noisy-source"
        install_candidate_runtime(destination, source=staging)
        self.assertEqual(
            sorted(_CANDIDATE_PACKAGE_FILES), self.installed_names(destination)
        )

    def test_missing_symlinked_or_non_directory_package_root_refuses(self) -> None:
        real_package = self.stage_source() / "relay_core"

        symlinked = self.root / "symlinked-root"
        symlinked.mkdir()
        (symlinked / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        (symlinked / "relay_core").symlink_to(real_package, target_is_directory=True)

        regular = self.root / "file-root"
        regular.mkdir()
        (regular / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())
        (regular / "relay_core").write_text("not a package\n", encoding="utf-8")

        absent = self.root / "absent-root"
        absent.mkdir()
        (absent / "relay.py").write_bytes((SOURCE_DIR / "relay.py").read_bytes())

        for label, source in (
            ("symlink", symlinked),
            ("regular-file", regular),
            ("missing", absent),
        ):
            with self.subTest(root=label):
                destination = self.root / f"dest-root-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("package root", str(caught.exception))
                self.assertFalse(destination.exists())

    def test_missing_symlinked_or_non_regular_package_file_refuses(self) -> None:
        missing = self.stage_source()
        (missing / "relay_core" / "cli.py").unlink()

        linked = self.stage_source()
        member = linked / "relay_core" / "store.py"
        elsewhere = self.root / "store-elsewhere.py"
        elsewhere.write_bytes(member.read_bytes())
        member.unlink()
        member.symlink_to(elsewhere)

        piped = self.stage_source()
        self.stage_non_regular(piped / "relay_core" / "protocol.py", "fifo")

        foldered = self.stage_source()
        self.stage_non_regular(foldered / "relay_core" / "__init__.py", "directory")

        for label, source in (
            ("missing", missing),
            ("symlink", linked),
            ("fifo", piped),
            ("directory", foldered),
        ):
            with self.subTest(member=label):
                destination = self.root / f"dest-member-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("package file", str(caught.exception))
                self.assertFalse(destination.exists())

    def test_missing_symlinked_or_non_regular_entrypoint_refuses(self) -> None:
        linked = self.stage_source()
        entry = linked / "relay.py"
        elsewhere = self.root / "entry-elsewhere.py"
        elsewhere.write_bytes(entry.read_bytes())
        entry.unlink()
        entry.symlink_to(elsewhere)

        missing = self.stage_source()
        (missing / "relay.py").unlink()

        foldered = self.stage_source()
        self.stage_non_regular(foldered / "relay.py", "directory")

        for label, source in (
            ("symlink", linked),
            ("missing", missing),
            ("directory", foldered),
        ):
            with self.subTest(entrypoint=label):
                destination = self.root / f"dest-entry-{label}"
                with self.assertRaises(AssertionError) as caught:
                    install_candidate_runtime(destination, source=source)
                self.assertIn("entrypoint", str(caught.exception))
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
