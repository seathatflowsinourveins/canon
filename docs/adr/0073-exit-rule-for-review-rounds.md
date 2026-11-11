---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-11
review-by: 2026-12-11
---

# An exit rule for review rounds: a path leaves after two rounds with no finding that changes behaviour, and a family found twice is redesigned as one model, tuned from each round's count

## Context and Problem Statement

On 2026-11-10 the session window had five review rounds, and the gate three that have saved reports (the
third reader's round behind #141 has none). Every round found new defects. Nothing said when a path had
been reviewed enough, or when to stop patching a kind of defect and redesign instead. ADR 0045 sets who
reviews; this record sets when a path stops being reviewed.

## Decision Drivers

* A round ends on evidence a later reader can check, not on fatigue.
* A defect a test cannot show changes nothing a trader or a gate sees, and should not hold a path open.
* Readers stay independent (ADR 0045, ADR 0046 rule 2).

## Considered Options

1. No exit rule: every change to a high-effort path is read by every reader, with no end (the state).
2. A fixed number of rounds per path.
3. Exit after two consecutive clean rounds by independent readers, re-entry on any change, and a redesign
   for a family found twice.
4. As 3, with "clean" meaning no finding that changes behaviour, and the count written by the owning lane.

## Decision Outcome

Chosen: **option 4**, accepted by the lead under the owner's delegation of 2026-11-11 and applying from
then. Option 2 would have stopped the gate after round 2, but round 3 still found a
defect that let a mixed study pass at DSR 0.9922 (Codex, currency precision; whether the normal workflow
ever changes the currency's registration is UNVERIFIED in that report, and Muse showed a catalog round
trip resets it). Option 3 would hold a path
open for a vacuous test or a log line. Its only evidence so far is one night's counts, and no path has met
it yet; each owning lane writes its round's count in the fix message (rule 5), and a path's next round
reviews the redesigned model (rule 4).

1. A *path* is the set of files a high-effort review covers: the session window; the gate; the paper
   runner and automation; the adapter's order and data paths. A *round* is one or more independent readers
   on one commit of the path. Readers are independent when they run on different runtimes, and none has
   seen another's findings.
2. A finding *changes behaviour* if the owning lane can show, by a test that fails before its fix, that it
   changes an order or position at the venue, a record (heartbeat, firing, ledger), a gate result, a lock or
   deadline outcome, or an exit status. A test-only defect, an unclear doc, a missing log line and an
   absorption the code already states do not count.
3. A path leaves the loop after **two consecutive rounds with no finding that changes behaviour**. Any
   change to a file of the path puts it back. The first round after re-entry may be one reader.
4. **A family found in two rounds is redesigned as one model before a third round.** A family is a set of
   defects one mechanism could have prevented: the account check's notion of "settled", or what a trial's
   key and digest hold. The redesign names that mechanism in the path's record, and the next round reviews
   the model, not the patches. If a redesigned family turns up again, the model is reopened, not patched.
5. The owning lane writes the round's count in the fix's commit message: findings, those that change
   behaviour, new families, and members of known families. That count is the rule's evidence, and the
   rule is tuned from it.

Added 2026-11-11, decided by the lead under ADR 0074. For the gate, a finding counts against a clean
round only when the state it needs can be produced by one of the ledger's named writers, or by an input
that passes the gate's own input check. A finding that needs a ledger record written by hand is recorded
once, as a class, among the gate's limits: the gate trusts the ledger as its writers write it and does
not defend against someone with write access forging a record, which no input check could prevent. Each
reader's task states this rule.

**What it would have done on 2026-11-10** (counts under Confirmation):

* *Session window.* The account check's family (which orders and closes are settled) appeared in round 1
  and again in round 2, where all four readers found the "sent" heuristic. The rule would have forced the
  redesign after round 2. It landed after round 4 (#123: "twelve defects, all one question asked badly"),
  so rounds 3 and 4 patched the old model and raised 33 more findings in known families (raw, across
  readers; #123 counts twelve distinct defects in the account check's family).
* *Gate.* The data-identity and config-key families appeared in rounds 1 and 2. Their redesign (#133
  refuses an unknown keyword; #141 compares instrument definitions the node's own way) would have come
  before round 3, which found both again.
* *Adapter.* The replace-chain family appeared in rounds 1 and 2: 14 of round 2's 20 findings. The rule
  would have forced the one-record model (#135) after round 2. After #135, two readers still found 11
  distinct defects, five of them labelled high, in the replace chain and in fill counting, so rule 4's
  reopening applies now.
* *Runner and automation.* No family reached two rounds before its fix. But a misread `security` exit
  status was found in the paper tasks (sweep A, fixed in #140) and again in OmniRoute's keychain tasks
  (that review's finding 6). Under rule 4 the next fix is one shared reading of `security`'s status, not
  another copy.
* No path met rule 3: every saved round found something that changes behaviour. All four stay in the loop,
  and each one's next round reviews the redesigned model.

*Amended 2026-11-11 by ADR 0074:* rules 3 and 4 apply to the trading paths (the adapter's order and data
paths, the session window, the gate) and to the runner's keychain tasks. A harness or tooling path gets
the review ADR 0017 requires and at most one outside round.

### Consequences

* Good: a round has a stopping condition, and a repeated family becomes one design, not a series of
  patches.
* Bad: "changes behaviour" and "family" are judged by the owning lane. A lane can mislabel, so the count
  goes in the commit message, where a later reader can dispute it.
* Bad: two clean rounds are not proof. A path that leaves can still hold a defect no reader found.

### Confirmation

Counts from the second readers' saved reports of 2026-11-10, kept outside the repository. *Raw* is the
findings as each reader numbered them, summed across readers; for a sweep, the merged entries on the
path's files. *Distinct* merges the same defect reported by
several readers. *High* counts only findings a reader labelled high, P1, severe or critical. Antigravity,
and some Codex and Muse rounds, label none, so this column undercounts. Families are this record's grouping
of the readers' one-line statements, not the readers' own.

| Path | Round (commit) | Readers | Raw | Distinct | High | New families | Known-family members |
|---|---|---|---|---|---|---|---|
| Session window | 1 (#83) | Ag | 7 | 7 | - | 4 | 3 |
| | 2 (#97) | Ag Cx Ds Mu | 21 | 15 | 3 | 2 | 19 |
| | 3 (#101, #105) | Cx Ds Mu | 15 | 9 | 2 (one defect) | 1 | 14 |
| | 4 (#110, #114) | Ag Cx Ds Mu | 20 | 14 | 2 | 1 | 19 |
| | 5 (sweep B) | Ds Mu | 10 entries | 10 | - | 2 (a default or guard on absence; stand-in fidelity) | 8 |
| Gate | 1 (#87) | Ag Cx Mu | 19 | 17 | - | 6 | 13 |
| | 2 (#100, #109) | Ag Cx Mu | 14 | 12 | 2 | 0 | 14 |
| | 3 (#116, #120) | Ag Cx Mu | 10 | 9 | 0 | 1 (vacuous test) | 9 |
| Runner and automation | 1 (#85, #95) | Ag Cx Mu | 26 | 24 | - | 5 | 21 |
| | 2 (#112, #115) | Cx Mu | 12 | 11 | 1 | 1 (installer, launchctl) | 11 |
| | 3 (sweep A) | Ag Cx | 19 entries | 19 | 1 | 3 (`security` status, dropped task arguments, a constant test) | 16 |
| Adapter | 1 (#75, #77) | Ag | 9 | 9 | - | 4 | 5 |
| | 2 (#99, #107) | Ag Cx Mu | 20 | 19 | - | 1 | 19 (14 replace chain) |
| | 3 (after #135) | Cx Mu | 13 | 11 | 5 | 0 | 13 |

Ag Antigravity, Cx Codex, Ds DeepSeek Harness, Mu Muse. In each row, new families plus known-family members equals raw. A dash means no reader in the round labels
severity. Session window families: which orders and closes are settled; time bounds; node stop and worker
waits; errors not caught or masked; the strategy's order state; disposal before the reads; reconciliation
onto `EXTERNAL`; vacuous tests. Gate families: what the digest holds; what the key holds; records not in
the writer's shape; not-run marks; sample bounds and timestamps; a refused `tell`; vacuous tests.

The rule is followed when each high-effort fix's message carries its round's count (rule 5), and the brief
for a path still in the loop names the families found so far.

*Addition of 2026-11-11, the fix marker:* rule 2 counts a finding by a test that fails before its fix,
rule 5's count and ADR 0057's cases (a case's fail-to-pass set) rest on that test, and ADR 0046 notes
that "the test failed or hung before the fix rests on the fix's message", in prose. A commit that fixes a
finding now carries git trailers (git-interpret-trailers(1)): `Fails-before: <test file>::<test
function>`, one per test that failed before the fix, and optionally `Confirms: <round or
reader>/<finding>`. No maintained convention names the test that failed before a fix: git's trailer
documentation lists none; AOSP's `Test:` and Fuchsia's `Test:` and `Multiply:` name tests run on a change;
the Linux kernel's `Fixes:` and `Closes:` name the commit that introduced a bug and a report, and
`Confirms` overlaps them and Fuchsia's `Fixed:` only in tying a fix to a report. `scripts/land` adds every
commit's two trailers to the squash message's last paragraph, prints one warning line when a change on
the trading path says it fixes a finding (a `Confirms` trailer, or fix, finding or defect as a word in
its text; a heuristic, so a fix worded otherwise is missed and a mention of `fix-index` warns) but names
no `Fails-before`, and one for each named test, file or class the tree lacks. It never refuses a change
for a missing or malformed marker. Its `TRADING_PATHS` list holds the paths of rule 1 that ADR 0074 item
4 calls the trading path (the whole adapter package, the session window and what it runs, the gate),
plus the paper runner and the sandbox node on Alpaca's live data. `scripts/fix-index` prints landed
commits with the two trailers as JSON, parsed by git's `%(trailers)` format. GitHub's squash merge kept
the one-trailer last paragraph `scripts/land` passed it on #183 to #186; a trailer added by this change
reaching main through GitHub is UNVERIFIED until the first fix lands with one.

## Evidence

Read 2026-11-10 and 2026-11-11.

* https://docs.alpaca.markets/us/docs/working-with-orders.md — cited by #123 for the close under a client
  order ID of the check's own, the redesign the session window's family led to (read in #123's Evidence;
  not fetched here, UNVERIFIED by this record).
* Commits on `main` from ec6bd5e to 5df7c9f (`git log`): #123's Summary ("Four independent reviews of the
  account check ... found twelve defects, all one question asked badly"); #133, #135 and #141 for the
  other redesigns; #140 for the `security` status (read).
* The second readers' reports per round, kept outside the repository: session window #83, #97, #101/#105,
  #110/#114 and sweep B; gate #87, #100/#109, #116/#120; runner #85/#95, #112/#115 and sweep A; adapter
  #75/#77, #99/#107 and the two reviews of 5df7c9f after #135; OmniRoute's reviews of #139 (read, counted
  by hand). An independent recount on 2026-11-11 corrected the adapter's round 3 (a second report was
  missed) and the two sweep rows.
* The merged sweep of four readers' silent-fallback reports, 71 entries (read).
* The reachability rule added 2026-11-11 is the lead's decision under ADR 0074, given to the lanes of
  the gate's seventh round. ADR 0054 term 7 holds the class it names, with the two earlier findings it
  reclassifies, #172 and #177 (`git log`, read).
* https://git-scm.com/docs/git-interpret-trailers and https://git-scm.com/docs/pretty-formats — the
  trailer block, key syntax and `--no-divider`; `%(trailers:key=...,valueonly,unfold,separator=...)`,
  `key=` repeatable and case-insensitive; no documented trailer names a test (fetched 2026-11-11, and
  re-read by the evidence-verifier).
* https://docs.kernel.org/process/submitting-patches.html — `Fixes:` takes a commit, `Closes:` a report's
  URL, no tag names a failing test (fetched 2026-11-11, summarised by the fetch tool).
* https://android.googlesource.com/platform/tools/repohooks/+/refs/heads/master/README.md (`Test:` line),
  https://fuchsia.dev/fuchsia-src/contribute/commit-message-style-guide and
  https://fuchsia.dev/fuchsia-src/development/testing/testing_for_flakiness_in_cq (`Test:`, `Multiply:`),
  https://www.conventionalcommits.org/en/v1.0.0/ (no such footer) — found and read by the
  evidence-verifier, 2026-11-11, not fetched again here. `scripts/survey-github report
  canon/github-practice.json`: the survey measures co-author trailers only (run 2026-11-11).
* `git log --format='%s | %(trailers:key=Co-Authored-By,valueonly)' f76675e..349fc9e` (#183 to #186), and
  `scripts/test-land`'s fix-marker cases (run 2026-11-11). An evidence-verifier pass on 2026-11-11
  corrected the convention claim and the trading path's scope.
