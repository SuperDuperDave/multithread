# Try the no-account demo

This is a runnable development preview of Multithread's coordination mechanics.
The actors are scripts named `codex` and `claude`, not model sessions.
You do not need provider credentials, a network connection or an existing
Multithread installation. Read the checkout before running its code.

## Run it

From the repository root on an ordinary x86-64 Linux/WSL2 account:

```sh
/usr/bin/python3 -I -S -B examples/no_account_demo.py
```

For a machine-readable evidence summary:

```sh
/usr/bin/python3 -I -S -B examples/no_account_demo.py --json
```

Prerequisites are Python 3.12, Git, bubblewrap at `/usr/bin/bwrap`,
enabled rootless user/mount/network namespaces, and the
[installed runtime profile](engineering/LEDGER-ADMISSION.md):
Landlock ABI3+, procfs and a filesystem with the required birth-time support.
The demonstrated profile is WSL2/Linux on ext4, not native Windows.
The command refuses unsupported isolation; it does not fall back to your real
account or change system settings. Namespace restrictions in CI or another
distribution may require a different supported runner.

The harness creates a temporary OS-account view, two Git worktrees and an
offline installed release. Only its new temporary home and temporary workspace
are writable. The real user home, credentials and project directories are not
mounted; the source bootstrap and bundle are read-only during installation and
absent during the workflow. Each phase has a separate network namespace with
only loopback. It never enables provider hooks or changes your installed Multithread.
Temporary files are removed when the run ends.

## The story

A progress indicator can show negative values or exceed 100%. The fixture
starts with two failing tests out of four. A scripted implementer fixes it and
commits the change.

| Step | What actually happens | What it demonstrates |
|---|---|---|
| Implement | The author holds a resource claim, fixes the function and emits a handoff containing the full Git commit OID | Work has an inspectable, immutable artifact |
| Stop | The producer process exits with code75 after the handoff, before any notification | Restart does not require its conversation or in-memory state |
| Rediscover | A fresh process queries the pending ledger; two more reads simulate duplicate wake handling | A notification/read is not consumption |
| Contend | A second actor's claim and a wrong-session release return conflict73 | Ownership is exact; stopping a process does not expire a claim |
| Review | The scripted original owner releases explicitly; the reviewer claims the resource, checks out the exact OID in its worktree, inspects the change set and runs four tests | The review concerns the committed artifact, not an author's mutable files |
| Consume | The reviewer deliberately acknowledges; a later-session retry returns the same receipt | One handoff leads to one acknowledgement |

An exact handoff retry also returns the existing receipt. Changing its meaning
under the same handoff identity is refused. A separate read-only SQLite
connection checks integrity, all six events, and the ACK's link to the exact
reviewed signal. Both claims are released at the end.

The scripted recovery uses the original holder label deliberately. Labels
are coordination identities, **not authentication** against someone with access
to the same OS account. Neither a wake nor a stored message grants permission
to impersonate a holder or perform an external action.

## Expected result

The terminal shows seven short steps ending in:

```text
PASS: 6 real ledger events; SQLite integrity ok.
Artifact: git:d8c304e092db7313e142bb57d18f7152b847d748
```

The JSON summary includes the exact release and scenario hashes, fixture commit
and file digest, initial/final test results, conflict checks and event counts.
With the same source and demonstrated Git profile, repeated runs produce the
same summary. Internal enrollment IDs, inodes and timestamps vary normally;
this is deterministic behavior/evidence, not a byte-identical SQLite database.

The current result is six events: two claim acquisitions, two releases, one
handoff and one linked acknowledgement. The demo verifies these counts rather
than printing success solely because the last command returned zero.

## What this does not prove

This is an executable explanation of a real installed protocol. It is not a
Codex or Claude integration test, a wake-transport test, autonomous code review,
an authorization system, a power-loss durability test, or permission to publish.
The producer stop and duplicate notifications are controlled fixture scenarios.
The public-profile and crash suites cover additional cases separately.

The [bounded native workflow](PROVIDERS.md#bounded-native-workflow) separately
exercises actual Codex and Claude sessions. This demo remains a scripted,
no-account tour. See [hosted checks](CI.md#hosted-result) for the public-source
run and [v0.1.0](https://github.com/SuperDuperDave/multithread/releases/tag/v0.1.0) for package and
anonymous-onboarding results. The [acceptance matrix](engineering/REQUIREMENTS.md)
keeps these scopes separate.

Implementation: [harness](../examples/no_account_demo.py) and
[scripted scenario](../examples/demo_scenario.py). Do not run the internal
scenario directly against a real project.
