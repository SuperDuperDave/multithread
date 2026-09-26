"""Source-absent setup observes real shared work; only Codex's hook listing runs."""

import json
import subprocess
import unittest

import test_public_profile as profile


_SETUP = profile._COMMON + r'''
assert not pathlib.Path('/source').exists() and not pathlib.Path('/bundle').exists()
base = [str(launcher), '--repo', str(project), '--json']
before_project = snapshot(project)
before_foreign = snapshot(foreign)
unchecked = subprocess.run(base + ['setup'], text=True, capture_output=True)
assert unchecked.returncode == 1, unchecked.stdout + unchecked.stderr
report = json.loads(unchecked.stdout)
assert report['runtime']['state'] == 'verified' and report['state'] == 'not_ready'
assert snapshot(project) == before_project and snapshot(foreign) == before_foreign
assert not (home / '.local/share/relay/enrollments').exists()

provider = pathlib.Path('/tmp/never-run-provider')
provider.write_text('#!/bin/sh\ntouch /tmp/provider-was-started\nexit 1\n')
provider.chmod(0o700)
# Setup asks Codex only for its hook listing; this fake lists the invocation's
# session-flag hooks as untrusted and records every request it receives.
fake_codex = pathlib.Path('/tmp/fake-codex')
fake_codex.write_text("""#!/usr/bin/python3 -I
import json, sys, tomllib
assert sys.argv[-3:] == ["app-server", "--listen", "stdio://"], sys.argv
hooks = {}
for index, value in enumerate(sys.argv[1:-3], 1):
    if value == "-c":
        hooks.update(tomllib.loads(sys.argv[index + 1])["hooks"])
with open("/tmp/fake-codex-requests", "a") as log:
    for line in sys.stdin:
        message = json.loads(line)
        log.write(message["method"] + "\\n")
        log.flush()
        if message["method"] == "initialize":
            print(json.dumps({"id": message["id"], "result": {}}), flush=True)
        elif message["method"] == "hooks/list":
            listed = [{"eventName": event[0].lower() + event[1:], "command": groups[0]["hooks"][0]["command"],
                       "handlerType": "command", "source": "sessionFlags", "enabled": True,
                       "trustStatus": "untrusted", "timeoutSec": 3, "matcher": None, "async": False}
                      for event, groups in hooks.items()]
            print(json.dumps({"id": message["id"], "result": {"data": [
                {"cwd": message["params"]["cwds"][0], "hooks": listed}]}}), flush=True)
""")
fake_codex.chmod(0o700)
requests = pathlib.Path('/tmp/fake-codex-requests')
flags = ['--codex', str(fake_codex), '--claude', str(provider)]
report = call(base + ['setup', '--apply', *flags])
assert report['state'] == 'ready', report
assert report['repository']['doctor']['state'] == report['repository']['status']['state'] == 'verified'
assert report['providers']['claude']['state'] == 'prepared', report
codex = report['providers']['codex']
assert codex['state'] == 'needs_hook_review', codex
assert set(codex['hook_trust']['events'].values()) == {'untrusted'}, codex
assert report['provider_started'] is True
assert requests.read_text() == 'initialize\ninitialized\nhooks/list\n', requests.read_text()
assert not pathlib.Path('/tmp/provider-was-started').exists()
hook_commands = {codex['plan']['relay_plan']['hook_command']}
git = ['/usr/bin/git', '-C', str(project), '-c', 'core.hooksPath=/dev/null',
       '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
       '-c', 'commit.gpgsign=false']
subprocess.run(git + ['commit', '--allow-empty', '-qm', 'Synthetic setup witness'], check=True)
linked = pathlib.Path('/tmp/linked')
subprocess.run(git + ['worktree', 'add', '-qb', 'setup-witness', str(linked)], check=True)
claim = call(base + ['claim', 'code:setup', '--agent', 'codex', '--session', 'setup-owner', '--purpose', 'Preserve ownership'])['claim']
handoff = call(base + ['signal', 'work.handoff', '--agent', 'codex', '--session', 'setup-owner',
    '--target', 'claude', '--work-id', 'setup-witness', '--commit', 'HEAD', '--summary', 'Preserve pending review'])['event']
before_events = call(base + ['events'])
before_status = call(base + ['status'])
before_registry = snapshot(home / '.local/share/relay/enrollments')
before_hooks = snapshot(project / '.git/hooks')
activation = call([str(launcher), 'runtime', 'status'])['activation']
for checkout in (project, linked, project):
    checked = call([str(launcher), 'setup', '--repo', str(checkout), '--apply', '--json', *flags])
    assert checked['state'] == 'ready', checked
    assert checked['repository']['identity']['git_common_dir'] == str(project / '.git')
    assert checked['providers']['codex']['state'] == 'needs_hook_review', checked
    hook_commands.add(checked['providers']['codex']['plan']['relay_plan']['hook_command'])
# One reviewed Codex hook command serves the main checkout and its worktree.
assert hook_commands == {str(launcher) + ' provider-hook --client codex'}, hook_commands
assert requests.read_text() == 'initialize\ninitialized\nhooks/list\n' * 4, requests.read_text()
assert call(base + ['events']) == before_events
assert call(base + ['status']) == before_status
assert snapshot(home / '.local/share/relay/enrollments') == before_registry
assert snapshot(project / '.git/hooks') == before_hooks
assert snapshot(foreign) == before_foreign
assert call([str(launcher), 'runtime', 'status'])['activation'] == activation
assert not pathlib.Path('/tmp/provider-was-started').exists()
print(json.dumps({'source_absent': True, 'shared_work_preserved': True,
                  'codex_hook_listings': 4, 'other_providers_started': 0,
                  'linked_worktree_verified': True}))
'''


_FOREIGN_ALIAS_HELPERS = profile._COMMON + r'''
assert not pathlib.Path('/source').exists() and not pathlib.Path('/bundle').exists()
base = [str(compatibility_launcher), '--repo', str(project), '--json']
call(base + ['init'])
git = ['/usr/bin/git', '-C', str(project), '-c', 'core.hooksPath=/dev/null',
       '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',  # synthetic alias-refusal identity
       '-c', 'commit.gpgsign=false']
subprocess.run(git + ['commit', '--allow-empty', '-qm', 'Synthetic alias refusal witness'], check=True)
call(base + ['claim', 'code:alias', '--agent', 'codex', '--session', 'alias-owner',
             '--purpose', 'Preserve ownership'])
call(base + ['signal', 'work.handoff', '--agent', 'codex', '--session', 'alias-owner',
             '--target', 'claude', '--work-id', 'alias-witness', '--commit', 'HEAD',
             '--summary', 'Preserve pending review'])
before_events = call(base + ['events'])
before_status = call(base + ['status'])
activation = call([str(compatibility_launcher), 'runtime', 'status'])['activation']

provider = pathlib.Path('/tmp/never-run-provider')
provider.write_text('#!/bin/sh\n/usr/bin/touch /tmp/provider-was-started\nexit 1\n')
provider.chmod(0o700)
task = pathlib.Path('/tmp/alias-task.txt')
task.write_text('Read-only fixture task; report any inability to inspect.\n')
assert launcher.is_symlink() and os.readlink(launcher) == str(compatibility_launcher)
launcher.unlink()
launcher.write_text('#!/bin/sh\n/usr/bin/touch /tmp/foreign-command-was-started\nexit 1\n')
launcher.chmod(0o700)
before_home = snapshot(home)
before_project = snapshot(project)
before_foreign = snapshot(foreign)
invocations = [
    ['setup', '--check', '--codex', str(provider), '--claude', str(provider)],
    ['setup', '--apply', '--codex', str(provider), '--claude', str(provider)],
    ['update', '--check'],
    ['launch', 'codex', '--provider', str(provider)],
    ['launch', 'claude', '--provider', str(provider)],
    ['peer', 'codex', '--provider', str(provider), '--task-file', str(task), '--dry-run'],
    ['peer', 'claude', '--provider', str(provider), '--task-file', str(task), '--dry-run'],
]
for arguments in invocations:
    refused = subprocess.run(base + arguments, cwd=project, text=True,
                             capture_output=True, timeout=15)
    assert refused.returncode == 74 and refused.stdout == '', (arguments, refused)
    assert 'preferred command is unverified' in refused.stderr, (arguments, refused.stderr)
    assert 'relay runtime inspect' in refused.stderr, refused.stderr
    assert not pathlib.Path('/tmp/foreign-command-was-started').exists(), arguments
    assert not pathlib.Path('/tmp/provider-was-started').exists(), arguments
    assert snapshot(home) == before_home, (arguments, 'account state changed')
    assert snapshot(project) == before_project, (arguments, 'project state changed')
    assert snapshot(foreign) == before_foreign, (arguments, 'foreign state changed')

inspection = call([str(compatibility_launcher), 'runtime', 'inspect'])
assert inspection['state'] == 'degraded', inspection
assert inspection['command_alias'] is None and inspection['activation'] == activation
assert any(issue['scope'] == 'command-alias' and issue['code'] == 'unverified'
           for issue in inspection['issues']), inspection
assert inspection['writes'] == [] and not inspection['enrollment_changed']
assert snapshot(home) == before_home and snapshot(project) == before_project
assert snapshot(foreign) == before_foreign
assert call(base + ['events']) == before_events
assert call(base + ['status']) == before_status
assert not pathlib.Path('/tmp/foreign-command-was-started').exists()
assert not pathlib.Path('/tmp/provider-was-started').exists()
print(json.dumps({'source_absent': True, 'refused_invocations': len(invocations),
                  'shared_work_preserved': True, 'foreign_commands_started': 0,
                  'providers_started': 0, 'runtime_inspection': 'degraded'}))
'''


class PublicSetupTests(unittest.TestCase):
    def test_source_absent_setup_preserves_shared_work_and_only_lists_codex_hooks(self):
        fixture = profile.PublicProfileTests(methodName='test_public_installed_ledger_in_fresh_rootless_account')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        built = subprocess.run(['/usr/bin/python3', '-I', '-S', '-B', str(profile.SOURCE / 'relay_bootstrap.py'),
            'build-release', '--output', str(fixture.bundle), '--version', '0.2.0-setup-witness'],
            env=fixture.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release_id = json.loads(built.stdout)['release_id']
        fixture.sandbox(profile._INSTALL, release_id, include_source=True)
        result = fixture.sandbox(_SETUP, release_id, include_source=False)
        self.assertEqual({'source_absent': True, 'shared_work_preserved': True,
                          'codex_hook_listings': 4, 'other_providers_started': 0,
                          'linked_worktree_verified': True}, result)

    def test_source_absent_legacy_helpers_refuse_foreign_preferred_command_before_execution(self):
        fixture = profile.PublicProfileTests(methodName='test_public_installed_ledger_in_fresh_rootless_account')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        built = subprocess.run(['/usr/bin/python3', '-I', '-S', '-B', str(profile.SOURCE / 'relay_bootstrap.py'),
            'build-release', '--output', str(fixture.bundle), '--version', '0.4.0-alias-refusal-witness'],
            env=fixture.env, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        release_id = json.loads(built.stdout)['release_id']
        fixture.sandbox(profile._INSTALL, release_id, include_source=True)
        result = fixture.sandbox(_FOREIGN_ALIAS_HELPERS, release_id, include_source=False)
        self.assertEqual({'source_absent': True, 'refused_invocations': 7,
                          'shared_work_preserved': True, 'foreign_commands_started': 0,
                          'providers_started': 0, 'runtime_inspection': 'degraded'}, result)


if __name__ == '__main__':
    unittest.main()
