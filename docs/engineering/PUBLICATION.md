# Publication boundary

The [public source](https://github.com/SuperDuperDave/multithread) began as a fresh parentless snapshot
of 67 explicitly selected files with 25 exact reviewed exceptions. The export
passed all 327 strict tests plus isolated installation, package inspection and
the no-account demo before upload. No development history, ignored files or
unselected content was imported. Consult [v0.1.0](https://github.com/SuperDuperDave/multithread/releases/tag/v0.1.0)
for final release-archive and onboarding results.

The project is [MIT licensed](../../LICENSE), copyright (c) 2026 David Jones,
and its selected source was reviewed for publication.
That approval does not release videos, casts, transcripts or other recording
artifacts; those require separate review before public distribution.

## Immutable snapshot audit

Run the read-only command against a full commit OID:

```sh
python3 -I -S -B tools/publication_audit.py --repo /absolute/repository --revision FULL_COMMIT_OID
```

The manifest is `tools/publication-manifest.json` **inside that same commit**.
A dirty working tree cannot silently change audited files or exceptions.
The report binds the exact commit/tree, manifest and each selected blob.
It records an auditor-file hash observed unchanged at the beginning and end,
not proof of the bytes Python loaded or self-authentication. The flag
auditor_loaded_bytes_verified:false makes this explicit. Run trusted frozen
tooling; this check does not prove absence of replace-and-restore races.
The auditor verifies raw Git object hashes before using their bytes, ignores
replacement refs and disables lazy fetching and inherited Git configuration.
It refuses shallow, grafted or alternate-object storage and requires an
independent Git directory. Selected files must be regular bounded UTF-8 blobs;
symlinks, submodules, invalid encodings and NUL-containing files are unsupported.

Repository-local Git configuration and storage must be trusted. Inherited
configuration is disabled, but Git can still read repository-local config
includes and follow symlinked object storage outside the repository. This tool
is not a sandbox for hostile Git metadata and does not claim to confine those
reads. Only explicitly selected, trusted repositories are appropriate inputs.

Only categories, file/line locations and hashes appear in findings. Matched
values and raw Git diagnostics are never intentionally emitted. Locations are
not evidence of whether a finding is a real credential: review them privately.
The command creates a temporary isolated read profile; it does not change the
repository, run selected code, create an export, contact a provider or upload.

Exit0 means only that the **selected snapshot checks** found no unsuppressed
matches or missing inline local Markdown targets. Exit1 reports snapshot findings.
Exit2 means the audit refused incomplete/unsupported input. None authorizes
publication. Reachable commit metadata findings are reported separately even
when selected snapshot checks pass.

## Narrow, reviewed exceptions

The candidate contains individually reviewed exact exceptions for synthetic
test credentials, reserved-domain identities, disposable account paths and
sanitizer syntax. New matches require their own review; no directory is exempt.
After direct inspection, a reviewed exception may bind one exact path, detector
rule and SHA256 of the complete UTF-8 line, with a fixed reason category.
Duplicate, stale or multiply matching exceptions refuse. Editing the line or
moving it to another path invalidates the exception; there is no blanket
test-directory exclusion.
A hash exception is a reviewed false-positive decision, not proof of safe code.

The detectors cover selected credential shapes, private key headers, bearer and
credential assignments, private receiver URLs, personal-home paths and email
addresses. They are heuristics: encoding, split strings, unknown secret formats,
legal/ownership issues and semantic private information require further review.
No clean report is described as a comprehensive secret guarantee.

## Selection and history

The candidate includes reusable code, tests, examples and standalone technical
guides/evidence. Local operational instructions, raw journals, original source
inventory and operator-specific planning remain excluded. Removed links are
retargeted to public-safe technical documentation, not silently broken.

All reachable commit metadata is inspected, including every merge parent.
Historical blobs are **not** inspected by this command. The report always states
`historical_blobs_scanned:false`, `existing_history_export_allowed:false` and
`publication_authorized:false`. An old deleted secret is not made safe by a
clean current tree. Do not push this development repository's existing history.

The final public artifact must copy only reviewed, exact bytes into a fresh
history, verify its tree and approved public author/committer/message metadata,
rerun tests and the demonstration, and inspect packaged distributions too.
Source-bound historical receipts preserve their original `base_commit` values;
those commits will not resolve in a history-free public snapshot. Their source
hashes remain useful evidence, not proof that the public export was rerun.

## Executed private fixture

The [private export receipt](private-export-result.json) records an actual
disposable validation of the exact 56-file candidate at development commit
ee6c19663012e5587093e9fd29b43017c60325a5. It was not a public export or a
license/attribution decision. The following 57-file checkpoint added its receipt.
The earlier 61-file selection added the CI workflow, CI/support guides and
strict-runner tests. Later edits were not part of that frozen 56-file runtime run;
their strict local evidence is separately recorded in development-test-result.json.
The earlier 63-file selection added independent external-recovery controls and
the public real-ledger recovery witness. The later 66-file selection added the
rebind guide and independent/public move tests, with one additional exact
reserved-domain fixture exception (25 total). That candidate passed a separate
private fresh-history check, all 327 strict tests and the scripted demo.

Adding LICENSE brings the current selection to 67 files with the same 25 exact
exceptions. Both the 56-file and 66-file export results retain their historical
scope: they do not cover subsequent license or documentation edits. The later
67-file public-source export was validated separately as described above; each
subsequent source commit and packaged distribution needs its own exact checks.
Historical native probes retain the source and executable hashes recorded in
their individual receipts.

The 56-file fixture copied only hash-verified selected blobs, never the working tree,
source Git directory or old object cache. Fresh raw Git objects and a synthetic
parentless commit were generated; read-tree created the index without Git-add
filters. Exact recursive file/directory inventory included all Git metadata and
unreachable storage, not just git status. Raw Git object reads, complete storage
closure, one reference/root commit, clean checkout and strict fsck agreed.
All 56 files and 69 objects matched before and after execution. The two metadata
email findings were the known synthetic fixture identity, not public attribution.

Four fresh outer user/PID/network namespaces exercised the actual public-ledger
preflight, package build/inspection, full 286-test suite and no-account demo.
Only the export and system dependencies were mounted read-only; original account,
development checkout/history and host temporary files were unavailable.
The namespace-local proc mount stayed writable so nested rootless namespaces
could be created. This is the reviewed Linux/WSL UID1000 profile, not universal
platform support or a hostile-code sandbox.

All 286 tests passed with zero failures/errors/skips in 110.44s. Independent
package inspection found exactly 11 regular files: reviewed bootstrap, nine
runtime modules and canonical release record. Their bytes matched the exported
sources; the builder still reported approved:false. The scripted demo reproduced
two failing tests, committed the fix, recovered after a stop before notification,
rejected ownership/retry conflicts and verified one handoff/ACK against six real
SQLite events. Its final four regression tests passed and integrity was ok.
No provider account or model was used.

Eight independently authored private controls also passed. They reject stale
selection, existing foreign outputs, extra ignored files/empty directories,
missing or altered bytes/modes, links, hidden objects/refs/config and incomplete
Git proof. SHA1/SHA256 and EOL-attribute cases verify that exact bytes are never
silently normalized. The private harness is not a shipped general-purpose
exporter and assumes trusted source metadata and a cooperating same account.
Temporary fixture cleanup completed before success was emitted.

## Public source and release evidence

[Hosted CI](../CI.md#hosted-result) records its exact public commit and run.
[Support](../SUPPORT.md#useful-safe-support-information) links the public issue
tracker and private vulnerability-reporting channel. The
[v0.1.0 release](https://github.com/SuperDuperDave/multithread/releases/tag/v0.1.0) is the reference for the tagged
source, archive checksums, package verification and anonymous clone/download
results. Only recorded results for that source and archive establish those checks.
The [bounded native workflow](../PROVIDERS.md#bounded-native-workflow) does not
validate a later public snapshot or distribution.

Historical private fixtures cover only their exact candidates. The auditor
creates no repository, remote or upload and never approves exporting development
history. Recording artifacts remain outside the positive selection pending
their separate review.
