"""Installed provider-hook acceptance; no provider process or real account config."""

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import unittest

import test_installed as installed


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
               "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
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

        def run(cwd, event, **fields):
            # Payload paths name the other checkout; they must never select it.
            payload = {"cwd": str(beta if cwd != beta else alpha), "transcript_path": str(beta), **fields}
            return self.hook("codex", event, "moving-session", payload=payload, cwd=cwd)

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
            for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt"):
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
        self.silent(result, degraded=True)
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

    def test_strict_input_and_invalid_sessions_fail_open_without_mutation(self):
        self.fixture.initialize()
        payloads = ["{", "[]", "null", '{"hook_event_name":"SessionStart","session_id":"ok","session_id":"bad"}',
                    '{"hook_event_name":"SessionStart","session_id":"ok","extra":NaN}',
                    '{"hook_event_name":"SessionStart"}',
                    json.dumps({"hook_event_name": "SessionStart", "session_id": "ok", "extra": "x" * (256 * 1024)})]
        payloads.extend(json.dumps({"hook_event_name": event, "session_id": value})
                        for event in ("SessionStart", "UserPromptSubmit")
                        for value in (None, 7, [], "", "bad\nPRIVATE_INPUT_SENTINEL", "x" * 201))
        for raw in payloads:
            with self.subTest(length=len(raw), prefix=raw[:55]):
                before = snapshot(self.fixture.base)
                result = self.hook("claude", raw=raw)
                self.silent(result, degraded=True)
                self.assertNotIn("PRIVATE_INPUT_SENTINEL", result.stderr)
                self.assertLess(len(result.stderr), 512)
                self.assertEqual(before, snapshot(self.fixture.base))
        self.assertEqual([], self.rows())

    def test_unenrolled_and_unknown_events_do_not_create_state_or_config(self):
        before = snapshot(self.fixture.base)
        for event in ("SessionStart", "UserPromptSubmit"):
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
            self.silent(self.hook("claude", event), degraded=True)
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
        self.silent(result, degraded=True)
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
        self.silent(result, degraded=True)
        self.assertNotIn("PRIVATE_FAILURE_SENTINEL", result.stderr)
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
            "Pending targeted/broadcast signals (oldest first; acknowledge after reading):",
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
