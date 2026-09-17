# Set up Multithread

Install the **selected published preview**, enroll your Git repository and prepare
native Codex/Claude collaboration. No Multithread source checkout is needed.
The path to a first result is:

1. [Install and enroll](#install-and-enroll), or give your agent the [setup prompt](#ask-your-coding-agent).
2. [Review native trust](#review-native-trust) for the chosen repository and provider.
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
Multithread does not edit shell `PATH`, provider settings, permissions or sign-ins,
and setup does not execute a provider or start a model session.

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
Preserve existing work, ledger state, provider settings, sign-ins and permissions.
Do not replace an unknown command, provision providers, repair the OS, change
global settings or launch model sessions. No Multithread source checkout is needed.
Finish with verified runtime/repository readiness, each provider's preparation
state, exact next launch commands and any remaining native action. Then point
me to the first-collaboration prompt in docs/PEER.md#first-collaboration.
```

The agent should continue through the authorized steps it can verify. If the
repository is ambiguous, establish the intended target before enrollment.
`--yes` applies the selected installer release without its interactive prompt;
it approves installing or updating to that exact embedded release, including
replacing a recognized active version. To require a particular prior activation,
also pass `--expected-activation` with its observed ID (`none` for a first
installation). This approval does not authorize unrelated changes or provider usage.

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
Setup does not run provider executables, including version checks.

To explicitly enroll a new chosen repository, then run the same checks:

```sh
~/.local/bin/multithread setup --repo "$PWD" --apply
```

Select reviewed provider paths explicitly when necessary:

```sh
~/.local/bin/multithread setup --repo "$PWD" --check \
  --codex /absolute/path/to/codex --claude /absolute/path/to/claude
```

| Stage | What setup establishes | What remains |
|---|---|---|
| Runtime verified | Healthy installed status and exact release/activation identity | Any installation refusal needs its specific inspection or recovery action. |
| Repository verified | Enrollment, exact Git identity, healthy integrity check and matching ledger status | Preserve state on refusal or unavailable observation; do not delete or forge enrollment markers. |
| Provider prepared | Executable path and matching invocation plan | Provider version, sign-in, native trust and tool capability are not checked. A missing provider can be installed or located through its normal interface. |
| Hook/context delivery | Not checked by setup | Observe the Multithread context in an authorized native session. A generated plan or zero hook exit does not prove delivery. |
| Provider tools | Not checked by setup | Observe an authorized native tool action. Tool execution alone does not establish a completed collaboration workflow. |

“Multithread is ready for this repository” means the runtime and repository passed.
Providers may still be missing or need attention. A successful installation
followed by incomplete enrollment remains a successful code installation with
repository setup unresolved. After a timeout or uncertain enrollment result,
run the printed read-only check before deciding whether to retry. Keep local
diagnostics private and sanitize anything shared.

## Review native trust

To start an interactive session with invocation-only Multithread hooks, run the exact
command setup printed. With the usual launcher and provider on `PATH`:

```sh
~/.local/bin/multithread launch codex --repo "$PWD"
```

Use `claude` for Claude Code. Review the displayed provider path, repository and
hooks, then type `launch`. Add `--json` to prepare this plan without starting the
provider. Existing sessions do not acquire new invocation arguments. Complete
native sign-in or hook trust through the provider's normal interface, and review
existing hooks for duplicates or conflicting overrides.
For Codex, open `/hooks` and review the exact generated commands. Claude's
noninteractive peer mode does not show the interactive workspace trust dialog;
review the repository and its provider configuration before calling. See
[provider-specific preparation](PEER.md#before-calling).

An agent authorized only for setup should report these commands for the user.
Provider launches and peer calls require authorization for that use. A prepared
plan or a listed trusted hook does not prove hook delivery or native tool execution.

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
