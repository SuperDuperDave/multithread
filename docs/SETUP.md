# Set up Multithread

Install the **selected published preview**, enroll your Git repository and prepare
native Codex/Claude collaboration. No Multithread source checkout is needed.
The path to a first result is:

1. [Install and enroll](#install-and-enroll), or give your agent the [setup prompt](#ask-your-coding-agent).
2. [Connect every session](#connect-every-session): install the user-level hooks once and choose how Codex trusts them.
3. [Request one collaboration](#start-collaborating), assess its result, then choose any exact-session follow-up.

These commands use the current `multithread` entry point. For older releases and
the retained `relay` command, see [compatibility](#compatibility).

Use an ordinary x86-64 Linux account with Python 3.12 at `/usr/bin/python3`, Git
at `/usr/bin/git`, Landlock ABI3+, procfs and supported filesystem birth times.
WSL2 with ext4 is the exercised profile. Native Windows, macOS and ARM Linux
are outside that profile. Bubblewrap and rootless/nested namespaces are extra
demo/test prerequisites. See [support](SUPPORT.md#exercised-profile).

Multithread uses your existing provider installations and sign-ins. Missing providers
do not prevent Multithread installation or repository enrollment. Provider/OS setup
uses their normal interfaces and remains separate from Multithread setup.

## Install and enroll

Start in the Git repository you intend to use. Review the
[publisher and selected release](https://github.com/SuperDuperDave/multithread/releases/latest)
before running its installer:

```sh
multithread_setup=$(mktemp) &&
  curl -fsSL --proto '=https' \
    https://github.com/SuperDuperDave/multithread/releases/latest/download/install.py \
    -o "$multithread_setup" &&
  /usr/bin/python3 -I -S -B "$multithread_setup" --enroll-repo "$PWD"
```

The downloaded installer contains the exact release version, source commit,
runtime digest, archive checksum and file manifest. It downloads that pinned
archive over HTTPS and validates its contents before installation. Review the
displayed release, existing activation and chosen repository, then type
`install` once to approve that scope. Trust rests on the reviewed publisher and
release obtained over HTTPS. Embedded checksums identify the selected bytes;
they are not an independent publisher signature.

The installer installs account-local code, enrolls the chosen repository and
checks readiness. It reuses a healthy matching installation. An existing
different release is included in the displayed update selection; unexpected or
uncertain state refuses instead of replacing unknown files. Installation and
enrollment preserve existing claims and pending work.

Ctrl-C exits with code 130 and reports the last known installation state.
If installation was interrupted after it began, its outcome may be unknown;
inspect using the reported recovery route before retrying. If code installation
completed but repository setup was interrupted, preserve that installation and
run the printed read-only repository check before retrying enrollment.

Use the exact launcher path printed by the installer, normally
`~/.local/bin/multithread`. Account paths come from the OS account database.
Installation and setup do not edit shell `PATH`, provider settings, permissions
or sign-ins, and start no model session: setup's only provider execution is
Codex's read-only hook listing. Provider hook settings change only through
[`multithread hooks`](#connect-every-session), after you approve its exact plan.

For a fixed published version, replace `releases/latest/download/install.py` in
the command with `releases/download/v0.4.4/install.py` after confirming that tag
lists the installer asset. To inspect its embedded selection
without installing or enrolling, add `--check --json`. To install code without
enrolling any project, omit `--enroll-repo`. The lower-level
[offline guide](engineering/INSTALLATION.md) covers manual archive review,
source builds, recovery and deliberate rollback.

## Ask your coding agent

Paste this into a functioning coding agent already working in your chosen Git
repository:

```text
Set up the current published Multithread release for this Git repository using
https://github.com/SuperDuperDave/multithread/blob/main/docs/SETUP.md.
Confirm the Git root/common directory and compatibility first. Review the
publisher's versioned install.py release asset and its embedded version, source
commit and digests. I authorize that reviewed account-local installation or
update, this repository's enrollment, and launch preparation for my existing
Codex and Claude installations. Use --yes with the reviewed installer selection
and --enroll-repo with this repository's absolute path.
Then install Multithread's user-level hooks with multithread hooks install:
show me its plan, and apply exactly that plan. For Codex hook trust I choose
agent-assisted: run multithread hooks trust, show me its plan, and record exactly
that. Preserve existing work, ledger state, other hooks, provider settings,
sign-ins and permissions. Do not replace an unknown command, provision
providers, repair the OS, change other global settings or launch model sessions.
No Multithread source checkout is needed. Finish with verified runtime/repository
readiness, multithread hooks status, each provider's preparation state and any
remaining step that is mine. Then point me to the first-collaboration prompt in
docs/PEER.md#first-collaboration.
```

To review Codex hook trust yourself, replace `agent-assisted: run multithread
hooks trust, show me its plan, and record exactly that` with `manual: give me
the exact /hooks steps`. Either way the agent shows you every plan before it
changes a provider file.

The agent should continue through the authorized steps it can verify. If the
repository is ambiguous, establish the intended target before enrollment.

`--yes` applies the selected installer release without its interactive prompt;
it approves installing or updating to that exact embedded release, including
replacing a recognized active version. To require a particular prior activation,
also pass `--expected-activation` with its observed ID (`none` for a first
installation). This approval does not authorize unrelated changes or provider usage.

For repeated use, an optional [Multithread skill](../skills/multithread/SKILL.md)
gives agents a short route to the installed CLI and public guides without copying
the manuals into every project's instructions. It is a reviewed starting point,
not installed by setup or required for collaboration. Codex discovers project
skills at `.agents/skills/multithread/SKILL.md`; Claude Code uses
`.claude/skills/multithread/SKILL.md`. Ask your agent to place the skill in the
provider location you use, review it with your project guidance, and preserve
any existing local skill. If you use both providers, keep one project-local copy
and point the other provider's skill path to it where supported. Project-specific
collaboration preferences belong in that project's guidance; public command
syntax and behavior remain in the installed help and linked docs. Existing
sessions may need to discover a newly added skill before using it.

To add that optional guidance, give your agent this prompt in the enrolled
repository:

```text
Add the reviewed Multithread skill from
https://github.com/SuperDuperDave/multithread/blob/main/skills/multithread/SKILL.md
to the project-local skill location for the coding provider(s) this repository
uses. Preserve any existing skill or project instructions; if both providers
use it, keep one maintained copy where supported. Verify the resulting paths
and tell me how each provider will discover it. Do not change provider settings
or run a peer call as part of this task.
```

For local identity checks, use:

```sh
git rev-parse --show-toplevel
git rev-parse --path-format=absolute --git-common-dir
```

Linked worktrees with the same common directory share a ledger. Setup needs no
test handoff, acknowledgement or claim release.

## Check readiness or enroll another project

From the chosen checkout:

```sh
~/.local/bin/multithread setup --repo "$PWD" --check
```

This read-only check verifies runtime identity, repository integrity and ledger
status, then prepares invocation plans for providers found on `PATH`. It prints
exact next commands. Add `--json` for structured observations and next actions.
For Codex it also asks Codex's app server for this checkout's hook listing, the
same check a Codex peer call makes before any task: `initialize` and `hooks/list`
only, with no thread, turn, model call or trust change. Setup runs no other
provider executable and no version checks.

To explicitly enroll a new chosen repository, then run the same checks:

```sh
~/.local/bin/multithread setup --repo "$PWD" --apply
```

A checkout that is not enrolled yet reports `not_enrolled`, with this command as
its one next step. `status`, `doctor`, `peer` and `launch` in that checkout
name the same command.

Select reviewed provider paths explicitly when necessary:

```sh
~/.local/bin/multithread setup --repo "$PWD" --check \
  --codex /absolute/path/to/codex --claude /absolute/path/to/claude
```

| Stage | What setup establishes | What remains |
|---|---|---|
| Runtime verified | Healthy installed status and exact release/activation identity | Any installation refusal needs its specific inspection or recovery action. |
| Repository verified | Enrollment, exact Git identity, healthy integrity check and matching ledger status | Preserve state on refusal or unavailable observation; do not delete or forge enrollment markers. |
| Provider prepared | Executable path, where its hooks come from (`hook_source`: `user` for the installed user-level hooks, `session_flags` when only launch and peer pass them) and a matching plan; for Codex, all five Multithread hooks listed as trusted for this checkout | Provider version, sign-in and tool capability are not checked. `needs_hook_review` names each event Codex lists as untrusted, modified or disabled; `needs_hook_configuration` names hooks that are missing, duplicated or not as installed. A missing provider can be installed or located through its normal interface. |
| Coverage | `hook_coverage`: recent Codex and Claude sessions in this repository that left no ledger events, with the cause and fix | See [when a session cannot reach its ledger](#when-a-session-cannot-reach-its-ledger). |
| Hook/context delivery | Not checked by setup | Observe the Multithread context in an authorized native session. A generated plan or zero hook exit does not prove delivery. |
| Provider tools | Not checked by setup | Observe an authorized native tool action. Tool execution alone does not establish a completed collaboration workflow. |

“Multithread is ready for this repository” means the runtime and repository passed.
Providers may still be missing or need attention; when Codex's hooks need review,
the heading says so and the next action gives the exact review step. A successful installation
followed by incomplete enrollment remains a successful code installation with
repository setup unresolved. After a timeout or uncertain enrollment result,
run the printed read-only check before deciding whether to retry. Keep local
diagnostics private and sanitize anything shared.

### Folder permissions

Enrollment refuses a checkout when another user could change a directory it
relies on: the checkout and each folder above it, its Git directory (for a
linked worktree, also the shared `.git/worktrees` entries) and Multithread's own
state. None of them may allow group or other write, and Multithread's private
state and account folders allow no group or other access at all. A umask of
002, which Ubuntu gives login sessions of accounts with their own group, creates
directories with group write, so a fresh clone can be refused. The refusal lists
every such directory at once, each with a `chmod` that changes only that
directory:

```text
multithread: enrollment directory permissions are unsafe: other users could change 2 directories this checkout's enrollment relies on, so Multithread refuses it. Run each command below (it changes only the directory it names), then check again:
  chmod g-w,o-w /work/app   (observed mode 0775; group/other write access is not allowed)
  chmod g-w,o-w /work/app/.git   (observed mode 0775; group/other write access is not allowed)
A umask of 002 creates directories with group write, so a fresh clone can start this way.
```

Run the listed commands, then check or enroll again. Nothing was written before
the refusal. A clone made under `umask 022` starts without group write.

## Connect every session

Hooks connect a Codex or Claude session to its checkout's ledger: the session
records its lifecycle and receives the coordination brief. Installed once for
your account, they reach every session in an enrolled repository however it was
started: the Codex app, a terminal, an IDE, `launch` or a peer call. In a
repository you have not enrolled they do nothing.

```sh
~/.local/bin/multithread hooks install
```

Install shows the exact change to Codex's `~/.codex/hooks.json` and Claude
Code's `~/.claude/settings.json` (`$CODEX_HOME` and `$CLAUDE_CONFIG_DIR` when
set), then asks you to type `install`. It appends one handler for each
lifecycle event after any existing hooks, so every other hook keeps its place
and Codex keeps trusting it. It never edits, reorders or removes another hook,
reports the ones it leaves in place, and keeps a private copy of each file it
changes beside it for recovery. An agent first runs `hooks install --json` to show you the
plan, then applies exactly that plan with `--yes --expected-plan
<plan_sha256>`; a file that changed in between refuses. Files change one at a
time: if the second fails after the first changed, the result is
`partly_applied` and names which file changed, its kept copy, and which did not.

### Choose how Codex trusts the hooks

Codex runs a user hook only after its exact definition is trusted. Claude Code
has no per-hook trust. Choose one route:

- **Manual.** Use a terminal: Codex's terminal `/hooks` review records trust,
  and the desktop app's hook screen may not
  ([openai/codex#47283](https://github.com/openai/codex/issues/47283)). Run
  `codex` in your home directory, type `/hooks`, and for SessionStart,
  UserPromptSubmit, Stop, SessionEnd and Interrupt trust the hook from
  `~/.codex/hooks.json` whose command is exactly the one `hooks status` prints.
  Leave other hooks as you choose.
- **Agent-assisted.** Your agent runs `~/.local/bin/multithread hooks trust`.
  It asks Codex itself to list the hooks, then checks each one's source file,
  position, event, exact command, timeout and matcher, and Codex's own hash
  against the hash of the definition Multithread installed. Any difference
  refuses the whole plan. The plan shows each hook's key and hash; once
  approved, it records trust through Codex's configuration API, the same
  `hooks.state` write Codex's own `/hooks` review makes, guarded by the
  configuration version it read. It never includes another hook, and
  `hooks trust --revoke` removes exactly the records holding Multithread's
  hashes. Once the write is sent it never reports "nothing changed": a lost
  answer is `uncertain` and a failed check after an acknowledged write is
  `applied_unverified`, each naming the read-back to run before any retry.

`~/.local/bin/multithread hooks status` reports installation and trust for
both providers, each hook command, and the next step. A Codex conversation
that started earlier may keep running without them: start a new one after
trusting. Claude Code normally picks up hook changes in a running session.

With user-level hooks installed, `launch` and peer calls add no invocation copy,
so each event runs once; a partial or altered installation makes them refuse
and name the fix. To take the hooks out, run `hooks trust --revoke` (Codex),
then `hooks remove`. Prefer these fresh, guarded plans to restoring a kept copy:
a copy also erases anything written to the file since. An agent that keeps its
own copies before a change puts them in a directory it has just created, owned
by you and closed to others; `mkdir -m 700 -p` leaves an existing directory's
mode as it was, so it checks the owner, the mode and that the path is not a
symbolic link. A settings file that does not exist is recorded as absent, not
as a failed copy.

To retire another tool's hooks that you no longer want, for example an older
lifecycle dispatcher, name each exact command with `--command`, first for its
Codex trust records and then for the hooks themselves:

```sh
~/.local/bin/multithread hooks trust --revoke --command 'EXACT COMMAND' --json
~/.local/bin/multithread hooks remove --client codex --command 'EXACT COMMAND' --json
```

Each shows its plan: the trust records Codex lists for exactly that command
(the whole record is removed only when trust is all it holds), and the handlers
it takes out with every other hook left in place and any that move named.
Apply each with `--yes --expected-plan`. Do this before `hooks install`, so the
positions Codex keys trust to are settled first.

### Confirm delivery in a real session

A trusted listing, or a row in the ledger, does not show that a session
received its brief. To check delivery end to end, note `last_seq` from
`~/.local/bin/multithread --repo "$PWD" --json status`, start a new
conversation in the enrolled checkout and note its session ID. Then confirm
both halves: `~/.local/bin/multithread --repo "$PWD" --json events --after
LAST_SEQ` shows `session.started` for that exact session ID, and the session
can quote the `MULTITHREAD AGENT CONTRACT v1` line from its own context. If
either is missing, `hooks status` and `setup --check` name what to fix.

### When a session cannot reach its ledger

In an enrolled checkout, a hook that cannot deliver verified ledger context
says so. The agent receives one `MULTITHREAD WARNING` line with the reason and
the fix, to tell you, and you see a short notice: this step has no brief, and
the session's record may be incomplete. A repository nobody enrolled stays
silent.

`doctor` also looks for silence. It compares the Codex and Claude sessions that
worked in this repository and its worktrees in the last 24 hours with the
ledger, and names any that left no events, with the cause and the fix, for
example that the user-level hooks are not installed or that Codex has not
trusted them. It reads session records for identity, directory and time only,
never their content. `status` and `brief` repeat each finding as one warning
line, and `setup --check` shows it as coverage.

## Review native trust

With [user-level hooks](#connect-every-session) installed, start providers
however you like. Without them, `launch` passes invocation-only hooks for one
interactive session: run the exact command setup printed. With the usual
launcher and provider on `PATH`:

```sh
~/.local/bin/multithread launch codex --repo "$PWD"
```

Use `claude` for Claude Code. Review the displayed provider path, repository and
hooks, then type `launch`. Add `--json` to prepare this plan without starting the
provider. Existing sessions do not acquire new invocation arguments. Complete
native sign-in or hook trust through the provider's normal interface, and review
existing hooks for duplicates or conflicting overrides.
For Codex, open `/hooks` and review the exact generated commands. The Codex hook
command names no checkout: Codex records trust per hook event and exact command
text, not per repository, so one review covers every enrolled checkout and
worktree until the command changes. Each hook finds its checkout from the Codex
session's working directory, which launch and peer set; an unenrolled directory
records nothing. The command always names the account launcher setup prints:
launch and peer give another path to that same launcher its account spelling
and refuse a different file, since a second spelling would move Codex's one
trusted command away from every other session. Claude's
noninteractive peer mode does not show the interactive workspace trust dialog;
review the repository and its provider configuration before calling. See
[provider-specific preparation](PEER.md#before-calling).

Launch is the person's step: an agent never runs `multithread launch`, and
reports the printed command instead. An agent changes provider hook settings or
Codex trust only through `multithread hooks install`, `trust` or `remove`, only
after the person chose that route, and shows the exact plan first. It never
edits hook files or Codex's `config.toml` directly, never runs Codex's `/hooks`
review for the person, and reports any remaining native step. Peer calls
require authorization for that use. A prepared plan or a listed trusted hook
does not prove hook delivery or native tool execution.

## Start collaborating

Use the copyable [first-collaboration prompt](PEER.md#first-collaboration) once
preparation and any required native trust review are complete. It authorizes
one read-only native review and asks the calling agent to inspect and assess the
returned findings. It addresses your current unresolved project change or
question, with a documented-feature trace as a fallback. Peer calls have no extra
interactive confirmation:
`multithread peer claude --json` executes the call; add `--dry-run` to inspect
without execution.

Check the [result and any unresolved work](PEER.md#read-the-result-before-continuing).
An unavailable provider, refused action, uncertain outcome, returned answer and
completed review establish different things. If further work is warranted and
authorized, [choose a fresh call or exact-session follow-up](PEER.md#follow-up-in-the-same-native-session):
resume in the same checkout when prior investigation helps, or start fresh for
independent judgment or a different scope. The peer guide includes a continuation
packet and separately covers [input to a running call](PEER-REFERENCE.md#update-a-running-peer).

Calls use the existing provider's normal environment and permission mode.
Subscription sign-in can be used; API keys or other provider configuration can
change the billing path. Check that through the provider's normal interface.
Multithread does not certify account billing.

The [two-worktree review walkthrough](PROVIDERS.md#try-a-review-across-two-worktrees)
is an alternative using manual wakes and explicit Git operations. The
[no-account demo](DEMO.md) is an optional scripted introduction with extra
namespace requirements; it does not establish provider readiness.

## Update an existing installation

```sh
~/.local/bin/multithread update
```

The updater reads the latest published metadata and asks you to approve that
publisher selection before running new code. It then verifies the pinned archive
and applies the exact selection. A matching release is reported as
up to date. It uses the exact observed activation, preserves prior releases and
does not enroll another repository unless you explicitly add `--enroll-repo`.

For read-only inspection, use `multithread update --check` or:

```sh
~/.local/bin/multithread update --json
```

This checks metadata and current installation; it does not download and verify
the archive or apply an update. When an update is available, the JSON includes
`apply_argv` and `apply_command`, pinned to the version, runtime digest and
observed activation with `--yes`. An agent should review that selection, then
execute the exact argv when updating is authorized. Do not pass JSON through
shell `eval`. Applying an unattended update requires those exact selection
fields; `--yes` alone is insufficient. Use `--version 0.4.4` to select that fixed
published version for inspection or interactive application.

Updates run only when requested; there is no background updater. Running
workers keep their loaded code while subsequent commands use the selected
release. Finish active coordination before switching between incompatible
releases, and start new sessions deliberately when needed. An update does not
restart providers or certify compatibility of active work. For an older release,
use [explicit rollback](engineering/INSTALLATION.md#upgrades-and-explicit-rollback).

## Compatibility

Multithread was previously called **Agent Relay**. From v0.4, use `multithread`
for new scripts and instructions. `relay` remains supported this release, using
the same installation and ledger; it has no automatic expiry, removal or per-hook
warning. Retiring it requires a future deliberate migration of saved hooks and
scripts. Release filenames, repository URLs and internal `.relay` storage remain
unchanged.

| Installed release | Command and peer capabilities |
|---|---|
| v0.4.x | Preferred `multithread` command, with compatible `relay`; both native providers, exact-session resume and live input. |
| v0.3.0 | Use `relay` for both native providers, exact-session resume and live input; `~/.local/bin/relay update` reviews an update. |
| v0.2.0 | Use `relay`; Claude call/return and exact-session resume, without the v0.3 additions. |
| v0.1.0 | No peer command or `relay update`; use the [installer above](#install-and-enroll) to review and apply a new selection. |

Check the selected release's version and package results before installation.
Capabilities and native observations have separate scopes: the
[peer evidence](PEER-REFERENCE.md#coordination-and-verification-scope) retains installed
Claude v0.2 observations and the v0.3 source-entry observations, including
interrupted calls and separately observed artifacts. They do not establish
fresh-account onboarding or broader platform support. For management after
rollback, follow the [latest reviewed bootstrap guidance](engineering/INSTALLATION.md#upgrades-and-explicit-rollback).
