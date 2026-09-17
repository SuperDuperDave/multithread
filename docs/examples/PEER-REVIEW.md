# A peer review that improved v0.4.6

During Multithread’s v0.4.6 development, a protocol fixture exposed a concrete
failure: a malformed optional token counter stopped interpretation before a
valid answer could be retained. The
[initial change](https://github.com/SuperDuperDave/multithread/commit/37a72e190586bb968dc7be76f84d0a3ee5c234b1)
separated measurement validation from answer and control validation. Invalid
measurements became unavailable with named warnings; independently valid
answers survived, while identity, permission and completion checks still applied.

One fresh native Claude review inspected that candidate and returned **three
findings**. The initiating agent checked each against the source:

- **Warnings could outlive the observations they described.** Accepted. A later
  valid measurement could still carry an earlier error, and unrelated Claude
  results could taint retained task metrics. Warnings now follow the selected
  observations; earlier evidence remains available.
- **A missing whole Codex usage-update payload silently cleared measurements.**
  Accepted the warning requirement, but rejected the suggestion to retain old
  counters as current. Missing or null usage objects now produce a named warning
  and unknown current usage. Previous counters remain historical evidence.
- **Extension-retention guidance obscured a provider difference.** Accepted as
  a documentation correction. Claude receipts retain uninterpreted extensions;
  Codex receipts select known counters, leaving extensions in raw output.

The
[follow-up commit](https://github.com/SuperDuperDave/multithread/commit/e943dad30b65afdabce8b6d0f85c392470e0e308)
implemented those dispositions and added **six additional regression tests**
covering observation replacement, unrelated results, recovery and history
preservation. A separate source review checked the correction. The original
native review and its evidence were retained; no second native call was made
merely to improve reporting.

[PR #12](https://github.com/SuperDuperDave/multithread/pull/12) merged the changes
for [v0.4.6](https://github.com/SuperDuperDave/multithread/releases/tag/v0.4.6).
All 687 strict local tests passed at `e943dad`, and the
[Linux hosted checks for that commit](https://github.com/SuperDuperDave/multithread/actions/runs/34772394207)
passed. Those checks exercised protocol fixtures, installed runtime behavior and
the no-account workflow. The Claude review itself inspected source without
running project tests; it did not establish native execution of the final
installed release.

This records a useful contribution to one change. It does not establish an
advantage over same-family review, lower total cost, subscription-quota savings
or broad reliability.
