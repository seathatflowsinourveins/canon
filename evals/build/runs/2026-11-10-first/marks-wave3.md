# Marks, wave 3 (16 runs)

Read by the same method and readers as wave 2 (`marks-wave2.md` gives the method, the blocker numbers
and the record of the 24 runs whose first reading was redone). Every run's `grade.json` shows
`resolved: true`, an empty `flags` list, nothing lost or restored, and the three code checks passing.

Four runs broke a term and score nothing, whatever their marks (README, "Scoring"): each report says
the run wrote files outside its tree. They are ra5b2 (report line 135, a probe under `/tmp`), r7eaf
(line 205, probes under `/tmp`), r9400 (lines 113 to 149, three probes under `/tmp`) and r514e (line
120, three tests in a scratchpad outside the tree). Their marks are kept below as read.

## cancel-again-and-unholdable-id

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r3f5d | land | land | none | none |
| ra5b2 (broken term) | fix | land | ADR 0036's decision reversed in place with no superseding record or dated line; a stale loop comment | none (a stale reason in a comment, a nit) |
| rcce5 | land | fix | none | the new test module copies the node harness (about 200 lines); unneeded guards in `_modify_order` and `_adopt` |
| redd3 | land | fix | none | `_root_id` caches under a refused own ID, so the shared `""` key reports an unrelated closed order as another order's cancel (scratch test); ADR 0049 cites the case brief as its source |

## modify-follows-replace

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r085f | land | fix | none | a 422 or `order_replace_rejected` with no pending entry dropped with no log line; `_order_report` writes modify state undocumented; five ID sets grow |
| r4afc | fix | fix | ADR 0036's UNVERIFIED line on a replacement's `rejected` after a fill left untrue; `_order_status_report` now raises for a `replaced` order with no `replaced_by`, where it returned no report | a `KeyError` on an unexpected status; late ends and unmatched rejections dropped with no log line; two ID sets grow |
| r514e (broken term) | fix | fix | the same ADR 0036 line left untrue; `_replaced` settles the modify before the replacement is read, so if the read fails a later `rejected` of it is only logged | a rejection after a reconnect logged and not emitted; the `PARTIALLY_FILLED` promotion general (stated); ADR 0036's decision edited in place |
| r86bb | fix, then no (4) | no (4) | a late `canceled` of an intermediate replacement still closes the node's order; a rejection with no modify pending dropped with no log line | modify B sent before A's `replaced`, A's `replaced` applied while B's PATCH is out, B refused with 422: no OrderModifyRejected for B (the starting tree emits it) |

## gate-reads-what-the-run-read

Each `no` below is the 10,050-bar sample told `FAIL` (see `marks-wave2.md`).

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r3971 | no (4) | no (4) | the 10,050-bar sample; the bound stated as a limit, not fixed | the same; `variant_study._to_record` not aligned with the gate |
| r552f | no (4) | no (4) | the 10,050-bar sample; the bound stated as a limit | the same; the bound declined as "daily bars stay below 10,000" |
| r7da4 | no (4, 2) | no (4, 2) | the 10,050-bar sample; a coverage merge by hand where upstream's `get_missing_intervals_for_request` exists | the same two; the repository already calls that upstream method |
| r7eaf (broken term) | no (4) | no (4) | the 10,050-bar sample; the report never states the bound | the same; creation completion not limited to an empty study |

## ledger-remaining-defects

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r61f1 | land | land | none | none |
| r9400 (broken term) | land | land | none | none |
| ra9b2 | land | land | none | none |
| ree8b | land | land | none | none (one long docstring line, a nit) |

## Disagreements and how each was settled

* r86bb: first `fix`, second `no` (4). Both reread the blocker. The first reader ran the second
  reader's sequence on the starting tree, which passes, and on r86bb, which fails: a second modify
  refused after the first one's `replaced` gets no OrderModifyRejected, a regression shown by the case
  statement's own race with one timing change. The first reader had set that candidate aside because the
  case's fix keys modifies the same way, which is not the README's test. Settled `no` (4).
* r085f: first `land`, second `fix`. As in wave 2, a `land` against a `fix` is not reread and the
  sheet counts it `fix`.
* ra5b2: first `fix`, second `land` (broken term; scores nothing).
* rcce5: first `land`, second `fix`.
* redd3: first `land`, second `fix`.

## Raw agreement

Before settling, the readers gave the same mark on 11 of 16 runs, and agreed on `no` against not-`no`
on 15 of 16. After settling: 12 of 16, and 16 of 16.

Waves 2 and 3 together: 23 of 32 the same mark before settling (72%), 30 of 32 on `no` against
not-`no`; after settling, 25 of 32 and 32 of 32.
