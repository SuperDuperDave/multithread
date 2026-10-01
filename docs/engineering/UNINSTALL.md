# Installed-code uninstall

This preview supports explicit removal of verified installed Multithread code. It is
not an account-data purge, provider-config cleanup, or a command to stop running
processes. Disable only moves the active selector and retains code; uninstall
actually unlinks the verified code files and removes their empty directories.

## Operator workflow

Stop Multithread work and separately configured invocations first. This command does
not remove hooks, edit provider settings, stop receivers, or terminate commands.
If you installed user-level hooks, take them out while the command still works:
`multithread hooks trust --revoke`, then `multithread hooks remove`. Otherwise
every Codex and Claude session would keep running a hook whose launcher is gone.
Keep a separately reviewed source bootstrap and an approved release bundle
outside the installation: after removing its own command, Multithread needs that
bootstrap for inspection, retry or reinstall.
Use the latest reviewed bootstrap to manage a rolled-back installation that
retains newer layouts or receipts; an older release's manager may not understand
those objects even though its ordinary ledger commands still run.

With a working installed command:

```sh
~/.local/bin/multithread runtime uninstall-plan
~/.local/bin/multithread runtime uninstall --expected-plan EXPECTED_PLAN_SHA256
```

Review the plan's scope, targets, issues and retained_metadata. Supply the exact
expected_plan printed by that observation. The digest is a stale-state token,
not a secret. Planning is read-only: it neither creates an installation root nor
creates/acquires the activation lock. An existing installation needs its safe,
owner-writable root and existing valid activation.lock; damage refuses.

If the launcher is absent, damaged, or was removed before an interruption, use
the independently reviewed source bootstrap, never a suspect installed file:

```sh
/usr/bin/python3 -I -S -B src/relay_bootstrap.py uninstall-plan
/usr/bin/python3 -I -S -B src/relay_bootstrap.py uninstall \
  --expected-plan FRESH_EXPECTED_PLAN_SHA256
```

Read the JSON, not just the diagnostic command's exit status. uninstall-plan
can exit successfully with state:blocked, can_uninstall:false, issues and no
expected_plan. A stale/blocked apply refuses with a nonzero exit status before
removal. If a problem occurs after the valid receipt is verified and removal
processing begins, apply reports
state:partial, uninstalled:false, its receipt and a next action, and exits
nonzero. A successful apply reports scope:installed-code, state:complete and
uninstalled:true. An already absent installation is an idempotent no-op.

## Exact scope

The plan includes all verified releases, not only the active release:

- Installed bootstrap.py, release.json and the closed runtime payload files.
- Verified launch activation records and deterministic wrappers.
- The verified installation-owned `multithread` alias to the absolute account
  `relay` path, the recognized active `relay` selector, and verified retained
  .relay-switch-ID / .relay-disabled-ID selectors.
- Their empty release, launch and payload directories.

Code files are checked against the closed release manifest; wrappers and launch
records are validated without executing them. Selectors must point to a verified
launch in this installation. A corrupt or incomplete inactive release also blocks
the initial operation; uninstall does not guess which unknown bytes are safe to
discard. Unrelated files in the command directory are left alone. Unknown objects
inside the installation, unrecognized reserved selector names, unsafe ownership,
symlinks in place of code, or changed observations block removal.

These are outside the removal set and are not scanned as uninstall targets:

- Account enrollment records in the sibling enrollments directory.
- Project .relay markers, SQLite databases, WAL/SHM sidecars and ledger anchors.
- Project Git metadata, hooks, source and unrelated commands.
- Provider settings, credentials, sessions, receivers and external services.

The installation root, stable activation.lock, immutable uninstall-SHA256.json
receipt and empty uninstalled-SHA256.json completion marker remain. These are
non-code metadata; artifact_purge_complete is explicitly false. Completed receipts
are recognized by inspect and permit reinstall without removing the root or lock.
No metadata-purge command is provided.

## Interruption and retry

Apply rechecks the plan under the existing management lock. Before deleting
anything it writes and syncs a receipt containing the exact original target set,
object identities, file digests and selector targets. Files/selectors are moved
without replacement to a receipt-derived .relay-uninstall-SHA256-INDEX name in the
same directory, rechecked, then unlinked. Directories are removed only when empty:
there is no recursive deletion or broad cleanup of a guessed path.

The staging names are temporary, not a successful quarantine-only uninstall.
Once a verified file is unlinked it has no managed backup. Reinstall requires an
approved bundle; preserving project data does not preserve executable code.

After process death, run a fresh plan. A valid pending receipt authorizes only its
matching original or staged remaining objects; already absent targets are treated
as removed. New, changed, duplicate or unknown objects block a retry and remain for
review. The pending operation prevents cooperating install/activate/recover/disable
writes. Resume with the fresh expected_plan until completion. A completed marker
makes another apply a read-only no-op, and a subsequent explicit install can reuse
the retained metadata root.

If a replacement arrives just before staging, it can be moved to the deterministic
staging name but is checked before deletion. A mismatch reports partial failure and
preserves the moved object. The receipt's target index and relative parent identify
its staging path; do not delete that path merely to force a retry.

A process killed while the receipt itself is being written can leave an invalid
receipt. No code deletion has begun at that point, but automatic retry is deliberately
blocked. Corrupt receipts or multiple incomplete operations require separate review;
this interface is not a general repair/purge engine. Directory inventories are
bounded to 256 entries and the removal set to 2048 objects; exceeding a bound refuses,
including accumulated receipt metadata. There is no automatic history pruning.

Completed writes and removals sync their files/directories. Tests exercise process
death at receipt publication, before/after staging, before/after removal, and before/
after completion marking. These are process-interruption witnesses, not a proof of
every filesystem or power-loss ordering.

## Concurrency and remaining limits

The OS account is trusted. The management lock serializes cooperating management
commands; it does not stop ledger activity or freeze the namespace against another
same-account process. No-follow retained ancestry, same-directory staging and
rechecks catch the tested substitutions. Linux pathname unlink/rmdir is not an
inode-conditional operation: an uncooperative process can substitute an entry in
the final check-to-delete gap. Do not run competing manual filesystem changes
during uninstall. The result is not a hostile-same-account deletion guarantee.

Uninstall does not remove user-level hooks itself: `multithread hooks remove`
and `hooks trust --revoke` do that as their own approved steps, before code
removal. This command cannot promise a complete provider uninstall; the
operator must separately stop/remove any configured invocation. Repository rebind, repairing damaged release files, recovery without
a separately verified release/bootstrap, and full metadata purge remain separate
follow-ons.

## Evidence

tests/runtime/test_uninstall.py covers read-only/exact plans, stale activation and
changed-code refusal, unknown preservation, actual code/selector deletion,
protected synthetic project/account data, completed-metadata reinstall, seven
process-cut subcases, changed staged objects and corrupt receipts. All fixtures
are disposable /tmp roots; no live account installation or ledger is touched.
tests/runtime/test_public_uninstall.py additionally exercises the actual installed
wrapper in a disposable OS-account namespace: initialize a real ledger, append an
event, create an active claim, remove installed code, reinstall, and reopen the
same enrollment/events/claim. Whole-project (including Git marker/hooks and
DB/WAL/SHM) and account-registry byte/metadata snapshots remain unchanged through
removal and reinstall before reopening; the reopened SQLite integrity check passes.
