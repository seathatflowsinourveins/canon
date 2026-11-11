# Marks, wave 2 (16 runs)

Read by the method in `evals/build/README.md` ("Scoring"): two readers working apart, each with the
patch applied to the case's starting tree (`harness.py tree <case>`), the case file, the run's report and
that section. Neither reader saw the other's marks or the table of workers while marking. The first
reader's reading is ADR 0017's review, `/code-review` at `high` on each copy, with every candidate
checked against the code. Every run's `grade.json` shows `resolved: true`, an empty `flags` list,
nothing lost or restored, and the three code checks passing.

The second reader's marks for three cases (cancel-again-and-unholdable-id, gate-reads-what-the-run-read
and ledger-remaining-defects, 24 runs across waves 2 and 3) reached the first reader before its marks
were final, in the message that closed the second reader's task, so the first reading of those 24 runs
was redone by a fresh first reader who had seen no marks, and the one first mark made before then
(r41c4, `fix`) is set aside. The eight modify-follows-replace runs were first-read by the scoring task,
whose marks were final before it read the second reader's.

One run broke a term and scores nothing, whatever its marks (README, "Scoring"): its report says it
ran scratch scripts outside its tree (rd2e5, report line 81). Its marks are kept below as read.

Blockers are numbered as in the README: 1 the defect remains, 2 its own mechanism or a new dependency,
3 a hidden failure, 4 broken behaviour shown by an input or a sequence of events, 5 a report that
states what the change does not do.

## cancel-again-and-unholdable-id

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r41c4 | land | land | none | none |
| r8844 | fix | fix | ADR 0036's new Evidence line ends with a to-do about landing | the same Evidence line; `_root_id` caches under a refused own ID, so the shared `""` key reports an unrelated closed order under another order's ID (scratch test) |
| rc27f | land | land | none | none |
| rd0ef | fix | fix | ADR 0036's accepted cancel-all decision reversed in place, with no superseding record or dated line | the new test module copies the node harness (about 150 lines); dead branches for a `None` ID and unneeded guards in `_modify_order` and `_adopt` |

## modify-follows-replace

Both readers ran scratch scenarios on the starting tree and on every run. The first reader's seven
scenarios fail three times on the starting tree and pass on the case's fix.

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r2a55 | no (1, 4) | no (4) | defect 5 remains: `replaced` and a late `canceled` of the old order queued together still close the node's order, since the guard reads the cache's venue order ID, which lags the queued update; two partial fills queued before `replaced` give two OrderUpdated | a second modify's 422 after the first's `replaced` is dropped with no OrderModifyRejected (the starting tree emits it) |
| r7312 | no (4) | no (4) | the replacement's `expired` or `canceled` before the old order's `replaced` is dropped, and `replaced` then adopts the closed replacement: the node's order stays working | the same sequence: the order never closes (the starting tree closes it) |
| r807d | land | fix | none | a failed lookup in `_is_superseded` swallowed without a log line; three ID sets grow for the session; the `PARTIALLY_FILLED` promotion wider than recorded |
| rf2a3 | fix | no (4), then fix | after cleared state, a replacement's `new` before `replaced` seeds `_current`, so `replaced` gives no OrderUpdated (not shown to occur); ADR 0036's UNVERIFIED line on a replacement's `rejected` after a fill left untrue | the same sequence, first as a blocker |

## gate-reads-what-the-run-read

The engine's cache keeps 10,000 bars of a bar type by default (`CacheConfig().bar_capacity`, run in the
copies' NautilusTrader 2.0.0rc8). A run that reads its bars from the cache without raising that bound
tells a 10,050-bar sample `FAIL` where the starting tree records it; both readers showed it with a
scratch test.

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| ra964 | no (4, 2) | no (4, 2) | the 10,050-bar sample told `FAIL`; coverage computed by hand over `get_intervals` beside upstream's `get_missing_intervals_for_request` | the same two |
| rd2e5 (broken term) | no (4) | no (4) | the 10,050-bar sample told `FAIL`; the bound stated as a limit, not fixed | the same |
| rd814 | no (4) | no (4) | the 10,050-bar sample told `FAIL`; `run_backtest` with no bounds now refuses a two-load catalog with an unrecorded weekend that it ran before | the same two |
| reb94 | fix | land | the rank scans unresolved records over every sample, disagreeing with `_to_record` once a sample moves beneath a record | none (a wording nit in ADR 0041) |

## ledger-remaining-defects

| run | first | second | first reader's findings | second reader's findings |
|---|---|---|---|---|
| r0a51 | fix | fix | `returns_matrix`'s docstring promises one refusal for non-finite and missing-day series but raises them apart; an added concurrency test cannot fail; a convoluted comprehension | an ADR 0035 Evidence line names a review role the change did not come from |
| r11f8 | land | fix | none | ADR 0035 term 2 not updated for a series `daily_series` rejects, now told `FAIL` with no value |
| rad66 | land | land | none | none |
| rce8e | land | land | none | none |

## Disagreements and how each was settled

The README settles only a `no` against a `land` or `fix`. A `land` against a `fix` is not reread; the
score sheet counts it `fix`, since one reader listed findings a review round would have fixed, the
rule wave 1's sheet set (the README sets none).

* rf2a3: first `fix`, second `no` (4). Both reread the blocker. The sequence needs Alpaca to send a
  `new` for a replacement before `replaced`, and ADR 0042 records no such update in 9 of 9 paper
  replaces, so the sequence is not shown to occur; the second reader moved to `fix`. Settled `fix`.
  The other `no` marks on this case rest on sequences the case statement itself names: r7312's on an
  update of the replacement arriving before `replaced` (item 4 names a fill arriving so), r2a55's and
  r86bb's on two modifies racing (item 8). rf2a3's rests on an update ADR 0042 records Alpaca not
  sending. Neither standard is the README's wording, which asks only for an input or a sequence of
  events; it is the readers' reading.
* r807d: first `land`, second `fix`.
* reb94: first `fix`, second `land`.
* r11f8: first `land`, second `fix`.

## Raw agreement

Before settling, the readers gave the same mark on 12 of 16 runs, and agreed on `no` against not-`no`
on 15 of 16. After settling: 13 of 16, and 16 of 16.
