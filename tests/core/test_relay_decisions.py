#!/usr/bin/env python3
"""Adversarial tests for Relay's fenced engineering-decision protocol."""

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

from relay_core.protocol import (  # noqa: E402
    ConflictError,
    DECISION_ROLLOUT_FENCE,
    StateError,
    ValidationError,
    normalize_event,
)
from relay_core.store import RelayStore  # noqa: E402


CLI = SOURCE_DIR / "relay.py"
ARTIFACT = f"git:{'a' * 40}"


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
    `subprocess.run`, so a child can only bind to the disposable repository by
    executing a copy that physically lives inside it.  The copy is a sealed set
    of exactly `_CANDIDATE_PACKAGE_FILES`: every source is lstat-verified AND
    fully read before anything is written — the entrypoint payload included, so
    no read ever follows a write — and a missing, symlinked or non-directory
    root, or a missing, symlinked or non-regular member or entrypoint, refuses
    loudly and leaves no partial destination behind.
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


class DecisionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="relay-decision-test-"
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
        (self.repo / "README.md").write_text(
            "decision fixture\n", encoding="utf-8"
        )
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
        self.work_id = "decision-work"

        # Bind BOTH halves to this disposable repository before anything opens a
        # store: a copied candidate for every subprocess, and the private
        # resolver patched at its definition lookup site for in-process calls.
        # Cleanup is registered before the first open so a failure here cannot
        # leak the patch into another test.
        self.repo_common = git_common_dir(self.repo)
        self.cli = install_candidate_runtime(self.repo)
        self._binding_patch = mock.patch(
            "relay_core.store._expected_workspace_binding",
            return_value=self.repo_common,
        )
        self._binding_patch.start()
        self.addCleanup(self._binding_patch.stop)

        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            store.emit(
                {
                    "v": 1,
                    "id": "intent:decision-protocol-fixture",
                    "kind": "work.intent",
                    "agent": "claude",
                    "session": "claude-fixture",
                    "work_id": self.work_id,
                    "summary": "Bounded decision protocol fixture",
                }
            )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                sys.executable,
                str(self.cli),
                "--repo",
                str(self.repo),
                "--home",
                str(self.home),
                "--json",
                *args,
            ],
            capture_output=True,
            timeout=15,
            env=sanitized_env(),
        )

    def event_count(self, kind: str | None = None) -> int:
        connection = sqlite3.connect(self.home / "relay.sqlite3")
        try:
            if kind is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)
                ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def request(
        self,
        store: RelayStore,
        *,
        decision_id: str = "import-set-taxonomy",
        summary: str = "Choose the canonical set taxonomy",
        options: Any = ("preserve", "derive"),
        **overrides: Any,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "agent": "claude",
            "session": "claude-decision-session",
            "decision_id": decision_id,
            "work_id": self.work_id,
            "scope": "import/export",
            "summary": summary,
            "artifact": ARTIFACT,
            "authority_hint": "engineering",
            "option_ids": options,
            "rollout_fence": DECISION_ROLLOUT_FENCE,
        }
        values.update(overrides)
        return store.decision_request(**values)

    @staticmethod
    def response_event(
        *,
        event_id: str,
        resolution: str,
        authority_class: str,
        choice: str | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "decision_id": "authority-matrix",
            "request_seq": 2,
            "request_event_id": "decision-request:authority-matrix",
            "resolution": resolution,
            "authority_class": authority_class,
        }
        if choice is not None:
            meta["choice"] = choice
        return {
            "v": 1,
            "id": event_id,
            "kind": "decision.responded",
            "agent": "codex",
            "session": "codex-decision-session",
            "work_id": "decision-work",
            "target": "claude",
            "scope": "import/export",
            "summary": "Bounded judgment",
            "artifact": ARTIFACT,
            "meta": meta,
        }

    def test_internal_kinds_are_rejected_by_generic_emit(self) -> None:
        request = {
            "v": 1,
            "id": "decision-request:generic-rejection",
            "kind": "decision.requested",
            "agent": "claude",
            "session": "claude-decision-session",
            "work_id": self.work_id,
            "target": "codex",
            "scope": "import/export",
            "summary": "Question",
            "artifact": ARTIFACT,
            "meta": {
                "decision_id": "generic-rejection",
                "authority_hint": "engineering",
            },
        }
        response = self.response_event(
            event_id="decision-response:generic-rejection",
            resolution="directive",
            authority_class="engineering",
        )
        for event in (request, response):
            with self.subTest(kind=event["kind"]):
                with self.assertRaises(ValidationError):
                    normalize_event(event)
                with RelayStore.open(
                    repo=self.repo, state_home=self.home
                ) as store:
                    with self.assertRaises(ValidationError):
                        store.emit(event)
                    with self.assertRaises(ValidationError):
                        store.emit(event, internal=True)

    def test_generic_emit_cannot_forge_request_acknowledgement(self) -> None:
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            request = self.request(
                store, decision_id="forged-request-ack"
            )["event"]
            forged_ack = {
                "v": 1,
                "id": "ack:forged-request",
                "kind": "delivery.acknowledged",
                "agent": "codex",
                "session": "codex-forged-ack",
                "work_id": self.work_id,
                "target": "codex",
                "scope": "signal:{}".format(request["seq"]),
                "summary": "Forged acknowledgement",
                "meta": {
                    "signal_seq": request["seq"],
                    "signal_event_id": request["id"],
                    "target_agent": "codex",
                },
            }
            normalize_event(forged_ack, internal=True)
            baseline = self.event_count()
            with self.assertRaises(ValidationError):
                store.emit(forged_ack, internal=True)
            self.assertEqual(baseline, self.event_count())
            self.assertEqual(0, self.event_count("delivery.acknowledged"))
            self.assertEqual(
                [request["seq"]],
                [
                    item["seq"]
                    for item in store.brief("codex")["pending_signals"]
                ],
            )

    def test_generic_emit_internal_flag_cannot_acquire_authority(self) -> None:
        public_event = {
            "v": 1,
            "id": "evt:generic-public-canary",
            "kind": "work.blocked",
            "agent": "codex",
            "session": "codex-generic-canary",
            "work_id": self.work_id,
            "target": "claude",
            "summary": "Public event canary",
        }
        privileged_drop = {
            "v": 1,
            "id": "evt:generic-drop-bypass",
            "kind": "ratchet.decided",
            "agent": "codex",
            "session": "codex-generic-canary",
            "target": "generic-internal-escape",
            "summary": "Attempt internal ratchet semantics",
            "meta": {
                "fingerprint": "generic-internal-escape",
                "mode": "drop",
                "home": "tooling",
            },
        }
        normalize_event(public_event)
        normalize_event(privileged_drop, internal=True)
        baseline = self.event_count()
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            for event in (public_event, privileged_drop):
                with self.subTest(kind=event["kind"]):
                    with self.assertRaisesRegex(
                        ValidationError, "cannot acquire internal event authority"
                    ):
                        store.emit(event, internal=True)
        self.assertEqual(baseline, self.event_count())

    def test_rollout_fence_fails_before_request_append_or_response_ack(self) -> None:
        request_args = [
            "decision",
            "request",
            "--agent",
            "claude",
            "--session",
            "claude-cli",
            "--decision-id",
            "rollout-fence",
            "--work-id",
            self.work_id,
            "--scope",
            "import/export",
            "--summary",
            "Fence this question",
            "--artifact",
            ARTIFACT,
            "--authority-hint",
            "engineering",
        ]
        baseline = self.event_count()
        omitted = self.run_cli(*request_args)
        self.assertEqual(2, omitted.returncode, omitted.stderr.decode())
        wrong = self.run_cli(
            *request_args, "--rollout-fence", "clients-maybe-refreshed"
        )
        self.assertEqual(2, wrong.returncode, wrong.stderr.decode())
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            with self.assertRaises(ValidationError):
                self.request(
                    store,
                    decision_id="rollout-direct",
                    rollout_fence="",
                )
        self.assertEqual(baseline, self.event_count())

        created = self.run_cli(
            *request_args,
            "--rollout-fence",
            DECISION_ROLLOUT_FENCE,
        )
        self.assertEqual(0, created.returncode, created.stderr.decode())
        request_seq = json.loads(created.stdout)["event"]["seq"]
        before_response = self.event_count()
        response_args = [
            "decision",
            "respond",
            str(request_seq),
            "--agent",
            "codex",
            "--session",
            "codex-cli",
            "--judgment",
            "Use the bounded directive",
            "--resolution",
            "directive",
            "--authority-class",
            "engineering",
        ]
        omitted_response = self.run_cli(*response_args)
        self.assertEqual(
            2, omitted_response.returncode, omitted_response.stderr.decode()
        )
        wrong_response = self.run_cli(
            *response_args,
            "--rollout-fence",
            "clients-maybe-refreshed",
        )
        self.assertEqual(
            2, wrong_response.returncode, wrong_response.stderr.decode()
        )
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            with self.assertRaises(ValidationError):
                store.decision_respond(
                    request_seq,
                    agent="codex",
                    session="codex-direct",
                    judgment="Directive",
                    resolution="directive",
                    authority_class="engineering",
                    choice=None,
                    rollout_fence="",
                )
            pending = store.brief("codex")["pending_signals"]
        self.assertEqual(before_response, self.event_count())
        self.assertEqual([request_seq], [item["seq"] for item in pending])

    def test_request_dedupes_exact_retry_and_conflicts_on_body_drift(self) -> None:
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            first = self.request(store)
            retry = self.request(store)
            self.assertEqual(first["event"]["seq"], retry["event"]["seq"])
            self.assertTrue(retry["duplicate"])
            resumed = self.request(
                store, session="claude-resumed-decision-session"
            )
            self.assertEqual(first["event"]["seq"], resumed["event"]["seq"])
            self.assertTrue(resumed["duplicate"])
            self.assertEqual(
                "claude-decision-session", resumed["event"]["session"]
            )
            with self.assertRaises(ConflictError):
                self.request(store, summary="A changed question body")
            other_work_id = "decision-work-two"
            store.emit(
                {
                    "v": 1,
                    "id": "intent:decision-work-two-fixture",
                    "kind": "work.intent",
                    "agent": "claude",
                    "session": "claude-fixture",
                    "work_id": other_work_id,
                    "summary": "Second work-local decision fixture",
                }
            )
            work_local = self.request(store, work_id=other_work_id)
            self.assertNotEqual(
                first["event"]["id"], work_local["event"]["id"]
            )
            corrected = self.request(
                store,
                decision_id="import-set-taxonomy-v2",
                summary="Corrected canonical taxonomy question",
            )
            self.assertGreater(
                corrected["event"]["seq"], first["event"]["seq"]
            )

        self.assertEqual(3, self.event_count("decision.requested"))

    def test_request_validates_linkage_shape_options_and_credentials(self) -> None:
        invalid_cases = (
            ({"agent": "codex"}, ValidationError),
            ({"decision_id": "Uppercase"}, ValidationError),
            ({"work_id": " decision-work "}, ValidationError),
            ({"scope": " import/export "}, ValidationError),
            ({"summary": "x" * 301}, ValidationError),
            ({"summary": "Bearer very-secret-value"}, ValidationError),
            ({"artifact": f"git:{'b' * 64}"}, ValidationError),
            ({"authority_hint": "operator"}, ValidationError),
            ({"options": ("only",)}, ValidationError),
            ({"options": tuple(f"o{index}" for index in range(9))}, ValidationError),
            ({"options": ("same", "same")}, ValidationError),
            ({"options": ("valid", "Upper")}, ValidationError),
            ({"options": "not-an-array"}, ValidationError),
            ({"work_id": "unknown-work"}, ConflictError),
        )
        for index, (overrides, error) in enumerate(invalid_cases):
            values = dict(overrides)
            options = values.pop("options", ("preserve", "derive"))
            decision_id = values.pop("decision_id", f"invalid-case-{index}")
            with self.subTest(index=index, overrides=overrides):
                with RelayStore.open(
                    repo=self.repo, state_home=self.home
                ) as store:
                    with self.assertRaises(error):
                        self.request(
                            store,
                            decision_id=decision_id,
                            options=options,
                            **values,
                        )
        self.assertEqual(0, self.event_count("decision.requested"))

    def test_resolution_authority_matrix_rejects_laundering(self) -> None:
        valid = {
            ("choice", "engineering"),
            ("directive", "engineering"),
            ("escalate", "human-only"),
            ("escalate", "external-authorization"),
            ("needs-evidence", "undetermined"),
        }
        resolutions = ("choice", "directive", "escalate", "needs-evidence")
        authorities = (
            "engineering",
            "human-only",
            "external-authorization",
            "undetermined",
        )
        counter = 0
        for resolution in resolutions:
            for authority in authorities:
                counter += 1
                event = self.response_event(
                    event_id=f"decision-response:matrix-{counter:02d}",
                    resolution=resolution,
                    authority_class=authority,
                    choice="preserve" if resolution == "choice" else None,
                )
                if (resolution, authority) in valid:
                    normalize_event(event, internal=True)
                else:
                    with self.assertRaises(ValidationError):
                        normalize_event(event, internal=True)

        for resolution, authority in (
            ("directive", "engineering"),
            ("escalate", "human-only"),
            ("needs-evidence", "undetermined"),
        ):
            with self.subTest(irrelevant_choice=resolution):
                event = self.response_event(
                    event_id=f"decision-response:irrelevant-{resolution}",
                    resolution=resolution,
                    authority_class=authority,
                    choice="preserve",
                )
                with self.assertRaises(ValidationError):
                    normalize_event(event, internal=True)

    def test_request_cannot_disappear_without_atomic_response(self) -> None:
        request_canary = "RAW_REQUEST_PROMPT_CANARY_7f9d"
        judgment_canary = "RAW_JUDGMENT_PROMPT_CANARY_a36c"
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            request = self.request(
                store,
                decision_id="no-standalone-ack",
                summary=request_canary,
            )["event"]
            with self.assertRaises(ConflictError):
                store.acknowledge(
                    request["seq"],
                    agent="codex",
                    session="codex-standalone-ack",
                )
            self.assertEqual(
                [request["seq"]],
                [
                    item["seq"]
                    for item in store.brief("codex")["pending_signals"]
                ],
            )
            self.assertEqual(
                {"v": 1, "pending": None, "more": False},
                store.channel_pending(
                    agent="claude",
                    source_agent="codex",
                    work_id=self.work_id,
                    limit=1,
                ),
            )

            response = store.decision_respond(
                request["seq"],
                agent="codex",
                session="codex-response",
                judgment=judgment_canary,
                resolution="choice",
                authority_class="engineering",
                choice="preserve",
                rollout_fence=DECISION_ROLLOUT_FENCE,
            )
            response_event = response["event"]
            acknowledgement = response["acknowledgement"]["event"]
            self.assertEqual("decision.responded", response_event["kind"])
            self.assertEqual(request["work_id"], response_event["work_id"])
            self.assertEqual(request["scope"], response_event["scope"])
            self.assertEqual(request["artifact"], response_event["artifact"])
            self.assertEqual("claude", response_event["target"])
            self.assertEqual(request["seq"], response_event["meta"]["request_seq"])
            self.assertEqual(
                request["id"], response_event["meta"]["request_event_id"]
            )
            self.assertEqual(
                f"signal:{request['seq']}", acknowledgement["scope"]
            )
            self.assertEqual("codex", acknowledgement["agent"])
            self.assertEqual([], store.brief("codex")["pending_signals"])
            self.assertEqual(
                [response_event["seq"]],
                [
                    item["seq"]
                    for item in store.brief("claude")["pending_signals"]
                ],
            )
            projection = store.channel_pending(
                agent="claude",
                source_agent="codex",
                work_id=self.work_id,
                limit=1,
            )
            self.assertEqual(
                {
                    "v": 1,
                    "pending": {
                        "seq": response_event["seq"],
                        "kind": "decision.responded",
                    },
                    "more": False,
                },
                projection,
            )
            encoded_projection = json.dumps(projection)
            self.assertNotIn(request_canary, encoded_projection)
            self.assertNotIn(judgment_canary, encoded_projection)
            self.assertEqual({"v", "pending", "more"}, set(projection))
            self.assertEqual({"seq", "kind"}, set(projection["pending"]))

            count = self.event_count()
            retry = store.decision_respond(
                request["seq"],
                agent="codex",
                session="codex-resumed-response",
                judgment=judgment_canary,
                resolution="choice",
                authority_class="engineering",
                choice="preserve",
                rollout_fence=DECISION_ROLLOUT_FENCE,
            )
            self.assertTrue(retry["duplicate"])
            self.assertTrue(retry["acknowledgement"]["duplicate"])
            self.assertEqual(
                "codex-response", retry["event"]["session"]
            )
            self.assertEqual(count, self.event_count())
            with self.assertRaises(ConflictError):
                store.decision_respond(
                    request["seq"],
                    agent="codex",
                    session="codex-resumed-response",
                    judgment="Conflicting second answer",
                    resolution="choice",
                    authority_class="engineering",
                    choice="derive",
                    rollout_fence=DECISION_ROLLOUT_FENCE,
                )
            self.assertEqual(count, self.event_count())

    def test_choice_must_be_one_requested_option(self) -> None:
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            request = self.request(
                store, decision_id="choice-membership"
            )["event"]
            baseline = self.event_count()
            with self.assertRaises(ValidationError):
                store.decision_respond(
                    request["seq"],
                    agent="codex",
                    session="codex-choice",
                    judgment="Select an absent option",
                    resolution="choice",
                    authority_class="engineering",
                    choice="unrequested",
                    rollout_fence=DECISION_ROLLOUT_FENCE,
                )
            self.assertEqual(baseline, self.event_count())

            without_options = self.request(
                store,
                decision_id="choice-without-options",
                options=None,
            )["event"]
            with self.assertRaises(ValidationError):
                store.decision_respond(
                    without_options["seq"],
                    agent="codex",
                    session="codex-choice",
                    judgment="Cannot select an absent option list",
                    resolution="choice",
                    authority_class="engineering",
                    choice="preserve",
                    rollout_fence=DECISION_ROLLOUT_FENCE,
                )

    def test_ack_fault_rolls_back_response_and_request_remains_pending(self) -> None:
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            request = self.request(
                store, decision_id="atomic-rollback"
            )["event"]
        connection = sqlite3.connect(self.home / "relay.sqlite3")
        try:
            connection.execute(
                """
                CREATE TRIGGER inject_decision_ack_failure
                BEFORE INSERT ON events
                WHEN NEW.kind = 'delivery.acknowledged'
                BEGIN
                  SELECT RAISE(ABORT, 'injected decision ACK failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        baseline = self.event_count()
        with RelayStore.open(repo=self.repo, state_home=self.home) as store:
            with self.assertRaises(StateError):
                store.decision_respond(
                    request["seq"],
                    agent="codex",
                    session="codex-atomic",
                    judgment="This entire transaction must roll back",
                    resolution="directive",
                    authority_class="engineering",
                    choice=None,
                    rollout_fence=DECISION_ROLLOUT_FENCE,
                )
            self.assertEqual(
                [request["seq"]],
                [
                    item["seq"]
                    for item in store.brief("codex")["pending_signals"]
                ],
            )
        self.assertEqual(baseline, self.event_count())
        self.assertEqual(0, self.event_count("decision.responded"))
        self.assertEqual(0, self.event_count("delivery.acknowledged"))

    def test_dedicated_cli_round_trip(self) -> None:
        request = self.run_cli(
            "decision",
            "request",
            "--agent",
            "claude",
            "--session",
            "claude-cli-round-trip",
            "--decision-id",
            "cli-round-trip",
            "--work-id",
            self.work_id,
            "--scope",
            "import/export",
            "--summary",
            "Choose through the dedicated command",
            "--artifact",
            ARTIFACT,
            "--authority-hint",
            "engineering",
            "--option",
            "preserve",
            "--option",
            "derive",
            "--rollout-fence",
            DECISION_ROLLOUT_FENCE,
        )
        self.assertEqual(0, request.returncode, request.stderr.decode())
        request_seq = json.loads(request.stdout)["event"]["seq"]
        response = self.run_cli(
            "decision",
            "respond",
            str(request_seq),
            "--agent",
            "codex",
            "--session",
            "codex-cli-round-trip",
            "--judgment",
            "Preserve the source taxonomy",
            "--resolution",
            "choice",
            "--authority-class",
            "engineering",
            "--choice",
            "preserve",
            "--rollout-fence",
            DECISION_ROLLOUT_FENCE,
        )
        self.assertEqual(0, response.returncode, response.stderr.decode())
        payload = json.loads(response.stdout)
        self.assertEqual("decision.responded", payload["event"]["kind"])
        self.assertEqual(
            "delivery.acknowledged",
            payload["acknowledgement"]["event"]["kind"],
        )


class DecisionSourceProfileTests(unittest.TestCase):
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
