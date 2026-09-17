# No-account CI

Run the strict local suite and no-account demo below to check your checkout.
The recorded GitHub Actions result below covers one public commit and hosted
runner. Native-provider evidence has its separate scope.

## Hosted result

[This hosted run](https://github.com/SuperDuperDave/multithread/actions/runs/34542612312) passed for public commit `2bfca836d7c267c514313b602dcef91799d75e2f`
on `ubuntu-24.04` with the explicit CI compatibility profile described below.
The privilege and two-level namespace preflight passed. All 327 discovered tests
ran with zero failures, errors, skips, expected failures or unexpected successes.
The no-account demo and immutable publication-selection check also passed.
The run log records the runner image and dependency versions. Provider accounts
have separate evidence; other runner or AppArmor configurations need their own checks.

## One strict local command

From a trusted checkout on the [supported test profile](SUPPORT.md):

```sh
/usr/bin/python3 -I -S -B tests/run_tests.py --suite all --strict
/usr/bin/python3 -I -S -B examples/no_account_demo.py --json
```

Strict mode requires nonzero test discovery, every discovered test to run, and
zero failures, errors, skips, expected failures or unexpected successes.
Its JSON reports discovered_tests, tests, strict and each result category.
The exit status is nonzero for incomplete coverage even if unittest says OK.
This prevents a missing namespace prerequisite from silently skipping the
installed-runtime witnesses. Nine independent tests exercise the real runner
in tiny copied-project fixtures, including early stop and import failure.

Without --strict, the runner keeps unittest's ordinary skip/expected-failure
semantics for targeted development. Use the complete strict command as the
release/CI gate, not a core-only run or a success message copied from old logs.

The runner launches a child with fresh home/XDG/tmp/Git-template directories,
an explicit environment allowlist and disabled inherited Git configuration and
hooks. It does not pass provider secrets or execute the optional native probes.
That environment isolation is **not** a host filesystem/network sandbox.
The public installation tests and demonstration separately use disposable
namespace profiles; their exact isolation claims are documented in the receipts.

## Hosted workflow

[ci.yml](../.github/workflows/ci.yml) targets a fresh GitHub-hosted ubuntu-24.04
runner and installs the distribution's bubblewrap and AppArmor packages.
GitHub documents these
[hosted VMs and their administrative privileges](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

The job compiles and loads a temporary, enforced `relay-ci` AppArmor profile.
It has no executable attachment: the CI launcher enters it explicitly, and
child executables inherit it. Its broad file, capability, network and namespace
allowances are a compatibility exception, not a tight AppArmor sandbox.
The profile uses `attach_disconnected` for Bubblewrap's retained procfs
descriptors during user mapping. That flag can alias paths; this profile is
not relied on for filesystem isolation.
The profile exists only on the disposable hosted VM and is removed with that VM.
It does not replace an existing profile, disable AppArmor globally, change
sysctls or change a developer's machine. Ubuntu describes the underlying
[application-specific user-namespace policy](https://discourse.ubuntu.com/t/understanding-apparmor-user-namespace-restriction/58007).

Only trusted system utilities execute with host privileges during entry.
The launcher immediately returns to the captured ordinary runner UID/GID,
clears supplementary groups and all host capability sets, and sets
`no_new_privs` before executing the Python gate or any repository code.
Each gate verifies the actual real/effective/saved IDs, empty groups, five zero
capability masks, `no_new_privs` and the expected enforced profile. A preflight
also verifies two nested levels of user, mount, PID and network namespaces,
with the same profile inherited at both levels. These checks precede the
unchanged strict suite, demo and immutable publication audit. Their namespace
and runtime Landlock boundaries remain in place.

The workflow uses push/pull_request/manual events, read-only repository-content
permission, a full-SHA-pinned checkout action and persist-credentials:false.
There are no provider secrets, caches, artifact uploads, deployments, write
permissions or privileged pull_request_target/workflow_run triggers.
These choices follow [GitHub's secure-use guidance](https://docs.github.com/en/actions/reference/security/secure-use).
The pin resolves to the official [checkout v7.0.1 commit](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1);
updates require review rather than following a mutable tag.

Use /usr/bin/python3 explicitly. The launcher and namespace witnesses invoke
that system path and mount /usr, so selecting another interpreter through PATH
or setup-python would not change the interpreter exercising the installed code.

The [runner image inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
is a changing dependency description, not a compatibility witness. Hosted
kernel/LSM policy, nested namespaces, Landlock and fixture-filesystem creation
times must actually pass. Unsupported profiles fail; the scoped compatibility
profile above does not globally disable host protections, skip required checks
or fall back to an unisolated demo.

Checkout fetches complete history because the immutable auditor refuses shallow
history. The audit reads the manifest from the exact event commit and reports
only categories, locations and hashes. A clean selected snapshot is not rights
clearance, approval of all historical blobs or permission to publish; see the
[publication boundary](engineering/PUBLICATION.md).

## Checking later runs

For each later run, inspect the actual event commit,
runner image, dependency versions, full discovered/run counts, demo artifact
and SQLite outcome, and immutable audit report. Record the workflow URL and
tested commit. A cancelled, skipped, partial or never-started job is not a pass.

Fork pull requests run only on disposable GitHub-hosted runners with the limited
permissions above. Do not move untrusted contribution jobs onto a persistent
self-hosted machine holding live workspaces, credentials or Multithread state.
