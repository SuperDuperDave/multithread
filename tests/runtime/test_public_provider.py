"""Source-absent public hook commands; synthetic provider inputs, no provider call."""
import json
import subprocess
import unittest

import test_public_profile as profile


_HOOKS = profile._COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
launcher = home / ".local/bin/multithread"
base = [str(launcher), "--repo", str(project), "--json"]
codex_hook = [str(launcher), "provider-hook", "--client", "codex"]

def hook(client, name, **fields):
    payload = {"hook_event_name": name, "session_id": client + "-fixture-session",
               "cwd": str(foreign), "transcript_path": str(foreign / "preserved"),
               "prompt": "SYNTHETIC-PRIVATE-PROMPT", "permission_mode": "bypassPermissions",
               "last_assistant_message": "SYNTHETIC-PRIVATE-RESPONSE", **fields}
    # Codex runs its checkout-free generated command in the session directory.
    command = codex_hook if client == "codex" else base + ["provider-hook", "--client", client]
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True,
                            timeout=15, cwd=project)
    assert result.returncode == 0, result
    assert "SYNTHETIC-PRIVATE" not in result.stdout + result.stderr
    return result

before_project = snapshot(project)
before_home = snapshot(home)
refused = hook("codex", "SessionStart")
assert refused.stdout == "" and refused.stderr
assert snapshot(project) == before_project and snapshot(home) == before_home
assert not (project / ".relay").exists()

git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-F", "-"],
               input="Public provider hook fixture baseline\n", text=True, env=git_env,
               check=True, capture_output=True)
call(base + ["init"])
generated = call(base + ["provider-config", "--client", "codex", "--launcher-name", "multithread"])
import shlex
assert shlex.split(generated["hook_command"]) == codex_hook, generated
handoff = call(base + ["signal", "work.handoff", "--agent", "codex", "--session", "author-fixture",
                       "--work-id", "hook-proof", "--target", "claude", "--commit", "HEAD",
                       "--summary", "Review the fixture artifact; a brief is not an ACK"])["event"]
claim = call(base + ["claim", "code:provider-fixture", "--agent", "codex", "--session", "author-fixture",
                     "--purpose", "A lifecycle stop must not release this"])["claim"]
settings = home / ".config/provider-fixture"
settings.mkdir(parents=True)
(settings / "hooks.json").write_text('{"synthetic":"not Relay owned"}\n')
before_settings = snapshot(settings)
before_foreign = snapshot(foreign)
before_registry = snapshot(home / ".local/share/relay/enrollments")
contexts = []
for client in ("codex", "claude"):
    start = hook(client, "SessionStart", source="startup")
    assert not start.stderr, start.stderr
    output = json.loads(start.stdout)
    assert set(output) == {"hookSpecificOutput"}
    context = output["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert "MULTITHREAD AGENT CONTRACT v1" in context["additionalContext"]
    assert "MULTITHREAD BRIEF v1" in context["additionalContext"]
    assert client + "-fixture-session" in context["additionalContext"]
    events = call(base + ["events"])
    assert events[-1]["kind"] == "session.started" and events[-1]["agent"] == client
    assert "last_seq=" + str(events[-1]["seq"]) in context["additionalContext"]
    if client == "claude":
        assert "Review the fixture artifact" in context["additionalContext"]
    contexts.append(len(context["additionalContext"].encode("utf-8")))
    before_prompt = snapshot(project)
    shm_before = (project / ".relay/relay.sqlite3-shm").stat()
    refreshed = hook(client, "UserPromptSubmit", prompt_id="prompt-fixture")
    assert not refreshed.stderr and json.loads(refreshed.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    after_prompt = snapshot(project)
    # SQLite read locks may update the held SHM coordination object. This is
    # SQL-readonly, not a byte-for-byte no-write promise for every sidecar.
    before_prompt.pop(".relay/relay.sqlite3-shm")
    after_prompt.pop(".relay/relay.sqlite3-shm")
    assert after_prompt == before_prompt, "prompt hook changed objects outside SHM coordination"
    shm_after = (project / ".relay/relay.sqlite3-shm").stat()
    assert (shm_after.st_dev, shm_after.st_ino, shm_after.st_mode, shm_after.st_size) == (shm_before.st_dev, shm_before.st_ino, shm_before.st_mode, shm_before.st_size)
    stopped = hook(client, "Stop", turn_id="turn-fixture", prompt_id="prompt-fixture")
    assert stopped.stdout == stopped.stderr == ""
    repeated = hook(client, "Stop", turn_id="turn-fixture", prompt_id="prompt-fixture")
    assert repeated.stdout == repeated.stderr == ""
    ended = hook(client, "SessionEnd")
    assert ended.stdout == ended.stderr == ""
interrupt = hook("codex", "Interrupt", turn_id="interrupted-fixture")
assert interrupt.stdout == interrupt.stderr == ""
before_ignored = snapshot(project)
ignored = hook("claude", "Interrupt", turn_id="not-a-claude-event")
assert ignored.stdout == ignored.stderr == "" and snapshot(project) == before_ignored
events = call(base + ["events"])
assert len(events) == 9, events
assert "SYNTHETIC-PRIVATE" not in json.dumps(events)
status = call(base + ["status"])
assert len(status["active_claims"]) == 1 and status["active_claims"][0]["claim_id"] == claim["claim_id"]
pending = call(base + ["brief", "--agent", "claude"])["pending_signals"]
assert any(row["seq"] == handoff["seq"] for row in pending)
import sqlite3
with sqlite3.connect((project / ".relay/relay.sqlite3").as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 9
    kinds = dict(ledger.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind"))
    assert kinds.get("delivery.acknowledged", 0) == 0 and kinds.get("claim.released", 0) == 0
assert snapshot(settings) == before_settings and snapshot(foreign) == before_foreign
assert snapshot(home / ".local/share/relay/enrollments") == before_registry
assert max(contexts) <= 8192
print(json.dumps({"public_provider_hook": True, "source_absent": True,
                  "ordered_startup_brief": True, "prompt_refresh_readonly": True,
                  "pending_handoff_preserved": True, "active_claim_preserved": True,
                  "provider_settings_unchanged": True, "sqlite_integrity": "ok",
                  "events": 9, "acknowledgements": 0, "provider_calls": 0}))
"""


class PublicProviderTests(unittest.TestCase):
    def test_source_absent_public_provider_hook_contract(self):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(case.bundle), "--version", "0.0.0-public-provider-hook"],
            env=case.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox(_HOOKS, release["release_id"], include_source=False)
        self.assertEqual({
            "public_provider_hook": True, "source_absent": True,
            "ordered_startup_brief": True, "prompt_refresh_readonly": True,
            "pending_handoff_preserved": True, "active_claim_preserved": True,
            "provider_settings_unchanged": True, "sqlite_integrity": "ok",
            "events": 9, "acknowledgements": 0, "provider_calls": 0,
        }, result)
