# First reader: r56d4
Mark: no
Blocker: 4. After a second modify, an `expired` or `canceled` update for the newest replacement is ignored as a "superseded order" when the earlier replacement was adopted and the second `replaced` has not arrived, so the node's order stays open at the older venue order ID after Alpaca has closed the order it holds (the starting code and the case's fix close it), shown by modify A→B with its `replaced` delivered, modify B→C with its `replaced` missed or late, then `expired` or `canceled` for C, or the node's own cancel, which `_cancel_refused` sends on from B to C (probes `test_probe_f1_*`).
## Findings
Line numbers are in `first/copy` (the change) unless another tree is named. `execution.py` is `packages/canon-alpaca/src/canon_alpaca/execution.py`.
1. `packages/canon-alpaca/src/canon_alpaca/execution.py:1520-1527`. Blocker 4. The block ignores a `canceled` or `expired` for any order other than the one in `_adopted`, and it runs before `_adopt_if_live` (1545-1549). The newest replacement's own close is therefore dropped as if it were a superseded order's, and the log calls the newest order "superseded".
   - With A→B adopted and B→C's `replaced` not delivered, C's `expired` or `canceled` leaves the node's order PENDING_UPDATE at B. No OrderExpired or OrderCanceled is generated.
   - If B's late `replaced` then arrives, `_replaced` adopts the expired C. The node's order gets an OrderUpdated and is ACCEPTED at an order Alpaca has expired.
   - The node's own cancel ends stuck in PENDING_CANCEL at B. The cancel of B is refused with 422 (the starting code's reading of "Delete Order by ID" at 281-282; Alpaca's page itself is UNVERIFIED this session). `_cancel_refused` (1094-1101) then cancels C, and C's `canceled` is dropped here.
   - The starting tree and the case's fix generate OrderExpired or OrderCanceled in each case.
   - Statement items 1 and 4 take a missed `replaced`, and a replacement's update before its `replaced`, as events that happen. The case's tests produce them with the `silent` replace script (for example `test_execution_client.py:1440`).
   - The suite does not cover this: all 114 tests in `test_execution_client.py` pass on the change.
   - Shown by the four `test_probe_f1_*` probes below. (severity: blocker)
2. `r56d4/report.md:43-45`. Blocker 5. The report says "A replacement's fill, cancel or expiry arriving before `replaced` now adopts the replacement's terms first (`_adopt_if_live`). It will move forward through a chain of replacements". The same claim is in `execution.py:23-25`, `packages/canon-alpaca/README.md:217-220` and `docs/adr/0036-alpaca-adapter-order-management.md:73-74`.
   - For a fill the claim holds: probe `test_probe_fill_on_newer_replacement_before_its_replaced` adopts C before C's fill.
   - For a cancel or expiry of the second replacement it does not. The change returns at 1520-1527 before `_adopt_if_live` and adopts nothing, as Finding 1's probes show.
   - This has the same cause as Finding 1. (severity: blocker)
3. `execution.py:2055-2056` (`_replacement_took_over`). Blocker 3. `except APIError, OSError: return False` swallows a failed read of the old order and logs nothing.
   - `_order_report` (2011-2021) then returns None for the filled replacement. Statement item 1's filled order goes unreported again, and the error appears in no log line.
   - Every other handler of `APIError` or `OSError` in the client logs, raises again, generates an event or acts on a named 404. Examples: 1083-1088, 1238-1242, 1579-1584, 1942-1945, and 2007-2009 in the same function ("is not reported: tracing it failed"). The one other exception is the same new pattern at 1642-1643; see not-taken item 7.
   - Shown by probe `test_probe_took_over_lookup_failure`: with the read succeeding the report is (B, FILLED); with the second read of A failing it is None, and nothing is logged.
   - The case's fix makes no such read, because its `_order_report` skips only rejected replacements. It reports (B, FILLED) both ways.
   - A log line as at 2007-2009 would remove this, as would the fix's check of the replacement's own status. (severity: blocker)
4. `execution.py:787-793`, `1183-1191` and `1587-1590`. Per-modify state is kept for the session.
   - `_handled_replacements` and `_settled_modifies` gain one entry per modify, and `_adopted` one per modified order. None of them is pruned when the order ends; only `_disconnect` clears them (860-863).
   - That is the same build-up that statement item 9 removes for `_answers`, and that the change's own comment at 1177-1180 names.
   - `_modifying` is written (1185) and deleted (1190-1191), but the client never reads it. Only the case's test reads it (`test_execution_client.py:1452`). Defect 10's real outcome comes from the log line and `_settled_modifies`, so this is not blocker 1.
   - `_replaced` adds to `_handled_replacements` twice (1587, 1590), and `_replacements` is set at 1589 and again in `_adopt` (1661).
   - Shown by probe `test_probe_state_left_after_three_modifies_and_a_fill`. After three modifies and a fill: `_handled_replacements` 3, `_settled_modifies` 3, `_adopted` 1. The fix gives `_adopted` 0, pruned in its `_finish`, and keeps no per-modify set. (severity: fix)
5. `r56d4/report.md:32` and `:86`. Two wording slips in the report.
   - Line 32 says "All changes are in `packages/canon-alpaca/src/canon_alpaca/execution.py`", but the change also edits the README and ADR 0036. The same report names those edits at 63-65.
   - Line 86 explains its 13 passed by "the two `test_fill_on_a_replacement...` parameter cases". As `--collect-only` shows, 13 comes from two parametrised tests of two cases each: that one and `test_replaced_whose_replacement_has_moved_on_still_updates`.
   - Neither slip claims a behaviour the change lacks. (severity: fix)
## Review findings not taken
1. `execution.py:2057` (the review: `_replacement_took_over` returns True for any closed replacement whose prior is REPLACED, so if replacement B is rejected while A stays live and A is then replaced by C, B is reported REJECTED).
   - This needs a replacement rejected while its old order stays live and is later replaced again.
   - The one cause a source gives for a replacement's rejection is the old order filling first (Alpaca's text quoted at 1488-1490; statement item 10). After that, A cannot be replaced.
   - ADR 0036's paper observation saw a refused replace answered with 422 and no replacement created. No source shows a live A beside a rejected B.
2. `execution.py:320` (the review: mapping `pending_replace` as open gives two open reports for one client order ID).
   - Statement item 6 requires `pending_replace` to be reported open, and partly filled when it has fills. The case's fix maps it the same way.
   - No source shows Alpaca listing the replacement as open while the old order is still `pending_replace`. ADR 0036 lists `pending_replace` among what no run showed.
   - What NautilusTrader would do with such a pair is under Declined to judge.
3. `execution.py:1504` (the review: a replacement's `rejected` keys on B while the 422 and `order_replace_rejected` key on A, so both arriving give two rejections).
   - This needs `order_replace_rejected` for A and `rejected` for B from one replace, and no source shows that pair. ADR 0036 lists `order_replace_rejected` as unobserved, and statement item 2 pairs the HTTP refusal with `order_replace_rejected`.
   - The documented replacement rejection, where the old order filled first, is keyed on `replaces` (1498-1499).
4. `execution.py:1186` (the review: dedup is per venue order and `_begin_modify` clears it, so a late stream refusal of modify 1 is reported against modify 2).
   - Alpaca's `order_replace_rejected` names the order, not the request, so no client can tell two attempts on one venue order apart.
   - By reading, the case's fix keys the same way: `_modifying[target]` is set at its line 1119 and popped at its line 1200. It gives the same events for this sequence, so this is not a defect of this change against the case.
   - The report's "at most one `OrderModifyRejected` per attempt" (report.md:52) holds only up to this limit.
5. `execution.py:1514` (the review: a cancel or expiry of an order with `replaced_by` goes to `_replaced` without checking takeover, so a rejected B is adopted when a live A is canceled). This needs the same state no source shows as item 1: a live old order whose `replaced_by` names a rejected replacement.
6. `execution.py:577` (the review: `replaces_as_asked` compares an omitted qty with the replaced order's qty, which can misjudge a retry on a partly filled order).
   - It rests on what a replacement's `qty` means for a partly filled order, which ADR 0036 lists as unknown ("a modify of a partially filled order (what `qty` means then)").
   - The case's fix compares with the node order's quantity instead. That equals the replaced order's qty in every sequence the sources show.
7. `execution.py:1632` (the review: up to 16 serial reads in the consumer, and a lookup error returns False, so the fill is applied under the old qty).
   - For the replacement directly after the adopted order, `_replaces_chain_contains` returns at 1638-1639 without a read (probe fill-chain).
   - A chain two or more links past the adopted order cannot come from the node's own modifies. A modify names the venue order the node holds, and a second replace of a replaced order is refused with 422 "order already replaced" (ADR 0036, observed).
   - A backward walk returns False whether its read succeeds or fails.
   - The starting code's `_node_order_id` already walks up to 16 links in the same consumer (1426-1445, unchanged).
   - The silent handler the review names has a reachable twin, taken as Finding 3.
8. `execution.py:1511` (the review: the `_finished` guard now sits below `replaced`, so a late `replaced` for a closed order makes reads and can emit OrderUpdated on it).
   - In the sequences the case and its sources show, a late `replaced` for a finished order resolves through `_live_order` to the replacement the node already names. `_already_adopted` (1651-1656) then returns before `_adopt`.
   - The review's case needs a `replaced` delivered after a restart while the node names an older order. No source shows the stream re-sending one.
   - The extra read is a cost, not a wrong event. The wrong OrderUpdated in Finding 1's late-`replaced` probe comes from 1520-1527 leaving the order open, not from this guard.
## Per-defect check
Each named test (11 tests, 13 cases) passes on the change and fails on the starting tree; both runs were made this session.
1. "When the `replaced` update never reaches the client and the replacement then fills, reconciliation reports neither the replacement nor the old order, so the node's order is never reported filled and its fill is lost": removed. `test_filled_replacement_is_reported_when_replaced_was_missed` passes. Probe took_over[read-ok] gives (B, FILLED) on the change and the fix, and None on the starting tree. The removal depends on a read of the old order; if that read fails, the defect silently returns (Finding 3).
2. "A replace that Alpaca refuses on the HTTP answer and again with an `order_replace_rejected` update gives two `OrderModifyRejected` events for one modify": removed. `test_replace_rejected_twice_over_is_one_modify_rejection` passes. By reading: the first refusal to reach `_modify_rejected` records A in `_settled_modifies` (1207-1209), and the second returns at 1207.
3. "A `replaced` update delivered twice gives two `OrderUpdated` events": removed. `test_replaced_delivered_twice_is_one_update` passes; the second `replaced` stops at `_already_adopted` (1591).
4. "A fill on the replacement that arrives before `replaced` is applied to the node's order under the old terms: a fill larger than the old quantity is dropped as an overfill, and the order takes the replacement's terms only after the fill": removed. Both cases of `test_fill_on_a_replacement_before_its_replaced_applies_the_replacement_first` ([5] and [1]) pass. Probe fill-chain adds a second replacement C while B is adopted: on the change and the fix, C's terms are adopted before its fill; on the starting tree they are not.
5. "A `canceled` or `expired` update for the old order, arriving after it was replaced, closes the node's order while its replacement still works at Alpaca": removed. `test_late_cancel_of_the_replaced_order_does_not_cancel_the_replacement` passes, and an order with `replaced_by` goes to `_replaced` (1514-1519). The block after it overreaches: the newest replacement's own cancel or expiry is now dropped too (Finding 1).
6. "`pending_replace` is not a status the client maps, so an open order that is mid-replace fails start-up reconciliation; one that has fills should be reported partly filled": removed. `test_open_order_pending_replace_at_startup_is_reported` and `test_pending_replace_with_fills_is_reported_partly_filled` pass. The mapping is at 320 and 632.
7. "When the replacement read at `replaced` is itself pending replace, or already replaced again, the client raises and generates no `OrderUpdated` instead of following it to the live order": removed. Both cases of `test_replaced_whose_replacement_has_moved_on_still_updates` ([pending_replace] and [replaced]) pass, through `_live_order` (1596-1613).
8. "A second modify sent before the first's `replaced`, and refused by Alpaca, is taken for alpaca-py's retry of the first when the first replacement already holds the terms it asked for, so it gets no `OrderModifyRejected`": removed. `test_second_modify_refused_after_the_first_replaced_is_rejected` passes. `replaces_as_asked` (554-583) now fills the request's omitted fields from the replaced order.
9. "A PATCH answer that arrives after its `replaced` was applied is stored and never removed, so answers build up for the whole session": removed for answers. `test_answer_after_replaced_is_not_kept` passes, and probe state shows `_answers` 0. Two new per-modify sets now build up the same way (Finding 4).
10. "When the old order fills before Alpaca rejects its replacement, the client returns before the branch written for that case, so nothing says what became of the modify": removed. `test_rejected_replacement_after_the_old_order_filled_settles_the_modify` passes. The branch at 1486-1501 now runs before the `_finished` guard, logs "which had filled first; the fill is the outcome" and settles the modify.
## Probes
The fragment is `$S/r56d4/first/scratch/probes_tail.py.txt`. It is appended to the starting tree's `test_execution_client.py` to make `packages/canon-alpaca/tests/test_probe_r56d4.py`, which is byte-identical in three trees:
- `first/copy` is the change. The module was removed from it afterwards, so the copy again holds only the change.
- `first/base` is the starting tree, made with `harness.py tree modify-follows-replace`.
- `first/fix` is `first/base` with `execution.py` from the fix 52ac69cc. Its tests and conftest already match the fix commit's, and the fix's diff is at `first/scratch/fix-exec.diff`.

Run from each tree's root: `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_probe_r56d4.py -q -p no:cacheprovider -rA -k "probe_f1 or probe_took_over or probe_state or probe_fill_on_newer" -s`. In every probe, A is the node's first order and B the first replacement, whose `replaced` is delivered. C is the second replacement, whose `replaced` is withheld unless the probe says otherwise.

| Probe | Change | Starting tree | Fix |
|---|---|---|---|
| `test_probe_f1_newer_replacement_closed_before_its_replaced[expired]` | FAILED: no OrderExpired; PENDING_UPDATE at B; log "Alpaca expired of superseded order C ... is ignored; the node's order is its replacement" | passed: OrderExpired, EXPIRED | passed: OrderExpired, EXPIRED |
| `test_probe_f1_newer_replacement_closed_before_its_replaced[canceled]` | FAILED: no OrderCanceled; PENDING_UPDATE at B; the same log line for `canceled` | passed: OrderCanceled, CANCELED | passed: OrderCanceled, CANCELED |
| `test_probe_f1_late_replaced_after_the_newest_expired` (C expires, then B's `replaced` arrives) | FAILED: PENDING_UPDATE at B after the expiry, then OrderUpdated and ACCEPTED at C, which Alpaca has expired | passed: EXPIRED, unchanged by the late `replaced` | passed: EXPIRED, unchanged |
| `test_probe_f1_node_cancel_after_the_second_replace_unseen` (cancel of a replaced order refused 422 "order is not cancelable") | FAILED: cancels sent to B, then C; C canceled at Alpaca; the node's order PENDING_CANCEL at B, no OrderCanceled | passed: OrderPendingCancel, then OrderCanceled | passed: OrderPendingCancel, then OrderCanceled |
| `test_probe_took_over_lookup_failure[read-ok]` (`replaced` missed, B filled, single-order report asked for A) | passed: (B, FILLED); 2 reads of A | FAILED: None (defect 1) | passed: (B, FILLED); 1 read of A |
| `test_probe_took_over_lookup_failure[read-fails-once]` (the second read of A raises APIError 500) | FAILED: None; the error is in no log line | FAILED: None (defect 1; only one read, so nothing failed) | passed: (B, FILLED); one read, so nothing failed |
| `test_probe_state_left_after_three_modifies_and_a_fill` (records sizes; no assertion) | `_answers` 0, `_replacements` 3, `_adopted` 1, `_handled_replacements` 3, `_settled_modifies` 3, `_modifying` 0 | `_answers` 0, `_replacements` 3; the other four do not exist | `_answers` 0, `_replacements` 3, `_adopted` 0, `_modifying` 0; the other two do not exist |
| `test_probe_fill_on_newer_replacement_before_its_replaced` (C fills before B's `replaced`) | passed: OrderUpdated twice, then OrderFilled; FILLED at C | FAILED: one OrderUpdated (B's), then OrderFilled without C's terms | passed: as the change |

Totals: on the change 5 failed and 3 passed; on the starting tree 3 failed and 5 passed; on the fix 8 passed.

Other runs this session, on `first/copy` unless named:
- The 13 named cases: 13 passed. On `first/base`: 13 failed.
- `test_execution_client.py`: 114 passed.
- `packages/canon-alpaca/tests` with the probe module ignored: 242 passed, 19 skipped, as the report says. The skipped modules are the paper tests, which skip unless a key pair is in the environment. None was, so no key was read and nothing reached Alpaca.
- On `execution.py`: `ruff check --no-cache`, `ruff format --check --no-cache` and `ty check` all passed.
- `scripts/canon lint`: "canon: lint clean".
- `diff -r` of the copy against `first/base` shows only the three changed files, so "No test file was edited" holds. `grade.json` has `flags: []`.

One departure from the brief: the fix's diff was first written to `/tmp/r56d4-fix-exec.diff`, outside `first/`. It was deleted at once and kept at `first/scratch/fix-exec.diff` instead.
## Declined to judge
- How NautilusTrader reconciles two open reports under one client order ID with different venue order IDs (review item for line 320): I did not read the pinned NautilusTrader source for this. UNVERIFIED.
- Whether a NautilusTrader open-order check would later close the order Finding 1 leaves open, and what the engine does with a None single-order report (Finding 3): not read this session. UNVERIFIED. Neither changes what the probes show the client generating.
- How Alpaca actually delivers these trade updates (a missed or late `replaced`, a replacement's update before its `replaced`, `order_replace_rejected`, `pending_replace`): taken from the case's statement and ADR 0036. No paper run was made, since the brief forbids it. UNVERIFIED.
- The 422 for cancelling a replaced order, used in the cancel probe: taken from the "Delete Order by ID" quote at execution.py:281-282 and the existing test at `test_execution_client.py:1007`. Alpaca's page was not fetched this session. UNVERIFIED.
- The whole-workspace run (370 passed, 21 skipped) and ty over the whole workspace: these come from `grade.json` and the report and were not re-run. UNVERIFIED.
