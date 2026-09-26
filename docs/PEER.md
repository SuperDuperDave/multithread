# Call another native provider and continue your task

With `multithread peer`, a coding agent calls another native provider, receives
its answer as the command result, and continues the same task. There is no
second application or message-forwarding service.

Start with [setup and native trust review](SETUP.md), then request a
[first collaboration](#first-collaboration), [assess its result](#read-the-result-before-continuing)
and choose any [exact-session follow-up](#follow-up-in-the-same-native-session).
The [compatibility notes](SETUP.md#compatibility) cover older releases and `relay`.
For diagnostics, measurement definitions, live input and native evidence, use
the [peer reference](PEER-REFERENCE.md). The [source entry](#use-the-source-entry)
is available for reviewed development work.

See the
[project page](https://mainthread.ai/work/multithread/) for the Multithread introduction.

Use it when a second perspective is worth the extra provider usage. Either
agent can call Claude or Codex. The initiating agent keeps the continuing goal.
This command does not control an already-open desktop window or wake an
independently idle session.

## Choose the contribution you need

Collaboration is optional by default. Use it when a different perspective could
change a consequential decision, expose an overlooked assumption, or help when
repeated attempts are not producing a satisfactory result. Complexity is one
reason to collaborate; uncertainty about a design or a stalled investigation
can be equally useful reasons. Extra calls consume provider usage, and neither
agreement nor using two model families guarantees a better answer or lower
total effort.

| Situation | A useful contribution | How the main thread assesses it |
|---|---|---|
| A sensitive change or architectural choice | Challenge the assumptions, identify failure cases and inspect the relevant code/tests. | Check the cited evidence and resolve material findings against the task's acceptance criteria. |
| Repeated attempts are not moving the task forward | Examine the retained attempts and propose a different explanation or approach. | Test the explanation against existing evidence before repeating or expanding the work. |
| A design works but does not achieve the intended look and feel | Critique the rendered evidence the peer can actually inspect against the user's references and feedback; state what it could not see and propose a small number of distinct directions with rationale. | Inspect the result in its intended medium and obtain the required user acceptance. Agent agreement does not establish visual quality. |

Give the peer the goal, relevant evidence, scope and expected contribution.
Ask it to challenge the approach where warranted, rather than agree with the
main thread. Share only authorized material through supported tools; if the
peer cannot inspect a rendering or other necessary evidence, keep that limit
visible. Use a deliberate [same-session follow-up](#follow-up-in-the-same-native-session)
when discussion would resolve a concrete question. Stop when the contribution
has been assessed or the remaining work needs a different input; a thread
allocation is a ceiling, not a quota to fill.

An explicit task requirement takes precedence over the optional default. For
example, "have Claude review this before we merge" requires that review; a
suggestion that Claude is available does not. Permission to use a provider and
a requirement to obtain its contribution are separate. Optional use still
requires authorization for the actual provider usage and sharing scope.

## Before calling

Use a functioning provider environment and an enrolled checkout. Follow
[setup](SETUP.md). The installation also includes `multithread launch`
for interactive Codex/Claude launches; `multithread launch claude --json` only prepares
that interactive launch plan.

The peer flags are available in Claude Code 2.1.267; `--permission-prompts none`
requires 2.1.259 or later. Version compatibility is not a guarantee of native
tool readiness. Review the chosen repository and provider configuration first:
Claude print mode loads normal instructions, hooks, skills and configured MCP
servers, and does not show its interactive workspace trust dialog.
The Codex adapter uses the stable App Server interface. The v0.3 native
source-entry observations use Codex 0.153.4 and Claude Code 2.1.269. Codex hook
trust remains a separate native review: use `multithread launch codex` for the selected
checkout, open `/hooks`, and review the exact generated commands. A changed hook
definition can need review again. Listing a trusted hook does not prove it ran.

Multithread inherits the provider's normal environment, sign-in and permission mode.
It never selects bare mode, copies credentials, changes permission rules or
disables native sandboxing. Subscription sign-in can be used; an API key or
other provider configuration can change the effective billing path. Check it
through the provider's normal interface. Multithread does not certify account billing.
[Claude programmatic use](https://code.claude.com/docs/en/headless) ·
[Authentication](https://code.claude.com/docs/en/authentication)

## First collaboration

After [setup preparation](SETUP.md#check-readiness-or-enroll-another-project),
paste this into your coding agent in the enrolled repository. It authorizes one
native call and its scoped provider usage. Preparation alone does not establish
native sign-in, hook trust or tool execution; complete any required
[native trust review](SETUP.md#review-native-trust) through the provider's normal
interface before calling.

```text
Use Multithread for one scoped, read-only collaboration in this Git repository.
Follow https://github.com/SuperDuperDave/multithread/blob/main/docs/PEER.md.
Verify the Git root/common directory and installed readiness. Choose an existing,
functioning Claude or Codex provider, preferably the other provider from yours.
I authorize one native peer call through my existing provider installation and
access, sharing only the repository code and context needed for this review,
with its normal provider usage. Keep existing sign-in, trust and permission rules.

Give the peer my current unresolved project change or question, its intended
result and acceptance criteria, and the exact revision or relevant working-tree
diff. Limit inspection to the files and existing tests needed to assess that
scope. If I have no unresolved project task, choose one documented command or
feature and trace its implementation and existing tests.
Answer the question and return up to three concrete defects or documentation
mismatches, with file/line evidence and why they matter, or explicitly report no
material findings. State what you inspected and any uncertainty. Do not edit
project files, run programs from the project, install anything, invoke another
peer, commit, publish, or acquire/release claims or acknowledge handoffs.

Use the installed ~/.local/bin/multithread peer command with a small task file
or stdin; ~/.local/bin/relay is compatible on older installations. Private local
call evidence is authorized. Review the dry-run, then execute that same scoped
call with --json and a 600-second timeout. A dry-run is preparation, not the call.
Keep credentials, account files and unrelated private material out of the task.

Read the receipt and retained result before continuing. Independently inspect
the cited files and assess each finding; do not apply changes. Finish with the
findings you accept or reject, why, any remaining work, the result state,
evidence directory and any verified session ID. Distinguish an
unavailable provider, a refused action, an uncertain outcome, a returned answer
and a completed review. Do not infer completion from exit status or returned
text, widen permissions, or automatically retry an uncertain call.
```

A useful first result names the reviewed question or feature, returns an
evidence-backed assessment, and shows how the initiating agent checked it.
“No material findings” can satisfy the review if the scope was actually inspected.
A denied tool or missing observation remains visible; useful partial findings can be assessed
without claiming the whole review completed. This task needs no claim or test
handoff, and it does not itself verify hook delivery or a ledger workflow.

## Send a scoped task

Write a small UTF-8 task file with the goal, scope and acceptance criteria. For
example, after publishing a commit-backed Multithread handoff for Claude:

```text
Review Multithread handoff <sequence> for work <work-id>.
Inspect the exact commit and relevant tests. Work only in this checkout and
within the approved review scope. Report actionable defects with evidence,
or explicitly say no material findings. Acknowledge the handoff after review;
release any claim you acquired. Report anything that remains incomplete.
```

Use the installed launcher's actual path, normally:

```sh
~/.local/bin/multithread peer claude --repo /absolute/enrolled/reviewer-checkout \
  --task-file review-task.txt --json
```

Select `codex` in the same command to call Codex. Either initiating provider can
use these commands through its ordinary shell tools. The caller's provider does
not determine the peer's provider.

`--json` returns structured output **and executes the call**. Add `--dry-run`
to inspect the task hash and native arguments without starting a provider or writing
call evidence. Unlike `multithread launch`, an explicit peer call has no additional
interactive confirmation prompt. Provider usage must already be authorized.
Task text goes through stdin as data, never shell evaluation or command-line
prompt interpolation. Use `--task-file -` to supply it from stdin directly.

The task limit is 64 KiB. Larger artifacts belong in the repository and can be
referenced by the task. The default timeout is 600 seconds; use `--timeout`
with an integer from 1 through 3600 seconds to adjust it for the work.
Multithread imposes no native turn cap by default. Supply `--max-turns` with an
integer from 1 through 3600 for an explicit Claude agentic-turn limit. Source
inspection and tool use can consume this budget before a final answer; it does
not reserve a final-answer turn. Codex performs one native turn with its normal
tool loop. Choose bounds proportionate
to the task so the peer has time to inspect evidence and produce a useful
answer. Multithread makes one invocation and never automatically retries it.

For Claude, `--model opus --effort high` requests those settings for this call
without changing the account profile. The dry run and private receipts preserve
the request; a clean follow-up preserves both flags. Native streaming can report
a model name, though an alias may resolve to another literal name. Effective
effort is not verified. These options are not available for the Codex peer.

For a review of working-tree changes, `multithread peer packet --repo
/absolute/repo --path src/example.py --output-file /absolute/new/packet.txt`
creates a private, bounded diff packet with a digest. Repeat `--path` for the
explicit files or directories to include and use `--base COMMIT` for a reviewed
base revision. The packet includes tracked text changes through the working
tree; it refuses untracked or binary changes in the selection. **Inspect the
packet before giving its path to a peer.** It does not scan secrets or change
the peer's tool permissions. [Packet details](PEER-REFERENCE.md#freeze-a-review-packet).

For a concrete example of a review contribution and its limits, see
[a peer review that improved v0.4.6](examples/PEER-REVIEW.md).

Normal permission rules remain in force. When a tool needs approval that this
noninteractive call cannot obtain, Claude denies it and reports the denial.
For Codex, configured native approval review remains in effect; requests routed
to this unattended client are declined. Multithread never supplies extra permissions.
Multithread retains useful partial output. The initiating agent should report any
required human decision; it must not silently widen permissions to finish.

If collaboration is optional, continue independent authorized work and disclose
any missing contribution when assessing the result. If the task requires peer
work, keep that requirement visibly incomplete in the task's existing plan or
handoff, with the blocker and useful partial findings. Continue work that does
not depend on it, but do not silently substitute a solo review or claim the
required collaboration completed. The first-collaboration prompt above requests
a peer review as part of its result. A stopped provider turn is not a completed
task, and an unavailable peer does not itself justify changing approval policy.

## Read the result before continuing

| Field or state | Meaning |
|---|---|
| `returned` | A matching final provider result arrived. Assess its content and `needs_attention`; a later streaming-process or recording fault does not erase an observed answer. This is not acceptance of the task. |
| `provider_error` | A matching native error, interruption or limit result. Legacy Claude calls also classify a nonzero process exit this way. Useful text and bounded `provider_errors` remain available. |
| `unavailable` | Preparation or spawning failed; `provider_started` and `unavailable_stage` identify what was reached. |
| Refused action | A preparation refusal or native permission denial prevented that action. Inspect its stated reason and the call result; refusal is not a successful operation or proof that all other work failed. |
| `uncertain` | The call started but timed out, was interrupted, or lacked a valid matching result. External work may already have happened. |
| `needs_attention`, `permission_denials`, `terminal_reason` | Check these even when the process exits zero. Tool denials and stopped/deferred work can accompany useful output. |
| `session_id` | The verified native session identity: a UUID for Claude, an opaque native thread ID for Codex. Claude's requested UUID is recorded before launching; Codex assigns a fresh thread ID during initialization. Resume always targets the exact supplied identity. |
| `relay_acknowledgement`, `workflow_completion` | Always `not_checked` by the helper. Inspect actual ledger state and artifacts separately. |

Each call retains a private directory containing its request, task, native
stdout/stderr and interpreted result. Use `--output-dir /absolute/new/directory`
to choose a durable location; an existing directory is refused without changes.
Redirect command stdout outside that directory; creating a file there first
would make the evidence directory exist before the call begins.
The default is a retained temporary directory, subject to the OS's cleanup
policy. Its location is printed before launch, together with Claude's requested
session UUID or a note that Codex will assign the identity.

Keep these files private: native output and task text can contain sensitive
project information. Nothing is uploaded or published by Multithread's recorder.

While waiting, the owning call prints a content-free diagnostic to stderr about
every 30 seconds: elapsed time against the chosen call limit, its observed
stage, and input availability when known. An advertised input target does not
establish native acceptance. These messages show that the caller is waiting,
not that the provider is making progress; silence does not establish a stall.
The terminal or calling application may buffer or hide stderr. `--json` stdout
still contains only the final result. Ordinary Claude calls retain their normal
final-JSON mode; waiting feedback does not inspect native transcripts or change
permissions, deadlines or retry behavior.

Add `--stream-progress` to a Claude call when intermediate native observations
would help. It uses Claude's event stream without opening a live input channel;
`--live-input` also uses that stream. Feedback can report the last observed
event and bounded frame counts, never task text or proof of continuing progress.
The default final-JSON call cannot observe intermediate provider activity.

Where a submission stage is unobserved, the waiting message says so. Ordinary
Claude calls distinguish writing the task from waiting after a complete pipe
write; opted-in streaming Claude calls now expose the same distinction while
waiting. If input closes early, a matching successful native reply cannot establish
an answer to the complete task: the call is `uncertain`, needs attention and
retains useful text as `partial_result` and in raw output. A native refusal
remains `provider_error`; interrupted calls remain uncertain. Inspect the
unwritten-byte observation and retained work before any follow-up, without
automatically resending. The support report includes these delivery observations
without exposing task or answer text.

After timeout or an uncertain result, inspect the local evidence and durable
work before deciding whether to continue. If only `requested_session_id` is
recorded, check native session state before choosing resume or a fresh call;
the requested identity does not confirm that a resumable session exists.
Multithread stops only the process group
created for that call. Killing a process does not prove that prior external
operations were undone. Never release someone else's claim to tidy the result.

For detailed interpretation, see [receipt fields and capture limits](PEER-REFERENCE.md#receipt-fields-and-capture-limits)
and [usage measurements](PEER-REFERENCE.md#usage-measurements).

## Follow up in the same native session

After assessing the previous result, choose whether further provider usage is
warranted and authorized. Resume when the prior investigation helps answer the
remaining question; start fresh when independent judgment or a different scope
is more useful. Use the exact verified `session_id` and the same checkout for a
deliberate resume. Claude uses a session UUID:

```sh
~/.local/bin/multithread peer claude --repo /absolute/enrolled/reviewer-checkout \
  --resume <exact-session-uuid> --task-file follow-up.txt \
  --output-dir /absolute/private/new-peer-follow-up --json
```

For Codex, select `codex` and pass its returned `session_id`
unchanged to `--resume`. Treat that thread ID as opaque; do not convert it to a
UUID or substitute `native_session_id`.

Keep a concise continuation packet in the task's existing private record. After
checking the prior outcome and session ownership, use it to write `follow-up.txt`:

```text
Provider and verified session_id: <provider> / <identity from prior result.json>
Same enrolled checkout: <absolute checkout path>
Prior receipt and outcome: <private result.json path; state and any partial work>
Accepted findings: <independently checked findings and rationale>
Changed source/evidence: <new revision or diff; relevant new observations>
Remaining question and acceptance criteria: <what this contribution must resolve>
Authorized scope and limits: <permitted inspection/actions and explicit exclusions>
```

After a clean return and completed owned cleanup, the private JSON result can
include `follow_up_preparation.argv_prefix`. This argument array preserves the
selected Multithread launcher, provider wrapper, verified session, checkout and
call bounds, using the source entry when supplied. It includes `--dry-run --json`
and ends with `--task-file`: append the
path of the new follow-up task, optionally followed by a fresh `--output-dir`.
Keep arguments separate when executing it, or quote each argument for the shell.
It reuses neither the prior task nor its output directory. Review the preparation,
which starts no provider, then remove `--dry-run` only for the authorized call.
The prefix is absent for uncertain or adverse outcomes; inspect those outcomes
before deciding whether to use the manual resume command above. A prepared
invocation does not establish session ownership or completion of the prior task.

Use a new `--output-dir` for each call to retain its evidence durably; an existing
directory is refused. Preserve ordinary authorization, sign-in, trust and
permissions. Possessing a session identity does not authorize taking it over.
There is no latest-session lookup or automatic concurrent resume. A fresh call
without `--resume` creates a fresh session.

The peer process can end while the provider keeps the native conversation.
History retention, compaction and prompt caching remain provider behavior;
resume does not guarantee a cache hit or unchanged context.

## Prepare a support report from an existing call

For investigation, prepare a [read-only support report from a retained call](PEER-REFERENCE.md#prepare-a-support-report-from-an-existing-call).
That reference covers the command, selected diagnostics and reporting limits.

## Update a running peer

For an owned call that is still running, see [live input and its receipts](PEER-REFERENCE.md#update-a-running-peer).
That reference covers provider-specific targets, input outcomes and limits.

## Coordination and verification scope

The helper obtains the existing public hook configuration through the admitted
installed worker. The provider itself runs outside that worker's restricted
environment, using its ordinary tools and sandbox. Hooks can report unavailable
observations without blocking Claude; `hook_delivery` therefore stays unknown
until independently observed. The helper never claims, acknowledges, commits,
merges or releases on a participant's behalf.

See the [automated checks and scoped native observations](PEER-REFERENCE.md#coordination-and-verification-scope)
for what has been exercised and what remains unestablished.

## Use the source entry

For reviewed development work, see the [source-entry commands and compatibility limits](PEER-REFERENCE.md#use-the-source-entry).
