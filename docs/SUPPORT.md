# Multithread support and troubleshooting

Multithread (formerly Agent Relay) is an [MIT-licensed](../LICENSE) preview
exercised on the Linux/WSL2 profile below. Installation, scripted demonstration
and bounded native provider work have separate evidence.
[Hosted checks](CI.md#hosted-result) record the public commit and runner; consult
the [selected release](https://github.com/SuperDuperDave/multithread/releases) for
package and onboarding results.

Use [setup](SETUP.md) for the current installation and first collaboration path.
[Compatibility notes](SETUP.md#compatibility) cover older releases and the retained
`relay` command. Historical evidence below retains its recorded names and scope.

## Exercised profile

The recorded installed-runtime and no-account witnesses use x86-64 Linux under
WSL2, ext4, Python 3.12.3, SQLite 3.45.1, Git 2.43.0 and bubblewrap 0.9.0.
The installed worker requires Landlock ABI3 or newer, procfs, supported statx
creation-time evidence and the reviewed Linux syscall/device behavior.
It runs as an ordinary trusted OS user, not root.

The complete tests/demo additionally require /usr/bin/python3, /usr/bin/git,
/usr/bin/bwrap, a POSIX shell and enabled unprivileged user, mount, PID and
network namespaces. Nested namespaces must be permitted for nested witnesses.
An LSM policy, container boundary or host restriction can deny them even when
the bwrap executable is installed.

For one concrete policy mechanism, see Ubuntu's
[AppArmor namespace restrictions](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/).
The appropriate policy depends on the environment. Multithread's installer and demo
leave developer-machine policy unchanged; the hosted job uses the explicit
CI profile described below.

Fixture namespaces map their ordinary account to UID/GID1000. This is not an
instruction to renumber the host user. Other host profiles need their own
execution evidence. The [recorded hosted run](CI.md#hosted-result) additionally
exercises its Ubuntu runner with a temporary, explicitly entered `relay-ci`
AppArmor compatibility profile. That enforced profile grants broad operations
for the CI process tree; it is not a tight AppArmor sandbox. The system launcher
drops host privileges before repository code runs, and a preflight checks two
nested namespace levels under the same profile. See the
[exact CI setup](CI.md#hosted-workflow), which keeps AppArmor enabled globally
and leaves developer machines unchanged. Other Ubuntu/AppArmor configurations need
their own checks. Native Windows, macOS, ARM Linux, network filesystems and
other container/runner profiles remain outside these witnesses.

No provider account is needed for the complete default suite or scripted demo.
The [native walkthrough and evidence](PROVIDERS.md#bounded-native-workflow) cover
Codex 0.153.4 and Claude 2.1.267 with manual reviewer wakes, explicit author task
instructions and controller Git commits. Trying that workflow requires your own provider
access and reviewed permissions; the historical credential-free probes retain
their narrower scope.

The published v0.3.0 release includes Codex peer calls and live input. Its
[source-entry observations and limitations](PEER-REFERENCE.md#coordination-and-verification-scope)
use Codex 0.153.4 and Claude Code 2.1.269 on the existing developer profile.
They include native artifacts from interrupted calls and do not establish
installed-native v0.3 support. The earlier v0.2.0 release retains its
separate installed Claude call/return and exact-session resume evidence.

An ordinary installed v0.4.0 development review with Claude Code 2.1.269 also
returned a review artifact, acknowledged its exact handoff and consumed a live
input. Its [scope and measurements](PEER-REFERENCE.md#coordination-and-verification-scope)
do not establish native v0.4.1 execution or independent onboarding.

## Diagnose without changing live configuration

Use `~/.local/bin/multithread --version` for the verified installed version,
without a network request or repository access. Root `--help` points to runtime
status, inspection and command help. Version discovery does not check for updates.

Start with `~/.local/bin/multithread setup --check --repo /absolute/checkout` for a
read-only runtime/repository check and available provider launch plans. Add
`--json` for structured stages and exact next commands. Provider authentication,
hook delivery and tool execution remain separately unobserved. Use
`~/.local/bin/multithread update --check` for an explicit online release check;
network failure means unavailable information, not an up-to-date installation.

When runtime and repository checks pass, continue with any required
[native trust review](SETUP.md#review-native-trust) and a
[first collaboration](PEER.md#first-collaboration). Repeating installation does
not establish provider sign-in, hook delivery or a completed review.

For a first look, run the [no-account demo](DEMO.md) on the supported profile.
The [strict suite](CI.md) checks the full local acceptance contract. Both use
disposable fixtures without enrolling this checkout or enabling provider hooks.
For an installed command, start with `~/.local/bin/multithread runtime status`, using
the exact launcher path printed during installation if it differs. Project
status requires an [enrolled repository](SETUP.md#check-readiness-or-enroll-another-project).

| Observation | Meaning and next action |
|---|---|
| Missing /usr/bin/bwrap | The namespace test dependency is absent. Install the distribution package through your normal reviewed administration process; do not substitute a random downloaded binary. |
| Operation not permitted during namespace setup | A kernel, LSM or container policy may deny the requested namespaces. Preserve the error category and investigate that environment. Do not disable host protections globally to obtain a pass. |
| Tests skipped, zero discovered, or fewer run than discovered | The strict acceptance gate must fail. Read the test diagnostics and fix the cause; do not relabel the reduced run as complete coverage. |
| Landlock or creation-time evidence unavailable | The installed write boundary cannot be established on that profile. Use a verified environment/filesystem; there is no permissive fallback. |
| Intended repository is unenrolled | Run `~/.local/bin/multithread setup --repo /absolute/checkout --apply` to explicitly enroll it and check readiness, using the exact installed launcher path if it differs. Follow the reported next actions and [readiness guide](SETUP.md#check-readiness-or-enroll-another-project). |
| Repository identity is ambiguous or state is inaccessible | Preserve existing state and inspect the reported refusal; unavailable state does not mean an unenrolled repository. Follow [initialization boundaries](engineering/INSTALLATION.md#choose-and-initialize-a-repository); do not forge markers or overwrite or delete state to bypass a refusal. |
| Initialized repository moved on the same filesystem | Stop all its Multithread users and follow [explicit rebind](engineering/REBIND.md). The original physical objects must remain; copied/restored ledgers and cross-filesystem migration are unsupported. |
| Interrupted rebind or inconsistent transition history | Preserve retained records. Only a valid unfinished transition to its exact pinned target is retryable; malformed, conflicting or incomplete history remains blocked without pruning. |
| Unknown multithread/relay command, changed selector or uncertain install | Preserve existing files and inspect using a separately reviewed bootstrap. Use fresh exact observations, not deletion or blind retry. |
| `verified launcher unavailable: unsafe launcher ancestry` | The launcher refused before loading Multithread because an installation-path ancestor appeared owned by neither root nor this account, or writable by another user outside a root-owned sticky temporary directory. In the failing environment, start with `namei -l ~/.local/share/relay/installation` (if `namei` is available), then inspect the selected release path if those ancestors are safe. Compare with the ordinary account environment when a tool sandbox is involved: it can present different ownership even when the host installation is healthy. Preserve the refusal; do not blindly change permissions or weaken the launcher. |
| Provider login or hook-trust failure | This is distinct from SSH connectivity and ledger installation. The no-account tests cannot establish provider authentication or trusted hook execution. |
| Peer input accepted or consumed, but the call needs attention | Input receipts and task completion are separate. Inspect the call result, retained native observations and actual artifacts before a deliberate follow-up; see [peer results](PEER.md#read-the-result-before-continuing). |
| Streaming stdout limit exceeded | Streaming calls retain only a bounded prefix, close native input and clean up their owned process. A previously observed answer can survive with `needs_attention`; later output is unavailable. See [capture limits](PEER-REFERENCE.md#receipt-fields-and-capture-limits). |

For a damaged launcher, follow [installation recovery](engineering/INSTALLATION.md).
If you own an unrelated command occupying the `multithread` path, deliberately
relocate it through its own setup before retrying; Multithread does not move or delete it.
For removal, follow [verified code uninstall](engineering/UNINSTALL.md).
Neither workflow is a general ledger purge, provider logout, automatic claim
expiry or permission to delete unknown state. If no separately verified release
or bootstrap is available, preserve the evidence and stop modifying the install.

## Useful, safe support information

For a retained peer call, start with the
[read-only support report](PEER-REFERENCE.md#prepare-a-support-report-from-an-existing-call):

```sh
~/.local/bin/multithread peer report --call-dir /absolute/peer-call --json
```

Review this selected diagnostic summary before sharing it. Reporting a call
does not run it again or establish that it succeeded. A missing final receipt
leaves the call outcome unknown. Keep the original private evidence intact.

Newer reports can include a provider-reported version observed during that call.
Older receipts and ordinary Claude final-JSON calls leave it unrecorded; see
[version provenance](PEER-REFERENCE.md#prepare-a-support-report-from-an-existing-call).
If you supply a CLI version separately, include when you observed it. The version
installed after an update does not identify the provider used by an earlier call.

Share the command name, exit category, platform/architecture, relevant dependency
versions, test counts and sanitized JSON outcome. Identify the reviewed commit
and whether the issue is install, enrollment, test environment or provider trust.
A success toast or green transport connection is not a durable-write witness.

Do not post auth files, environment dumps, raw provider transcripts, live SQLite
databases, private receiver URLs, personal Git metadata or unreviewed full logs.
Use a minimal disposable reproduction with artificial identifiers.
Report ordinary bugs through [GitHub Issues](https://github.com/SuperDuperDave/multithread/issues).
Report vulnerabilities through [private vulnerability reporting](https://github.com/SuperDuperDave/multithread/security/advisories/new),
not a public issue. No response-time commitment is made.

## Trust limits

Multithread coordinates cooperating agents under one trusted OS account. Session
labels are not hostile-user authentication; claims do not grant shell or cloud
permissions. Retained-byte verification and exact file-write confinement are not
a general malicious-code sandbox. Physical power-loss behavior and every
same-account namespace race are not proven.

See [architecture](ARCHITECTURE.md) and the [acceptance matrix](engineering/REQUIREMENTS.md)
for exercised guarantees and remaining work. Support claims grow from new
execution evidence, not from widening a version string or ignoring a refusal.
