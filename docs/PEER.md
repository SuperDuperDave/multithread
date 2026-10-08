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
independently idle session; to point an open Codex conversation or Claude Code
session at new work, [wake it](#wake-an-existing-conversation).

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

Select the checkout with `--repo` either before `peer` or in its arguments.
Conflicting selections are refused before preparing or starting the call.
Use one selection when moving from a main checkout to a linked worktree.

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
source-entry observations use Codex 0.153.4 and Claude Code 2.1.269. With
[user-level hooks](SETUP.md#connect-every-session) installed, a peer call uses
them and passes no copy; trust them once by hand in `/hooks` or with
`multithread hooks trust`. Otherwise Codex hook trust is a separate native
review: use `multithread launch codex` in any enrolled checkout, open `/hooks`,
and review the exact generated commands. One review covers every enrolled
checkout and worktree; a changed hook definition can need review again. Listing a trusted hook does not prove it ran. Before any
task, a Codex peer call refuses when a hook is not trusted and names each
unready event with the one step that resolves it; `setup --check` reports the
same listing. Launch and the manual `/hooks` review are the person's steps;
`multithread hooks trust` is available to the agent when the person chose
agent-assisted trust. Otherwise the agent reports the refusal and its remedy,
and never runs `launch`. Leave
`--multithread` unset; a Codex call accepts only the account launcher setup
prints, gives another path to it that spelling, and refuses a different file.

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
If the call refuses because hooks are not ready, report its remedy to me; do not
run multithread launch or change hook trust.

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

Publish that handoff only when the task authorizes a ledger write, using the
initiating session's exact coordination identity from its Multithread context:

```sh
~/.local/bin/multithread --repo /absolute/enrolled/author-checkout signal work.handoff \
  --agent codex --session EXACT_AUTHOR_SESSION --work-id review-example \
  --target claude --scope src/example.py --summary 'Review the committed change' \
  --commit HEAD
```

`--commit` resolves a revision in the checkout selected by global `--repo`
(the current directory if omitted) to its immutable full Git OID and records a
bounded commit capsule. That checkout also selects the enrolled ledger.
Alternatively use `--artifact git:FULL_COMMIT_OID` or
`--artifact sha256:FULL_64_HEX_DIGEST` for reviewed frozen bytes. A pathname,
branch name, or bare digest is not an immutable artifact. The digest identifies
bytes; it does not deliver the file or authorize sharing it. Do not combine
`--commit` with `--artifact` or `--commit-oid`.

For source owned by another repository, keep `--repo` pointed at the intended
ledger and use `--artifact git:FULL_COMMIT_OID`, optionally with
`--commit-oid FULL_COMMIT_OID` metadata. Those options do not look up, fetch or
deliver the source. Include its owning checkout and repo-relative file path in
the bounded summary so the recipient can locate and inspect the immutable bytes
before acknowledging. A commit's presence in the ledger checkout is not required
for this form of handoff.

Use both `--agent` and `--session` for ledger mutations unless their exact
values are already supplied by `RELAY_AGENT` and `RELAY_SESSION`. These identify
the caller, not the peer's native resume session or a role name. Replace the
example identity with your own; do not invent or borrow another session's ID.
To address one reviewer session, use `--target claude --target-session EXACT_REVIEWER_SESSION`;
`--target claude` addresses Claude generally. See [the session inbox](#inspect-pending-work-and-role-handovers).
Exact recipients are stored as a compact JSON pair, such as `["claude","reviewer-session"]`.
For direct event emission use a string containing that pair as `target`; bare labels containing
colons retain their existing generic-agent meaning.
`signal --target` addresses an agent identity; it does not resolve a role binding.
For example, `wake reviewer` uses the role's current native route, while a handoff
for one Codex reviewer uses `--target codex --target-session EXACT_REVIEWER_SESSION`.
Inspect `roles reviewer --json` to establish the current binding. A Codex binding's
`thread` is its conversation ID; a Claude binding's recorded holder is not proof
of the recipient's session identity. Obtain that identity from the recipient's
actual coordination context before targeting it. A handoff's stored recipient
does not follow a role handover.
The [first read-only collaboration](#first-collaboration) needs no handoff.

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
call evidence. A dry run does not check readiness, such as Codex's hook trust,
since that starts the provider: it reports `readiness: not_checked` with the
`setup --check` command that does, in `readiness_check`. A real Codex call checks
its hooks before submitting any task. Unlike `multithread launch`, an explicit peer call has no additional
interactive confirmation prompt. Provider usage must already be authorized.
Task text goes through stdin as data, never shell evaluation or command-line
prompt interpolation. Use `--task-file -` to supply it from stdin directly.

A fresh Claude dry run generates an illustrative session UUID for its plan;
it does not reserve that session. Executing the command generates a new UUID.
Use the actual call's verified `session_id` for any follow-up, rather than the
dry-run UUID. A dry run with `--resume` keeps the exact identity you supplied.

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

`--model` and `--effort` request settings for this call without changing the
account profile. Claude receives them as its native options, as in `--model opus
--effort high`, and accepts `low`, `medium`, `high`, `xhigh` or `max`. Codex
receives both with the turn the call starts, and each Codex model advertises its
own efforts. Without a flag, a new call uses the provider's configured settings.
A resumed Codex thread instead keeps the settings it last ran with, including an
earlier call's override, because Codex applies a turn's settings to the thread's
later turns: omitting a flag never resets it, and changing only the model keeps
the thread's effort.

Codex accepts any effort; the provider then rejects a pair the model does not
advertise, or runs it unverified. So when a call names a model or an effort,
Multithread checks the pair the turn will run with, the requested settings
completed by the thread's own, before submitting anything. It reads Codex's model
list, hidden models included, and treats the efforts listed there as the contract.
When the listed model does not advertise the effort, the call is refused with
those efforts named and the flag to change. That includes an effort the provider
would accept but Codex does not list, such as `none` where Codex omits it. A model
Codex does not list, which may be an alias or another provider's model, proceeds
unverified, as does a pair whose kept setting the thread does not report. If the
list is unavailable, the call proceeds and the provider decides. `settings_check`
records which of these happened. The dry run and private receipts preserve the
request; a clean follow-up preserves both flags.

Claude reports a model name when its native stream starts, and in any output
mode its usage names the models that have served the session; when that is
exactly one, the receipt records it. An alias may resolve to another literal name, and
Claude's effective effort is not verified. Codex reports a
thread's settings when it opens the thread, so during the call a requested
setting stays `unknown`; Codex confirms it when a later call resumes that thread.
The receipt records what Codex reports, including a model it reroutes the turn
to, as `model_observation` and `effective_effort`.
[Observation details](PEER-REFERENCE.md#receipt-fields-and-capture-limits).

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
  --output-dir /absolute/private/new-peer-follow-up --dry-run --json
```

For a manual resume, preserve the previous call's selected provider entry
(`request.json`'s `argv[0]`) with `--provider`, and any explicitly reviewed
launcher selection. Retain or deliberately revise the prior timeout, turn limit,
model, effort and input/progress options; the example uses defaults. Review the
dry-run, then remove `--dry-run` for the authorized call. A dry run checks neither
readiness nor the output directory.

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
path of the new follow-up task, followed by a fresh `--output-dir` to retain
the next call's evidence in your chosen private location.
Keep arguments separate when executing it, or quote each argument for the shell.
It reuses neither the prior task nor its output directory. Review the preparation,
which starts no provider and does not check the output directory, then remove
`--dry-run` only for the authorized call.
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

## Wake an existing conversation

`peer` starts its own native session. To reach a Codex conversation that is
already open, such as one a person is working in, bind it to a role once, then
send it short wakes through the shared Codex daemon's control socket in your
Codex home. A wake carries a pointer to the work, never the work itself:

```sh
multithread bind operator --thread <conversation uuid> --scope project/workstream \
  --charter 'Review this workstream and resolve its open questions' \
  --agent claude --session <session>
multithread wake operator --ref /absolute/path/task.md --agent claude --session <session>
```

Without an observed sender role, the conversation receives
`Multithread wake from claude: /absolute/path/task.md`.
A ledger sequence also works as `--ref`; the message then names this checkout's
ledger. The binding and every attempt live in the ledger of the checkout you
run them in, or of `--repo`.

`--ref ledger:/absolute/checkout#N` names sequence N in another checkout's ledger.
A conversation stays in the ledger where it started and only the wake crosses:
the recipient reads, acknowledges and replies there with
`multithread --repo /absolute/checkout ...`. `signal KIND --wake --target A
--target-session S` records the signal here, then wakes S through its binding
here or, when S holds a role in another checkout, through that checkout's
binding, pointing back at the signal. `bind` records where each recipient holds
a role in an account index (`wake-recipients.json` beside the ledgers' state:
`RELAY_HOME`, else `~/.local/share/relay`; owner only); the index is only a hint, and a wake goes out only when that checkout's
own binding names the exact session. Running `bind` again records an existing
binding; `multithread wake-index rebuild` adds every enrolled checkout's
bindings at once, with one read per ledger and no other write. It removes
nothing and reorders nothing `bind` recorded, since each use is verified, and
names any ledger it couldn't read. A recipient bound nowhere reachable is reported `NOT BOUND` with the
command that wakes it by hand. A ledger is named by its primary checkout, which
outlives the linked worktrees that share it. As with a local rebind, sending the
same signal again after its recipient binds in another checkout wakes it there
again. The recipient's brief in its own checkout lists
wakes pointing at other ledgers from the last 24 hours, so a lost wake does not
leave it unaware. An older Multithread reads such a ledger's events, briefs
and inbox, but refuses its wake bindings until this release is reinstalled.

On unreleased main, an exact sender binding adds compact context:
`Multithread wake from claude (project/reviewer): /absolute/path/task.md`.
The source is the command's original current directory; `--sender-repo` selects
another source checkout, independently of the recipient's `--repo`. A Codex
sender matches its exact conversation, while a Claude sender matches the binding's
recorded owner agent and session. Pausing incoming wakes does not remove a held
sender role. If several roles match, `--sender-role reviewer` selects one and
requires that exact source binding before recording or sending.

Automatic lookup keeps the plain header when the sender is unbound, ambiguous
or unavailable. JSON distinguishes those states through `sender_state`; an
unavailable source is never reported as an empty ledger. A safe project label
comes from the shared checkout's directory name, so linked worktrees use the
same label. Other directory names produce role-only context. The original
attempt retains the source ledger, role, generation and nullable project under
`sender`; later rebinding cannot relabel it or change duplicate suppression.
These labels describe an observed binding and grant no authority. A role in a
filename can identify its author, but a forwarded file's author can differ from
the sender. No filename convention is required for wake attribution.

- **Bind** asks the daemon whether the conversation exists, shows its directory
  and status, and records the binding; its sequence is the binding's
  generation. It refuses an unknown conversation, an unreachable daemon, or a
  role already bound elsewhere; `--replace` alone does not authorize moving
  another holder or recipient. See [role handovers](#inspect-pending-work-and-role-handovers).
  When the conversation has
  left no events in the ledger of its own checkout, bind warns: that
  conversation gets no Multithread brief, so each wake must carry a task-file
  path. `multithread bind` alone lists bindings and their last wake;
  `multithread roles` also shows scope, charter and holder;
  `multithread unbind`, `pause` and `resume` change one under the ownership rules
  below.
- **Wake queues by default.** The message starts a new turn when the
  conversation is loaded and eligible, or after its current turn if eligible.
  An unloaded conversation can accept a queue entry without starting a turn;
  an interrupted turn can suppress automatic pickup even when runtime status
  is idle. Before queue submission, wake makes one bounded, metadata-only
  runtime read. JSON exposes this dated snapshot as `recipient_runtime`,
  separate from the latest historical turn in `recipient_state`. Missing or
  malformed runtime state is unknown. A loaded snapshot never guarantees pickup.
  For an unloaded conversation, the result keeps the accepted queue UUID and
  directs the owner to load its existing task with execution settings preserved.
  Wake never resumes, force-starts, reorders or resends that queue entry.
  Cold resume may use current configuration, so omitting overrides does not
  guarantee identical permissions or instructions. Approval/input waits and
  deliberate interruptions remain the recipient's responsibility.
  Use `--steer` only for
  news about the recipient's current work: it joins the running turn at its next
  input boundary, and queues when no turn is running. If Codex refuses the steer
  before accepting it, the wake queues once with the same message id. Any other
  error, or a reply without the expected turn's receipt, is `UNCERTAIN` and
  nothing is queued.
- Each attempt is recorded before it is sent and concluded after. The default
  message id comes from the ledger, role, binding generation and reference, and
  for a task file from its bytes too: the attempt records the file's sha256 and
  size, matching the content-addressed `--artifact sha256:<digest>` that a
  [work signal](#send-a-scoped-task) can record explicitly. A file that changed
  is a new message; the same unchanged file
  sent twice reports `ALREADY SENT` and names the `--id` that sends it again on
  purpose. A rebind starts a new generation, so message ids start over: a file
  already sent under the old binding can be sent again. `--dry-run` decides and
  sends nothing.

On unreleased main, callers can require the binding they inspected before a
wake is admitted. Supply the complete provider-specific expectation:

```sh
multithread wake reviewer --ref /absolute/path/task.md \
  --expect-generation <binding sequence> --expect-provider codex \
  --expect-thread <conversation uuid> --agent claude --session <sender session>
multithread wake reviewer --ref /absolute/path/task.md \
  --expect-generation <binding sequence> --expect-provider claude \
  --expect-bound-agent <recorded owner agent> --expect-bound-session <recorded owner session> \
  --agent codex --session <sender session>
```

Codex requires generation, provider and exact thread UUID. Claude requires
generation, provider and the recorded binding-owner pair; that pair does not
independently verify who receives the inbox message. A partial or mixed
expectation refuses before the task file is read or the ledger is opened.
A missing or different binding returns `NOT SENT` before recording an attempt,
deduplicating its id or contacting a provider. Read the binding again and
reconcile deliberately; do not retry automatically. Omitting all expectation
flags preserves existing wake behavior. A matching paused binding still refuses.

The comparison and attempt record share one ledger write transaction.
`--dry-run` checks the expectation without recording or reserving a later send;
the real send checks again. Provider transport runs after the transaction and
uses the admitted snapshot. A subsequent pause or rebind cannot cancel that
transport or retarget the attempt. Admission and delivery confer no authority
and do not establish consumption. `--status` cannot take expectation flags.

| Status | Exit | Meaning |
|---|---|---|
| `STEERED` | 0 | Joined the running turn |
| `QUEUED` | 0 | Queued as asked, because no turn was running, or because Codex refused the steer before accepting it: the turn ended, another turn was running, or the running turn can't take a steer |
| `DELIVERED TO INBOX` | 0 | A Claude Code session's inbox took the whole message |
| `DRY RUN` | 0 | Decided; nothing sent or recorded |
| `STATUS` | 0 | Read original-binding attempts and recorded consumption evidence; nothing sent, and recipient reachability/current turn state were not probed |
| `ALREADY SENT` | 3 | This message id went out, or its earlier outcome is unknown; nothing sent |
| `NOT SENT` | 4 | Nothing reached the conversation: the daemon was unreachable, refused to list the conversation's turns, or answered the turn list unreadably; the conversation is unknown; Codex couldn't be found or run, or `codex queue` refused before sending; the inbox is gone, failed its checks or refused the connection; the role is paused or unbound; or the arguments, expected binding, reference or ledger couldn't be used. Follow the stated remedy before deliberately retrying |
| `NOT RUNNING` | 4 | The bound Claude Code session isn't running: no inbox it reported is still owned by its process, and the stored one isn't the process that was bound. It is reachable again at its next prompt; send then with the same id |
| `UNCERTAIN` | 5 | Sent, with no receipt that it arrived: the daemon didn't answer the steer, or `codex queue` didn't finish; the steer drew an unreadable reply, an error that doesn't show it was refused before acceptance, or no receipt for the expected turn; `codex queue` printed no receipt for the bound conversation, or failed after starting; or the inbox connection dropped while sending. The id stays blocked; check with the recipient before sending again |

Every result names the next step, as text or with `--json`.

A daemon or inbox accepting a wake is not the recipient reading it. Use
[wake status and explicit acknowledgement](#inspect-pending-work-and-role-handovers)
to inspect consumption separately. A wake grants no authority beyond the work
it points to. Nothing is sent automatically
when a signal is recorded. The command's tests use a fake daemon and a fake
inbox. Live observations with Codex 0.159.2 exercised the same daemon methods
before this command existed: a queued message into an idle conversation started
a turn within the same second, a queued message waited for a busy turn to end,
and one steered message reached the model at its next input boundary, 108
seconds after Codex accepted it. The duplicate check is Multithread's own; how
Codex treats a repeated `clientUserMessageId` is untested.

### Observe a Codex role without sending

On unreleased main, `observe` reads a bound Codex conversation's runtime, latest
turn and bounded queue identities. Capture the role's generation and exact
thread first, then supply all recipient guards:

```sh
multithread --repo /path/to/repository --json observe reviewer \
  --expect-generation 42 --expect-provider codex \
  --expect-thread a0000000-0000-7000-8000-000000000001 \
  --queue-id d0000000-0000-7000-8000-00000000000a
```

The optional queue UUID nominates the original entry to inspect. It is separate
from `clientUserMessageId`, and never defaults to the latest recorded wake.
Queued input is returned only as canonical byte counts and digests, without its
text or file references. An older original can remain relevant after later wakes.
The digest covers an opaque array of input objects encoded as compact, sorted-key
UTF-8 JSON with unescaped Unicode and no nonfinite numbers. It is a comparison
profile, not a hash of the original wire bytes or validation of each input type.
Queue UUIDs and required client message IDs are checked independently; latest
turn IDs remain opaque strings.

This is an active provider diagnostic. It does not subscribe, load, resume,
start, steer, resend, acknowledge or mutate a queue. It respects paused bindings
and requires the observed recipient to match before and after the reads. Binding
drift or unavailable final validation cannot establish a usable current result.
Even matching guards are not an atomic fence for a later recovery action.

Keep complete scans, bounded partial observations and unavailable or malformed
state distinct. Pagination is not an atomic queue snapshot; an original not seen
in a completed scan is not proof of ingestion. Loaded/idle status does not prove
eligible pickup, subscriber ownership or preserved execution context. Approval,
user-input waits and interrupted turns remain separate observations owned by the
recipient. No observation supplies permission to recover a task.

The operation allows 15 seconds overall, two seconds per native request, three
pages of at most 20 queue entries and 1 MiB per native message. It makes no
retries. JSON includes timestamps, binding guards, runtime and latest-turn
metadata, queue completeness, the nominated original's standing, limits and
unavailable components. Valid earlier observations remain visible after a later
failure; `usable` is true only for a complete, guarded `OBSERVED` result.

| Result | Exit | Meaning |
| --- | --- | --- |
| `OBSERVED` | 0 | All requested facts read and queue scan complete, with matching guards. |
| `PARTIAL` | 3 | Some facts read, but a component or queue page is unavailable or bounded. |
| `UNAVAILABLE` | 4 | No native facts read, or binding validation is unavailable. |
| `STALE` | 5 | Binding differs from the expectation, is paused/unbound or changed during the reads. |

`original.standing` is `observed`, `not_seen` or `unknown`; it never means
consumed. Even a complete result describes several dated reads, not one atomic
instant. Invalid command arguments exit 2 before contacting the provider.

`wake --status` remains a ledger-only read of recorded attempts and ACKs;
`observe` contacts the provider to obtain dated runtime facts. Use the original
wake reference for status/history, which can be a file path or ledger sequence.
A file-path wake does not automatically associate a separate signal's ACK.

### Claude Code sessions

A Claude Code session (2.1.224 or later) listens on a private inbox socket and
exports its path to its own shell. Bind from inside the session you want to
wake, so the role names that session's inbox:

```sh
multithread bind reviewer --claude-socket "$CLAUDE_CODE_MESSAGING_SOCKET" \
  --scope project/workstream --charter 'Review the requested change' \
  --agent claude --session <session>
```

Bind checks that the path is a socket you own with mode 600 and not a symbolic
link. `multithread wake reviewer --ref ...` then writes the wake to that inbox
as one user message: an idle session starts a new turn, and a busy one reads it
between tool calls without interrupting a running tool. Claude Code has no
separate steer, so `--steer` changes nothing there and the result says so.
`DELIVERED TO INBOX` means the socket took the whole message. Claude Code
shows it as sent by another Claude session, not by the person, and says a peer
cannot approve anything; the text names the real sender. The session's inbound
settings (`crossSessionInbound`) may still hold or refuse it, and Claude Code
drops identical repeats sent close together.

A binding names the session, which survives `claude --resume`; its inbox is
named after the session's process and moves on every restart. So each
session's Multithread hook reports its current inbox at start and on every
prompt, with that process's identity (boot, process id and start time), into
an owner-only map beside the recipient index (`claude-inboxes.json`, in the
account's home, never an ambient `HOME`). Only a process the inbox is named
after, running above the hook, can report it. One process can switch sessions
(`/clear`, `/resume`), so only a session's start may take a socket another
live session holds; a later report or a `bind` only creates or refreshes, and
Claude Code runs these hooks to completion before continuing. A wake goes to
the inbox the bound session last reported while that exact process still runs,
and checks the process listening on the connected socket before sending. A
restarted or resumed session is reachable again from its first prompt without
binding again; a binding with no report, such as one made by an earlier
release, is reachable from its session's next prompt. Anything else is
`NOT RUNNING` (exit 4, nothing sent, the message id stays unused): a socket
another session now holds never receives a wake meant for this one. An inbox that is
gone, fails its checks or refuses the connection is still `NOT SENT`. The
same owner can refresh its binding; a new holder needs release or
[authorized handover](#inspect-pending-work-and-role-handovers). In one live test before this command existed, an idle Claude
Code session started a turn within 8 seconds of such a message.

A Codex desktop turn that wakes a Claude Code session runs inside Codex's
sandbox. There the installed launcher refuses by design ("unsafe launcher
ancestry"), and the sandbox may also block a Claude Code inbox socket: run
`multithread wake` through Codex's approved escalation outside the sandbox, and
never work around the launcher check. That route has not yet been run live.

## Inspect pending work and role handovers

Read the inbox with your exact coordination identity, using `next_after` as the
next `--after` cursor while `has_more` is true:

```sh
multithread inbox --agent codex --session EXACT_SESSION --after 0 --limit 30 --json
multithread brief --agent codex --session EXACT_SESSION --json
```

Inbox pages run oldest first. The bounded brief prioritizes the newest
exact-session signals, then the oldest agent-wide and broadcast signals.
`pending_count` counts all pending signals for that identity; `targeted_count`
counts those for its exact session. Counts and truncation show when the brief
is incomplete. Reading either view is not an ACK. After consuming an authorized
signal, explicitly record `multithread acknowledge SEQ --agent codex --session
EXACT_SESSION`; an ACK records consumption, not approval or task completion.
An exact-session signal requires that recipient's exact identity. Do not
acknowledge on another session's behalf.
If an ACK refuses because a signal names a role instead of your agent identity,
keep your `--agent` and `--session` unchanged. Read the full event with
`events --after PREVIOUS_SEQ --limit 1`, where PREVIOUS_SEQ is the signal's
sequence minus one. Preserve any written consumption assessment. If a new delivery
or durable handoff is needed within the existing task authority, its sender verifies
the intended recipient and issues a new handoff with the correct agent/session.
The original signal remains evidence; rebinding a role does not repair its target
or authorize consuming it as another identity. Wake events are notification
records, not ACK-eligible handoffs. Decision requests use `decision respond`
rather than a standalone ACK.

Inspect the role before changing it, and page its original-binding attempts
with the returned `next_before` cursor:

```sh
multithread roles --json
multithread roles operator --json
multithread roles operator --history --before SEQ --limit 30 --json
multithread wake operator --status --json
multithread wake operator --status --ref /absolute/path/task.md --json
multithread wake operator --status --ref 42 --json
```

For the first history page omit `--before`; use the returned sequence for older
pages. `roles` shows the binding generation, holder, declared scope and charter.
Use `--ref` with the original task-file path or ledger sequence to narrow wake
status history. `--id` selects a sending/dry-run message ID and is rejected with
`--status`.
History preserves each attempt's original generation and recipient, transport
outcome, optional observed sender and consumption evidence. `wake --status` is read-only: it neither
rereads the task file (which may have vanished), probes the recipient nor sends
again. `QUEUED`, `STEERED` and `DELIVERED TO INBOX` record transport acceptance.
File-pointer and non-delivery-event consumption remain `unknown`; a wake naming
an acknowledgeable ledger signal can show
`not_acknowledged`, `acknowledged` by its original recipient, or
`acknowledged_elsewhere`. A later turn alone does not establish consumption.

The binding owner or its Codex recipient can pause, resume or unbind its own
role; a same-owner Claude binding can refresh its inbox. A different holder or
recipient needs the holder's release, or actual user authorization for the
handover. For that authorized replacement, add `--replace
--expected-generation N --reason 'why this handover is authorized'
--approval-ref sha256:FULL_64_HEX_DIGEST` to the new `bind` command, preserving
the approved scope and charter. The immutable reference identifies the reviewed
approval artifact. These flags audit authority already held; they do not grant
permission or authenticate a user. For another holder's `unbind`, `pause` or
`resume`, the same authorization and generation/reason/approval-reference flags
are required. A stale generation refuses; inspect again before deciding.

An authorized handover records targeted notices for the old and new recipients.
Outstanding messages keep their original recipients and binding generations;
claims keep their exact owners. Nothing redirects old messages or expires claims.
Review that outstanding work explicitly before accepting the new responsibility.

When configured and trusted, `PostToolUse` hooks can remind a session about
pending work between tools. They read the ledger without writing events and
include pending counts, up to three signals and the inbox command for
the rest. They are nonblocking and never ACK automatically. For the same
repository, provider and session, an identical notice is suppressed for ten
seconds; changed reminder content bypasses that suppression. The temporary
cache records only suppression state, not delivery or consumption. A cache
failure permits repeat notices, never an ACK or a claim that the inbox is empty.
Adding this event remains an explicit user-reviewed settings/trust step.
Installation or configuration does not prove native reminder delivery; see
[hook delivery](SETUP.md#confirm-delivery-in-a-real-session).

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
