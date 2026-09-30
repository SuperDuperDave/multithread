# Architecture and tradeoffs

Multithread's first job is to preserve coordination across processes and worktrees:
who holds a shared resource, which exact artifact needs review, and whether
the intended recipient has deliberately consumed a handoff.

Multithread is a local coordination preview with an exercised installed runtime
and bounded native coding/review evidence on Linux/WSL2. The
[provider guide](PROVIDERS.md#bounded-native-workflow) describes the actual
historical sessions, manual notifications and controller Git-commit boundary;
the [peer guide](PEER.md) covers current native call/return and its separate evidence.

## Three boundaries

**Protocol and ledger** live in `src/relay_core`. Typed, bounded events are
canonicalized before append. SQLite constraints and transactions enforce the
state machine, including scarce-resource claims and atomic decision response
plus acknowledgement. Pending projections are derived from this ledger;
a notification service does not get its own competing work queue.

**Workspace admission** lives in `src/relay_runtime`. Installed code is not
permission to operate on any directory that resembles a Git repository.
Explicit enrollment ties a normal repository and its registered linked
worktrees to account-held identity evidence. Missing, moved, replaced or
ambiguous state refuses rather than silently initializing a fresh ledger.
The worker retains directory/file identity checks across the actual operation.

**Distribution selection** lives in `src/relay_bootstrap.py`. An offline
release is a closed pair: reviewed bootstrap plus an exact runtime module set.
A release digest identifies exact bytes, not a trusted publisher. Installation
uses account defaults from the OS account database rather than ambient HOME.
The command selector activates one coherent pair with a fresh activation ID;
an installed launch cannot silently keep executing as the active version
after another version has been selected.

The no-account demo exercises all three through the actual public command.
It does not import runtime implementation objects or test fixtures.

## Why the coordination rules matter

A successful send means only that a transport accepted a send. It does not
mean the recipient read the handoff, inspected its artifact, finished a review
or was authorized to take an external action. Multithread keeps pending work until
an explicit matching acknowledgement. The demo's two repeated pending reads
leave both event count and pending status unchanged.

An exact retry can reuse the same canonical identity. Reusing a handoff
identity with changed meaning is a conflict, not an update to history.
Acknowledgements are a set keyed by signal and recipient, so retrying from a
later recipient session returns the existing ACK.

Claims have exact holder identity and no automatic timeout. A stopped agent
does not prove that its external work stopped. Ordinary release needs the
original holder; explicit claim breaking is a separate audited operation.
These labels are not authentication against hostile processes under the same
OS account.

A handoff can pin an actual Git commit using `signal ... --commit OID`.
The demo retrieves the full event after its bounded pending notification,
checks route and artifact, checks out the exact commit in a second worktree,
checks the change set, and runs its tests before acknowledging.
A stored hash alone is not evidence that code was reviewed.

## A failure that shaped the runtime

An early experiment tried to use a held directory descriptor as the boundary
for SQLite paths. SQLite canonicalized the path and could open sidecars by a
later pathname. When the namespace changed, a held directory alone did not
prevent writes to foreign WAL/SHM files.

The implemented Linux profile therefore admits the exact DB/WAL/SHM objects
and runs each command in a fresh worker with object-specific Landlock write
grants. It checks custody again before accepting buffered success output.
Independent tests substitute the database or state directory immediately
before a real INSERT and verify foreign bytes/metadata remain unchanged and
no successful receipt is forwarded. This is an exercised failure boundary,
not merely a mocked filesystem test.

The runtime also needs the kernel null sink for standard subprocess behavior.
It checks a held no-follow character device with exact device number1:3.
User-namespace ownership presentation is not the null driver's identity;
that distinction was verified with a real rootless account and negative
device/file substitutions.

See [ledger admission](engineering/LEDGER-ADMISSION.md) and
[null-device identity](engineering/NULL-DEVICE-IDENTITY.md) for mechanism,
primary-source rationale, test evidence and limits.

## Intentional limits

- The native launch/peer adapter runs outside the confined
  ledger worker. It obtains hook configuration through an admitted read-only
  worker, then invokes the provider with its normal environment and permissions.
  Process return is separate from ledger acknowledgement and workflow completion;
  see [peer calls](PEER.md). It does not provision the provider or OS.
- User-level hook management (`multithread hooks`) also runs outside the
  worker. It edits only its own entries in the provider's user hook file, after
  approval of an exact plan, and records Codex trust only through Codex's own
  configuration API. Coverage evidence for `doctor`, `status` and `brief` is
  gathered before the worker starts, as provider session identities, directories
  and times; the confined worker compares them with the ledger.
- One trusted local OS account is the initial model. Agent labels are not
  credentials, and local coordination is not a multi-user authorization system.
- The exercised worker profile is x86-64 Linux/WSL with Python3.12, LandlockABI3+,
  procfs and filesystem birth-time support. Native Windows is not equivalent.
- Object-write confinement is not a hostile-code sandbox. It does not claim
  to restrict all reads, network use or metadata operations.
- A digest proves selection/integrity, not publisher identity or source rights.
- The initial fixed Codex/Claude channel and decision routes are inherited
  contracts, not a claim of general multi-provider interoperability.
- Disable, code uninstall, provider unconfiguration and deleting project data
  are different operations; a narrower one must not be advertised as all four.
- Fresh-process/crash tests are not a physical power-loss guarantee.

The [acceptance matrix](engineering/REQUIREMENTS.md) separates observed
behavior from unfinished release requirements. For a short executable tour,
run the [no-account demo](DEMO.md).
