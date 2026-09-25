"""Public installer proof in a disposable OS-account filesystem view.

No runtime-root injection, host account changes or production configuration.
Includes source-absent public ledger commands using actual OS account defaults
and the genuine null device with a namespace-mapped owner.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
BWRAP = Path("/usr/bin/bwrap")


_COMMON = r"""
import hashlib, json, os, pathlib, pwd, stat, subprocess, sys
home = pathlib.Path("/home/relay-fixture")
foreign = pathlib.Path("/tmp/foreign-home")
project = pathlib.Path("/tmp/project")
installation = home / ".local/share/relay/installation"
launcher = home / ".local/bin/multithread"
compatibility_launcher = home / ".local/bin/relay"
assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
assert os.getuid() == os.geteuid() == os.getgid() == os.getegid() == 1000
assert pwd.getpwuid(os.getuid()).pw_dir == str(home)
assert os.environ["HOME"] == str(foreign)
assert os.environ["PYTHONPATH"] == str(project)
assert os.readlink("/proc/self/ns/net") != sys.argv[1]
interfaces = [row.split(":")[0].strip() for row in pathlib.Path("/proc/net/dev").read_text().splitlines()[2:]]
assert interfaces == ["lo"], interfaces
assert all(os.statvfs(path).f_flag & os.ST_RDONLY for path in ("/", "/usr", "/etc", "/proc", "/dev"))
assert not os.statvfs(home).f_flag & os.ST_RDONLY
assert not os.statvfs("/tmp").f_flag & os.ST_RDONLY

def snapshot(root):
    root_info = root.stat()
    result = {".": [root_info.st_mode, root_info.st_size, root_info.st_mtime_ns, None]}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = (os.readlink(path) if stat.S_ISLNK(info.st_mode)
                   else hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = [info.st_mode, info.st_size, info.st_mtime_ns, content]
    return result

def call(arguments):
    result = subprocess.run(arguments, cwd=project, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, (arguments, result.returncode, result.stdout, result.stderr)
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)
"""


_INSTALL = _COMMON + r"""
assert os.statvfs("/source").f_flag & os.ST_RDONLY
assert os.statvfs("/bundle").f_flag & os.ST_RDONLY
before_home = snapshot(home)
before_foreign = snapshot(foreign)
before_project = snapshot(project)
assert set(before_home) == {"."}, before_home
release_id = sys.argv[2]
public = ["/usr/bin/python3", "-I", "-S", "-B", "/source/src/relay_bootstrap.py"]
plan = call(public + ["plan", "--release", "/bundle", "--approve-sha256", release_id])
assert plan["expected_activation"] is None, plan
assert plan["launcher"] == str(launcher), plan
assert plan["writes"] == [str(installation), str(compatibility_launcher), str(launcher)], plan
assert not plan["enrolls_projects"] and not plan["changes_hooks"] and not plan["network_access"], plan
assert snapshot(home) == before_home, "plan created account state"
assert snapshot(foreign) == before_foreign, "plan wrote ambient HOME"
assert snapshot(project) == before_project, "plan touched target Git code or hooks"
installed = call(public + ["install", "--release", "/bundle", "--approve-sha256", release_id,
                          "--expected-activation", "none"])
assert installed["installed"], installed
assert installed["activation"]["release_id"] == release_id, installed
assert not installed["enrolls_projects"] and not installed["changes_hooks"] and not installed["network_access"]
assert launcher.is_symlink()
assert compatibility_launcher.is_symlink() and os.readlink(launcher) == str(compatibility_launcher)
agents = call([str(launcher), "agent", "muse", "list"])
assert agents == {"kind": "muse", "agents": []}, agents
assert not (home / ".local/share/relay/agents.json").exists()
assert not (home / ".local/share/relay/enrollments").exists()
assert not (project / ".relay").exists()
assert snapshot(foreign) == before_foreign, "installation wrote ambient HOME"
assert snapshot(project) == before_project, "installation touched target Git code or hooks"
assert {p.name for p in (home / ".local/share/relay").iterdir()} == {"installation"}
assert {p.name for p in (home / ".local/bin").iterdir()} == {"multithread", "relay"}
record = json.loads((installation / "releases" / release_id / "release.json").read_text())
body = (installation / "releases" / release_id / "bootstrap.py").read_bytes()
assert hashlib.sha256(body).hexdigest() == record["bootstrap"]["sha256"]
assert body == pathlib.Path("/bundle/bootstrap.py").read_bytes()
launch_body = launcher.read_text()
assert "exec /usr/bin/python3 -I -S -B -c " in launch_body
assert release_id in launch_body and record["bootstrap"]["sha256"] in launch_body
print(json.dumps({"plan": plan, "installed": installed, "network_interfaces": interfaces,
                  "account_home": str(home), "bootstrap_sha256": record["bootstrap"]["sha256"]}))
"""


_AGENT = _COMMON + r"""
assert not pathlib.Path("/source").exists()
assert not pathlib.Path("/bundle").exists()
client = pathlib.Path("/tmp/muse-client.py")
calls = pathlib.Path("/tmp/muse-client-calls")
client.write_text('import json, pathlib, sys\n'
                  'pathlib.Path("/tmp/muse-client-calls").write_text("called")\n'
                  'print(json.dumps({"repo": sys.argv[2], "action": sys.argv[3]}))\n')
registry = home / ".local/share/relay/agents.json"
dry = call([str(launcher), "agent", "muse", "register", "Buddy", "--client", str(client), "--dry-run"])
assert dry["registered"] is False and not registry.exists(), dry
registered = call([str(launcher), "agent", "muse", "register", "Buddy", "--client", str(client)])
assert registered["registered"] and registered["changed"], registered
assert registry.stat().st_mode & 0o777 == 0o600
assert call([str(launcher), "agent", "muse", "list"])["agents"] == ["Buddy"]
assert call([str(launcher), "agent", "muse", "inspect", "buddy"])["sha256"] == registered["sha256"]
project_result = call([str(launcher), "agent", "muse", "project", "BUDDY"])
assert project_result["result"] == {"repo": str(project), "action": "project"}, project_result
global_repo = call([str(launcher), "--repo", str(project), "agent", "muse", "project", "Buddy"])
assert global_repo["result"] == project_result["result"], global_repo
sent = subprocess.run([str(launcher), "agent", "muse", "send", "Buddy", "/tmp/synthetic-packet"],
                      cwd=project, text=True, capture_output=True, timeout=15)
assert sent.returncode == 1 and json.loads(sent.stdout)["state"] == "uncertain", sent
assert calls.read_text() == "called"
calls.unlink()
client.write_text(client.read_text() + "# changed\n")
refused = subprocess.run([str(launcher), "agent", "muse", "project", "Buddy"], cwd=project,
                         text=True, capture_output=True, timeout=15)
assert refused.returncode == 1 and "adapter bytes changed" in refused.stderr, refused
assert not calls.exists(), "changed adapter executed"
print(json.dumps({"nickname": registered["nickname"], "registry_mode": oct(registry.stat().st_mode & 0o777),
                  "project_routed": True, "send_uncertain": True, "changed_client_refused": True}))
"""


_LAUNCH = _COMMON + r"""
assert not pathlib.Path("/source").exists(), "checkout accidentally available to installed launcher"
assert not pathlib.Path("/bundle").exists(), "build bundle accidentally available to installed launcher"
before_home = snapshot(home)
before_foreign = snapshot(foreign)
before_project = snapshot(project)
status = call([str(launcher), "runtime", "status"])
assert status["installed"], status
assert status["activation"]["release_id"] == sys.argv[2], status
assert status["launcher"] == str(launcher), status
assert status["preferred_command_available"] is True
assert call([str(compatibility_launcher), "runtime", "status"]) == status
assert not status["enrollment_changed"] and not status["hooks_changed"], status
assert snapshot(home) == before_home, "runtime status changed installation"
assert snapshot(foreign) == before_foreign, "launcher used poisoned HOME/imports"
assert snapshot(project) == before_project, "launcher executed target Git code"
assert not (home / ".local/share/relay/enrollments").exists()
print(json.dumps(status))
"""


_TAMPER = _COMMON + r"""
assert not pathlib.Path("/source").exists()
assert not pathlib.Path("/bundle").exists()
before_home = snapshot(home)
before_foreign = snapshot(foreign)
before_project = snapshot(project)
result = subprocess.run([str(launcher), "runtime", "status"], cwd=project,
                        text=True, capture_output=True, timeout=15)
assert result.returncode != 0, result
assert result.stdout == "", result.stdout
assert "installed bootstrap failed release verification" in result.stderr, result.stderr
assert snapshot(home) == before_home, "refusal modified installation"
assert snapshot(foreign) == before_foreign, "unverified bootstrap executed"
assert snapshot(project) == before_project, "refusal touched target Git code"
print(json.dumps({"returncode": result.returncode, "stderr": result.stderr}))
"""


_DISCOVERY = _COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
before = [snapshot(path) for path in (home, foreign, project)]
status = call([str(launcher), "runtime", "status"])
activation = status["activation"]
assert activation["release_id"] == sys.argv[2], activation
record = json.loads((installation / "releases" / sys.argv[2] / "release.json").read_text())
for entry in (launcher, compatibility_launcher):
    for arguments in (["--version"], ["--help"], ["runtime", "--help"]):
        result = subprocess.run([str(entry), *arguments], text=True, capture_output=True, timeout=15)
        assert result.returncode == 0 and not result.stderr, (arguments, result)
        if arguments == ["--version"]:
            assert result.stdout == "Multithread " + record["version"] + "\n", result.stdout
        else:
            assert "build-release" not in result.stdout, result.stdout
            if arguments == ["--help"]:
                for command in ("--version", "runtime status", "runtime inspect", "runtime --help"):
                    assert "multithread " + command in result.stdout, result.stdout
            else:
                assert "status" in result.stdout and "inspect" in result.stdout, result.stdout
    for arguments in (["--version", "status"], ["runtime", "build-release", "--output", "/tmp/forbidden-build",
                                               "--version", "0.0.0-forbidden"]):
        result = subprocess.run([str(entry), *arguments], text=True, capture_output=True, timeout=15)
        assert result.returncode != 0 and result.stdout == "", (arguments, result)
    assert call([str(entry), "runtime", "status"]) == status
    assert call([str(entry), "runtime", "inspect"])["activation"] == activation
if activation["previous_id"]:
    stale = installation / "launches" / activation["previous_id"] / "relay"
    for arguments in (["--version"], ["--help"]):
        result = subprocess.run([str(stale), *arguments], text=True, capture_output=True, timeout=15)
        assert result.returncode != 0 and result.stdout == "", result
        assert "no longer active" in result.stderr, result.stderr
assert [snapshot(path) for path in (home, foreign, project)] == before
assert not (home / ".local/share/relay/enrollments").exists()
assert not (project / ".relay").exists() and not pathlib.Path("/tmp/forbidden-build").exists()
print(json.dumps({"version": record["version"], "activation_id": activation["activation_id"],
                  "stale_launcher_refused": bool(activation["previous_id"])}))
"""


_DISCOVERY_TAMPER = _COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
retained = pathlib.Path(os.readlink(compatibility_launcher))
release = installation / "releases" / sys.argv[2]
targets = [
    (retained.parent / "activation.json", b"{}"),
    (release / "release.json", b"{}"),
    (release / "payload/relay_runtime/cli.py", b"raise AssertionError('unverified payload executed')\n"),
    (release / "bootstrap.py", b"raise AssertionError('unverified bootstrap executed')\n"),
    (compatibility_launcher, "/tmp/unrecognized-command"),
]
for target, replacement in targets:
    symlink = target.is_symlink()
    original = os.readlink(target) if symlink else target.read_bytes()
    if symlink:
        target.unlink()
        target.symlink_to(replacement)
    else:
        target.write_bytes(replacement)
    before = [snapshot(path) for path in (home, foreign, project)]
    for arguments in (["--version"], ["--help"], ["runtime", "--help"]):
        result = subprocess.run([str(retained), *arguments], text=True, capture_output=True, timeout=15)
        assert result.returncode != 0 and result.stdout == "", (target, arguments, result)
        assert "verified launcher unavailable" in result.stderr, result.stderr
        assert "AssertionError" not in result.stderr, result.stderr
    assert [snapshot(path) for path in (home, foreign, project)] == before
    if symlink:
        target.unlink()
        target.symlink_to(original)
    else:
        target.write_bytes(original)
assert not (home / ".local/share/relay/enrollments").exists() and not (project / ".relay").exists()
print(json.dumps({"refused_corruptions": len(targets)}))
"""


_RECOVER_DISABLE = _COMMON + r"""
public = ["/usr/bin/python3", "-I", "-S", "-B", "/source/src/relay_bootstrap.py"]
first = call([str(launcher), "runtime", "status"])["activation"]
second = call(public + ["install", "--release", "/bundle", "--approve-sha256", sys.argv[2],
                       "--expected-activation", first["activation_id"]])["activation"]
assert second["release_id"] != first["release_id"]
# These are synthetic protected artifacts, not enrolled or opened by Relay.
registry = home / ".local/share/relay/enrollments"
registry.mkdir(mode=0o700)
(registry / "preserve-fixture.json").write_text('{"synthetic":"do not change"}')
(project / ".relay").mkdir(mode=0o700)
(project / ".relay/preserve-fixture").write_text("not a real ledger")
before_registry = snapshot(registry)
before_project = snapshot(project)
before_foreign = snapshot(foreign)
damaged = installation / "releases" / second["release_id"] / "bootstrap.py"
damaged.write_text('from pathlib import Path\n'
                  'Path("/tmp/foreign-home/unverified-execution").write_text("bad")\n'
                  'print("forged success")\n')
damaged_bytes = damaged.read_bytes()
refused = subprocess.run([str(launcher), "runtime", "inspect"], text=True, capture_output=True)
assert refused.returncode != 0 and refused.stdout == ""
before_inspect = snapshot(home)
inspection = call(public + ["inspect"])
assert inspection["state"] == "degraded" and inspection["activation"] is None
assert inspection["selector"]["activation_id"] == second["activation_id"]
assert snapshot(home) == before_inspect, "inspection mutated damaged state"
option = next(row for row in inspection["recovery_options"]
              if row["release_id"] == first["release_id"])
assert option["command"] == "recover"
recovered = call(public + ["recover", "--release-sha256", first["release_id"],
                          "--expected-selector", option["expected_selector"]])
active = call([str(launcher), "runtime", "status"])["activation"]
assert active == recovered["activation"]
assert active["release_id"] == first["release_id"]
assert active["activation_id"] not in (first["activation_id"], second["activation_id"])
assert damaged.read_bytes() == damaged_bytes, "recovery repaired unknown code"
old_token = option["expected_selector"]
stale = subprocess.run(public + ["recover", "--release-sha256", first["release_id"],
                                "--expected-selector", old_token], text=True, capture_output=True)
assert stale.returncode != 0 and stale.stdout == "", "stale recovery was accepted"
before_plan = snapshot(home)
plan = call([str(launcher), "runtime", "disable-plan"])
assert plan["can_disable"] and not plan["removes_release_files"]
assert snapshot(home) == before_plan, "disable plan wrote state"
disabled = call([str(launcher), "runtime", "disable",
                 "--expected-selector", plan["expected_selector"]])
assert disabled["disabled"] and not launcher.exists() and launcher.is_symlink()
assert not compatibility_launcher.exists() and not compatibility_launcher.is_symlink()
retained = pathlib.Path(disabled["retained_selector"])
assert retained.is_symlink() and os.readlink(retained) == active["target"]
old_wrapper = subprocess.run([active["target"], "runtime", "status"], text=True, capture_output=True)
assert old_wrapper.returncode != 0 and old_wrapper.stdout == "", "inactive wrapper ran"
inactive = call(public + ["inspect"])
assert inactive["selector"] is None and inactive["activation"] is None
# Corrupt retained code keeps the inventory degraded even with no active selector.
assert inactive["state"] == "degraded"
assert str(retained) in [row["path"] for row in inactive["retained_selectors"]]
assert any(row["command"] == "activate" and row["expected_activation"] is None
           and row["release_id"] == first["release_id"] for row in inactive["recovery_options"])
enabled = call(public + ["activate", "--release-sha256", first["release_id"],
                        "--expected-activation", "none"])["activation"]
assert enabled["activation_id"] != active["activation_id"]
assert call([str(launcher), "runtime", "status"])["activation"] == enabled
assert snapshot(registry) == before_registry, "management changed enrollment data"
assert snapshot(project) == before_project, "management changed ledger, source or Git hooks"
assert snapshot(foreign) == before_foreign, "management executed poisoned code or used ambient home"
assert damaged.read_bytes() == damaged_bytes, "disable/re-enable changed retained damage"
print(json.dumps({"recovered": True, "disabled": True, "reenabled": True,
                  "stale_recovery_refused": True, "protected_objects_unchanged": True}))
"""


_LEDGER = _COMMON + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
null_info = os.stat("/dev/null", follow_symlinks=False)
assert stat.S_ISCHR(null_info.st_mode) and null_info.st_rdev == os.makedev(1, 3)
assert null_info.st_uid == 65534, "fixture must exercise the actual unmapped owner"
before_foreign = snapshot(foreign)
before_hooks = snapshot(project / ".git/hooks")
registry = home / ".local/share/relay/enrollments"
def relay(*arguments, repository=project, success=True, payload=None):
    result = subprocess.run([str(launcher), "--repo", str(repository), "--json", *arguments],
                            cwd=repository, input=payload, text=True, capture_output=True, timeout=20)
    if success:
        assert result.returncode == 0, (arguments, result.returncode, result.stdout, result.stderr)
        assert not result.stderr, result.stderr
        return json.loads(result.stdout) if result.stdout else None
    assert result.returncode != 0 and result.stdout == "", (arguments, result)
    return result

# Unknown workspaces cannot become empty-looking success or acquire claims.
relay("status", success=False)
relay("claim", "code:sample", "--agent", "codex", "--session", "unknown",
      "--purpose", "must refuse", success=False)
assert not registry.exists() and not (project / ".relay").exists()

# One fixed fixture commit supplies an immutable handoff artifact.
git_env = dict(os.environ, GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-F", "-"],
               input="Public installed fixture baseline\n", text=True, env=git_env,
               check=True, capture_output=True)
oid = subprocess.check_output(["/usr/bin/git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
initialized = relay("init")
assert initialized["initialized"]
assert relay("init") == initialized
assert len(list(registry.glob("ledger-*.json"))) == 1
assert {p.name for p in (project / ".relay").iterdir()} == {
    "enrollment.json", "relay.sqlite3", "relay.sqlite3-wal", "relay.sqlite3-shm"}
assert relay("doctor")["ok"]

# Claims conflict, wrong sessions cannot release, and worktrees share identity.
claim = relay("claim", "code:sample", "--agent", "codex", "--session", "implementer",
              "--purpose", "bounded fixture implementation")["claim"]
legacy_base = [str(compatibility_launcher), "--repo", str(project), "--json"]
assert call(legacy_base + ["status"])["active_claims"] == relay("status")["active_claims"]
legacy_conflict = subprocess.run(legacy_base + ["claim", "code:sample", "--agent", "claude",
    "--session", "legacy-contender", "--purpose", "same ownership boundary"],
    cwd=project, text=True, capture_output=True, timeout=20)
assert legacy_conflict.returncode != 0 and legacy_conflict.stdout == ""
relay("claim", "code:sample", "--agent", "claude", "--session", "reviewer",
      "--purpose", "contender", success=False)
relay("release", claim["claim_id"], "--agent", "codex", "--session", "wrong", success=False)
linked = pathlib.Path("/tmp/linked-worktree")
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "core.hooksPath=/dev/null",
                "worktree", "add", "-q", "--detach", str(linked), oid],
               check=True, capture_output=True)
assert relay("doctor", repository=linked)["ok"]
assert relay("init", repository=linked) == initialized
relay("release", claim["claim_id"], "--agent", "codex", "--session", "implementer",
      repository=linked)

# Process-per-command restarts plus exact retry do not duplicate work.
signal_args = ("signal", "work.intent", "--agent", "claude", "--session", "author",
               "--work-id", "public-demo", "--summary", "Review the fixture implementation")
intent = relay(*signal_args)
assert intent["event"]["seq"] == relay(*signal_args, repository=linked)["event"]["seq"]
request = relay("decision", "request", "--agent", "claude", "--session", "author",
                "--decision-id", "fixture-choice", "--work-id", "public-demo",
                "--scope", "code:sample", "--summary", "Choose the fixture implementation",
                "--artifact", "git:" + oid, "--authority-hint", "engineering",
                "--option", "keep", "--option", "change", "--rollout-fence", "active-clients-refreshed")
before_answer = relay("events")
assert not any(row["kind"] == "delivery.acknowledged" for row in before_answer)
answer_args = ("decision", "respond", str(request["event"]["seq"]), "--agent", "codex",
               "--session", "reviewer", "--judgment", "Keep the tested fixture",
               "--resolution", "choice", "--authority-class", "engineering", "--choice", "keep",
               "--rollout-fence", "active-clients-refreshed")
answer = relay(*answer_args)
assert answer["event"]["seq"] == relay(*answer_args, repository=linked)["event"]["seq"]
payload = '{"hook_event_name":"SessionStart","session_id":"public-fixture"}'
for _ in range(2):
    relay("hook", "--client", "codex", payload=payload)
events = relay("events")
kinds = [row["kind"] for row in events]
assert kinds.count("work.intent") == 1
assert kinds.count("claim.acquired") == kinds.count("claim.released") == 1
assert kinds.count("decision.responded") == kinds.count("delivery.acknowledged") == 1
assert kinds.count("session.started") == 1

# A direct read-only SQLite witness confirms the public commands wrote real state.
import sqlite3
db = project / ".relay/relay.sqlite3"
with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as ledger:
    ledger.execute("PRAGMA query_only=ON")
    assert ledger.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(events)
assert relay("doctor")["ok"]
assert snapshot(foreign) == before_foreign and snapshot(project / ".git/hooks") == before_hooks
assert not (linked / ".relay").exists()
print(json.dumps({"public_runtime": True, "source_absent": True, "null_owner": null_info.st_uid,
                  "initialized": True, "linked_worktree_shared": True, "claim_conflict_refused": True,
                  "wrong_holder_refused": True, "exact_retries_idempotent": True,
                  "decision_response_and_ack": True, "lifecycle_once": True,
                  "direct_sqlite_integrity": "ok", "events": len(events), "provider_calls": 0}))
"""


class PublicProfileTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != "linux" or not BWRAP.is_file():
            self.skipTest("public profile requires Linux and /usr/bin/bwrap")
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(prefix="relay-public-profile-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        for name in ("home", "tmp", "etc"):
            (self.base / name).mkdir(mode=0o700)
        self.foreign = self.base / "tmp/foreign-home"
        self.foreign.mkdir(mode=0o700)
        (self.foreign / "preserved").write_text("foreign home must not change\n")
        self.project = self.base / "tmp/project"
        self.project.mkdir(mode=0o700)
        self.env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
            "HOME": str(self.foreign), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
        }
        subprocess.run(["/usr/bin/git", "init", "-q", "--template=", str(self.project)],
                       env=self.env, check=True, capture_output=True, timeout=15)
        canary = ('from pathlib import Path\n'
                  'Path("/tmp/foreign-home/import-executed").write_text("unexpected import")\n'
                  'raise RuntimeError("target code must not execute")\n')
        for name in ("sitecustomize.py", "usercustomize.py", "relay_bootstrap.py",
                     "relay_core.py", "relay_runtime.py"):
            (self.project / name).write_text(canary)
        (self.project / ".git/hooks").mkdir(mode=0o700)
        (self.project / ".git/hooks/pre-commit").write_text("# fixture hook: preserve exactly\n")
        (self.base / "etc/passwd").write_text(
            "relay-fixture:x:1000:1000:Disposable test account:/home/relay-fixture:/bin/sh\n")
        (self.base / "etc/group").write_text("relay-fixture:x:1000:\n")
        (self.base / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files\n")
        self.parent_network = os.readlink("/proc/self/ns/net")
        self.bundle = self.base / "bundle"

    def sandbox(self, script, release_id, *, include_source):
        args = [
            str(BWRAP), "--die-with-parent", "--new-session", "--unshare-user",
            "--uid", "1000", "--gid", "1000", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--hostname", "relay-fixture",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "LC_ALL", "C.UTF-8",
            "--setenv", "HOME", "/tmp/foreign-home",
            "--setenv", "XDG_CONFIG_HOME", "/tmp/foreign-home/config",
            "--setenv", "XDG_DATA_HOME", "/tmp/foreign-home/data",
            "--setenv", "XDG_CACHE_HOME", "/tmp/foreign-home/cache",
            "--setenv", "PYTHONPATH", "/tmp/project",
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", str(self.base / "etc"), "/etc", "--dir", "/home",
            "--bind", str(self.base / "home"), "/home/relay-fixture",
            "--bind", str(self.base / "tmp"), "/tmp",
        ]
        if include_source:
            args += ["--ro-bind", str(ROOT), "/source", "--ro-bind", str(self.bundle), "/bundle"]
        args += [
            "--proc", "/proc", "--dev", "/dev", "--remount-ro", "/proc",
            "--remount-ro", "/dev", "--remount-ro", "/", "--chdir", "/tmp/project",
            "--", "/usr/bin/python3", "-I", "-S", "-B", "-c", script,
            self.parent_network, release_id,
        ]
        result = subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=60)
        unsupported = (
            "No permissions to create new namespace",
            "Creating new namespace failed: Operation not permitted",
            "Creating new namespace failed: Invalid argument",
            "Failed to unshare user namespace",
        )
        if result.returncode and result.stderr.startswith("bwrap:") and any(
                reason in result.stderr for reason in unsupported):
            self.skipTest("bwrap namespaces unavailable: " + result.stderr.strip())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_optional_muse_adapter_in_fresh_installed_account(self):
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(self.bundle), "--version", "0.0.0-muse-fixture"],
            env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release_id = json.loads(built.stdout)["release_id"]
        self.sandbox(_INSTALL, release_id, include_source=True)
        result = self.sandbox(_AGENT, release_id, include_source=False)
        self.assertEqual("Buddy", result["nickname"])
        self.assertEqual("0o600", result["registry_mode"])
        self.assertTrue(result["project_routed"])
        self.assertTrue(result["send_uncertain"])
        self.assertTrue(result["changed_client_refused"])

    def test_public_installed_ledger_in_fresh_rootless_account(self):
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(self.bundle), "--version", "0.0.0-public-ledger"],
            env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        self.sandbox(_INSTALL, release["release_id"], include_source=True)
        result = self.sandbox(_LEDGER, release["release_id"], include_source=False)
        self.assertTrue(result["public_runtime"])
        self.assertEqual(65534, result["null_owner"])
        self.assertEqual("ok", result["direct_sqlite_integrity"])
        self.assertEqual(0, result["provider_calls"])

    def test_public_recovery_disable_and_reenable_preserve_project_and_enrollment(self):
        release_ids = []
        for number in (1, 2):
            self.bundle = self.base / ("recovery-bundle-" + str(number))
            built = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", "-B", str(SOURCE / "relay_bootstrap.py"),
                 "build-release", "--output", str(self.bundle),
                 "--version", "0.0." + str(number) + "-recovery-fixture"],
                env=self.env, text=True, capture_output=True, timeout=20)
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)
            release = json.loads(built.stdout)
            self.assertFalse(release["approved"])
            release_ids.append(release["release_id"])
            if number == 1:
                self.sandbox(_INSTALL, release["release_id"], include_source=True)
        self.assertNotEqual(*release_ids)
        result = self.sandbox(_RECOVER_DISABLE, release_ids[1], include_source=True)
        self.assertEqual({"recovered": True, "disabled": True, "reenabled": True,
                          "stale_recovery_refused": True, "protected_objects_unchanged": True}, result)

    def test_public_install_and_source_absent_launcher_verify_real_account_release(self):
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(self.bundle), "--version", "0.0.0-profile-test"],
            env=self.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        release_id = release["release_id"]
        # Approval is explicit fixture policy, not inferred production provenance.
        self.assertEqual(release_id, hashlib.sha256((self.bundle / "release.json").read_bytes()).hexdigest())
        installed = self.sandbox(_INSTALL, release_id, include_source=True)
        self.assertEqual("/home/relay-fixture", installed["account_home"])
        self.assertEqual(["lo"], installed["network_interfaces"])
        active = self.sandbox(_LAUNCH, release_id, include_source=False)
        self.assertEqual(installed["installed"]["activation"]["activation_id"],
                         active["activation"]["activation_id"])
        self.assertFalse((self.base / "home/.local/share/relay/enrollments").exists())
        self.assertFalse((self.project / ".relay").exists())
        self.assertEqual(["preserved"], sorted(p.name for p in self.foreign.iterdir()))

        # Corrupt only this test's installed bootstrap; the actual executable must
        # reject its bytes before either its code or target-checkout code executes.
        bootstrap = self.base / "home/.local/share/relay/installation/releases" / release_id / "bootstrap.py"
        bootstrap.write_text(
            'from pathlib import Path\n'
            'Path("/tmp/foreign-home/bootstrap-executed").write_text("unverified")\n'
            'print("forged success")\n')
        refused = self.sandbox(_TAMPER, release_id, include_source=False)
        self.assertNotEqual(0, refused["returncode"])
        self.assertEqual(["preserved"], sorted(p.name for p in self.foreign.iterdir()))

    def test_public_offline_discovery_tracks_the_verified_release_and_active_launcher(self):
        previous = None
        for number in (1, 2):
            version = "0.0." + str(number) + "-discovery-fixture"
            self.bundle = self.base / ("discovery-bundle-" + str(number))
            built = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", "-B", str(SOURCE / "relay_bootstrap.py"),
                 "build-release", "--output", str(self.bundle), "--version", version],
                env=self.env, text=True, capture_output=True, timeout=20)
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)
            release_id = json.loads(built.stdout)["release_id"]
            if previous is None:
                self.sandbox(_INSTALL, release_id, include_source=True)
            else:
                switch = _COMMON + r"""
current = call([str(launcher), "runtime", "status"])["activation"]
result = call([str(launcher), "runtime", "install", "--release", "/bundle",
               "--approve-sha256", sys.argv[2], "--expected-activation", current["activation_id"]])
assert result["activation"]["previous_id"] == current["activation_id"], result
print(json.dumps(result))
"""
                self.sandbox(switch, release_id, include_source=True)
            result = self.sandbox(_DISCOVERY, release_id, include_source=False)
            self.assertEqual(version, result["version"])
            self.assertEqual(previous is not None, result["stale_launcher_refused"])
            self.assertNotEqual(previous, result["activation_id"])
            previous = result["activation_id"]
        refused = self.sandbox(_DISCOVERY_TAMPER, release_id, include_source=False)
        self.assertEqual(5, refused["refused_corruptions"])


if __name__ == "__main__":
    unittest.main()
