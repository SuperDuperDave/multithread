# Peer command reference and evidence

Use the [peer guide](PEER.md) for a first collaboration, result assessment and
deliberate follow-up. This reference covers:

- [Receipt fields and capture limits](#receipt-fields-and-capture-limits)
- [Usage measurements](#usage-measurements)
- [Support reports from retained calls](#prepare-a-support-report-from-an-existing-call)
- [Frozen review packets](#freeze-a-review-packet)
- [Input to a running peer](#update-a-running-peer)
- [Coordination and native verification evidence](#coordination-and-verification-scope)
- [The reviewed source entry](#use-the-source-entry)

## Receipt fields and capture limits

The everyday [result states and assessment](PEER.md#read-the-result-before-continuing)
apply alongside these additional observations.

| Field or state | Meaning |
|---|---|
| `caller_stop_reason` | When recorded: `timeout` means the call wait reached its limit; `interrupted` means caller execution was interrupted; `shutdown_timeout` means the wait for provider process exit expired. These describe the caller's observation, not the cause of provider silence or failure. A previously observed answer remains separately assessed. Other local faults that force cleanup can leave this field absent, as can old receipts; elapsed time and exit code cannot reconstruct it. |
| `requested_session_id` | The requested identity, when known before launch. It remains unverified until native output confirms it. A missing verified `session_id` does not prove that no session started; the requested identity alone is not a resume instruction. |
| `observed_session_id` | If present on an identity mismatch, the unverified native identity reported by the provider. It is diagnostic, not a resume instruction; inspect the retained raw output. |
| `resumed` | Whether this call requested resume (`true`) or a fresh session (`false`), not independent proof of restored history or a cache hit. |
| `provider_version` | Optional provider-reported version from this call's native initialization, with `status`, `version` and `source`. It describes the responding process, not the current installation, model, Multithread release or an earlier turn in a resumed conversation. |
| `requested_model`, `requested_effort`, `model_observation`, `effective_effort` | Claude call settings requested by the caller. Streaming initialization can report a model name; a different name is `different_name_unverified`, not proof of a model mismatch. A repeated init with no model keeps the last reported name but marks its relation `prior_init_only`. Final-JSON calls lack this native model observation. Effective effort remains `unknown`. |
| `native_progress` | In Claude streaming mode, content-free latest attributed event time and frame counts. Observed activity is not proof of continuing progress or task completion. |
| `task_delivery`, `native_input_unwritten_bytes` | When recorded, the task's pipe-write observation and the native input queue's remaining byte count. Count scope depends on capture mode; zero does not establish full task delivery. A complete pipe write does not prove native consumption. Missing fields remain unknown. |

For ordinary Claude final-JSON calls, the byte count records initial task bytes
not written. Streaming Claude records its last serialized outgoing queue, which
can include follow-up frames or be cleared after input closes. That queue count
does not substitute for the separate `task_delivery` observation.

`stdout_observation` records the byte count and SHA-256 of observed stdout.
For Codex and Claude `--live-input` or `--stream-progress`, capture is limited to 16 MiB, plus one byte
to detect overflow. Exceeding that bound sets `truncated`, stops interpretation,
closes native input and leads to owned-process cleanup. Only the captured prefix
is retained; a previously observed answer survives with `needs_attention`.
Claude calls without either streaming option capture raw stdout directly. Their
`stdout_observation.scope` is `bounded_read`: the byte count and digest describe
the read prefix, with the same 16 MiB plus one byte limit. Their raw file can
exceed that summary limit. After timeout or interruption, this observation is
still recorded, without interpreting those bytes as a completed result.
If the read fails, `stdout_observation_error` reports unavailable observation;
the byte count and digest remain unknown. A successful empty read establishes
only that no stdout bytes were observed, not that the provider was idle.
Native stderr and directly captured stdout are not sealed: a
provider descendant may still hold their file descriptors. Compare observed
bytes before reusing a summary if they changed.

SIGINT, SIGTERM and SIGHUP also trigger owned-process cleanup and an uncertain
receipt unless a valid streaming result has already been observed or the caller
already ignores that signal (for example, SIGHUP
under `nohup`). Once the provider exits, ordinary signals allow the bounded
result read and receipt to finish. SIGKILL, host failure and a provider that
escapes that process group cannot be handled this way.

Before provider spawn, the call writes `checkpoint.json` in its private evidence
directory. After a successful spawn it replaces that checkpoint atomically.
When `result.json` is missing, `peer report` may show this nonterminal checkpoint:
`before_spawn` means launch may have happened before the caller stopped;
`spawned` confirms a process was started. In either case the outcome remains
unknown, and the checkpoint does not identify a safe resume target or say whether
the provider still runs. Inspect native output and durable work before follow-up.

A streaming result can precede native process exit. Claude receives the remaining
call time for its background work; Codex's owned server gets a short shutdown
allowance after its terminal turn. Cleanup is limited to the process group this
call created. A valid answer survives incomplete stdout observation, marked with
`needs_attention`. Neither a terminal result nor termination proves that every
background operation completed.

## Usage measurements

The response includes measured call duration and provider-reported usage/turns
when available. `estimated_cost_usd` is a provider estimate, not an observed
subscription charge. Missing measurements remain unknown. Starting in v0.4.6,
known optional numeric measurements are validated independently of answers and
control messages. Invalid values become `null`, with bounded field names in
`measurement_errors`; human output warns that measurement is unavailable.
Warnings follow the selected measurements: later valid observations replace
earlier warnings, and unrelated Claude loop metrics do not invalidate the
retained task's metrics. Earlier observations remain in raw output and, for
Claude streams, the bounded `native_results` records.
These warnings alone do not change the task outcome or `needs_attention`.
Malformed protocol JSON, unverified identities and invalid completion messages
still prevent accepting a result. Raw native evidence remains private and intact;
unrecognized native measurement extensions are not certified by this validation.
Claude receipts retain such extensions; Codex receipts select known counters,
with extensions retained only in raw output. A Codex usage-update notification
with no usage object records a measurement warning and unknown current usage,
not the previous snapshot as if it were current.

Use the stable scope IDs in new receipts rather than matching their explanatory
prose:

| Measurement | Scope ID and interpretation |
|---|---|
| Claude `usage`, ordinary call | `usage_scope_id: native_main_loop` — this query's main loop, excluding subagents. |
| Claude `usage`, live-input call | `usage_scope_id: latest_related_native_result` — the latest result answering this call's input, not a sum of the stream. |
| Claude `model_usage` | `model_usage_scope_id: native_query_cumulative` — latest reported query totals by model, including native subagents and compaction; not every provider helper call. Available in ordinary and live-input receipts. |
| Claude `estimated_cost_usd` | `cost_scope_id: cumulative_through_latest_native_result` — latest reported cumulative estimate, including background results in a stream and earlier session spend after resume. It is not the resumed call's incremental cost. Do not add successive snapshots or treat it as billing. |
| Codex `usage.last` / `usage.total` | `usage_scope_id: native_thread_last_and_total` — native last-request and running thread totals, not incremental usage of this Multithread call. `model_context_window` is separately reported when available. |

Provider-native counters can overlap; do not add every field into a total.
The resumed Claude cost estimate can include earlier spend in the same session;
other usage measurements retain their own native scopes. A single resumed
receipt is not a per-call spend estimate.
The [Claude accounting reference](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
explains its query and stream scopes. These measurements do not establish net
token savings, subscription-quota consumption or broad reliability.

## Prepare a support report from an existing call

When a call needs investigation, use its retained directory:

```sh
~/.local/bin/multithread peer report --call-dir /absolute/peer-call --json
```

Available from v0.4.3. Omit `--json` for a short human summary. This read-only
command prints selected diagnostics from the private `result.json` receipt.
The usual global `--repo` prefix is accepted but unused by this command.
It requires no provider, repository enrollment or network access and does not
change the call. It never opens task, request, raw stdout/stderr or input-mailbox
files. The result receipt itself can contain private answers and diagnostics;
the report uses a positive, typed selection rather than trying to redact text.

The output excludes task/answer text, arbitrary native diagnostics, paths,
session/tool identifiers, hashes, model names and usage maps. It includes the
recorded call outcome, task-submission and pipe-delivery observations, process exit, attention flag,
elapsed time, available provider turn/duration/cost estimates, counts of retained
errors/denials/unsupported native requests and stdout observation limits.
It can show the requested effort enum and a literal model comparison without
disclosing the model name or claiming effective effort.
Starting in v0.4.11, the report also selects `caller_stop_reason`: `timeout`,
`interrupted` or `shutdown_timeout` have the meanings above; `not_recorded`
means the field was absent, while `unknown` means its recorded value was
unrecognized. Neither supplies a diagnosis of provider behavior.
Starting a provider does not establish task submission. Codex records whether
submission was requested or accepted; an absent observation remains `not_recorded`.
Missing measurements are JSON `null`, not zero. Invalid auxiliary provider
metrics also become `null`; `invalid_measurements` includes safe categories
recorded by measurement validation, without copying bad values or model names.
The known call outcome remains available. Invalid JSON or required call fields still
prevent reporting rather than producing a guessed outcome.
Counts refer to the retained lists, which may already be bounded. The report
recognizes the [usage and cost scope IDs](#usage-measurements); older receipts remain readable
through their known prose labels. An explicit unrecognized ID stays `unknown`,
even if its prose resembles a known scope. Missing scope remains unknown; do not
treat those measurements as whole-call totals or sum cumulative values.
No cost estimate establishes actual billing.

For two completed Claude calls in the same verified session, `peer report
--call-dir /absolute/current --compare-call-dir /absolute/earlier --json`
can subtract the earlier cumulative estimate from the current one. Both private
receipts must have the same known cumulative scope, the current call must request
resume, and the total must not decrease. The result is a **difference between
two selected receipts**; intervening session activity is not excluded, so it is
not certified per-call billing. Uncomparable receipts return `unavailable`.

`report_state: reported` and exit 0 mean a report was produced, even when
`call.state` is `uncertain` or `provider_error`. Otherwise exit 1 and
`receipt_status` distinguish missing, unavailable, malformed, unsupported-schema
and oversized receipts. A valid checkpoint with a missing terminal receipt gives
`report_state: incomplete` and still exits 1. No call outcome is inferred.
An absent receipt does not establish whether a provider is running or finished.
The independent receipt-read limit is 16 MiB; JSON expansion can make a valid
receipt larger than this. Preserve such evidence for deliberate local inspection.

The report describes recorded observations. It does not re-read current stdout,
verify native identity, check hooks/tools or ledger state, diagnose the cause,
or certify workflow completion. Receipts without an explicit stdout observation
or scope retain that uncertainty, including current streaming receipts whose
capture scope is not recorded. A streaming capture is not labeled as a bounded
read of a potentially larger retained file.

Starting in v0.4.9, the report also selects `provider_version` from an explicitly
attributed initialization observation. Codex uses the leading build-version token
in its App Server user agent (`source: codex_initialize_user_agent`); Claude
`--live-input` uses its initialization version (`source: claude_system_init`).
The surrounding user agent, platform details and client version are excluded.
Only bounded numeric versions and recognized alpha/beta/rc numeric prereleases
are shared; custom/build formats stay unknown. A `reported` value is a provider
statement, not independent binary verification or a compatibility guarantee.

Missing native metadata is `not_reported`; unsupported metadata is `unrecognized`.
Both have a null version. Each Claude initialization replaces this observation,
so an unavailable later version does not leave an earlier one appearing current.
Old receipts and ordinary Claude final-JSON calls have no such initialization
observation: the report says `not_recorded`, including when a legacy private
`native_version` field exists. Malformed or wrongly attributed receipt metadata
becomes `invalid`, without discarding the recorded call outcome. The reporter
never runs a provider or infers a historical version from today's installation.
Supply separately observed versions with their observation date when needed;
upgrading now cannot establish which executable handled an old call.

The call-time Multithread release version remains unrecorded. Starting in
v0.4.4, private request/result receipts record `producer_runtime`: the retained
runtime manifest SHA256 when this helper was loaded through the verified importer,
or `unavailable`. That digest identifies the loaded payload, not a release version,
activation, provider, or hook invocation. Source-entry calls have no retained
runtime identity; older receipts leave it unrecorded. The report exposes only
`producer_runtime_identity` (`recorded`, `unavailable`, `not_recorded` or `invalid`),
never the digest, and invalid auxiliary provenance does not discard a useful
call outcome. Review the report
before sharing through the [support route](SUPPORT.md#useful-safe-support-information).

## Freeze a review packet

Use `multithread peer packet` to save a reviewable diff without changing Git
state or sending it to a provider:

```sh
multithread peer packet --repo /absolute/git-root --base HEAD \
  --path src/example.py --path tests/example.py \
  --output-file /absolute/new/private-packet.txt --json
```

The command resolves the base and HEAD to commit IDs, selects only tracked
paths named by `--path`, and includes staged and unstaged text changes through
the current working tree. It refuses binary changes, untracked files in the
selection, empty diffs, non-UTF-8 output and diffs over 1 MiB. Git runs with
external diff, text conversion, fsmonitor and system/global configuration
disabled. The private packet (mode 0600) records the exact selected paths,
revision IDs, diff byte count and SHA-256. Its digest covers the diff bytes
between the packet's markers, not the packet header or the peer task file.
The selected repository's local Git configuration and object storage still need
to be trusted. Local configuration can include other files, and tracked
`.gitattributes` can select locally configured filters. This helper is not a
sandbox for hostile Git metadata. It compares HEAD, status and diff again
before writing to catch ordinary concurrent edits; it cannot guarantee an
atomic filesystem snapshot against an adversarial replace-and-restore race.

Inspect the packet before sharing it. This is input preparation, not a secret
scan, enforced provider read-only mode or permission grant. A reviewer without
Git tools can inspect the supplied patch, but cannot independently prove its
working-tree provenance; the caller can recompute the digest locally.

## Update a running peer

Live input applies to a call that Multithread owns and that is still running. Codex
exposes input for its exact active turn. For Claude, add `--live-input` when
starting the call: an update can be picked up between tool calls or become a
later turn in the same session. This opt-in can use additional provider turns;
the original wall-time limit still applies to the whole call. It is not a
promise that an update will affect work already in progress.

Choose `--output-dir` when launching so another cooperating process can locate
the call while the initiating tool waits. The directory must not already exist.
Inspect its current input target:

```sh
~/.local/bin/multithread peer control status --call-dir /absolute/peer-call --json
```

An `open` mailbox does not by itself advertise a usable input target. The
`input_target` observation distinguishes `not_advertised`, `advertised`, `closed`
and `unavailable`. An advertised target identifies where input can be addressed;
it does not prove the owner is still running or guarantee native acceptance.
Use the exact target and inspect the resulting input receipt.

Use the advertised session and turn to send a Codex update:

```sh
~/.local/bin/multithread peer control send --call-dir /absolute/peer-call \
  --session <session-id> --turn <turn-id> --message-file update.txt --json
```

For a Claude call started with `--live-input`, use the same command with its
exact session and **omit `--turn`**. The helper refuses a different target; it
does not choose a latest session, reopen a call, or forward to an unrelated
desktop task. Native provider permissions remain in force.

| Input receipt | What it establishes |
|---|---|
| `pending` | The local request is retained, with no final native observation yet. The command exits 2 after a short wait and prints an inspection command. |
| `accepted` | Codex acknowledged the exact active-turn update. Consumption is not verified. |
| `consumed` | Claude's main-session output explicitly named this message UUID among the answered inputs. This does not establish task completion. |
| `rejected` | The input was refused locally or by the native provider. The original task may still return successfully. |
| `uncertain` | A dispatched input lacks the necessary native observation. Preserve it and inspect before any deliberate follow-up. |
| `unavailable` | The helper could not establish or record an input observation. This is not proof that nothing was sent. |

`send` prints a request UUID before submission. Keep it if the reply is lost.
The same UUID with identical content inspects the existing request; it never
resends dispatched input. Reusing it with different content is refused. Use
`receipt --call-dir … --request-id … --json` to inspect later. Final receipts
are immutable; a pending receipt can gain an observation while the owner runs.

If `send` cannot read its message file or stdin, it identifies that local input
failure and submits no new input in that invocation. Correct the input source;
if you reused a request UUID, inspect its receipt because earlier input may
already exist. A mailbox or recording failure still requires inspection before
retrying; it does not establish that nothing was sent.

Input acceptance closes at Codex's terminal turn or Claude's first result
answering this call's submitted input.
Already dispatched Claude updates can still produce subsequent results. Missing
message attribution does not settle the initial task or a queued update.
`native_results` retains bounded separate observations, including background
results. The input UUIDs attributed by a result determine whether it answers this
call's submitted input; an unrelated result cannot replace the task's answer.
An input echo proves only receipt, and a subagent's answer cannot satisfy the
main-session task.

The mailbox is private local coordination among cooperating processes under the
same OS account. It is not agent authentication or a background service. An open
target file alone does not prove its owner remains alive after a crash. Multithread
does not automatically replay pending work on restart. Per-call bounds keep this
channel finite; inspect explicit refusals rather than opening replacement calls
to evade them.

If the input mailbox becomes unavailable, new dispatch stops while the original
authorized task can continue. Check `control_fault` and the private receipts;
the availability of a receipt and the native result are distinct observations.

Claude streaming reports per-result usage separately from the latest cumulative
`model_usage` and cost estimate. Cumulative totals must not be added across
results. Provider-reported counts have their native scope and do not measure
human effort, actual subscription billing, or the caller's own usage.

## Coordination and verification scope

The [coordination boundary](PEER.md#coordination-and-verification-scope) applies
to these scoped observations.

Automated checks cover fake native processes, refusal and partial-result cases,
exact sessions, interrupted calls, private evidence, and installed commands with
the source checkout absent and real public hooks. They are not real-model
workflow evidence. The [historical native workflow](PROVIDERS.md#bounded-native-workflow)
keeps its original scope. A separate development check with Claude Code 2.1.267
used the public source entry and an existing installed development runtime:
Claude wrote a code review, acknowledged the exact handoff and released its
claim; its answer returned to the initiating Codex task without manual message
forwarding. The caller inspected the artifact and ledger and acted on the review.
The call took 520 seconds and 13 provider turns, with no reported permission
denials. That observation applies to the source entry.

The reviewed bundle was then installed through the normal account-level upgrade.
The initiating Codex task used the installed `relay peer` command to resume that
exact Claude session for a review of the fixes. Claude wrote a separate follow-up
artifact, acknowledged its new handoff and released its claim. The matching
result returned automatically in 359 seconds, with 12 provider-reported turns
and no reported permission denials. The caller independently checked both review
artifacts, unchanged input commits and ledger events. This verifies installed
call/return and native exact-session resume on that existing WSL2/ext4 developer
profile with Claude Code 2.1.267. Subsequent signal refinements have separate
regression evidence; they were not exercised by terminating a paid review.

The v0.3 source has separate native observations on the existing
developer profile with Codex 0.153.4 and Claude Code 2.1.269. Codex acknowledged
an update to its exact active turn, and its review artifact reflected that
update. Claude's main-session output named the submitted live-input UUIDs as
consumed. The first reverse workflow was interrupted by a bridge cleanup defect;
its evidence remains retained.

A deliberate resume of the same Claude session then led a resume of the exact
Codex thread. Codex returned a follow-up, wrote `codex-followup.md` and released
its own claim. Claude wrote an independent report and emitted a native
background final result. The original helper receipt for that Claude resume
remained `uncertain`: the bridge rejected repeated initialization of the same
session. Completed artifacts and that native result do not turn the original
receipt into a successful helper return.

After the bridge correction, an offline replay of the retained native stream
accepted its repeated same-session initialization, returned the result
attributed to the submitted input and retained the later background report as a
separate observation. The original receipt remains unchanged. Replay verifies
interpretation of those retained bytes; it is not a newly executed live workflow.
Independent ledger inspection found no active claims, including the released
Codex claim; no handoff acknowledgement was due for that follow-up.
These observations do not establish installed-native v0.3 support.

An ordinary development review used the installed public v0.4.0 `multithread peer`
command with Claude Code 2.1.269 on the existing WSL2/ext4 developer profile.
Claude reviewed a frozen setup-clarity candidate, wrote a review artifact and
acknowledged its exact commit handoff. A scoped input sent during the review was
recorded as consumed and covered by the matching native result. The answer
returned automatically in 772 seconds and 44 provider-reported turns, with no
reported permission denials. The caller checked the artifact and ledger and
addressed the verified findings. This observes useful installed v0.4 collaboration;
it is not a native run of the resulting v0.4.1 candidate or a fresh-user setup test.

These observations do not establish independent onboarding, broad reliability,
net usage savings or waking an independently idle task. The source-absent
installation checks remain separate from the normal native runs, which did not
hide source files or alter provider capabilities for isolation.

## Use the source entry

From the reviewed source checkout, `examples/call_peer.py` invokes the same
helper against a reviewed installed Multithread selected with `--multithread`
(`--relay` remains a compatibility spelling):

```sh
/usr/bin/python3 -I -S -B examples/call_peer.py codex \
  --multithread /absolute/reviewed/multithread --repo /absolute/enrolled/peer-checkout \
  --task-file task.txt --output-dir /absolute/new-peer-call --json
```

Select `claude` and add `--live-input` for Claude session input. In control
commands, replace `multithread peer` with the same source entry:

```sh
/usr/bin/python3 -I -S -B examples/call_peer.py control status \
  --call-dir /absolute/new-peer-call --json
/usr/bin/python3 -I -S -B examples/call_peer.py control receipt \
  --call-dir /absolute/new-peer-call --request-id <request-uuid> --json
```

Use that substitution for `control send` too. Printed installed-launcher
inspection commands require a runtime that contains v0.3 control support;
with v0.2.0, inspect through the source entry instead. Source-entry execution
does not prove that the installed runtime contains these additions.
