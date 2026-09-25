---
name: muse-agent
description: Use a configured Meta Muse agent by nickname for authorized asynchronous collaboration in a Git project. Apply when asked to prepare a task, check availability, or assess a task-bound reply; no Muse agent is required for ordinary Multithread use.
---

# Muse agents with Multithread

Meta's Muse agent is an optional collaborator identified by its nickname. On a
release that exposes `multithread agent muse`, register one reviewed,
account-owned adapter for that nickname; it then serves approved projects.
The nickname is a routing label, not authentication or permission. Follow the
project's instructions for the installed adapter and the
[Muse agent guide](../../docs/MUSE-AGENTS.md) for the shared outcome contract.
Do not assume every Multithread project has a Muse agent or that another
project's access applies here.

Check the installed command with `multithread agent --help`; an older release
may need the project's existing external client until an update is explicitly
selected. Check the intended Git repository, sharing scope and adapter
availability.
Prepare a narrow, inspectable task and retain its exact idempotency identity
before sending. A transport receipt proves storage; an acknowledgement proves
custody; task-bound worker activity requires its own evidence; a returned
artifact is a report to assess. Inspect an uncertain outcome before retrying
the same packet. Check whether the agent can read the pushed revision; local
uncommitted files are not available through a GitHub repository read.

If the initiating turn ends before the answer arrives, preserve the exact task
ID and dependent decision in the project's handoff. An external artifact does
not automatically resume an ended task. Keep credentials, raw private
conversations and unrelated project material out of packets and Git. A bridge
task confers no Git, provider, publication or remote execution authority.
