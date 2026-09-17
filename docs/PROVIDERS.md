# Provider hooks

Multithread packages [native peer calls](PEER.md), which return Claude's or
Codex's answer to the initiating task, and `multithread launch` for interactive
launches. Start with the [first-collaboration prompt](PEER.md#first-collaboration)
for one scoped read-only review. The manual walkthrough and historical evidence
below retain their separate scope. Commands use the preferred v0.4 launcher;
`relay` remains compatible with the same installation and ledger.

Multithread supplies a coordination contract and pending-work brief to Codex and
Claude through their native hooks. The installed `provider-config` command
generates arguments for one provider launch. A bounded native workflow used
Codex 0.153.4 as author and Claude 2.1.267 as reviewer on Linux/WSL2; its
[scope](#bounded-native-workflow) is separate from the historical startup probes.
Follow the [two-worktree walkthrough](#try-a-review-across-two-worktrees) to try
the integration with your own provider access and reviewed permissions.

Installing Multithread does not install hooks, trust code, change provider permissions,
launch providers, or acknowledge work. Do not add the new handler alongside an
existing Multithread lifecycle/brief handler: duplicate handlers may create duplicate
observations and repeated context. Invocation-only arguments provide opt-in
without persistent provider-setting edits; native review remains separate.

For a first installation, use the [setup guide](SETUP.md). Once the repository
is enrolled, the installed interactive launch command prepares the hooks and
displays the invocation for review before starting the provider:

```sh
~/.local/bin/multithread launch codex --repo /absolute/enrolled/checkout
```

Use `claude` for the other provider. Add `--json` to prepare a launch plan without
starting a provider. No source checkout is needed. Its plan does not
establish native trust, model-visible context or working provider tools.

## Command and scope

A reviewed hook definition must invoke the absolute account-installed launcher:

```text
/ABSOLUTE/ACCOUNT/HOME/.local/bin/multithread --repo /ABSOLUTE/ENROLLED/CHECKOUT provider-hook --client codex
```

Use `claude` for the other client. Shell-quote each actual argument; the paths above
are placeholders, not runnable commands. Enrollment must already exist from an
explicit `init`. Omission of `--repo` uses the real process working directory,
never a path from hook input. An explicit checkout is preferable for setup.

The command accepts one strict JSON object on stdin, bounded to 256 KiB. It
retains only the supported event name and exact session/prompt/turn identifiers.
Prompt text, assistant responses, transcript paths, supplied working-directory
paths, permission modes, tool arguments and credentials are not stored or used
as authority. Lifecycle records additionally contain sanitized Git identity from
the admitted checkout, so do not publish a live ledger as a demonstration asset.

## Lifecycle behavior

| Input event | Ledger action | Provider stdout |
|---|---|---|
| SessionStart | Append sanitized startup, then read a brief in the same handler | One JSON additional-context object |
| UserPromptSubmit | Read a brief; no event append | One JSON additional-context object |
| Stop | Observe response/turn end, not task success | Empty |
| SessionEnd | Observe session end; no release or ACK | Empty |
| Interrupt, Codex only | Observe interrupted turn, requiring turn_id | Empty |
| Other events, including Claude Interrupt | Ignore | Empty |

The context combines a minimal, versioned agent contract and the existing bounded
brief, with an 8 KiB UTF-8 total cap. It includes the exact coordination identity
and installed command argv. Identity labels are not authentication. Ledger fields
remain quoted data, not instructions. Read full events and independently inspect
handoff artifacts before deliberate consumption; decision responses retain their
atomic response/ACK contract. A hook never consumes signals, breaks claims,
changes permissions, or wakes another agent.

The rendered brief has its own 4 KiB UTF-8 cap. It keeps every section visible
when details do not fit, distinguishes omitted items from an empty section,
and identifies the first omitted signal by its exact sequence. Pending signals
receive the first use of the detail budget in each round. Use the same
`brief` query with `--json` for the selected details, then retrieve full events
as needed. Byte omissions and per-section item limits are separate; neither
removes ledger evidence or acknowledges a signal. Human `status` also marks
limited selections instead of presenting displayed counts as complete totals.

Matching provider hooks may run concurrently. Keeping startup and context in one
ordered command prevents the brief from preceding this command's startup record.
A successful observation followed by a failed brief can leave a durable event
with no context returned; the pair is deliberately not one all-or-nothing
transaction. Final custody checks still suppress stale context.

## Read-only and failure semantics

Prompt refresh uses SQLite read-only/query-only access and the existing confined
worker profile. The main database inode is not writable. SQLite can update the
held WAL/SHM coordination objects while reading; this is not a byte-for-byte
no-write guarantee for every file. The tests observed SHM content/mtime changes
with unchanged identity/mode/size, while DB/WAL and every other fixture object
remained unchanged. Separate negative tests attempt database-file writes and
mutating SQL.

Missing enrollment, malformed input, unsupported state or failed observation
returns exit zero with no context and a generic diagnostic. Unknown event names
are silently ignored. Exit zero is provider fail-open behavior, **not** evidence
of a successful ledger operation or an empty inbox. Installation/argument errors,
external termination and provider-enforced timeouts can still fail before this
handler completes. It creates no failure log and never repairs state.

Without a stable prompt identifier, startup/end observations are at-least-once:
each invocation receives a fresh nonce. This permits resume after Git changes
without conflating an old lifecycle record with new metadata. Stop uses Codex
turn_id or supplied prompt_id when available, otherwise the core's at-least-once
fallback. Exact stable-ID retries retain existing conflict checks; reusing an ID
with changed Git metadata can refuse. There is no fabricated exactly-once
lifecycle guarantee.

## Invocation-only opt-in

For ordinary interactive use, use `multithread launch` as shown above and in the
[two-worktree walkthrough](#try-a-review-across-two-worktrees). The advanced
`provider-config` interface below is for scripts that need to handle the native
argument list directly.

After explicitly installing Multithread and enrolling the checkout, generate a plan:

```text
/ABSOLUTE/ACCOUNT/HOME/.local/bin/multithread --repo /ABSOLUTE/ENROLLED/CHECKOUT --json provider-config --client codex --launcher-name multithread
```

Use `claude` for the other client. The JSON result contains the exact
`hook_command`, event list and `native_arguments`. It requires a healthy existing
ledger, reads it through the confined readonly path, and creates no provider
settings, executable, permissions, trust record or provider process.

The low-level schema-1 `provider-config` default keeps the `relay` hook entry so
existing native helpers receive the exact hook arguments they already validate.
Current native helpers and the advanced examples here explicitly select
`--launcher-name multithread`. This option chooses only the installed account's
`multithread` or `relay` entry; it cannot name an arbitrary executable. Use
`multithread` for new scripts and instructions; see the
[command compatibility policy](engineering/INSTALLATION.md#command-compatibility)
for saved hooks and older callers.

Review this output, then supply its `native_arguments` as additional argv entries
to the reviewed provider executable, running in the same checkout. Pass an argv
list through a process API; do not feed JSON to shell `eval`. The generated hook
command already shell-quotes each argument. Codex receives repeated `-c` TOML
overrides; Claude receives `--settings` and an inline JSON string, not a filename.

The following advanced Python example generates the plan, shows it for review,
and launches an interactive provider after you type `launch`. Replace the two
absolute paths with your enrolled checkout and installed provider executable.
Set `client` to `claude` and select its executable for Claude Code. The example
uses your existing provider sign-in and normal permissions.

```python
import json
import os
from pathlib import Path
import pwd
import subprocess

client = "codex"
checkout = Path("/absolute/path/to/enrolled-checkout")
provider = Path("/absolute/path/to/codex")
multithread = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/bin/multithread"

if not checkout.is_absolute() or not provider.is_absolute():
    raise SystemExit("Use absolute checkout and provider paths.")
plan = json.loads(subprocess.check_output(
    [str(multithread), "--repo", str(checkout), "--json",
     "provider-config", "--client", client, "--launcher-name", "multithread"],
    cwd=checkout, text=True,
))
if plan["schema"] != 1 or plan["provider"] != client or plan["repo"] != str(checkout):
    raise SystemExit("The generated plan does not match this launch.")
print(json.dumps(plan, indent=2))
if input("Review the hook command above; type launch to continue: ") != "launch":
    raise SystemExit("Provider was not started.")
raise SystemExit(subprocess.call(
    [str(provider), *plan["native_arguments"]], cwd=checkout,
))
```

Save the example as a local script and run it with `python3 -I -S -B` followed
by its filename. Native Codex may present its own hook review; verify the
displayed command before trusting it. Starting this example does not deliver a
task or wake another agent. Give each agent its task in the provider interface;
inspect pending work with Multithread's `brief` and `events` commands.

No permanent hook fragment needs installation or removal. A new provider process
started without these arguments has no Multithread invocation contribution. This does
not stop an existing session, undo lifecycle events, release claims, or erase
provider-owned caches/trust history. Native provider startup may write its normal
state; the configuration generator itself does not.

Existing user/project configuration is not disabled or rewritten. Review it
before launching a provider, especially in headless mode. Do not add a second
Multithread adapter or combine conflicting overrides for the same hook key/additional
`--settings` flags without inspecting the effective native configuration. This
is not a generic merger for arbitrary user-supplied invocation arguments.

Codex's exact-definition hook review is still a separate native step. The
generator deliberately includes no trust bypass or permission-policy flags.
Native trust covers the hook definition; Multithread's installer separately approves
the release behind the stable launcher. A configuration plan is not approval to
run an unreviewed release.

## Try a review across two worktrees

Use a disposable repository with a committed starting point for your first
attempt. Create separate author and reviewer worktrees, enroll that repository
once using [setup](SETUP.md#check-readiness-or-enroll-another-project), and verify
that both worktrees resolve to the same Git common directory. In separate
interactive terminals, launch Codex in the author checkout and Claude in the
reviewer checkout. In the author terminal:

```sh
~/.local/bin/multithread launch codex --repo /absolute/author-checkout
```

In the reviewer terminal:

```sh
~/.local/bin/multithread launch claude --repo /absolute/reviewer-checkout
```

Replace the checkout paths with your worktrees and use the exact installed
launcher path if it differs. If a provider is not on `PATH`, add
`--provider /absolute/path/to/provider` with its reviewed executable. Review
each launch's provider, checkout and hook command, then type `launch`. Complete
any native trust review presented by the provider.

Keep these two interactive sessions open and send later wakes and release
instructions to those same sessions. If one closes, use the provider's exact
session-resume facility with the same reviewed hook configuration. The launch
commands above supply hook arguments; they do not select a session to resume.

Give Claude a bounded assignment to follow the review and return-handoff flow
below for Codex's commits. Specify the agreed resource, scope, test command,
review artifact and who will create the Git commits. Tell it to wait for the
first handoff sequence before attempting a claim, reviewing or writing files.
Later sequence-only wakes announce pending work within that assignment; ledger
fields do not expand it.

Give Codex a small, bounded task with an explicit test command. Ask it to read
the current Multithread contract, record its intent, claim the agreed resource, make
the change and run the tests. For example, use `file:progress.py` as the resource
for a change confined to `progress.py`. Each agent must use the identity supplied
by its own hook; never copy the other session's identity into a mutation.

Commit the reviewed author changes through your normal Git workflow. Ask Codex
to inspect that exact commit and publish a `work.handoff` targeting `claude`,
with a work ID, scope and `--commit` set to the full commit OID. The handoff's
summary should specify the requested review, resource and test command, including
one claim attempt and a blocked response if the resource is occupied. Keep
the author's claim held for the contention check, then let its turn finish.
A completed turn does not release its claim.

Read the handoff's sequence from the actual command result. In Claude, send
`Wake: Multithread sequence N.` with that sequence in place of `N`. Claude should read
the full event and inspect its immutable artifact before acting. Its actual
claim attempt on the occupied resource must refuse, leaving the
handoff pending and the reviewer files unchanged. The reviewer should record
why it is blocked and stop. A notification or a final chat message is not an ACK.

Resume the original Codex session and have it release its exact held claim.
Inspect the release result. In the clean reviewer worktree, materialize the
author's full commit through your normal Git workflow, then deliver the same
sequence-only wake to Claude. It should recheck ownership, acquire its own claim,
review the committed changes and run the tests. Have it write a review artifact
without modifying the implementation, then stop for that artifact to be committed.

After committing the review, wake Claude again. It should verify the committed
review, explicitly acknowledge the original handoff, publish a commit-backed
return handoff targeting Codex, and release its own claim. Deliver the original
wake once more: existing ACK and return evidence should prevent another review
or duplicate acknowledgement. Finally, wake Codex with the return sequence and
have it inspect and acknowledge the review. Check the ledger for the linked
handoffs, acknowledgements and absence of held claims.

These v0.1.0 steps use manual notifications and explicit Git operations. Their
hook integration supplies durable coordination and refreshed context; it does
not launch peers, forward messages, commit files, merge changes or grant tool permissions. If a
session is interrupted or a command outcome is uncertain, inspect the ledger
and artifact before continuing. Claims do not expire, and a new session does not
inherit an old session's ownership.

## Bounded native workflow

A bounded run exercised actual Codex 0.153.4 author and Claude 2.1.267 reviewer
sessions in disposable linked worktrees on the documented x86-64 Linux/WSL2
profile. The same exact native sessions continued across phases, with approved
native hooks supplying the coordination contract and pending context.

Codex authored a regression fix and ran the tests. Claude attempted the occupied
resource and correctly remained blocked until the original Codex owner released
its claim. Claude ran the tests, acquired its own claim, reviewed the exact
committed fix and wrote a review artifact. Explicit acknowledgements and a commit-backed return handoff
completed the round trip; a repeated wake did not repeat the review or create a
new acknowledgement. Final inspection verified the linked evidence and that
both claims were released by their exact owners.

Reviewer wakes were manual/controller messages containing only an existing Relay
sequence. Codex author phases also received explicit, bounded task instructions.
The agents inspected full ledger events and immutable artifacts before acting.
A separate controller checked the model-authored bytes and tests and
created the scoped Git commits; the model-authored code and review were preserved.
Relay itself supplied neither message forwarding nor Git-commit authority.

This evidence applies to that bounded workflow and those provider versions.
The public walkthrough uses normal provider interfaces and explicit Git actions;
it does not require distributing the private account-specific validation harness.
Other provider versions and platform profiles require their own checks.
[Hosted checks](CI.md#hosted-result) exercise the no-account suite, not providers;
consult [v0.1.0](https://github.com/SuperDuperDave/multithread/releases/tag/v0.1.0) for package and onboarding results.

## Historical credential-free probes

The optional native probes are separate from the default regression suite. They
run reviewed local provider executables in disposable OS-account/mount/network
namespaces, without real credentials or a model request. Use absolute paths:

```sh
python3 -I -S -B tests/providers/codex_discovery_probe.py --codex /absolute/path/to/codex
python3 -I -S -B tests/providers/codex_discovery_probe.py --codex /absolute/path/to/codex --startup
python3 -I -S -B tests/providers/claude_startup_probe.py --claude /absolute/path/to/claude
```

These Linux fixture probes require the same rootless namespace prerequisites as
the no-account demo. The Codex path must resolve to a standalone native binary,
not a package-manager script. This bounded Claude profile requires both the
entry path and its resolved target inside read-only `/usr`; an arbitrary
`~/.local` installation is not supported by that probe. Provider binaries and
their credentials are not bundled or downloaded.

Codex0.153.4's real app-server `hooks/list` discovered all five hooks from the
public-installed generator as session flags, alongside an unrelated user hook.
All five Relay hooks remained untrusted. Changing a definition changed its native
hash; restarting without the flags removed only the invocation contribution.
No model turn was requested. No hook effects/notifications or Relay events were
observed before deliberate SIGTERM of each exact retained live child. Both modes
record graceful_shutdown:false; an already exited/crashed child, unexpected
exit status or stop timeout fails. This bounded observation does not prove
graceful completion or that pending lifecycle work could never execute.
The optional startup mode returns an ephemeral idle thread with read-only,
no-network, on-request policy; it is not trusted execution or context receipt.
The actual native TOML parser also roundtripped a quoted pathname containing
Unicode, an astral character, shell metacharacters and DEL. The
[Codex receipt](engineering/native-codex-discovery-result.json) binds source,
probe and executable hashes.

The exact native experimental schema exposes155 client request variants and
only hooks/list as a dedicated hook method, not a hook trust/revoke API. Native
CLI /hooks review remains a separately witnessed step, not an invented RPC or
raw trust-file edit. These historical probes did not verify model context,
exact session resume or controller-created Git commits; the bounded workflow
above has separate evidence for those steps. The receipts linked in this section retain
their original scope and contain no authenticated model run.

Claude2.0.5 accepted inline settings and dispatched startup without credentials,
then stopped at authentication. The optional integrated probe uses actual
public-installed Relay, not a canary replacement. Authentication failure is not
task success: this CLI can emit result subtype `success` while `is_error` is true
and the process exits nonzero. Always check the actual outcome and artifacts.
No model-visible context, model coding/review or authenticated workflow is proved
by unauthenticated startup.

Direct SQLite inspection confirmed one actual startup, Stop and session-end
observation matching the native Claude session, after the fixture claim/handoff.
Pending work and the active claim remained unchanged; no ACK or release occurred.
Installed code, enrollment and preexisting provider settings stayed unchanged.
Native Git inspection changed only the Git directory's mtime, not its contents
or child metadata. Hook stdout was not observed in native debug output, so
context receipt is explicitly unproved. See the
[Claude receipt](engineering/native-claude-startup-result.json).

The native mechanisms are also described in the official documentation:
[Codex hook configuration and review](https://learn.chatgpt.com/docs/hooks),
[app-server discovery](https://learn.chatgpt.com/docs/app-server),
[Claude settings](https://code.claude.com/docs/en/settings),
[Claude permissions](https://code.claude.com/docs/en/permissions).
Newer documented Claude fields are not assumed present in2.0.5; `--bare` is not
a subscription-preserving isolation shortcut.

Use the [acceptance matrix](engineering/REQUIREMENTS.md) for the remaining
release checks. Preserve unrelated settings and account state when configuring
your own sessions; review native trust and permissions for the actual launch.
