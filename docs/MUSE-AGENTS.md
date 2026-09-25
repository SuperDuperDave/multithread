# Optional Muse agent collaboration

[Muse is Meta's personal AI agent](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/).
This guide addresses a person's named Muse instance as a collaborator; it
does not assume that Meta supplies a Multithread transport.

Multithread's installed native `peer` command currently calls Codex or Claude
and returns a bounded answer to the initiating task. A Muse agent has a
different shape: a named, independently running collaborator may receive a
task, acknowledge durable custody, work later and return a task-bound artifact.
This guide defines how to use that collaboration alongside Multithread. The
optional account adapter router is available only in a release whose
`multithread agent --help` lists Muse; it does not install or control a Muse
runtime. Users without a Muse agent have no bridge to configure.

## Connect a named agent

The optional router stores one reviewed Python adapter path and SHA-256 per
nickname in a private account registry. It does not copy credentials, configure
the Muse runtime, contact the bridge during registration or enroll a project.
The adapter must support `--repo CHECKOUT project|check|prepare|send|status|replies`
and return a JSON object on successful calls. It owns its own credentials and
transport. The router executes the exact registered single-file Python bytes
under `-I -S -B`, so only Python's standard library is importable through
normal imports; ambient `PYTHONPATH`, site-packages and sibling modules are
unavailable. An adapter may deliberately
open other files or launch tools; review those dependencies and their trust
boundary before registration. Keep the source in a location where other
accounts cannot alter it while you review and register it. After an adapter
update, remove the old registration and register the reviewed new bytes
explicitly.

```sh
multithread agent muse register Buddy --client /absolute/path/to/reviewed-client.py --dry-run
multithread agent muse register Buddy --client /absolute/path/to/reviewed-client.py
multithread agent muse list
multithread agent muse project Buddy --repo /path/to/project
multithread agent muse check Buddy --repo /path/to/project
```

For a task whose sharing scope is authorized, prepare a narrow text file outside
Git, then inspect the adapter's retained packet before sending. The exact
packet and task ID are the recovery handles:

```sh
multithread agent muse prepare Buddy /path/to/task.txt --repo /path/to/project
multithread agent muse send Buddy /path/to/retained-packet.json --repo /path/to/project
multithread agent muse status Buddy TASK_UUID --repo /path/to/project
multithread agent muse replies Buddy TASK_UUID --repo /path/to/project
```

`register` does not run the adapter or contact the agent. The current Buddy
adapter prepares locally, while another adapter's `prepare` may have effects;
review its implementation before use. `send` may contact the agent. Check that
the packet directory is Git-ignored and review its exact payload and destination.
The router reports an uncertain send when the adapter does not return a valid
receipt; inspect the retained packet and remote state before retrying. It does
not interpret a returned body as a completed task. On adapter failure, a
bounded tail of its stderr is shown as private diagnostics; keep that output
out of public reports and inspect it when a send outcome is uncertain.

## Identity and availability

Every Muse agent has a nickname. Identify the intended agent by that nickname
in the task and resolve it to one reviewed adapter in the owner's environment.
A nickname or project thread is a routing label; the transport must authenticate
the actual actor. A project is available only when its adapter is configured,
the intended repository identity is established, sharing is authorized and any
requested Git access is verified from that agent's runtime. A successful health
check or self-reported presence does not prove task pickup.

The agent's code access and the bridge's message access are separate. A remote
GitHub read sees pushed commits, not an uncommitted working tree. Give the agent
the exact pushed revision or a scoped, explicitly shareable packet of local
facts. A private repository needs a separate grant for each agent that will
read or write there. Projects with different owners, agents or privacy
boundaries need transport-enforced isolation and distinct credentials;
client-side thread names are insufficient.

## Task lifecycle

| Observation | Supported conclusion |
| --- | --- |
| Prepared local packet | The task is reviewable; nothing was sent. |
| Stored transport receipt | The bridge accepted the exact packet; consumption is unknown. |
| Task-bound acknowledgement | The agent has custody; execution and completion are unknown. |
| Task-bound lease, heartbeat or checkpoint | Work state is observable to the extent the adapter reports it; a stale or expired lease alone does not prove an external effect did not happen. |
| Task-bound artifact | The agent posted content; assess substance, provenance, completeness and duplicates before treating it as a contribution. |
| Assessed contribution | The initiating project records accepted, rejected and unresolved findings against its own evidence. |

Use stable idempotency keys and retain the exact packet across uncertain sends.
Before any replay or worker takeover, inspect existing receipts and external
effects such as branches and pull requests. A worker that stopped may still
have acted. Split answers need explicit part identities or continuation
markers and validated reassembly; a probe, filler body or duplicate artifact
must not close the research question. A generic actor-wide presence row is
never a substitute for task-bound status.

Long work can overlap independent work by the initiating agent. If the
initiating session ends, keep the exact task ID and decision awaiting it in the
project handoff. A bridge cannot wake or resume an ended Codex or Claude task
merely by posting an answer; a later active session must retrieve and assess it.

## Integration boundary

The optional `multithread agent muse` router selects a registered adapter by
nickname and verifies its pinned bytes before each operation. It does not
implement a bridge, task custody, remote worker recovery or a generic
completion claim. An owner without a compatible installed release can use a
separately reviewed client directly. Do not pass an asynchronous bridge task
to `multithread peer` or record it as a native peer result. The adapter owns
transport credentials and remote task state; Multithread's local ledger
retains its existing claims, signals and acknowledgement rules. Neither layer
grants Git, provider, OS, publication or spending authority.

The router is a first step toward first-class agent support. A future adapter
contract should make authenticated actor identity and task-bound state
machine-readable across implementations, including useful artifact validation
and a completion cue for a later active session. The present bridge's client
supports one owner and one agent; another nickname must not be registered
against that same shared credential as if it were isolated. Acceptance for
broader support should exercise two projects, absent configuration, multiple
nicknames with separate trust boundaries, lost send responses, malformed or
segmented artifacts, stopped workers and handoff after the initiating turn ends.
