# Report: Alpaca replace arriving late, twice or out of order (task_37f1df5112fb)

## Result

All 11 named tests (13 cases with their parameters) now pass, and so does every other test:
`mise exec -- uv run --locked pytest` gives **370 passed, 21 skipped**. The skipped ones are the paper tests,
which need keys and were not run. `ruff check .`, `ruff format --check .`, `ty check` and `scripts/canon lint`
are all clean. No test file was edited and no dependency was added. The change is in the working tree,
not committed.

## What was wrong

The execution client only had a few separate facts about a modify: the PATCH answers (`_answers`), the
`replaces` trace cache, and the set of orders that had ended (`_finished`). It never recorded the modify
itself as something with exactly one outcome, and it judged "which Alpaca order does the node's order name"
by the cache. The defects, one by one:

1. **Missed `replaced`, replacement filled.** `_order_report` skipped every *closed* replacement whose ID
   the node's order did not name. That rule was meant for a replacement Alpaca *rejected* (the old order
   filled first). It also hid a filled, canceled or expired replacement, and its old order is `replaced`,
   which is skipped too. So nothing reported the node's order.
2. **Two `OrderModifyRejected` for one modify.** The HTTP 422 and the stream's `order_replace_rejected` each
   generated one. Nothing recorded that the modify was already settled.
3. **Two `OrderUpdated` for `replaced` delivered twice.** `_replaced` had no check that it had already been
   applied. With the answer gone, it re-read the replacement and updated again.
4. **Replacement fill before `replaced`.** The fill was applied to the node's order under the old terms,
   so a fill larger than the old quantity was an overfill. Nothing applied the replacement's terms first.
5. **Late `canceled` or `expired` of the old order.** These closed the node's order even though it already
   named the working replacement.
6. **`pending_replace` status.** It was not in `OPEN_STATUSES`, so an open order in that status raised and
   failed start-up reconciliation.
7. **Replacement already pending replace or replaced again.** `_replaced` passed it straight to
   `order_status_report`. That raises for `pending_replace` (unmapped) and for `replaced`, so no
   `OrderUpdated` was generated.
8. **Second modify refused "order already replaced".** It was taken for alpaca-py's retry whenever the
   replacement matched its terms, even when that replacement came from the *first* modify still in flight.
9. **PATCH answer after `replaced`.** The answer was stored after `_replaced` had already run, and nothing
   ever removed it.
10. **Replacement rejected after the old order filled.** The fill put the order in `_finished`, and
    `_apply` returned on that check before reaching the branch for this case. The modify was left with no
    outcome.

## What I changed, and why that way

All code changes are in `packages/canon-alpaca/src/canon_alpaca/execution.py`.

- **`_modifying: dict[str, ClientOrderId]`** (the name test 10 reads) records each replace in flight. The
  key is the Alpaca order it was sent to; the value is the node's order. It is set before the PATCH call
  and settled exactly once:
  - by `replaced` or by adopting the replacement;
  - by the first refusal, whether the HTTP 422, `order_replace_rejected` or the replacement's `rejected`;
  - or by the node's order ending (`_finish`).

  A later refusal of a settled modify is logged, not emitted again (defects 2 and 10). An answer is kept in
  `_answers` only while its modify is still in `_modifying`, and adopting a replacement removes its answer.
  So an answer that comes back after `replaced` is never kept, whichever of the two the loop runs first
  (defect 9).
- **Contended modifies (defect 8).** When a replace is sent, the client notes whether another replace of
  the same Alpaca order is already in flight. A refusal is taken for alpaca-py's retry only if the
  replacement holds the asked terms *and* nothing else was in flight. Otherwise the replacement may belong
  to the other modify, so this one is rejected.
- **`_named`** holds the Alpaca order each modified node order names, as of this client's last
  `OrderUpdated`. I added it because `ExecutionClient.generate_order_updated` only *queues* the event (its
  docstring in the installed `nautilus_trader/live/clients.py`: "Queue the order updated event"), so the
  cache can lag. This follows the pattern `_accepted` already uses for `OrderAccepted`.
- **`_adopt`** generates `OrderUpdated` once per replacement, and nothing if the node's order already names
  it. Both `_replaced` and the new early-adoption path use it:
  - `replaced` that the node's order is already past generates nothing (defect 3).
  - A not-yet-adopted replacement's own `fill`, `partial_fill`, `canceled` or `expired` adopts it from
    that update's order before the fill or end is applied (defect 4). `new` and `accepted` do not adopt,
    as the existing test `test_replacement_new_then_rejected_is_not_an_order_rejection` requires, since
    the replacement may still be rejected.
- **Superseded orders (defect 5).** A `canceled`, `expired` or `rejected` update for an Alpaca order the
  node's order no longer names is logged and ignored when that update is either the original order's or
  a replacement's that carries `replaced_by`.
- **Following the chain (defect 7).** `_replaced` follows `replaced_by` through a replacement that is
  itself `replaced`, up to the existing `MAX_REPLACE_CHAIN`. This is the same walk `_order_status_report`
  already does.
- **Statuses (defect 6).** `PENDING_REPLACE` maps to `ACCEPTED` in `OPEN_STATUSES`. `order_status_report`
  now reports any working (`ACCEPTED`-mapped) order that has fills as `PARTIALLY_FILLED`.
- **Reconciliation (defect 1).** `_order_report` now leaves out only a replacement whose status is
  `rejected`, the one case where it never took over.
- **Branch order (defect 10).** The replacement-rejected branch now runs before the `_finished` check. If
  the node's order has ended, it logs "...which had filled first; the fill is the outcome" and settles the
  modify. The test's own comment notes that NautilusTrader drops a modify rejection for a filled order.
  `_finished` became a dict holding the event that ended each order, so that message is exact.

**Text made untrue, now corrected:**
- The module header, the `_modify_order` and `order_status_report` docstrings and the `OPEN_STATUSES`
  docstring.
- The package README: the Modify bullet, the Stream bullet (it listed `replaced` as unmapped, which was
  already stale) and the `test_execution_client.py` row of the Tests table.
- ADR 0036's decision sentence "`OrderUpdated` comes only with `replaced`". It now also covers the
  replacement's own fill, cancel or expiry, once per replacement. That is a narrow correction inside
  option (b), which is still "wait for the venue's stream", not a new decision. If the owner reads it as
  a change of decision, it should become a superseding ADR instead.

## Upstream API or contract followed

- **NautilusTrader v2.0.0rc8 `nautilus_trader.live.clients.ExecutionClient`**: the `generate_order_updated`,
  `generate_order_modify_rejected`, `generate_order_filled` and so on signatures as installed. Its "Queue"
  semantics were read in the installed source this session. Upstream's execution policies, "Command
  outcomes", give one outcome per command; the module header already cites them. I did not re-fetch them
  (network was not allowed).
- **Alpaca's "Replace Order by ID"** and the paper observation recorded in ADR 0036: `replaced` carries the
  old order with `replaced_by`; a second replace is refused 422 "order already replaced"; a replacement is
  rejected if the old order fills first. These are quoted from the existing header and ADR, not re-fetched.
- **alpaca-py 0.45.0 `OrderStatus.PENDING_REPLACE`**, read in the installed `alpaca/trading/enums.py`.

## What I ran

| Command | Result |
|---|---|
| `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q`, before the change | 13 failed, 101 passed (the named tests) |
| Same, after the change, 4 runs | 114 passed each time |
| The 34 modify, replace and answer tests, `-k "replace or modify or replacement or answer"`, 8 runs | 34 passed each time (checking timing-dependent tests for flakiness) |
| `mise exec -- uv run --locked pytest -q` (whole workspace) | 370 passed, 21 skipped (paper) |
| `ruff check .`, `ruff format --check .`, `ty check` | all clean |
| `scripts/canon lint` | clean |
| Three throwaway tests in the scratchpad (not in the repo), using `test_execution_client.py`'s harness | passed: `replaced` after the replacement's own partial fill gives one `OrderUpdated` and leaves `_modifying` and `_answers` empty; a replacement canceled before `replaced` gives `OrderUpdated` then `OrderCanceled` on the replacement; a plain refusal gives one rejection and clears `_modifying` |

I added no test to the repo. The harness lives in `test_execution_client.py`, which I may not edit, and a
new file would have had to copy it or import it by path.

## Not run, and what I'm unsure of

- **No review ran.** `/code-review` would normally run before landing, but the brief rules out other
  agents, so this change has had no independent review. I did not run `scripts/ci` (the brief says this
  copy lacks files it expects) or any paper test.
- **`pending_replace` and the out-of-order cases are offline only.** ADR 0036 already lists
  `pending_replace` and `order_replace_rejected` as `UNVERIFIED`. Mapping `pending_replace` to working
  (`ACCEPTED`, or `PARTIALLY_FILLED` with fills) rests on the status name and the tests, not on Alpaca's
  Order Lifecycle page, which I could not fetch this session: `UNVERIFIED`.
- **The `PARTIALLY_FILLED` rule is general.** It also applies to `new`, `accepted` or `pending_new` with a
  nonzero `filled_qty`. I believe Alpaca never reports that, but it is `UNVERIFIED`.
- **The contended rule can misjudge one rare case.** If two replaces of the same order are in flight, the
  first is refused, and alpaca-py's retry of the second is then refused "already replaced" while Alpaca
  applied the second, then the second gets `OrderModifyRejected` followed by `OrderUpdated` when `replaced`
  arrives. I chose a false rejection, which the later `OrderUpdated` corrects, over a silent missing
  rejection.
- **After a restart, `_modifying` is empty.** A late `order_replace_rejected`, or a replacement's
  `rejected` for a modify sent before the restart, is then logged, not emitted. Reconciliation settles
  that order.
- **Superseded-update rule.** A `canceled` or `expired` for the *original* order after the node has
  adopted a replacement is ignored, on the reasoning that Alpaca does not cancel a replaced order
  separately. If Alpaca can cancel the old order *and* the adopted replacement survives some other way,
  reconciliation would have to resolve it: `UNVERIFIED`.
- **Not fixed here:** `_order_report` still logs "its type, side, class, quantity or time in force is not
  in this slice" for orders with status `replaced`, which it skips on purpose. That misleading warning was
  there before and is outside this defect.
