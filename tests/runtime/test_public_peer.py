"""Source-absent installed peer/launch commands with a synthetic native process."""

import json
import subprocess
import unittest

import test_public_profile as profile


_FAKE_PROVIDER = r"""
import argparse, json, os, pathlib, shlex, subprocess, sys
parser = argparse.ArgumentParser()
parser.add_argument("--settings", required=True)
parser.add_argument("--print", action="store_true", required=True)
parser.add_argument("--output-format", choices=("json",), required=True)
parser.add_argument("--permission-prompts", choices=("none",), required=True)
parser.add_argument("--max-turns", type=int, required=True)
parser.add_argument("--session-id", required=True)
args = parser.parse_args()
assert args.max_turns == 3
assert os.getcwd() == "/tmp/project"
assert os.environ["HOME"] == "/tmp/foreign-home"
assert os.environ["PYTHONPATH"] == "/tmp/project"
assert os.environ["XDG_CONFIG_HOME"] == "/tmp/foreign-home/config"
task = sys.stdin.buffer.read()
pathlib.Path("/tmp/fake-native-task.txt").write_bytes(task)
with pathlib.Path("/tmp/fake-native-calls.txt").open("a") as calls:
    calls.write("called\n")
settings = json.loads(args.settings)
assert set(settings) == {"hooks"}
names = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd")
assert set(settings["hooks"]) == set(names)
contexts = {}
for name in names:
    entries = settings["hooks"][name]
    assert len(entries) == 1 and set(entries[0]) == {"hooks"}
    handlers = entries[0]["hooks"]
    assert len(handlers) == 1 and handlers[0]["type"] == "command"
    assert handlers[0]["timeout"] == 3
    command = shlex.split(handlers[0]["command"])
    assert command[-3:] == ["provider-hook", "--client", "claude"]
    payload = {"hook_event_name": name, "session_id": args.session_id,
               "prompt_id": "synthetic-peer-" + name, "turn_id": "synthetic-peer-turn",
               "cwd": "/tmp/foreign-home", "prompt": task.decode("utf-8")}
    completed = subprocess.run(command, input=json.dumps(payload), text=True,
                               capture_output=True, timeout=10)
    assert completed.returncode == 0 and not completed.stderr, completed
    if name in ("SessionStart", "UserPromptSubmit"):
        context = json.loads(completed.stdout)["hookSpecificOutput"]
        assert context["hookEventName"] == name
        assert "MULTITHREAD AGENT CONTRACT v1" in context["additionalContext"]
        assert args.session_id in context["additionalContext"]
        assert "Preserve this pending handoff" in context["additionalContext"]
        contexts[name] = context["additionalContext"]
    elif name == "PostToolUse":
        context = json.loads(completed.stdout)["hookSpecificOutput"]
        assert context["hookEventName"] == name
        assert "MULTITHREAD PENDING v1" in context["additionalContext"]
        assert "Preserve this pending handoff" in context["additionalContext"]
        contexts[name] = context["additionalContext"]
    else:
        assert completed.stdout == ""
pathlib.Path("/tmp/fake-native-receipt.json").write_text(json.dumps({
    "session_id": args.session_id, "contexts": contexts, "argv": sys.argv[1:],
    "inherited_environment": True, "hooks_executed": list(names),
}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": args.session_id, "result": "Useful synthetic peer answer",
                  "permission_denials": [], "terminal_reason": "completed", "num_turns": 1}))
"""


_STREAM_PROVIDER = r'''
import json, os, pathlib, shlex, subprocess, sys, tomllib, uuid
assert os.getcwd() == '/tmp/project'
assert os.environ['HOME'] == '/tmp/foreign-home'
assert not pathlib.Path('/source').exists() and not pathlib.Path('/bundle').exists()
args = sys.argv[1:]
if 'sandbox' in args:
    raise SystemExit(0)  # the read-scope probe, answered as a confining sandbox
client = 'codex' if 'app-server' in args else 'claude'
if client == 'codex':
    hooks = {}
    for i, arg in enumerate(args):
        if arg == '-c': hooks.update(tomllib.loads(args[i+1]).get('hooks', {}))
    session, turn = 'native-codex-fixture', 'native-turn-fixture'
else:
    assert args[args.index('--output-format')+1] == 'stream-json'
    assert args[args.index('--input-format')+1] == 'stream-json'
    assert '--replay-user-messages' in args and '--verbose' in args
    hooks = json.loads(args[args.index('--settings')+1])['hooks']
    session = args[args.index('--session-id')+1]
    turn = 'native-claude-turn-fixture'

def hook(event):
    handler = hooks[event][0]['hooks'][0]
    assert handler['type'] == 'command' and handler['timeout'] == 3
    command = shlex.split(handler['command'])
    assert command[-3:] == ['provider-hook', '--client', client]
    result = subprocess.run(command, input=json.dumps({'hook_event_name': event,
        'session_id': session, 'turn_id': turn, 'prompt_id': client + '-' + event}),
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stderr, result
    if event in ('SessionStart', 'UserPromptSubmit'):
        context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        assert 'MULTITHREAD AGENT CONTRACT v1' in context and session in context

def emit(value): print(json.dumps(value), flush=True)

if client == 'claude':
    task = json.loads(sys.stdin.buffer.readline())
    assert task['session_id'] == session
    hook('SessionStart')
    emit({'type':'system', 'subtype':'init', 'session_id':session, 'cwd':os.getcwd()})
    hook('UserPromptSubmit')
    hook('Stop'); hook('SessionEnd')
    emit({'type':'result', 'subtype':'success', 'is_error':False, 'num_turns':1,
          'session_id':session, 'uuid':str(uuid.uuid4()), 'result':'Installed Claude stream answer',
          'user_message_uuid':task['uuid'], 'user_message_uuids':[task['uuid']]})
else:
    for line in sys.stdin:
        message = json.loads(line)
        method = message['method']
        if method == 'initialized': continue
        if method == 'initialize': result = {}
        elif method == 'hooks/list':
            result = {'data':[{'cwd':os.getcwd(), 'hooks':[
                {'eventName':name[0].lower()+name[1:], 'handlerType':'command',
                 'command':value[0]['hooks'][0]['command'], 'source':'sessionFlags',
                 'trustStatus':'trusted', 'enabled':True, 'timeoutSec':3, 'matcher':None, 'async':False}
                for name,value in hooks.items()]}]}
        elif method == 'thread/start':
            hook('SessionStart')
            result = {'thread':{'id':session, 'cwd':os.getcwd(), 'turns':[]}, 'cwd':os.getcwd()}
        elif method == 'turn/start':
            hook('UserPromptSubmit')
            emit({'id':message['id'], 'result':{'turn':{'id':turn, 'status':'inProgress', 'items':[]}}})
            hook('Stop'); hook('SessionEnd')
            emit({'method':'turn/completed', 'params':{'threadId':session, 'turn':{
                'id':turn, 'status':'completed', 'error':None, 'items':[{'type':'agentMessage',
                'id':'answer', 'phase':'final_answer', 'text':'Installed Codex answer'}]}}})
            continue
        else: raise AssertionError(method)
        emit({'id':message['id'], 'result':result})
'''


_PEER = profile._COMMON + "\nprovider_source = " + repr(_FAKE_PROVIDER) + "\nstream_source = " + repr(_STREAM_PROVIDER) + r"""
assert not pathlib.Path("/source").exists() and not pathlib.Path("/bundle").exists()
launcher = home / ".local/bin/multithread"
base = [str(launcher), "--repo", str(project), "--json"]
release_record = json.loads((installation / "releases" / sys.argv[2] / "release.json").read_text())
runtime_manifest = json.dumps(release_record["runtime"], sort_keys=True,
                              separators=(",", ":"), ensure_ascii=True).encode()
producer_runtime = {"status": "recorded",
                    "runtime_manifest_sha256": hashlib.sha256(runtime_manifest).hexdigest()}
git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_TERMINAL_PROMPT="0",
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
subprocess.run(["/usr/bin/git", "-C", str(project), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-F", "-"],
               input="Public peer fixture baseline\n", text=True, env=git_env,
               check=True, capture_output=True)
call(base + ["init"])
handoff = call(base + ["signal", "work.handoff", "--agent", "codex", "--session", "author-fixture",
                       "--work-id", "peer-proof", "--target", "claude", "--commit", "HEAD",
                       "--summary", "Preserve this pending handoff"])["event"]
claim = call(base + ["claim", "code:peer-fixture", "--agent", "codex", "--session", "author-fixture",
                     "--purpose", "A returned provider result must not release this"])["claim"]
provider = pathlib.Path("/tmp/fake-native")
pathlib.Path("/tmp/fake-native.py").write_text(provider_source)
provider.write_text('#!/bin/sh\nexec /usr/bin/python3 -I -S -B /tmp/fake-native.py "$@"\n')
provider.chmod(0o700)
settings = home / ".config/provider-fixture"
settings.mkdir(parents=True)
(settings / "settings.json").write_text('{"synthetic":"preserve existing settings"}\n')
before_settings = snapshot(settings)
before_foreign = snapshot(foreign)
before_hooks = snapshot(project / ".git/hooks")
before_registry = snapshot(home / ".local/share/relay/enrollments")
before_events = call(base + ["events"])
assert len(before_events) == 2

launch = call(base + ["launch", "claude", "--provider", str(provider)])
assert launch["state"] == "launch_prepared" and launch["provider_started"] is False
assert launch["argv"][0] == str(provider) and launch["repo"] == str(project)
assert launch["relay_plan"]["changes_provider_settings"] is False
assert launch["relay_plan"]["changes_permissions"] is False
assert not pathlib.Path("/tmp/fake-native-calls.txt").exists()
assert call(base + ["events"]) == before_events

task = 'Review this synthetic packet: 雪\r\nLiteral shell text: $(touch /tmp/peer-shell-canary) `false`\n'.encode("utf-8")
task_file = pathlib.Path("/tmp/peer-task.txt")
task_file.write_bytes(task)
evidence = pathlib.Path("/tmp/peer-evidence")
completed = subprocess.run(base + ["peer", "claude", "--provider", str(provider),
                                   "--task-file", str(task_file), "--output-dir", str(evidence),
                                   "--max-turns", "3", "--timeout", "20"],
                           cwd=project, text=True, capture_output=True, timeout=30)
assert completed.returncode == 0, (completed.returncode, completed.stdout, completed.stderr)
peer = json.loads(completed.stdout)
assert peer["state"] == "returned" and peer["provider_started"] is True
assert peer["process_exit_code"] == 0 and peer["needs_attention"] is False
assert peer["result"] == "Useful synthetic peer answer"
assert peer["session_id"] == peer["requested_session_id"]
assert peer["workflow_completion"] == "not_checked"
assert peer["relay_acknowledgement"] == "not_checked"
assert peer["hook_delivery"] == peer["provider_tools"] == "unknown"
assert str(evidence) == peer["evidence_directory"]
assert "multithread peer: session " + peer["session_id"] in completed.stderr
assert pathlib.Path("/tmp/fake-native-task.txt").read_bytes() == task
assert (evidence / "task.txt").read_bytes() == task
assert json.loads((evidence / "result.json").read_text()) == peer
assert peer["producer_runtime"] == producer_runtime
assert json.loads((evidence / "request.json").read_text())["producer_runtime"] == producer_runtime
assert json.loads((evidence / "request.json").read_text())["task_sha256"] == hashlib.sha256(task).hexdigest()
assert pathlib.Path("/tmp/fake-native-calls.txt").read_text() == "called\n"
assert not pathlib.Path("/tmp/peer-shell-canary").exists()
receipt = json.loads(pathlib.Path("/tmp/fake-native-receipt.json").read_text())
assert receipt["session_id"] == peer["session_id"] and receipt["inherited_environment"] is True
assert receipt["hooks_executed"] == ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]
assert set(receipt["contexts"]) == {"SessionStart", "UserPromptSubmit", "PostToolUse"}
assert all(len(context.encode("utf-8")) <= 8192 for context in receipt["contexts"].values())
events = call(base + ["events"])
assert events[:2] == before_events and len(events) == 5, events
assert [row["kind"] for row in events[2:]] == ["session.started", "turn.completed", "session.ended"]
assert all(row["agent"] == "claude" and row["session"] == peer["session_id"] for row in events[2:])
assert "Review this synthetic packet" not in json.dumps(events)
assert not any(row["kind"] in ("delivery.acknowledged", "claim.released", "work.completed") for row in events)
assert any(row["seq"] == handoff["seq"] for row in call(base + ["brief", "--agent", "claude"])["pending_signals"])
assert call(base + ["status"])["active_claims"][0]["claim_id"] == claim["claim_id"]
assert call(base + ["doctor"])["ok"]
assert snapshot(settings) == before_settings and snapshot(foreign) == before_foreign
assert snapshot(project / ".git/hooks") == before_hooks
assert snapshot(home / ".local/share/relay/enrollments") == before_registry
# Reporting needs neither a provider executable nor an enrolled current directory.
# The source and bundle are already absent in this installed-runtime witness.
provider.unlink()
report_before = snapshot(evidence)
report_run = subprocess.run([str(launcher), "peer", "report", "--call-dir", str(evidence), "--json"],
                            cwd="/tmp", capture_output=True, text=True, timeout=15)
assert report_run.returncode == 0 and not report_run.stderr, report_run
report = json.loads(report_run.stdout)
assert report["report_state"] == "reported" and report["receipt_status"] == "available"
assert report["call"]["state"] == "returned" and report["call"]["provider"] == "claude"
assert report["call"]["workflow_completion"] == "not_checked"
serialized = json.dumps(report)
assert peer["session_id"] not in serialized and peer["result"] not in serialized
assert str(evidence) not in serialized and str(project) not in serialized
assert producer_runtime["runtime_manifest_sha256"] not in serialized
assert snapshot(evidence) == report_before
assert pathlib.Path("/tmp/fake-native-calls.txt").read_text() == "called\n"
assert call(base + ["events"]) == events
assert snapshot(settings) == before_settings and snapshot(foreign) == before_foreign
assert snapshot(home / ".local/share/relay/enrollments") == before_registry
assert call(base + ["peer", "report", "--call-dir", str(evidence)]) == report
pathlib.Path('/tmp/fake-bin').mkdir()  # a provider's own directory is readable to its peer; keep it narrow
pathlib.Path('/tmp/fake-bin/fake-stream.py').write_text(stream_source)
stream_provider = pathlib.Path('/tmp/fake-bin/fake-stream')
stream_provider.write_text('#!/bin/sh\nexec /usr/bin/python3 -I -S -B /tmp/fake-bin/fake-stream.py "$@"\n')
stream_provider.chmod(0o700)
for client in ('codex', 'claude'):
    directory = pathlib.Path('/tmp/stream-' + client)
    result = subprocess.run(base + ['peer', client, '--provider', str(stream_provider),
        '--task-file', str(task_file), '--output-dir', str(directory), '--timeout', '20',
        *(['--live-input'] if client == 'claude' else [])],
        cwd=project, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    answer = json.loads(result.stdout)
    assert answer['state'] == 'returned' and answer['needs_attention'] is False, answer
    assert answer['producer_runtime'] == producer_runtime
    assert json.loads((directory / 'request.json').read_text())['producer_runtime'] == producer_runtime
    assert answer['workflow_completion'] == answer['relay_acknowledgement'] == 'not_checked'
    # Bind the report to actual driver output, including its measurement labels.
    support = call(base + ['peer', 'report', '--call-dir', str(directory)])['call']
    assert support['state'] == 'returned' and support['stdout_observation']['scope'] == 'unknown'
    if client == 'claude':
        assert support['provider_measurement_scope'] == 'latest_related_native_result'
        assert support['cost_scope'] == 'cumulative_through_latest_native_result'
        assert support['task_submission'] == 'not_recorded'
    else:
        assert support['task_submission'] == 'accepted'
    target = call([str(launcher), 'peer', 'control', 'status', '--call-dir', str(directory), '--json'])
    assert target['state'] == 'closed' and target['target']['session_id'] == answer['session_id']
    assert target['input_mode'] == ('active_turn' if client == 'codex' else 'session')
    current = call(base + ['events'])
    assert [row['kind'] for row in current[-3:]] == ['session.started','turn.completed','session.ended']
    assert all(row['session'] == answer['session_id'] for row in current[-3:])
    if client == 'codex': assert answer['hook_readiness']['state'] == 'ready'
    else: assert answer['native_input'][0]['consumption'] == 'consumed'
assert len(call(base + ['events'])) == 11
assert call(base + ['status'])['active_claims'][0]['claim_id'] == claim['claim_id']
assert snapshot(settings) == before_settings and snapshot(foreign) == before_foreign
print(json.dumps({"installed_peer": True, "source_absent": True, "launch_plan_only": True,
                  "exact_task_bytes": True, "exact_peer_identity": True,
                  "public_hooks_executed": 12, "lifecycle_events": 9,
                  "pending_handoff_preserved": True, "active_claim_preserved": True,
                  "workflow_completion_inferred": False, "provider_settings_unchanged": True,
                  "fake_provider_calls": 3, "real_provider_calls": 0,
                  "producer_runtime_manifest_recorded": True,
                  "installed_codex_and_claude_streaming": True, "installed_control_status": True}))
"""


class PublicPeerTests(unittest.TestCase):
    def test_source_absent_installed_peer_and_launch(self):
        case = profile.PublicProfileTests("test_public_installed_ledger_in_fresh_rootless_account")
        self.addCleanup(case.doCleanups)
        case.setUp()
        built = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(profile.SOURCE / "relay_bootstrap.py"),
             "build-release", "--output", str(case.bundle), "--version", "0.0.0-public-peer"],
            env=case.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release = json.loads(built.stdout)
        self.assertFalse(release["approved"])
        case.sandbox(profile._INSTALL, release["release_id"], include_source=True)
        result = case.sandbox(_PEER, release["release_id"], include_source=False)
        self.assertEqual({
            "installed_peer": True, "source_absent": True, "launch_plan_only": True,
            "exact_task_bytes": True, "exact_peer_identity": True,
            "public_hooks_executed": 12, "lifecycle_events": 9,
            "pending_handoff_preserved": True, "active_claim_preserved": True,
            "workflow_completion_inferred": False, "provider_settings_unchanged": True,
            "fake_provider_calls": 3, "real_provider_calls": 0,
            "producer_runtime_manifest_recorded": True,
            "installed_codex_and_claude_streaming": True, "installed_control_status": True,
        }, result)
