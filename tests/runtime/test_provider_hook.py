"""Installed provider-hook acceptance; no provider process or real account config."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import test_installed as installed
from relay_runtime import cli as runtimecli


def snapshot(root):
    """Bytes and namespace metadata, excluding access times changed by reads."""
    if not root.exists():
        return None
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        body = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                else hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(info.st_mode) else None)
        result[str(path.relative_to(root))] = (
            info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, body)
    return result


def strict_event(raw):
    """The event name the hook's strict reader would see, if any."""
    def unique(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("repeated key")
        return dict(pairs)

    def constant(name):
        raise ValueError(name)
    if len(raw.encode("utf-8")) > 256 * 1024:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=constant)
    except ValueError:
        return None
    return value.get("hook_event_name") if isinstance(value, dict) else None


class ProviderHookTests(unittest.TestCase):
    def setUp(self):
        # Compose the module's fixture; importing/subclassing its TestCase would
        # unintentionally discover all of InstalledTests for a second time.
        self.fixture = installed.InstalledTests("test_actual_lifecycle_hook_records_once")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.protected = []
        for relative in ("project/.claude", "project/.codex", "account/provider-settings"):
            directory = self.fixture.base / relative
            directory.mkdir(parents=True)
            (directory / "settings.fixture").write_bytes(b"unrelated synthetic config; preserve\n")
            self.protected.append(directory)
        self.protected.append(self.fixture.repo / ".git/hooks")

    def hook(self, client, event=None, session="provider-session", *, raw=None, **kwargs):
        payload = {"hook_event_name": event, "session_id": session}
        payload.update(kwargs.pop("payload", {}))
        return self.fixture.command("provider-hook", "--client", client,
                                    stdin=json.dumps(payload) if raw is None else raw, **kwargs)

    def context(self, result, client, event, session):
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({"hookSpecificOutput"}, set(value))
        output = value["hookSpecificOutput"]
        self.assertEqual({"hookEventName", "additionalContext"}, set(output))
        self.assertEqual(event, output["hookEventName"])
        context = output["additionalContext"]
        self.assertIsInstance(context, str)
        self.assertIn("MULTITHREAD BRIEF v1", context)
        self.assertIn(client, context)
        self.assertIn(session, context)
        self.assertLessEqual(len(context.encode("utf-8")), 8192)
        return context

    def warned(self, result, event, because, repo=None):
        """An enrolled checkout's missed ledger is visible, with its reason and fix."""
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("unavailable", result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({"systemMessage", "hookSpecificOutput"}, set(value))
        output = value["hookSpecificOutput"]
        self.assertEqual({"hookEventName", "additionalContext"}, set(output))
        self.assertEqual(event, output["hookEventName"])
        context = output["additionalContext"]
        self.assertTrue(context.startswith(
            "MULTITHREAD WARNING: this checkout is enrolled, but Multithread's " + event + " hook could not deliver "
            "verified ledger context this time (" + because + "). This session's Multithread record may be "
            "incomplete, and this step shows no brief. Tell the person; the fix starts with: "), context)
        # One failed step never claims the whole session went unrecorded.
        self.assertNotIn("not being recorded", result.stdout)
        self.assertTrue(value["systemMessage"].startswith(
            "Multithread could not deliver verified ledger context for this step (" + because
            + "); this session's record may be incomplete. Run: "), value["systemMessage"])
        self.assertNotIn("MULTITHREAD BRIEF", context)
        self.assertNotIn("\n", context)
        fix = " setup --repo " + shlex.quote(str(repo or self.fixture.repo)) + " --check"
        self.assertTrue(context.endswith(fix), context)
        self.assertIn(because, value["systemMessage"])
        self.assertTrue(value["systemMessage"].endswith(fix))
        self.assertLess(len(result.stdout), 1024)
        return value

    def silent(self, result, *, degraded=False):
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        if degraded:
            self.assertIn("unavailable", result.stderr)

    def rows(self, state=None):
        path = (state or self.fixture.state) / "relay.sqlite3"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            return connection.execute("SELECT seq, kind, agent, session FROM events ORDER BY seq").fetchall()

    def assert_sql_readonly_snapshot(self, before):
        # Existing read-only admission intentionally allows WAL/SHM reader
        # coordination. This witness permits only SHM bytes/mtime to change;
        # its inode/mode/size and every other fixture object remain identical.
        after = snapshot(self.fixture.base)
        shm = "project/.relay/relay.sqlite3-shm"
        self.assertEqual(before.keys(), after.keys())
        self.assertEqual(before[shm][:4], after[shm][:4])
        changed = [key for key in before if key != shm and before[key] != after[key]]
        self.assertEqual([], changed, {key: (before[key], after[key]) for key in changed})

    def test_startup_records_before_brief_and_ignores_payload_repository_and_content(self):
        self.fixture.initialize()
        foreign = self.fixture.base / "foreign-project"
        subprocess.run(["/usr/bin/git", "init", "-q", str(foreign)], check=True)
        transcript = foreign / "transcript.fixture"
        transcript.write_text("private transcript fixture must never be ingested")
        protected = [snapshot(path) for path in self.protected] + [snapshot(foreign)]
        marker = "PAYLOAD_PRIVATE_SENTINEL"
        for index, client in enumerate(("codex", "claude"), 1):
            session = client + "-startup"
            result = self.hook(client, "SessionStart", session, payload={
                "cwd": str(foreign), "project_dir": str(foreign),
                "transcript_path": str(transcript), "prompt": marker,
                "last_assistant_message": marker, "authorization": marker,
                "tool_input": {"command": marker}, "agent": "foreign-actor",
            }, extra_env={"CLAUDE_PROJECT_DIR": str(foreign),
                          "RELAY_AGENT": "foreign-actor", "RELAY_SESSION": "foreign-session"})
            context = self.context(result, client, "SessionStart", session)
            self.assertIn("last_seq=" + str(index), context)
            self.assertNotIn(marker, result.stdout + result.stderr)
            self.assertNotIn("foreign-actor", context)
            events = self.fixture.success("events")
            self.assertEqual(index, len(events))
            self.assertEqual(("session.started", client, session),
                             tuple(events[-1][key] for key in ("kind", "agent", "session")))
            self.assertNotIn(marker, json.dumps(events))
            self.assertNotIn(str(transcript), json.dumps(events))
        self.assertEqual(protected, [snapshot(path) for path in self.protected] + [snapshot(foreign)])
        self.assertEqual(2, len(self.rows()))

    def test_checkout_free_codex_hook_follows_the_session_directory(self):
        """The generated Codex hook has no --repo: Codex runs it in the session's
        working directory, and enrollment comes from that directory alone."""
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
               "-c", "user.name=Fixture", "-c", "user.email=fixture"]
        alpha, beta, stray = (self.fixture.base / name for name in ("alpha", "beta", "unenrolled"))
        for repo in (alpha, beta, stray):
            subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
            subprocess.run(git + ["-C", str(repo), "commit", "--allow-empty", "-qm", "Fixture"], check=True)
        linked = self.fixture.base / "alpha-linked"
        subprocess.run(git + ["-C", str(alpha), "worktree", "add", "-qb", "linked", str(linked)], check=True)
        plain = self.fixture.base / "not-a-checkout"
        plain.mkdir()
        for repo in (alpha, beta):
            self.assertTrue(self.fixture.success("init", repo=repo)["initialized"])

        def ledger(repo):
            return [(row["kind"], row["session"]) for row in self.fixture.success("events", repo=repo)]

        def run(cwd, event, stated=None, **fields):
            # Codex states the session directory; other payload paths never select.
            payload = {"cwd": str(cwd if stated is None else stated), "transcript_path": str(beta), **fields}
            return self.hook("codex", event, "moving-session", payload=payload, cwd=cwd)

        # A stated session directory can veto a mismatch but never select one.
        before = snapshot(self.fixture.base)
        for stated in (beta, self.fixture.base / "absent", "\0"):
            with self.subTest(stated=stated):
                self.warned(run(alpha, "SessionStart", stated=stated), "SessionStart",
                            "the provider's hook input could not be used", repo=alpha)
        self.assertEqual(before, snapshot(self.fixture.base))
        alias = self.fixture.base / "alpha-alias"
        alias.symlink_to(alpha)
        self.context(run(alpha, "UserPromptSubmit", stated=alias, prompt_id="alias-prompt"),
                     "codex", "UserPromptSubmit", "moving-session")
        unstated = self.hook("codex", "UserPromptSubmit", "moving-session", cwd=alpha,
                             payload={"prompt_id": "unstated-prompt"})
        self.context(unstated, "codex", "UserPromptSubmit", "moving-session")

        context = self.context(run(alpha, "SessionStart"), "codex", "SessionStart", "moving-session")
        self.assertIn(json.dumps(str(alpha)), context)
        self.assertEqual([("session.started", "moving-session")], ledger(alpha))
        self.assertEqual([], ledger(beta))
        # A linked worktree shares its repository's ledger.
        self.silent(run(linked, "Stop", turn_id="linked-turn", prompt_id="linked-prompt"))
        self.assertEqual(("turn.completed", "moving-session"), ledger(alpha)[-1])
        # The ledger follows the session's working checkout, including a resume
        # elsewhere; it is never chosen by the hook command or payload.
        before_alpha = ledger(alpha)
        self.context(run(beta, "SessionStart", source="resume"), "codex", "SessionStart", "moving-session")
        self.context(run(beta, "UserPromptSubmit", prompt_id="beta-prompt"), "codex", "UserPromptSubmit", "moving-session")
        self.silent(run(beta, "Interrupt", turn_id="beta-turn"))
        self.silent(run(beta, "SessionEnd"))
        self.assertEqual(before_alpha, ledger(alpha))
        self.assertEqual(["session.started", "turn.interrupted", "session.ended"],
                         [kind for kind, _ in ledger(beta)])
        # Unenrolled checkouts and plain directories degrade without state.
        before = snapshot(self.fixture.base)
        for directory in (stray, plain):
            for event in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd", "Interrupt"):
                with self.subTest(directory=directory.name, event=event):
                    self.silent(run(directory, event, turn_id="stray-turn"), degraded=True)
        self.assertEqual(before, snapshot(self.fixture.base))
        self.assertFalse((stray / ".relay").exists())

    def test_unborn_checkout_records_lifecycle_without_inventing_commit(self):
        unborn = self.fixture.base / "unborn-project"
        subprocess.run(["/usr/bin/git", "-c", "init.defaultBranch=unborn-fixture",
                        "init", "-q", str(unborn)], check=True)
        self.assertTrue(self.fixture.success("init", repo=unborn)["initialized"])
        self.context(self.hook("claude", "SessionStart", "unborn-session", repo=unborn),
                     "claude", "SessionStart", "unborn-session")
        first = self.fixture.success("events", repo=unborn)[0]
        self.assertEqual("session.started", first["kind"])
        self.assertEqual({"branch": "unborn-fixture", "client": "claude",
                          "worktree": "primary"}, first["meta"])

        subprocess.run(["/usr/bin/git", "-C", str(unborn),
                        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                        "-c", "user.name=Fixture", "-c", "user.email=fixture",
                        "commit", "--allow-empty", "-q", "-F", "-"],
                       input="First fixture commit\n", text=True, check=True)
        commit = subprocess.check_output(["/usr/bin/git", "-C", str(unborn),
                                          "rev-parse", "HEAD"], text=True).strip()
        self.context(self.hook("claude", "SessionStart", "committed-session", repo=unborn),
                     "claude", "SessionStart", "committed-session")
        events = self.fixture.success("events", repo=unborn)
        self.assertEqual(2, len(events))
        self.assertNotIn("commit", events[0]["meta"])
        self.assertEqual(commit, events[1]["meta"]["commit"])

    def test_noncommit_detached_head_does_not_record_lifecycle(self):
        self.fixture.initialize()
        tree = subprocess.check_output(["/usr/bin/git", "-C", str(self.fixture.repo),
                                        "rev-parse", "HEAD^{tree}"], text=True).strip()
        (self.fixture.repo / ".git" / "HEAD").write_text(tree + "\n")
        result = self.hook("claude", "SessionStart", "noncommit-head")
        self.warned(result, "SessionStart", "the ledger refused or could not complete this step")
        self.assertEqual([], self.rows())

    def test_legacy_resume_after_commit_and_modern_startup_retry(self):
        self.fixture.initialize()
        self.context(self.hook("claude", "SessionStart", "resumed-session"),
                     "claude", "SessionStart", "resumed-session")
        subprocess.run(["/usr/bin/git", "-C", str(self.fixture.repo),
                        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                        "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-q", "-F", "-"],
                       input="Changed fixture commit\n", text=True, check=True)
        context = self.context(self.hook("claude", "SessionStart", "resumed-session",
                                        payload={"source": "resume"}),
                               "claude", "SessionStart", "resumed-session")
        self.assertIn("last_seq=2", context)
        outputs = [self.hook("claude", "SessionStart", "modern-session",
                             payload={"prompt_id": "modern-startup-id"}) for _ in range(2)]
        for result in outputs:
            self.assertIn("last_seq=3", self.context(result, "claude", "SessionStart", "modern-session"))
        self.assertEqual(outputs[0].stdout, outputs[1].stdout)
        self.assertEqual(3, len(self.rows()))

    def test_user_prompt_submit_is_sql_readonly_except_pinned_shm_coordination(self):
        self.fixture.initialize()
        self.fixture.event()
        self.fixture.success("claim", "code:readonly", "--agent", "codex", "--session",
                             "still-owner", "--purpose", "Must not release on prompt")
        events = self.fixture.success("events")
        claims = self.fixture.success("status")["active_claims"]
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                before = snapshot(self.fixture.base)
                result = self.hook(client, "UserPromptSubmit", client + "-prompt",
                                   payload={"prompt": "PRIVATE_PROMPT_SENTINEL", "prompt_id": "prompt-1"})
                self.context(result, client, "UserPromptSubmit", client + "-prompt")
                self.assertNotIn("PRIVATE_PROMPT_SENTINEL", result.stdout + result.stderr)
                self.assert_sql_readonly_snapshot(before)
                self.assertEqual(events, self.fixture.success("events"))
                self.assertEqual(claims, self.fixture.success("status")["active_claims"])
        self.assertEqual(["work.intent", "claim.acquired"], [row[1] for row in self.rows()])

    def test_prompt_worker_denies_actual_database_open_for_write_and_mutating_sql(self):
        self.fixture.initialize()
        self.fixture.event()
        for client in ("codex", "claude"):
            before = snapshot(self.fixture.base)
            result = self.hook(client, "UserPromptSubmit", "readonly-witness", before="""
import sqlite3
from relay_core.store import RelayStore
from relay_core.protocol import StateError
original_brief = RelayStore.brief
def checked_brief(self, *args, **kwargs):
    try:
        fd = os.open(self.paths.database, os.O_WRONLY | os.O_CLOEXEC)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise StateError("readonly worker acquired writable database descriptor")
    try:
        self._db.execute("DELETE FROM events")
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_READONLY:
            raise
    else:
        raise StateError("readonly worker executed mutating SQL")
    return original_brief(self, *args, **kwargs)
RelayStore.brief = checked_brief
""")
            self.context(result, client, "UserPromptSubmit", "readonly-witness")
            self.assert_sql_readonly_snapshot(before)
        self.assertEqual(["work.intent"], [row[1] for row in self.rows()])

    def test_end_events_are_silent_idempotent_and_never_ack_or_release(self):
        self.fixture.initialize()
        claim = self.fixture.success("claim", "code:hook-test", "--agent", "codex", "--session",
                                     "owner", "--purpose", "still owned")["claim"]
        handoff = self.fixture.success("signal", "work.handoff", "--agent", "codex", "--session", "owner",
                                       "--commit", "HEAD", "--work-id", "hook-work", "--target", "claude",
                                       "--summary", "Await explicit review")["event"]
        pending_args = ("channel-pending", "--agent", "claude", "--source-agent", "codex",
                        "--work-id", "hook-work", "--limit", "1")
        pending = self.fixture.success(*pending_args)
        cases = (("codex", "Stop", {"turn_id": "turn-modern"}),
                 ("claude", "Stop", {"prompt_id": "prompt-modern"}),
                 ("codex", "Interrupt", {"turn_id": "turn-interrupted"}),
                 ("codex", "SessionEnd", {"prompt_id": "codex-ending"}),
                 ("claude", "SessionEnd", {"prompt_id": "claude-ending"}))
        for client, event, extra in cases:
            for _ in range(2):
                self.silent(self.hook(client, event, client + "-end", payload={
                    **extra, "stop_hook_active": False, "reason": "other"}))
        before = snapshot(self.fixture.base)
        self.silent(self.hook("claude", "Interrupt", payload={"turn_id": "not-supported"}))
        self.assertEqual(before, snapshot(self.fixture.base))
        self.assertEqual(pending, self.fixture.success(*pending_args))
        self.assertEqual(handoff["seq"], pending["pending"]["seq"])
        self.assertEqual([claim["claim_id"]], [row["claim_id"]
                         for row in self.fixture.success("status")["active_claims"]])
        kinds = [row[1] for row in self.rows()]
        self.assertEqual(2, kinds.count("turn.completed"))
        self.assertEqual(1, kinds.count("turn.interrupted"))
        self.assertEqual(2, kinds.count("session.ended"))
        self.assertNotIn("delivery.acknowledged", kinds)
        self.assertNotIn("claim.released", kinds)
        self.assertNotIn("claim.broken", kinds)

    def test_post_tool_use_prioritizes_exact_session_without_ledger_writes_or_payload_leakage(self):
        self.fixture.initialize()
        marker = "PRIVATE_TOOL_PAYLOAD_SENTINEL"
        for client in ("codex", "claude"):
            for index in range(5):
                self.fixture.success(
                    "signal", "work.handoff", "--agent", "sender", "--session", "author",
                    "--commit", "HEAD", "--target", client, "--work-id", f"generic-{client}-{index}",
                    "--summary", "Generic pending " + "\U0001f3cb" * 280)
            own = self.fixture.success(
                "signal", "work.handoff", "--agent", "sender", "--session", "author",
                "--commit", "HEAD", "--target", client + ":owner", "--work-id", "own-" + client,
                "--summary", "Exact session pending " + client)["event"]
            foreign = self.fixture.success(
                "signal", "work.handoff", "--agent", "sender", "--session", "author",
                "--commit", "HEAD", "--target", client + ":other", "--work-id", "other-" + client,
                "--summary", "OTHER_SESSION_MUST_NOT_APPEAR")["event"]
            before_rows, before_state = self.rows(), snapshot(self.fixture.base)
            result = self.hook(client, "PostToolUse", "owner", payload={
                "turn_id": "working-turn", "tool_use_id": "synthetic-tool",
                "tool_input": {"command": marker * 3000}, "tool_response": marker,
                "prompt": marker, "last_assistant_message": marker,
                "transcript_path": "/synthetic/never-read-transcript",
            })
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual({"hookSpecificOutput"}, set(value))
            output = value["hookSpecificOutput"]
            self.assertEqual({"hookEventName", "additionalContext"}, set(output))
            self.assertEqual("PostToolUse", output["hookEventName"])
            context = output["additionalContext"]
            self.assertTrue(context.startswith("MULTITHREAD PENDING v1\n"))
            self.assertIn(json.dumps({"agent": client, "session": "owner"}), context)
            self.assertIn("pending=6 exact_session=1", context)
            displayed = [int(seq) for seq in re.findall(r"^- seq=(\d+)\b", context, re.M)]
            self.assertEqual(3, len(displayed))
            self.assertEqual(own["seq"], displayed[0])
            self.assertNotIn(foreign["seq"], displayed)
            self.assertNotIn("OTHER_SESSION_MUST_NOT_APPEAR", context)
            self.assertNotIn(marker, result.stdout + result.stderr)
            self.assertNotIn("never-read-transcript", result.stdout + result.stderr)
            self.assertNotIn("MULTITHREAD AGENT CONTRACT", context)
            self.assertLessEqual(len(context.encode("utf-8")), 8192)
            self.assertEqual(before_rows, self.rows())
            self.assert_sql_readonly_snapshot(before_state)
            pending = self.fixture.success("inbox", "--agent", client, "--session", "owner")
            self.assertEqual(6, pending["pending_count"])

    def test_post_tool_use_no_pending_or_only_other_session_returns_empty_object(self):
        self.fixture.initialize()
        self.fixture.success(
            "signal", "work.handoff", "--agent", "sender", "--session", "author",
            "--commit", "HEAD", "--target", "codex:other", "--work-id", "other-session",
            "--summary", "Not for this session")
        before_rows, before_state = self.rows(), snapshot(self.fixture.base)
        for client in ("codex", "claude"):
            result = self.hook(client, "PostToolUse", "owner")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            self.assertEqual({}, json.loads(result.stdout))
        self.assertEqual(before_rows, self.rows())
        self.assert_sql_readonly_snapshot(before_state)

    def test_post_tool_use_explicit_ack_removes_pending_without_hook_consumption(self):
        self.fixture.initialize()
        event = self.fixture.success(
            "signal", "work.handoff", "--agent", "sender", "--session", "author",
            "--commit", "HEAD", "--target", "claude:reader", "--work-id", "explicit-review",
            "--summary", "Await the exact reader")["event"]
        first = self.hook("claude", "PostToolUse", "reader")
        self.assertIn("MULTITHREAD PENDING v1", first.stdout)
        self.assertEqual(["work.handoff"], [row[1] for row in self.rows()])
        self.fixture.success("acknowledge", str(event["seq"]), "--agent", "claude", "--session", "reader")
        before = self.rows()
        final = self.hook("claude", "PostToolUse", "reader")
        self.assertEqual({}, json.loads(final.stdout))
        self.assertEqual(before, self.rows())
        self.assertEqual(["work.handoff", "delivery.acknowledged"], [row[1] for row in before])

    def test_strict_input_and_invalid_sessions_fail_open_without_mutation(self):
        self.fixture.initialize()
        payloads = ["{", "[]", "null", '{"hook_event_name":"SessionStart","session_id":"ok","session_id":"bad"}',
                    '{"hook_event_name":"SessionStart","session_id":"ok","extra":NaN}',
                    '{"hook_event_name":"SessionStart"}',
                    json.dumps({"hook_event_name": "SessionStart", "session_id": "ok", "extra": "x" * (256 * 1024)})]
        payloads.extend(json.dumps({"hook_event_name": event, "session_id": value})
                        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse")
                        for value in (None, 7, [], "", "bad\nPRIVATE_INPUT_SENTINEL", "x" * 201))
        for raw in payloads:
            with self.subTest(length=len(raw), prefix=raw[:55]):
                before = snapshot(self.fixture.base)
                result = self.hook("claude", raw=raw)
                # A context event named by strictly readable input carries the
                # warning; input the hook cannot read names no event to answer.
                event = strict_event(raw)
                if event in ("SessionStart", "UserPromptSubmit"):
                    self.warned(result, event, "the provider's hook input could not be used")
                else:
                    self.silent(result, degraded=True)
                self.assertNotIn("PRIVATE_INPUT_SENTINEL", result.stdout + result.stderr)
                self.assertLess(len(result.stderr), 512)
                self.assertEqual(before, snapshot(self.fixture.base))
        self.assertEqual([], self.rows())

    def test_unenrolled_and_unknown_events_do_not_create_state_or_config(self):
        before = snapshot(self.fixture.base)
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            self.silent(self.hook("codex", event), degraded=True)
        self.silent(self.hook("claude", "UnknownFixtureEvent"))
        self.silent(self.hook("claude", "Interrupt", payload={"turn_id": "ignored"}))
        self.assertEqual(before, snapshot(self.fixture.base))
        self.assertFalse(self.fixture.state.exists())
        self.assertFalse(self.fixture.registry.exists())

    def test_missing_ledger_object_refuses_without_recreation(self):
        self.fixture.initialize()
        original = self.fixture.state / "relay.sqlite3-wal"
        held = self.fixture.base / "held-wal"
        original.rename(held)
        before = snapshot(self.fixture.base)
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            result = self.hook("claude", event)
            if event == "Stop":
                self.silent(result, degraded=True)
            else:
                self.warned(result, event, "the ledger refused or could not complete this step")
            self.assertFalse(original.exists())
            self.assertEqual(before, snapshot(self.fixture.base))

    def test_controller_custody_failure_withholds_actual_worker_context(self):
        self.fixture.initialize()
        held = self.fixture.state.with_name("held-hook-state")
        result = self.hook("codex", "SessionStart", "custody-session", before=f"""
def replace(stage):
    if stage == "worker-exited":
        state = pathlib.Path({str(self.fixture.state)!r})
        state.rename(pathlib.Path({str(held)!r}))
        state.mkdir(mode=0o700)
cli._checkpoint = replace
""")
        # The withheld context never appears; the warning replaces it.
        self.warned(result, "SessionStart", "the ledger refused or could not complete this step")
        self.assertEqual([], list(self.fixture.state.iterdir()))
        self.assertEqual([(1, "session.started", "codex", "custody-session")], self.rows(held))

    def test_brief_failure_after_startup_keeps_event_but_emits_no_false_context(self):
        self.fixture.initialize()
        result = self.hook("claude", "SessionStart", "brief-failed-session", before="""
from relay_core.store import RelayStore
from relay_core.protocol import StateError
def fail_brief(*args, **kwargs):
    raise StateError("PRIVATE_FAILURE_SENTINEL")
RelayStore.brief = fail_brief
""")
        self.warned(result, "SessionStart", "the ledger refused or could not complete this step")
        self.assertNotIn("PRIVATE_FAILURE_SENTINEL", result.stdout + result.stderr)
        self.assertEqual([(1, "session.started", "claude", "brief-failed-session")], self.rows())

    def test_full_context_including_contract_is_utf8_byte_bounded(self):
        self.fixture.initialize()
        pending_seqs = []
        for index in range(5):
            self.fixture.success("claim", "code:bounded-" + str(index), "--agent", "codex",
                                 "--session", "owner", "--purpose", "\U0001f3cb" * 250)
            self.fixture.success("signal", "work.intent", "--agent", "codex", "--session", "owner",
                                 "--work-id", "bounded-" + str(index), "--summary", "\U0001f3cb" * 300)
            handoff = self.fixture.success(
                "signal", "work.handoff", "--agent", "codex", "--session", "owner", "--commit", "HEAD",
                "--target", "claude", "--work-id", "bounded-" + str(index),
                "--summary", "\U0001f3cb" * 300)
            pending_seqs.append(handoff["event"]["seq"])
            self.fixture.success("friction", "bounded-friction-" + str(index),
                                 "--agent", "codex", "--session", "owner",
                                 "--category", "context", "--summary", "\U0001f3cb" * 300)
        context = self.context(self.hook("claude", "SessionStart", "bounded-session"),
                               "claude", "SessionStart", "bounded-session")
        self.assertIn("\U0001f3cb", context)
        brief = "MULTITHREAD BRIEF v1" + context.split("MULTITHREAD BRIEF v1", 1)[1]
        self.assertLessEqual(len(brief.encode("utf-8")), 4096)
        headings = (
            "Active claims:", "Recent work intents (newest first):",
            "Pending signals (exact session newest first, then generic/broadcast oldest first; acknowledge after reading):",
            "Actionable ratchet items:",
        )
        for index, heading in enumerate(headings):
            self.assertIn(heading, brief)
            section = brief.split(heading + "\n", 1)[1]
            if index + 1 < len(headings):
                section = section.split(headings[index + 1], 1)[0]
            self.assertNotIn("- none", section)
            self.assertIn("omitted by byte limit", section)
            if index == 2:
                displayed = [int(seq) for seq in re.findall(r"^- seq=(\d+)\b", section, re.M)]
                self.assertTrue(displayed, section)
                self.assertEqual(pending_seqs[:len(displayed)], displayed)
                self.assertLess(len(displayed), len(pending_seqs))
                self.assertRegex(section, rf"first omitted seq={pending_seqs[len(displayed)]}\b")
        rows = self.rows()
        self.assertEqual(21, len(rows))
        self.context(self.hook("claude", "UserPromptSubmit", "bounded-session"),
                     "claude", "UserPromptSubmit", "bounded-session")
        self.assertEqual(rows, self.rows())


class PendingReminderCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="relay-reminder-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.args = SimpleNamespace(repo="/synthetic/project", client="codex",
                                    provider_payload={"session_id": "exact-session"})
        self.body = json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "MULTITHREAD PENDING v1\nSYNTHETIC_PENDING_CONTENT seq=1",
        }})
        self.directory = self.root / ("multithread-reminders-" + str(os.getuid()))

    def remind(self, body=None, now=100):
        return runtimecli._reminder_output(self.body if body is None else body, self.args,
                                           cache_root=self.root, now=now)

    def test_identical_notice_is_suppressed_briefly_but_changed_notice_and_session_bypass(self):
        self.assertEqual(self.body, self.remind())
        self.assertEqual("{}", self.remind(now=109.9))
        self.assertEqual(self.body, self.remind(now=110))
        changed = self.body.replace("seq=1", "seq=2")
        self.assertEqual(changed, self.remind(changed, now=111))
        self.assertEqual("{}", self.remind(changed, now=112))
        self.args.provider_payload["session_id"] = "other-session"
        self.assertEqual(changed, self.remind(changed, now=112))
        self.assertEqual(changed, self.remind(changed, now=100), "clock reversal must not hide work")
        entries = list(self.directory.glob("*.json"))
        self.assertEqual(2, len(entries))
        for entry in entries:
            self.assertEqual(0o600, stat.S_IMODE(entry.stat().st_mode))
            value = json.loads(entry.read_text())
            self.assertEqual({"at", "fingerprint"}, set(value))
            self.assertRegex(value["fingerprint"], r"^[a-f0-9]{64}$")
            self.assertNotIn("SYNTHETIC_PENDING_CONTENT", entry.read_text())

    def test_malformed_cache_allows_notice_and_non_pending_outputs_never_create_cache(self):
        for body in ("{}", "null", "[]", "{broken", json.dumps({"hookSpecificOutput": {
                "additionalContext": "MULTITHREAD WARNING: unknown"}})):
            self.assertEqual(body, self.remind(body))
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.body, self.remind())
        cache = next(self.directory.glob("*.json"))
        for malformed in ("null", "[]", "7", "false", "{broken"):
            cache.write_text(malformed)
            self.assertEqual(self.body, self.remind(now=101))
            self.assertIsInstance(json.loads(cache.read_text()), dict)

    def test_unsafe_or_contended_cache_repeats_notice_without_following_links(self):
        self.assertEqual(self.body, self.remind())
        lock = self.directory / ".lock"
        lock.chmod(0o644)
        before = snapshot(self.directory)
        self.assertEqual(self.body, self.remind(now=101))
        self.assertEqual(before, snapshot(self.directory))
        lock.chmod(0o600)
        with lock.open("r+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = snapshot(self.directory)
            self.assertEqual(self.body, self.remind(now=101))
            self.assertEqual(before, snapshot(self.directory))
        cache = next(self.directory.glob("*.json"))
        cache.unlink()
        protected = self.root / "protected"
        protected.write_text("PRIVATE_SYNTHETIC_VALUE")
        cache.symlink_to(protected)
        before = snapshot(self.root)
        self.assertEqual(self.body, self.remind(now=101))
        self.assertEqual(before, snapshot(self.root))

    def test_cache_growth_bound_repeats_notice_without_deleting_existing_receipts(self):
        self.directory.mkdir(mode=0o700)
        for index in range(127):
            (self.directory / (str(index) + ".json")).write_text("retained synthetic cache")
        (self.directory / ".lock").touch(mode=0o600)
        before = snapshot(self.directory)
        self.assertEqual(self.body, self.remind())
        self.assertEqual(before, snapshot(self.directory))
