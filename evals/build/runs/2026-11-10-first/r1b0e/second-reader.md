# Second reader: r1b0e
Mark: no
Blocker: 4. If an earlier modify of the order had an unknown outcome, a later modify that succeeds through alpaca-py's own retry now gets `OrderUpdated` and then a false `OrderModifyRejected`. The starting code gave `OrderUpdated` only.

## Findings
1. packages/canon-alpaca/src/canon_alpaca/execution.py:1233-1243 (`_open_modify`), used at :1592 (`_replaced`) and checked at :1208 (`_replace_refused`). A modify whose answer is a non-422 HTTP error or a read timeout goes through `_log_unknown_replace` and is never settled. It stays in `_modifying` until the order ends. After that, `_open_modify`'s fallback ("the first sent without an answer yet") gives this stale modify to any later `replaced` or `order_replace_rejected` on the same Alpaca order that does not match an answer.

   The sequence: modify n0 is answered HTTP 500, and Alpaca does not apply it. Modify n1 reaches Alpaca, and its first attempt's answer is lost, as in the 504 that alpaca-py retries. Alpaca sends `replaced` A->B, and the repeat is refused 422 "order already replaced". This is the observed retry case in ADR 0036.
   - `_replaced` settles n0 instead of n1 and marks A superseded.
   - `_replace_refused` for n1 then finds `retried` true, but sees `open_` and `A in _superseded`, so it emits `OrderModifyRejected`.
   - The node sees `OrderUpdated` and then `OrderModifyRejected` for a modify that succeeded.

   Shown by scratch test `test_retry_after_an_unknown_modify`:
   - with the patch: `['OrderSubmitted', 'OrderAccepted', 'OrderUpdated', 'OrderModifyRejected']`
   - with the starting execution.py: `['OrderSubmitted', 'OrderAccepted', 'OrderUpdated']`, so the patch introduced this. The same probe without n0 passes on the patch.

   The same root cause leaves two defects in place under that precondition:
   - Defect 2: n0 is answered 500, then n1 is refused on both the stream and the HTTP answer. The result is two `OrderModifyRejected`, because the stream rejection settles n0. The starting code also gave 2.
   - Defect 9: n0 is answered 500, then n1's answer arrives after its `replaced`. `_answers` still holds `(A, 2)` at the end, because `replaced` settled n0 and n1 stayed open (execution.py:1159).

   Each unknown-outcome modify affects the rest of that order's life. (severity: blocker)
2. execution.py:1486-1492. An `order_replace_rejected` with no open modify used to give `OrderModifyRejected`. It now gives nothing and logs at `info`. This covers a modify sent before a disconnect or restart, which clears `_modifying` at :844. The node's order is left `PENDING_UPDATE` for reconciliation, and the only sign is an info line. It should be at least a warning, and should name that reconciliation settles the order. (severity: fix)
3. Report, "The order's end settles its open modifies (`_finish`), so neither map outlives the order". `_finish` runs only for a stream terminal event or `_reject` (execution.py:1363, :1527). An order that reconciliation closes leaves its `_modifying`, `_answers` and `_superseded` entries for the session. Together with finding 1's never-settled modifies, the claim is too strong. (severity: fix)

## Per-defect check
1. Removed: `_order_report` keeps a closed replacement when `_took_over` shows the old order `replaced` by it (execution.py ~1980-2028).
2. Removed in the plain case, via `_modifying`/`_settle` with the 422 path and the stream branch. Not removed when an earlier modify of the same Alpaca order is still open with an unknown outcome (finding 1).
3. Removed: `_superseded` drops a repeated `replaced` (execution.py:1473).
4. Removed for a single replace: `_take_over` sends `OrderUpdated` before the fill. A fill two replaces down with both `replaced` missed is not covered, which the report discloses.
5. Removed for a cancel or expiry after the replace was applied, via `_superseded`. One arriving before `replaced` is still applied, which the report discloses.
6. Removed: `PENDING_REPLACE` maps to `ACCEPTED`, or `PARTIALLY_FILLED` when the order has fills.
7. Removed: `_replaced` follows `replaced_by` up to `MAX_REPLACE_CHAIN`, and `pending_replace` is now mapped.
8. Removed: `replaces_as_asked` now counts the order's quantity, and the retry check excludes replacements another modify holds or has applied.
9. Removed in the plain case (the answer is kept only while its key is open). Not removed with a stale open modify (finding 1).
10. Removed: `_replacement_rejected` runs before the end check, settles the modify, and logs that the fill is the outcome.

## What I ran
- `mise exec -- uv run --locked pytest packages/canon-alpaca -q`: 245 passed, 19 skipped (patch applied).
- Scratch probes in `packages/canon-alpaca/tests/test_zz_reader_scratch.py` (saved to `second-reader-probe.py.txt` beside this sheet, then removed from the tree). Each one drives `client._modify_order` directly, as the case's `test_second_modify_refused_after_the_first_replaced_is_rejected` does:
  - `test_retry_after_an_unknown_modify`: FAILS on the patch (`OrderUpdated`, `OrderModifyRejected`); PASSES on the starting execution.py (`git show HEAD:...`, then restored and checked with `git diff --stat`).
  - `test_retry_alone`: passes on both.
  - `test_refused_twice_after_an_unknown_modify`: 2 rejections on both.
  - `test_answer_after_replaced_with_an_unknown_modify`: fails on the patch (`_answers` keeps `(A, 2)`).
- I did not run any paper tests, use the network beyond `uv`, or commit.
- `grade.json` `flags`: empty. Nothing points to blocker 1.
- Not verified: the NautilusTrader state transition for `OrderModifyRejected` on an `ACCEPTED` order. The strategy received the event, so it was published.

## Reread (settling the first reader's land)
Final mark: no. Blocker: 4. Stands. No two modifies need be in flight at once: a modify answered 500 or timed out stays open in `_modifying` until the order ends, so a later modify, even minutes later, can be matched to it. The ordering is the likely one: installed alpaca-py retries 429 and 504 after `DEFAULT_RETRY_WAIT_SECONDS = 3`, so the first attempt's `replaced` is normally handled before the repeat's 422. The starting code recognised the retry and gave `OrderUpdated` only. Probe rerun: fails on the patch, the control without n0 passes.
