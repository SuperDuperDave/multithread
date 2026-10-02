#!/usr/bin/env python3
"""Wake bindings and delivery attempts: protocol shape and ledger invariants."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest import mock


SOURCE_DIR = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from relay_core import cli as core_cli  # noqa: E402
from relay_core.protocol import (  # noqa: E402
    ConflictError,
    StateError,
    ValidationError,
    WAKE_ATTEMPT_KINDS,
    WAKE_BINDING_KINDS,
    canonical_wake_expectation,
    normalize_event,
    session_target,
)
from relay_core.store import RelayStore  # noqa: E402


THREAD = "a0000000-0000-7000-8000-000000000001"
OTHER = "b0000000-0000-7000-8000-000000000002"
ENDPOINT = "unix:///srv/codex/app-server-control/app-server-control.sock"
TASK = {"ref_sha256": "a" * 64, "ref_size": 15}


def content(ref):
    """What the wake helper measures for a file reference; a sequence has none."""
    return {} if ref.isdigit() else TASK


def bound(**meta):
    values = {"role": "operator", "provider": "codex", "thread": THREAD, "endpoint": ENDPOINT, "cwd": "/srv/work"}
    values.update(meta)
    return {"kind": "wake.bound", "agent": "claude", "session": "s", "target": values["role"],
            "summary": "Bound", "meta": {key: value for key, value in values.items() if value is not None}}


def concluded(**meta):
    values = {"role": "operator", "generation": 1, "attempt_seq": 2, "message_id": "wake-operator-1-abc",
              "outcome": "queued", "reason": "requested", "transport": "queue"}
    values.update(meta)
    return {"kind": "wake.concluded", "agent": "claude", "session": "s", "target": "operator",
            "summary": "Concluded", "meta": {key: value for key, value in values.items() if value is not None}}


class WakeProtocolTests(unittest.TestCase):
    def test_every_wake_kind_is_internal(self):
        for kind in (*WAKE_BINDING_KINDS, *WAKE_ATTEMPT_KINDS):
            with self.assertRaisesRegex(ValidationError, "emitted only by a dedicated Multithread transaction"):
                normalize_event({"kind": kind, "agent": "claude", "session": "s", "summary": "forged"})

    def test_binding_fields_are_exact(self):
        self.assertEqual("operator", normalize_event(bound(), internal=True).target)
        for meta, message in (
            ({"role": "Operator"}, "role must be a lowercase name"),
            ({"thread": THREAD.upper()}, "exact lowercase UUID"),
            ({"thread": "operator console"}, "exact lowercase UUID"),
            ({"provider": "slack"}, "provider must be codex or claude"),
            ({"provider": "claude"}, "a Claude Code wake target is its inbox, with no thread or cwd"),
            ({"provider": "claude", "thread": None}, "a Claude Code wake target is its inbox, with no thread or cwd"),
            ({"endpoint": "tcp://127.0.0.1:1"}, "endpoint must be unix://"),
            ({"endpoint": "unix://relative.sock"}, "endpoint must be an absolute path"),
            ({"cwd": "work"}, "cwd must be an absolute path"),
            ({"replaces": 0}, "replaces must be a positive integer"),
        ):
            with self.subTest(meta=meta), self.assertRaisesRegex(ValidationError, message):
                normalize_event(bound(**meta), internal=True)
        inbox = normalize_event(bound(provider="claude", thread=None, cwd=None,
                                      endpoint="unix:///run/user/1000/cc-socks/7739.sock"), internal=True)
        self.assertEqual({"role", "provider", "endpoint"}, set(inbox.meta))
        with self.assertRaisesRegex(ValidationError, "must target its role"):
            normalize_event({**bound(), "target": "reviewer"}, internal=True)
        with self.assertRaisesRegex(ValidationError, "metadata keys not allowed"):
            normalize_event({**bound(), "meta": {**bound()["meta"], "text": "task body"}}, internal=True)

    def test_a_conclusion_is_one_admissible_outcome(self):
        for outcome, reason, transport in (("steered", "live_turn", "steer"), ("queued", "turn_ended", "queue"),
                                           ("not_sent", "daemon_unreachable", None),
                                           ("uncertain", "no_answer", "queue")):
            native = "c0000000-0000-7000-8000-000000000003" if outcome == "steered" else None
            normalize_event(concluded(outcome=outcome, reason=reason, transport=transport, native_id=native),
                            internal=True)
        for meta in ({"outcome": "delivered"}, {"reason": "no_live_turn", "transport": "steer"},
                     {"outcome": "not_sent", "reason": "daemon_unreachable"},
                     {"outcome": "steered", "reason": "live_turn", "transport": "queue"},
                     {"outcome": "not_sent", "reason": "queue_refused", "transport": None}):
            with self.subTest(meta=meta), self.assertRaisesRegex(ValidationError, "admissible conclusion"):
                normalize_event(concluded(**meta), internal=True)
        with self.assertRaisesRegex(ValidationError, "records the native turn id"):
            normalize_event(concluded(outcome="steered", reason="live_turn", transport="steer"), internal=True)
        with self.assertRaisesRegex(ValidationError, "message id must be"):
            normalize_event(concluded(native_id="two words"), internal=True)

    def test_an_attempt_reference_is_a_file_path_or_a_sequence(self):
        attempt = {"kind": "wake.attempted", "agent": "claude", "session": "s", "target": "operator",
                   "summary": "Wake", "meta": {"role": "operator", "generation": 1, "provider": "codex",
                                               "thread": THREAD, "message_id": "m-1", "ref": "/srv/task.md",
                                               **TASK, "requested": "queue"}}
        normalize_event(attempt, internal=True)
        sequence = {key: value for key, value in attempt["meta"].items() if not key.startswith("ref_")}
        normalize_event({**attempt, "meta": {**sequence, "ref": "412"}}, internal=True)
        for meta, message in (({**sequence, "ref": "412", "ref_size": 3}, "carries no file sha256 or size"),
                              ({**sequence}, "needs its lowercase sha256"),
                              ({**sequence, "ref_sha256": "A" * 64, "ref_size": 1}, "needs its lowercase sha256"),
                              ({**sequence, "ref_sha256": "a" * 64}, "needs its size in bytes"),
                              ({**sequence, "ref_sha256": "a" * 64, "ref_size": -1}, "needs its size in bytes")):
            with self.subTest(meta=meta), self.assertRaisesRegex(ValidationError, message):
                normalize_event({**attempt, "meta": meta}, internal=True)
        for ref in ("task.md", "0412", "-1", "seq:412", "/" + "x" * 400):
            with self.subTest(ref=ref), self.assertRaises(ValidationError):
                normalize_event({**attempt, "meta": {**attempt["meta"], "ref": ref}}, internal=True)
        with self.assertRaisesRegex(ValidationError, "requested transport must be queue or steer"):
            normalize_event({**attempt, "meta": {**attempt["meta"], "requested": "interrupt"}}, internal=True)


class WakeLedgerCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-wake-core-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.make_repo("controller")
        self.home = self.base / "state"
        self.binding = mock.patch("relay_core.store._expected_workspace_binding",
                                  return_value=(self.repo / ".git").resolve())
        self.binding.start()
        self.addCleanup(self.binding.stop)
        self.store = RelayStore.open(repo=self.repo, state_home=self.home)
        self.addCleanup(self.store.close)

    def make_repo(self, name):
        repo = self.base / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@invalid", "commit", "-q", "--allow-empty", "-m", "seed"],
                       check=True)
        return repo

    def bind(self, thread=THREAD, *, replace=False, role="operator", endpoint=ENDPOINT):
        approval = {}
        if replace:
            approval = {"expected_generation": self.store.wake_bindings(role)["bindings"][0]["generation"],
                        "reason": "Synthetic user-approved handover", "approval_ref": "receipt:fixture-approval"}
        return self.store.wake_bind(role, thread=thread, endpoint=endpoint, cwd="/srv/work", replace=replace,
                                    agent="claude", session="binder", **approval)

    def begin(self, ref="/srv/task.md", requested="queue", message_id=None, role="operator"):
        return self.store.wake_begin(role, ref=ref, requested=requested, message_id=message_id,
                                     agent="claude", session="sender", **content(ref))

    def plan(self, ref="/srv/task.md", requested="queue", role="operator", **measured):
        return self.store.wake_plan(role, ref=ref, requested=requested, **(measured or content(ref)))

    def conclude(self, attempt, outcome="queued", reason="requested", transport="queue", native_id=None,
                 session="sender"):
        return self.store.wake_conclude(attempt, outcome=outcome, reason=reason, transport=transport,
                                        native_id=native_id, agent="claude", session=session)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = core_cli.main(["--repo", str(self.repo), "--home", str(self.home), *args])
        return code, out.getvalue(), err.getvalue()


class WakeExpectationProtocolTests(unittest.TestCase):
    def test_omission_is_legacy_and_complete_assertions_return_fresh_dicts(self):
        self.assertIsNone(canonical_wake_expectation(None))
        for source in (
            {"generation": 7, "provider": "codex", "thread": THREAD},
            {"generation": 8, "provider": "claude", "bound_agent": "claude:owner", "bound_session": "session:one"},
        ):
            with self.subTest(provider=source["provider"]):
                expected = canonical_wake_expectation(MappingProxyType(source))
                self.assertEqual(source, expected)
                self.assertIs(type(expected), dict)
                source["generation"] += 1
                self.assertNotEqual(source["generation"], expected["generation"])

    def test_partial_mixed_unknown_and_nonmapping_assertions_refuse(self):
        codex = {"generation": 1, "provider": "codex", "thread": THREAD}
        claude = {"generation": 1, "provider": "claude", "bound_agent": "claude", "bound_session": "owner"}
        malformed = [False, True, [], "codex", {}, {"generation": 1},
                     {**codex, "provider": "other"}, {**codex, "provider": ["codex"]},
                     {**codex, "bound_agent": "claude"}, {**claude, "thread": THREAD},
                     {**codex, "endpoint": ENDPOINT}, {**claude, "unknown": "field"}]
        malformed.extend({key: value for key, value in codex.items() if key != field} for field in codex)
        malformed.extend({key: value for key, value in claude.items() if key != field} for field in claude)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                canonical_wake_expectation(value)

    def test_null_boolean_generation_and_noncanonical_identities_refuse(self):
        codex = {"generation": 1, "provider": "codex", "thread": THREAD}
        claude = {"generation": 1, "provider": "claude", "bound_agent": "claude", "bound_session": "owner"}
        malformed = [{**codex, "generation": value} for value in (None, True, False, 0, -1, "1", 1.0)]
        malformed.extend({**codex, field: None} for field in codex)
        malformed.extend({**claude, field: None} for field in claude)
        malformed.extend({**codex, "thread": value} for value in (THREAD.upper(), " " + THREAD, "title"))
        for field in ("bound_agent", "bound_session"):
            malformed.extend({**claude, field: value} for value in ("", " owner", "owner ", "two words", False))
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                canonical_wake_expectation(value)


class WakeExpectedBindingTests(WakeLedgerCase):
    def expectation(self, binding=None):
        binding = binding or self.store.wake_bindings("operator")["bindings"][0]
        fields = ("generation", "provider", "thread") if binding["provider"] == "codex" else (
            "generation", "provider", "bound_agent", "bound_session")
        return {field: binding[field] for field in fields}

    def guarded_plan(self, expected, message_id=None):
        return self.store.wake_plan("operator", ref="/srv/task.md", requested="queue",
                                    message_id=message_id, expected_binding=expected, **TASK)

    def guarded_begin(self, expected, message_id=None):
        return self.store.wake_begin("operator", ref="/srv/task.md", requested="queue",
                                     agent="claude", session="sender", message_id=message_id,
                                     expected_binding=expected, **TASK)

    def test_matching_codex_plan_is_read_only_and_attempt_conclusion_shape_is_unchanged(self):
        binding = self.bind()["binding"]
        expected = self.expectation(binding)
        before = self.store.events()
        plan = self.guarded_plan(expected)
        self.assertEqual("ready", plan["status"])
        self.assertEqual(self.plan()["message_id"], plan["message_id"])
        self.assertEqual(before, self.store.events())
        begun = self.guarded_begin(expected)
        event = self.store.events(after=begun["attempt_seq"] - 1, limit=1)[0]
        self.assertEqual("begun", begun["status"])
        self.assertEqual(plan["message_id"], begun["message_id"])
        self.assertEqual(binding["generation"], event["meta"]["generation"])
        self.assertEqual(THREAD, event["meta"]["thread"])
        self.assertEqual({"role", "generation", "provider", "thread", "message_id", "ref",
                          "ref_sha256", "ref_size", "requested"}, set(event["meta"]))
        receipt = self.conclude(begun["attempt_seq"])
        self.assertEqual("queued", receipt["event"]["meta"]["outcome"])
        self.assertEqual("already_sent", self.guarded_begin(expected)["status"])

    def test_matching_claude_assertion_names_recorded_owner_not_verified_receiver(self):
        binding = self.store.wake_bind("operator", provider="claude", endpoint="unix:///srv/inbox.sock",
                                       replace=False, agent="claude:owner", session="session:one")["binding"]
        expected = self.expectation(binding)
        self.assertEqual({"generation": binding["generation"], "provider": "claude",
                          "bound_agent": "claude:owner", "bound_session": "session:one"}, expected)
        before = self.store.events()
        self.assertEqual("ready", self.guarded_plan(expected)["status"])
        self.assertEqual(before, self.store.events())
        begun = self.guarded_begin(expected)
        self.assertEqual("begun", begun["status"])
        receipt = self.conclude(begun["attempt_seq"], outcome="delivered", reason="inbox_accepted", transport="inbox")
        self.assertEqual("delivered", receipt["event"]["meta"]["outcome"])
        self.assertNotIn("thread", self.store.events(after=begun["attempt_seq"] - 1, limit=1)[0]["meta"])

    def test_codex_generation_provider_and_thread_mismatches_are_write_free(self):
        expected = self.expectation(self.bind()["binding"])
        wrong = [{**expected, "generation": expected["generation"] + 1}, {**expected, "thread": OTHER},
                 {"generation": expected["generation"], "provider": "claude",
                  "bound_agent": "claude", "bound_session": "binder"}]
        before = self.store.events()
        for assertion in wrong:
            for operation in (self.guarded_plan, self.guarded_begin):
                with self.subTest(assertion=assertion, operation=operation.__name__), self.assertRaisesRegex(
                        ConflictError, "wake recipient binding changed"):
                    operation(assertion)
                self.assertEqual(before, self.store.events())

    def test_claude_owner_tuple_and_provider_mismatches_are_write_free(self):
        binding = self.store.wake_bind("operator", provider="claude", endpoint="unix:///srv/inbox.sock",
                                       replace=False, agent="claude:owner", session="session:one")["binding"]
        expected = self.expectation(binding)
        wrong = [{**expected, "bound_agent": "claude"}, {**expected, "bound_session": "session:two"},
                 {**expected, "bound_agent": "claude", "bound_session": "owner:session:one"},
                 {"generation": expected["generation"], "provider": "codex", "thread": THREAD}]
        before = self.store.events()
        for assertion in wrong:
            for operation in (self.guarded_plan, self.guarded_begin):
                with self.subTest(assertion=assertion, operation=operation.__name__), self.assertRaises(ConflictError):
                    operation(assertion)
                self.assertEqual(before, self.store.events())

    def test_missing_binding_conflicts_with_assertion_and_retains_legacy_unbound_behavior(self):
        expected = {"generation": 1, "provider": "codex", "thread": THREAD}
        before = self.store.events()
        for operation in (self.guarded_plan, self.guarded_begin):
            with self.assertRaises(ConflictError):
                operation(expected)
        self.assertEqual("unbound", self.plan()["status"])
        self.assertEqual("unbound", self.begin()["status"])
        self.assertEqual(before, self.store.events())

    def test_malformed_assertions_refuse_before_binding_read_or_write_transaction(self):
        self.bind()
        before = self.store.events()
        malformed = ({}, {"generation": True, "provider": "codex", "thread": THREAD},
                     {"generation": 1, "provider": "claude", "bound_agent": "claude"})
        with mock.patch.object(self.store, "_transaction", side_effect=AssertionError("transaction started")), \
                mock.patch.object(self.store, "_wake_binding", side_effect=AssertionError("binding read")):
            for assertion in malformed:
                for operation in (self.guarded_plan, self.guarded_begin):
                    with self.subTest(assertion=assertion), self.assertRaises(ValidationError):
                        operation(assertion)
        self.assertEqual(before, self.store.events())

    def test_rebinding_after_plan_conflicts_before_begin_without_reserving_or_appending(self):
        expected = self.expectation(self.bind()["binding"])
        self.assertEqual("ready", self.guarded_plan(expected)["status"])
        self.bind(OTHER, replace=True)
        after_rebind = self.store.events()
        with self.assertRaises(ConflictError):
            self.guarded_begin(expected)
        self.assertEqual(after_rebind, self.store.events())
        fresh = self.expectation()
        begun = self.guarded_begin(fresh)
        self.assertEqual(OTHER, begun["binding"]["thread"])
        self.assertEqual(fresh["generation"], begun["binding"]["generation"])

    def test_matching_paused_binding_stays_paused_but_stale_expectation_conflicts_first(self):
        expected = self.expectation(self.bind()["binding"])
        self.store.wake_control("pause", "operator", agent="claude", session="binder")
        before = self.store.events()
        for operation in (self.guarded_plan, self.guarded_begin):
            self.assertEqual("paused", operation(expected)["status"])
            with self.assertRaises(ConflictError):
                operation({**expected, "generation": expected["generation"] + 1})
        self.assertEqual(before, self.store.events())

    def test_recipient_mismatch_precedes_same_message_id_deduplication(self):
        expected = self.expectation(self.bind()["binding"])
        begun = self.guarded_begin(expected, message_id="stable-guarded-message")
        self.conclude(begun["attempt_seq"])
        self.bind(OTHER, replace=True)
        before = self.store.events()
        with self.assertRaises(ConflictError):
            self.guarded_begin(expected, message_id="stable-guarded-message")
        fresh = self.expectation()
        duplicate = self.guarded_begin(fresh, message_id="stable-guarded-message")
        self.assertEqual("already_sent", duplicate["status"])
        self.assertEqual(THREAD, duplicate["prior"]["thread"])
        self.assertEqual(before, self.store.events())

    def test_matching_expectation_keeps_open_uncertain_and_not_sent_guards(self):
        expected = self.expectation(self.bind()["binding"])
        opened = self.guarded_begin(expected, message_id="guarded-open")
        self.assertEqual("already_sent", self.guarded_begin(expected, message_id="guarded-open")["status"])
        self.conclude(opened["attempt_seq"], outcome="uncertain", reason="no_answer", transport="queue")
        self.assertEqual("already_sent", self.guarded_begin(expected, message_id="guarded-open")["status"])
        refused = self.guarded_begin(expected, message_id="guarded-not-sent")
        self.conclude(refused["attempt_seq"], outcome="not_sent", reason="daemon_unreachable", transport=None)
        retry = self.guarded_begin(expected, message_id="guarded-not-sent")
        self.assertEqual("begun", retry["status"])
        self.assertGreater(retry["attempt_seq"], refused["attempt_seq"])

    def test_binding_snapshot_is_read_under_immediate_lock_and_lock_ends_at_commit(self):
        expected = self.expectation(self.bind()["binding"])
        original = self.store._wake_binding
        witnessed = []

        def read_with_independent_contender(role):
            binding = original(role)
            self.assertTrue(self.store._db.in_transaction)
            contender = sqlite3.connect(self.home / "relay.sqlite3", timeout=0, isolation_level=None)
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    contender.execute("BEGIN IMMEDIATE")
                witnessed.append(binding["generation"])
            finally:
                contender.close()
            return binding

        with mock.patch.object(self.store, "_wake_binding", side_effect=read_with_independent_contender):
            begun = self.guarded_begin(expected)
        self.assertEqual([expected["generation"]], witnessed)
        self.assertEqual("begun", begun["status"])
        contender = sqlite3.connect(self.home / "relay.sqlite3", timeout=0, isolation_level=None)
        try:
            contender.execute("BEGIN IMMEDIATE")
            contender.execute("ROLLBACK")
        finally:
            contender.close()


class WakeCliExpectedBindingTests(WakeLedgerCase):
    def test_parsed_plan_dispatch_enforces_expectations_without_main(self):
        binding = self.bind()["binding"]
        flags = ("--expect-generation", str(binding["generation"]), "--expect-provider", "codex",
                 "--expect-thread", THREAD)
        parser = core_cli.build_parser()

        def parsed(extra):
            return parser.parse_args(["wake-ledger", "plan", "operator", "--ref", "/srv/task.md",
                                      "--requested", "queue", "--ref-sha256", "a" * 64, "--ref-size", "15", *extra])

        before = self.store.events()
        for extra in ((), flags):
            with self.subTest(extra=extra):
                self.assertEqual("ready", core_cli._dispatch(self.store, parsed(extra))["status"])
        wrong = (*flags[:-1], OTHER)
        with self.assertRaises(ConflictError):
            core_cli._dispatch(self.store, parsed(wrong))
        with mock.patch.object(self.store, "wake_plan", side_effect=AssertionError("malformed plan dispatched")):
            with self.assertRaises(ValidationError):
                core_cli._dispatch(self.store, parsed(("--expect-generation", "1")))
        self.assertEqual(before, self.store.events())

    def command(self, action, flags):
        actor = ("--agent", "claude", "--session", "sender") if action == "begin" else ()
        return self.cli("--json", "wake-ledger", action, "operator", "--ref", "/srv/task.md",
                        "--requested", "queue", "--ref-sha256", "a" * 64, "--ref-size", "15",
                        *actor, *flags)

    def test_codex_flags_roundtrip_plan_and_begin_with_stale_assertion_refused(self):
        binding = self.bind()["binding"]
        flags = ("--expect-generation", str(binding["generation"]), "--expect-provider", "codex",
                 "--expect-thread", THREAD)
        before = self.store.events()
        code, out, err = self.command("plan", flags)
        self.assertEqual((0, ""), (code, err))
        plan = json.loads(out)
        self.assertEqual("ready", plan["status"])
        self.assertEqual(binding["generation"], plan["binding"]["generation"])
        self.assertEqual(before, self.store.events())
        code, out, err = self.command("begin", flags)
        self.assertEqual((0, ""), (code, err))
        begun = json.loads(out)
        self.assertEqual("begun", begun["status"])
        self.assertEqual(THREAD, begun["binding"]["thread"])
        self.bind(OTHER, replace=True)
        after_rebind = self.store.events()
        for action in ("plan", "begin"):
            code, out, err = self.command(action, flags)
            self.assertEqual((73, ""), (code, out))
            self.assertIn("wake recipient binding changed", err)
            self.assertEqual(after_rebind, self.store.events())

    def test_claude_owner_flags_roundtrip_and_session_mismatch_refuses(self):
        binding = self.store.wake_bind("operator", provider="claude", endpoint="unix:///srv/inbox.sock",
                                       replace=False, agent="claude:owner", session="session:one")["binding"]
        flags = ("--expect-generation", str(binding["generation"]), "--expect-provider", "claude",
                 "--expect-bound-agent", "claude:owner", "--expect-bound-session", "session:one")
        before = self.store.events()
        code, out, err = self.command("plan", flags)
        self.assertEqual((0, ""), (code, err))
        self.assertEqual("ready", json.loads(out)["status"])
        self.assertEqual(before, self.store.events())
        code, out, err = self.command("begin", flags)
        self.assertEqual((0, ""), (code, err))
        self.assertEqual("begun", json.loads(out)["status"])
        after_begin = self.store.events()
        wrong = (*flags[:-1], "session:two")
        code, out, err = self.command("begin", wrong)
        self.assertEqual((73, ""), (code, out))
        self.assertIn("wake recipient binding changed", err)
        self.assertEqual(after_begin, self.store.events())

    def test_partial_mixed_and_invalid_assertions_refuse_before_opening_ledger(self):
        malformed = (
            ("--expect-generation", "1"),
            ("--expect-generation", "1", "--expect-provider", "codex"),
            ("--expect-provider", "codex", "--expect-thread", THREAD),
            ("--expect-generation", "1", "--expect-provider", "claude", "--expect-bound-agent", "claude"),
            ("--expect-generation", "1", "--expect-provider", "codex", "--expect-thread", THREAD,
             "--expect-bound-agent", "claude"),
            ("--expect-generation", "0", "--expect-provider", "codex", "--expect-thread", THREAD),
        )
        with mock.patch.object(core_cli.RelayStore, "open", side_effect=AssertionError("ledger opened")):
            for flags in malformed:
                for action in ("plan", "begin"):
                    with self.subTest(flags=flags, action=action):
                        code, out, err = self.command(action, flags)
                        self.assertEqual((64, ""), (code, out))
                        self.assertIn("expected binding", err)


class WakeBindingTests(WakeLedgerCase):
    def test_a_binding_generation_is_its_event_sequence(self):
        first = self.bind()
        self.assertFalse(first["duplicate"])
        generation = first["binding"]["generation"]
        self.assertEqual("wake.bound", self.store.events(after=generation - 1, limit=1)[0]["kind"])
        self.assertEqual("active", first["binding"]["state"])
        self.assertTrue(self.bind()["duplicate"], "the same conversation is already bound")
        with self.assertRaisesRegex(ConflictError, f"already bound to conversation {THREAD} "
                                                   rf"\(binding {generation}\); pass --replace"):
            self.bind(OTHER)
        with self.assertRaisesRegex(ConflictError, "already bound through unix:///srv/codex"):
            self.bind(endpoint="unix:///srv/other/app-server-control.sock")
        moved = self.bind(OTHER, replace=True)
        self.assertEqual(generation, moved["replaced"]["generation"])
        self.assertGreater(moved["binding"]["generation"], generation)
        self.assertEqual(OTHER, self.store.wake_bindings("operator")["bindings"][0]["thread"])

    def test_pause_resume_and_unbind_change_only_the_current_generation(self):
        with self.assertRaisesRegex(ConflictError, "isn't bound in this ledger, so there is nothing to pause"):
            self.store.wake_control("pause", "operator", agent="claude", session="binder")
        self.assertTrue(self.store.wake_control("unbind", "operator", agent="claude", session="binder")["duplicate"])
        generation = self.bind()["binding"]["generation"]
        paused = self.store.wake_control("pause", "operator", agent="claude", session="binder")
        self.assertEqual(("paused", False), (paused["binding"]["state"], paused["duplicate"]))
        self.assertTrue(self.store.wake_control("pause", "operator", agent="claude", session="binder")["duplicate"])
        self.assertEqual("paused", self.begin()["status"])
        # Refresh and handover keep the pause until a separate deliberate resume.
        self.bind(OTHER, replace=True)
        self.assertEqual("paused", self.store.wake_bindings("operator")["bindings"][0]["state"])
        self.assertFalse(self.store.wake_control("resume", "operator", agent="claude", session="binder")["duplicate"])
        ended = self.store.wake_control("unbind", "operator", agent="claude", session="binder")
        self.assertEqual(OTHER, ended["ended"]["thread"])
        self.assertEqual("unbound", ended["binding"]["state"])
        self.assertEqual("unbound", self.begin()["status"])
        events = [e for e in self.store.events(after=generation - 1) if e["kind"].startswith("wake.")]
        self.assertEqual(["wake.bound", "wake.paused", "wake.bound", "wake.paused", "wake.resumed", "wake.unbound"],
                         [e["kind"] for e in events])
        self.assertEqual([generation, events[2]["seq"]],
                         [events[1]["meta"]["generation"], events[5]["meta"]["generation"]])

    def test_foreign_replacement_requires_authority_and_current_generation(self):
        first = self.bind()["binding"]
        before = self.store.events()
        options = dict(thread=OTHER, endpoint=ENDPOINT, cwd="/srv/work", replace=True, agent="codex", session=OTHER)
        with self.assertRaisesRegex(ConflictError, "explicit user authorization"):
            self.store.wake_bind("operator", **options)
        with self.assertRaisesRegex(ConflictError, "binding changed"):
            self.store.wake_bind("operator", **options, expected_generation=first["generation"] + 1,
                                 reason="Approved synthetic transfer", approval_ref="receipt:approval")
        with self.assertRaisesRegex(ValidationError, "immutable"):
            self.store.wake_bind("operator", **options, expected_generation=first["generation"],
                                 reason="Approved synthetic transfer", approval_ref="mutable.md")
        self.assertEqual(before, self.store.events(), "all refused mutations must be write-free")
        moved = self.store.wake_bind("operator", **options, expected_generation=first["generation"],
                                    reason="Approved synthetic transfer", approval_ref="receipt:approval")
        notices = [event for event in self.store.events() if event["kind"] == "work.handoff"]
        self.assertEqual({session_target("codex", THREAD), session_target("codex", OTHER)}, {event["target"] for event in notices})
        self.assertEqual({f"receipt:binding:{moved['binding']['generation']}"},
                         {event["artifact"] for event in notices})

    def test_holder_refresh_preserves_charter_scope_and_pause_without_transfer(self):
        first = self.store.wake_bind("operator", thread=THREAD, endpoint=ENDPOINT, cwd="/srv/work", replace=False,
                                     agent="codex", session=THREAD, charter="Review coordination", role_scope="local")
        self.store.wake_control("pause", "operator", agent="codex", session=THREAD)
        updated = self.store.wake_bind("operator", thread=THREAD, endpoint="unix:///srv/new.sock", cwd="/srv/work", replace=True,
                                       agent="codex", session=THREAD,
                                       expected_generation=first["binding"]["generation"])["binding"]
        self.assertEqual(("paused", "Review coordination", "local"),
                         (updated["state"], updated["charter"], updated["role_scope"]))
        self.assertFalse(any(e["kind"] == "work.handoff" for e in self.store.events()))

    def test_colon_colliding_owner_labels_cannot_refresh_or_control_another_tuple(self):
        self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///srv/first.sock",
                             replace=False, agent="claude", session="one:two")
        before = self.store.events()
        with self.assertRaises(ConflictError):
            self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///srv/other.sock",
                                 replace=True, agent="claude:one", session="two")
        for action in ("pause", "resume", "unbind"):
            with self.subTest(action=action), self.assertRaises(ConflictError):
                self.store.wake_control(action, "reviewer", agent="claude:one", session="two")
        self.assertEqual(before, self.store.events())
        refreshed = self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///srv/new.sock",
                                         replace=True, agent="claude", session="one:two")["binding"]
        self.assertEqual(("claude", "one:two"), (refreshed["bound_agent"], refreshed["bound_session"]))

    def test_foreign_control_and_stale_cas_are_write_free(self):
        first = self.bind()["binding"]
        before = self.store.events()
        for action in ("unbind", "pause", "resume"):
            with self.subTest(action=action), self.assertRaisesRegex(ConflictError, "explicit user authorization"):
                self.store.wake_control(action, "operator", agent="david", session="label-is-not-authority")
        with self.assertRaisesRegex(ConflictError, "binding changed"):
            self.store.wake_control("unbind", "operator", agent="claude", session="binder",
                                    expected_generation=first["generation"] + 1)
        self.assertEqual(before, self.store.events())

    def test_handover_notices_preserve_long_valid_claude_identities(self):
        agent, old, new = "a" * 200, "o" * 200, "n" * 200
        first = self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///srv/old.sock",
                                     replace=False, agent=agent, session=old)["binding"]
        moved = self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///srv/new.sock",
                                     replace=True, agent=agent, session=new,
                                     expected_generation=first["generation"], reason="Approved synthetic transfer",
                                     approval_ref="receipt:approval")["binding"]
        self.assertGreater(moved["generation"], first["generation"])
        for session in (old, new):
            notices = self.store.inbox(agent, session=session)["pending_signals"]
            self.assertEqual(1, len(notices))
        self.assertEqual(session_target(agent, session), notices[0]["target"])

    def test_a_change_to_a_stale_generation_fails_closed(self):
        generation = self.bind()["binding"]["generation"]
        self.bind(OTHER, replace=True)
        stale = normalize_event({"kind": "wake.paused", "agent": "claude", "session": "s", "target": "operator",
                                 "summary": "Paused", "meta": {"role": "operator", "generation": generation}},
                                internal=True)
        database = sqlite3.connect(self.home / "relay.sqlite3")
        with database:
            database.execute(
                "INSERT INTO events (event_id, protocol_version, kind, agent, session, target, summary, meta_json, "
                "canonical_json, body_hash) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stale.event_id, stale.kind, stale.agent, stale.session, stale.target, stale.summary,
                 json.dumps(stale.meta, sort_keys=True, separators=(",", ":")), stale.canonical_json,
                 stale.body_hash))
        database.close()
        with self.assertRaisesRegex(StateError, "changes a binding that is not current"):
            self.store.wake_bindings("operator")
        with self.assertRaises(StateError):
            self.begin()


class WakeAttemptTests(WakeLedgerCase):
    def test_history_keeps_original_recipient_and_requires_its_ack_for_consumption(self):
        generation = self.bind()["binding"]["generation"]
        signal = self.store.emit({"kind": "work.handoff", "agent": "claude", "session": "sender",
                                  "target": session_target("codex", THREAD), "artifact": "receipt:synthetic-work",
                                  "summary": "Read and review this synthetic contribution"})["event"]["seq"]
        attempt = self.begin(str(signal))["attempt_seq"]
        self.conclude(attempt)
        self.bind(OTHER, replace=True)
        original = self.store.wake_history("operator")["attempts"][0]
        self.assertEqual((generation, f"codex:{THREAD}", "queued", "not_acknowledged"),
                         (original["generation"], original["recipient"], original["outcome"],
                          original["consumption_state"]))
        with self.assertRaises(ConflictError):
            self.store.acknowledge(signal, agent="codex", session=OTHER)
        receipt = self.store.acknowledge(signal, agent="codex", session=THREAD)
        before = self.store.events()
        observed = self.store.wake_history("operator", ref=str(signal))["attempts"][0]
        self.assertEqual(("acknowledged", receipt["event"]["seq"], f"codex:{THREAD}"),
                         (observed["consumption_state"], observed["acknowledgement_seq"], observed["consumed_by"]))
        self.assertEqual(before, self.store.events(), "history is a read, not another delivery")

    def test_history_pages_attempts_and_keeps_file_consumption_unknown(self):
        self.bind()
        seqs = [self.begin(message_id=f"history-{index}")["attempt_seq"] for index in range(3)]
        first = self.store.wake_history("operator", limit=2)
        self.assertEqual(list(reversed(seqs[1:])), [item["seq"] for item in first["attempts"]])
        self.assertTrue(first["has_more"])
        rest = self.store.wake_history("operator", limit=2, before=first["next_before"])
        self.assertEqual([seqs[0]], [item["seq"] for item in rest["attempts"]])
        self.assertFalse(rest["has_more"])
        self.assertEqual("unknown", rest["attempts"][0]["consumption_state"])

    def test_non_delivery_sequence_has_unknown_consumption(self):
        generation = self.bind()["binding"]["generation"]
        attempt = self.begin(ref=str(generation))
        self.conclude(attempt["attempt_seq"])
        observed = self.store.wake_history("operator", ref=str(generation))["attempts"][0]
        self.assertEqual("unknown", observed["consumption_state"])
        self.assertNotIn("acknowledgement_seq", observed)

    def test_default_message_id_names_the_role_generation_and_reference(self):
        generation = self.bind()["binding"]["generation"]
        first = self.plan()["message_id"]
        self.assertRegex(first, rf"^wake-operator-{generation}-[0-9a-f]{{12}}$")
        self.assertEqual(first, self.plan(requested="steer")["message_id"])
        self.assertNotEqual(first, self.plan("/srv/other.md")["message_id"])
        # Another ledger with the same role, generation and reference names a different message.
        other = self.make_repo("other")
        with mock.patch("relay_core.store._expected_workspace_binding", return_value=(other / ".git").resolve()):
            with RelayStore.open(repo=other, state_home=self.base / "other-state") as store:
                store.wake_bind("operator", thread=THREAD, endpoint=ENDPOINT, cwd="/srv/work", replace=False,
                                agent="claude", session="binder")
                elsewhere = store.wake_plan("operator", ref="/srv/task.md", requested="queue",
                                            **TASK)["message_id"]
        self.assertEqual(first.rsplit("-", 1)[0], elsewhere.rsplit("-", 1)[0])
        self.assertNotEqual(first, elsewhere)
        # Other bytes at the same path name another message.
        self.assertNotEqual(first, self.plan(ref_sha256="b" * 64, ref_size=15)["message_id"])
        with self.assertRaisesRegex(ValidationError, "a file reference needs its lowercase sha256"):
            self.store.wake_plan("operator", ref="/srv/task.md", requested="queue")
        # A new generation names new messages.
        self.bind(OTHER, replace=True)
        self.assertNotEqual(first, self.plan()["message_id"])

    def test_an_id_is_blocked_once_delivered_uncertain_or_open_but_not_when_not_sent(self):
        self.bind()
        for outcome, reason, transport, native, blocks in (
                ("not_sent", "daemon_unreachable", None, None, False),
                ("not_sent", "queue_refused", "queue", None, False),
                ("queued", "requested", "queue", "c0000000-0000-7000-8000-000000000003", True),
                ("uncertain", "no_answer", "queue", None, True),
                (None, None, None, None, True)):
            message_id = f"id-{outcome}-{reason}"
            attempt = self.begin(message_id=message_id)
            self.assertEqual("begun", attempt["status"])
            if outcome is not None:
                self.conclude(attempt["attempt_seq"], outcome, reason, transport, native)
            again = self.begin(message_id=message_id)
            self.assertEqual("already_sent" if blocks else "begun", again["status"], message_id)
            if blocks:
                self.assertEqual(attempt["attempt_seq"], again["prior"]["seq"])
                self.assertEqual(outcome or "open", again["prior"]["outcome"])
        # Message ids are single-use across the whole ledger, not per role.
        self.bind(role="reviewer")
        self.assertEqual("already_sent", self.begin(message_id="id-queued-requested", role="reviewer")["status"])

    def test_one_conclusion_per_attempt_consistent_with_what_it_asked(self):
        self.bind()
        queue = self.begin(message_id="asked-queue")["attempt_seq"]
        steer = self.begin(message_id="asked-steer", requested="steer")["attempt_seq"]
        for attempt, args, message in (
                (queue, ("steered", "live_turn", "steer", "c0000000-0000-7000-8000-000000000003"), "asked to queue"),
                (queue, ("queued", "no_live_turn", "queue"), "asked to queue"),
                (steer, ("queued", "requested", "queue"), "asked to steer")):
            with self.subTest(args=args), self.assertRaisesRegex(ValidationError, message):
                self.conclude(attempt, *args)
        first = self.conclude(steer, "queued", "turn_ended", "queue", "d0000000-0000-7000-8000-00000000000a")
        self.assertEqual(f"wake-concluded:{steer}", first["event"]["id"])
        retry = self.conclude(steer, "queued", "turn_ended", "queue", "d0000000-0000-7000-8000-00000000000a",
                              session="later")
        self.assertEqual((True, first["event"]["seq"]), (retry["duplicate"], retry["event"]["seq"]))
        with self.assertRaisesRegex(ConflictError, "already has a different outcome"):
            self.conclude(steer, "uncertain", "no_answer", "steer")
        with self.assertRaisesRegex(ConflictError, "is not a wake attempt"):
            self.conclude(first["event"]["seq"])
        with self.assertRaisesRegex(ConflictError, "is not a wake attempt"):
            self.conclude(9999)

    def test_a_conclusion_keeps_the_native_reason_and_what_the_attempt_asked(self):
        self.bind()
        steer = self.begin(message_id="asked-steer-2", requested="steer")["attempt_seq"]
        queue = self.begin(message_id="asked-queue-2")["attempt_seq"]
        for reason in ("turn_changed", "turn_not_steerable"):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValidationError, "asked to queue"):
                self.conclude(queue, "queued", reason, "queue")
        receipt = self.store.wake_conclude(steer, outcome="uncertain", reason="unrecognized_error", transport="steer",
                                           native_id=None, agent="claude", session="sender",
                                           detail="Codex -32603: failed to steer turn: synthetic")
        self.assertEqual("Codex -32603: failed to steer turn: synthetic", receipt["event"]["meta"]["detail"])
        blocked = self.begin(message_id="asked-steer-2")
        self.assertEqual(("already_sent", "uncertain", "Codex -32603: failed to steer turn: synthetic"),
                         (blocked["status"], blocked["prior"]["outcome"], blocked["prior"]["detail"]))
        with self.assertRaisesRegex(ValidationError, "detail must be one line"):
            normalize_event(concluded(detail="line one\nline two"), internal=True)
        with self.assertRaisesRegex(ValidationError, "detail exceeds 300 characters"):
            normalize_event(concluded(detail="x" * 301), internal=True)

    def test_a_sequence_reference_must_exist_in_this_ledger(self):
        generation = self.bind()["binding"]["generation"]
        self.assertEqual("ready", self.plan(str(generation))["status"])
        with self.assertRaisesRegex(ValidationError, r"ledger sequence 999 doesn't exist here \(the latest is"):
            self.plan("999")

    def test_an_attempt_records_the_binding_it_used(self):
        generation = self.bind()["binding"]["generation"]
        begun = self.begin(requested="steer")
        attempt = self.store.events(after=begun["attempt_seq"] - 1, limit=1)[0]
        self.assertEqual({"role": "operator", "generation": generation, "provider": "codex", "thread": THREAD,
                          "message_id": begun["message_id"], "ref": "/srv/task.md", **TASK, "requested": "steer"},
                         attempt["meta"])
        self.assertEqual("Wake operator (steer requested): /srv/task.md", attempt["summary"])
        shown = self.store.wake_bindings()["bindings"][0]["last_attempt"]
        self.assertEqual(("open", begun["attempt_seq"]), (shown["outcome"], shown["seq"]))


class ClaudeInboxTests(WakeLedgerCase):
    INBOX = "unix:///run/user/1000/cc-socks/7739.sock"

    def bind_inbox(self, endpoint=INBOX, replace=False):
        return self.store.wake_bind("reviewer", provider="claude", endpoint=endpoint, replace=replace,
                                    agent="claude", session="self")

    def test_a_claude_binding_is_its_inbox_with_a_generation(self):
        bound = self.bind_inbox()["binding"]
        self.assertEqual(("claude", None, None, self.INBOX), (bound["provider"], bound["thread"], bound["cwd"],
                                                              bound["endpoint"]))
        self.assertTrue(self.bind_inbox()["duplicate"])
        with self.assertRaisesRegex(ConflictError, "already bound through unix:///run/user/1000/cc-socks/7739.sock"):
            self.bind_inbox("unix:///run/user/1000/cc-socks/8000.sock")
        moved = self.bind_inbox("unix:///run/user/1000/cc-socks/8000.sock", replace=True)
        self.assertEqual(bound["generation"], moved["replaced"]["generation"])
        self.assertEqual("Bound reviewer to the Claude Code inbox /run/user/1000/cc-socks/8000.sock, replacing "
                         f"binding {bound['generation']}",
                         self.store.events(after=moved["binding"]["generation"] - 1, limit=1)[0]["summary"])

    def test_an_inbox_attempt_concludes_only_with_inbox_outcomes(self):
        generation = self.bind_inbox()["binding"]["generation"]
        begun = self.begin(role="reviewer", requested="steer")
        attempt = self.store.events(after=begun["attempt_seq"] - 1, limit=1)[0]
        self.assertEqual({"role": "reviewer", "generation": generation, "provider": "claude",
                          "message_id": begun["message_id"], "ref": "/srv/task.md", **TASK, "requested": "steer"},
                         attempt["meta"])
        for args in (("queued", "requested", "queue"), ("steered", "live_turn", "steer", "c0000000-0000-7000-8000-000000000003"),
                     ("not_sent", "daemon_unreachable", None)):
            with self.subTest(args=args), self.assertRaisesRegex(ValidationError, "went to claude, which cannot"):
                self.conclude(begun["attempt_seq"], *args)
        self.conclude(begun["attempt_seq"], "delivered", "inbox_accepted", "inbox")
        self.assertEqual("already_sent", self.begin(role="reviewer")["status"])
        self.bind()
        codex = self.begin(message_id="to-codex")["attempt_seq"]
        with self.assertRaisesRegex(ValidationError, "went to codex, which cannot conclude delivered"):
            self.conclude(codex, "delivered", "inbox_accepted", "inbox")


class NotACheckoutTests(unittest.TestCase):
    def test_a_folder_outside_git_names_itself_and_the_fix(self):
        with tempfile.TemporaryDirectory(prefix="relay-not-git-") as folder:
            with self.assertRaises(StateError) as refused:
                RelayStore.open(repo=folder, state_home=Path(folder) / "state")
            self.assertEqual(f"{json.dumps(str(Path(folder).resolve()))} is not a Git checkout: run from an "
                             "enrolled checkout or pass --repo <checkout>", str(refused.exception))
            self.assertFalse((Path(folder) / "state").exists())


class WakeCommandTests(WakeLedgerCase):
    def test_control_commands_print_status_and_next_step(self):
        code, _, err = self.cli("pause", "operator", "--agent", "david", "--session", "desk")
        self.assertEqual(73, code)
        self.assertIn("bind it first: multithread bind operator --thread <codex conversation id>", err)
        generation = self.bind()["binding"]["generation"]
        expected = {
            "pause": f"PAUSED: Wakes to operator are paused (binding {generation}); nothing is sent until they are "
                     "resumed.\nNext: Resume with: multithread resume operator\n",
            "resume": f"RESUMED: Wakes to operator reach Codex conversation {THREAD} (binding {generation}).\n"
                      "Next: Nothing.\n",
            "unbind": f"UNBOUND: operator no longer names Codex conversation {THREAD} (binding {generation} ended). Its "
                      "wake records stay in the ledger.\nNext: Nothing. To bind it again: multithread bind operator "
                      "--thread <codex conversation id>\n",
        }
        for action in ("pause", "resume", "unbind"):
            self.assertEqual((0, expected[action], ""), self.cli(action, "operator", "--agent", "claude",
                                                                 "--session", "binder"))
        code, out, _ = self.cli("unbind", "operator", "--agent", "david", "--session", "desk")
        self.assertEqual("ALREADY UNBOUND: operator isn't bound in this ledger; nothing changed.\nNext: Nothing. To "
                         "bind it: multithread bind operator --thread <codex conversation id>, or multithread bind "
                         "operator --claude-socket \"$CLAUDE_CODE_MESSAGING_SOCKET\" from the Claude Code session\n",
                         out)
        self.store.wake_bind("reviewer", provider="claude", endpoint="unix:///run/user/1000/cc-socks/7739.sock",
                             replace=False, agent="claude", session="self")
        code, out, _ = self.cli("unbind", "reviewer", "--agent", "claude", "--session", "self")
        self.assertTrue(out.startswith("UNBOUND: reviewer no longer names the Claude Code inbox "
                                       "/run/user/1000/cc-socks/7739.sock"))
        self.assertTrue(out.endswith('To bind it again: multithread bind reviewer --claude-socket '
                                     '"$CLAUDE_CODE_MESSAGING_SOCKET" from the Claude Code session\n'))

    def test_wake_ledger_steps_speak_json(self):
        code, out, _ = self.cli("--json", "wake-ledger", "bind", "operator", "--agent", "claude", "--session", "s",
                                "--thread", THREAD, "--endpoint", ENDPOINT, "--cwd", "/srv/work")
        self.assertEqual(0, code)
        generation = json.loads(out)["binding"]["generation"]
        code, out, _ = self.cli("--json", "wake-ledger", "begin", "operator", "--agent", "claude", "--session", "s",
                                "--ref", "/srv/task.md", "--requested", "queue", "--ref-sha256", "a" * 64,
                                "--ref-size", "15")
        begun = json.loads(out)
        self.assertEqual(("begun", generation), (begun["status"], begun["binding"]["generation"]))
        code, out, _ = self.cli("--json", "wake-ledger", "conclude", str(begun["attempt_seq"]), "--agent",
                                "claude", "--session", "s", "--outcome", "not_sent", "--reason",
                                "daemon_unreachable")
        self.assertEqual("not_sent", json.loads(out)["event"]["meta"]["outcome"])
        code, out, _ = self.cli("--json", "wake-ledger", "observed", THREAD)
        self.assertEqual({"ledger": str(self.repo), "client": "codex", "session": THREAD, "observed": False},
                         json.loads(out))
        code, _, err = self.cli("--json", "wake-ledger", "plan", "operator", "--ref", "task.md",
                                "--requested", "queue")
        self.assertEqual(64, code)
        self.assertIn("ref must be an absolute task-file path or a ledger sequence number", err)


if __name__ == "__main__":
    unittest.main()
