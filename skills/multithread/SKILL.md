---
name: multithread
description: Use Multithread for authorized Codex–Claude collaboration in a Git repository. Apply when asked to set it up, consult a native peer, coordinate a handoff, or recover a Multithread call; ordinary single-agent work does not require it.
---

# Multithread

Multithread coordinates cooperating agents across sessions and worktrees. It does
not grant Git, OS, provider, billing, or publication authority. Follow the user's
task and this project's instructions; using another provider and sharing project
context require authorization for that scope. A second perspective is optional
unless the task requires it.

Use the installed, verified launcher and its `--help` for exact syntax. Do not
run a checkout-owned executable in place of the installed launcher. For first
installation, enrollment, readiness, or native trust review, follow the current
[setup guide](https://github.com/SuperDuperDave/multithread/blob/main/docs/SETUP.md).
`multithread setup --repo <checkout> --check` reports preparation and whether
Codex lists the Multithread hooks as trusted, not provider authentication, hook
delivery, tool capability, or a completed collaboration.

Choose the interaction that fits the task:

- For a scoped second perspective returned to the initiating agent, use
  `multithread peer --help` and the [peer guide](https://github.com/SuperDuperDave/multithread/blob/main/docs/PEER.md).
  Give the peer the goal, relevant evidence or revision, scope, acceptance
  criteria, and the contribution sought. Challenge an assumption or inspect a
  concrete question; avoid sending whole project histories by default. A peer
  dry run inspects the invocation; `peer ... --json` executes it.
- For durable coordination between separate sessions and worktrees, use the
  installed `status`, `brief`, and command help with the
  [provider walkthrough](https://github.com/SuperDuperDave/multithread/blob/main/docs/PROVIDERS.md).
  Hooks provide this session's coordination identity and bounded pending state.
  Respect exact claim ownership, immutable commit handoffs, explicit ACKs,
  documented manual wakes, and the operator's Git responsibilities.
- If the user has a configured Muse agent, identify it by nickname and use the
  optional [Muse agent skill](../muse-agent/SKILL.md). Its bridge has an
  asynchronous custody and reply lifecycle; it is separate from native
  `multithread peer` and from the local coordination ledger. Check whether the
  installed release exposes `multithread agent muse`; older releases do not.

Before retrying an uncertain operation, inspect its receipt and durable state.
A written task is not proven consumed; a returned answer is not automatically a
completed workflow. Assess peer findings against source and acceptance criteria,
and report accepted, rejected, or unresolved contributions with the result state
and available evidence. Do not infer missing observations from exit status.
For retained call diagnostics, use `multithread peer report --help` and the
[support guide](https://github.com/SuperDuperDave/multithread/blob/main/docs/SUPPORT.md);
keep raw tasks, transcripts, credentials, and unrelated private context out of
shared reports.

Keep project-specific preferences about when to collaborate, what may be shared,
and how contributions are recorded in that project's own guidance. This skill is
a route to the installed contract and maintained public docs, not a replacement
for either.
