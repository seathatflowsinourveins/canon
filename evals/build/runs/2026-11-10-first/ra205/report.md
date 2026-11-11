# Alpaca execution client: late, duplicate and out-of-order replace updates

## What was wrong

All ten defects were in the replace (modify) lifecycle in
`packages/canon-alpaca/src/canon_alpaca/execution.py`:

1. **Missed `replaced`, replacement fills.** `_order_report` dropped every closed order traced
   through `replaces` whose venue ID the node's order did not yet name. That guard was written for a
   *rejected* replacement, but it also dropped a *filled* replacement, so reconciliation reported
   neither the old order (status `replaced`, skipped) nor the replacement, and the fill was lost.
2. **Double modify rejection.** A replace refused with 422 on the HTTP answer went through
   `_replace_refused` and generated `OrderModifyRejected`; the stream's `order_replace_rejected`
   for the same modify generated a second one. Nothing recorded that the modify had been settled.
3. **Duplicate `replaced`.** `_replaced` generated `OrderUpdated` on every delivery; nothing
   recorded that a replacement's terms had already been applied.
4. **Fill before `replaced`.** A fill update for the replacement was applied to the node's order
   under the old quantity/price, so a fill larger than the old quantity was dropped as an overfill
   and the order took the replacement's terms only when `replaced` later arrived.
5. **Late `canceled`/`expired` for the old order.** Once the node's order named the replacement,
   a `canceled` or `expired` update carrying the old order still closed the node's order, although
   its replacement was still working at Alpaca.
6. **`pending_replace` unmapped.** `OPEN_STATUSES` had no `pending_replace`, so
   `order_status_report` raised for an open mid-replace order and start-up reconciliation failed;
   there was also no partly-filled reporting for one with fills.
7. **Replacement already moved on.** `_replaced` read the replacement named by `replaced_by` and
   called `order_status_report` on it directly; if that order was itself `replaced` the report was
   `None` and the client raised, generating no `OrderUpdated`, and `pending_replace` hit defect 6.
8. **Second modify mistaken for alpaca-py's retry.** `_replace_refused` treated a 422 as the
   retry of an earlier attempt whenever the replacement matched the fields the `ReplaceOrderRequest`
   explicitly set. A second modify that left the quantity unchanged sent `qty=None`, so a
   replacement holding a *different* quantity (from the first modify) still "matched", and the
   second modify got no `OrderModifyRejected`.
9. **PATCH answers leaked.** `_modify_order` stored the PATCH answer in `_answers` unconditionally.
   When `replaced` was applied before the answer returned — including the case where `_replaced`
   waited on the REST lock while the answer was stored — the stored answer was never popped and
   `_answers` grew for the whole session.
10. **Rejected replacement after the old order filled.** `_apply` returned early for any order in
    `_finished`, before the branch handling a replacement's `rejected`. The old order's fill adds
    the order to `_finished`, so that branch was unreachable exactly in the case it was written
    for: nothing logged what became of the modify and nothing settled it.

## What I changed, and why that way

In `packages/canon-alpaca/src/canon_alpaca/execution.py` only (plus the documentation corrections
below); no test file was edited and no dependency was added.

- **Track modifies awaiting an outcome.** A new `_modifying` map (venue order ID being replaced →
  node's client order ID) is set in `_modify_order` just before the PATCH is sent, and settled
  (`_settle_modify`) when the replace is applied, when a rejection is generated, or when the old
  order's fill settles it. `_modify_rejected_once` generates `OrderModifyRejected` only for a
  modify still in that map and settles it as it generates, so whichever of Alpaca's refusals
  arrives first (HTTP 422 via `_replace_refused`, `order_replace_rejected`, a replacement's
  `rejected`, a not-sent connect timeout) produces exactly one event (defects 2, 10).
- **Apply each replacement once.** A new `_replace_applied` set records replacement venue IDs
  already applied. `_replaced` and the fill path share a new `_apply_replacement`, which returns
  without generating if the replacement was already applied and, when it does apply, discards the
  stored PATCH answer for that replacement (defects 3, 9). `_modify_order` additionally refuses
  to store an answer whose replacement is already applied (by set membership or by the cached
  order already naming the replacement's venue ID). The `_apply_replacement` discard is what
  closes the lock race found while testing: `_replaced` pops `_answers` before awaiting
  `get_order_by_id`, and that read can wait behind the PATCH's own REST call, so the answer can be
  stored after the pop but before the apply.
- **A fill on the replacement applies its terms first.** In `_apply`, a `fill`/`partial_fill`
  whose order is a replacement the node's order does not yet name goes through
  `_apply_replacement` (with the fill update's own order, which carries the replacement's
  quantity and prices) before `_fill`, so the fill is checked against the replacement's quantity
  (defect 4). A `new` update for a replacement still does not adopt it — it may yet be rejected,
  which the existing test `test_replacement_new_then_rejected_is_not_an_order_rejection` covers.
- **Late closure of the replaced order is ignored.** A `canceled`/`expired` update whose Alpaca
  order has `replaced_by` set, or that names a superseded venue ID, is logged and dropped; the
  node's order follows its replacement (defect 5).
- **`pending_replace` maps.** `OPEN_STATUSES` gains `PENDING_REPLACE → ACCEPTED`, and
  `order_status_report` reports `PARTIALLY_FILLED` instead when the order has fills (defect 6).
- **`_replaced` follows the chain.** It now walks `replaced_by` links (bounded by the existing
  `MAX_REPLACE_CHAIN`) past replacements that are themselves `replaced`, to the live order, and
  reports a `pending_replace` replacement as it stands instead of raising (defect 7).
- **Retry detection compares effective terms.** `replaces_as_asked` now takes the node's current
  quantity and uses it when the request's `qty` is `None` (an unchanged quantity), matching how
  `_modify_order` already fills in the current limit/stop price. A second modify whose effective
  quantity differs from the replacement's is therefore rejected, while a genuine alpaca-py retry
  of the modify that produced the replacement still matches (defect 8, and the pre-existing
  `test_refused_repeat_of_a_replace_alpaca_took_is_not_a_rejection` still passes).
- **Filled-first rejection settles the modify.** The replacement-`rejected` branch in `_apply`
  moved before the `_finished` early return. When the node's order has already filled, it settles
  the modify and logs that Alpaca rejected the replacement "which had filled first; the fill is
  the outcome", generating no `OrderModifyRejected` (NautilusTrader would drop one for a filled
  order anyway); otherwise it goes through `_modify_rejected_once` (defect 10).
- **Reconciliation reports a filled replacement.** The `_order_report` guard that suppresses a
  traced order now applies only when that order's status is `REJECTED` (a replacement that never
  took over), so a filled/canceled replacement whose `replaced` was missed is reported under the
  node's client order ID (defect 1); the rejected case keeps its old behaviour, covered by
  `test_rejected_replacement_is_not_reported_after_a_restart`.

Documentation corrected because the change made it untrue: the module header's Modify paragraph
and the `OPEN_STATUSES`/`order_status_report` docstrings in `execution.py`, the Modify bullet and
test-table line in `packages/canon-alpaca/README.md`, and the Modify paragraph in
`docs/adr/0036-alpaca-adapter-order-management.md` (which said `OrderUpdated` comes *only* with
`replaced`). No new tests were added: the eleven named tests already in the checkout cover every
defect and are the maintained coverage; the task also forbids editing the existing test files.

## Which upstream API or contract the change follows

- Alpaca's "Replace Order by ID" (`TradingClient.replace_order_by_id`,
  https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md): the PATCH answer is "The new
  Order object with the new order ID" but "does NOT guarantee the existing open order has been
  replaced", so the answer still never generates `OrderUpdated` by itself; the stream's `replaced`
  (old order carrying `replaced_by`, from the 2026-11-10 paper observation, ADR 0036) remains the
  confirmation. The one addition is treating a replacement's own `fill`/`partial_fill` — whose
  `qty`/`price` are that fill's shares and price (Alpaca's websocket streaming page) — as proof the
  replacement is live before applying the fill, which is a stream event for the replacement itself,
  not the PATCH answer.
- Alpaca's order statuses from pinned alpaca-py 0.45.0 (`OrderStatus.PENDING_REPLACE`), mapped
  onto NautilusTrader's `OrderStatus` (`ACCEPTED`/`PARTIALLY_FILLED`) the way the neighbouring
  open statuses already map.
- NautilusTrader v2.0.0rc8's `ExecutionClient` contract (adapter guide, "Execution client
  hooks"): all events still go through the base class's `generate_*` methods
  (`generate_order_updated`, `generate_order_modify_rejected`, `generate_order_filled`, …).
  `_modify_order`, `_apply_trade_update` and the reconciliation hooks keep their signatures; the
  change is state tracking inside them (`_modifying`, `_replace_applied`), following the client's
  existing idiom of `_accepted`/`_finished`/`_replacements` sets. The filled-first logging follows
  the execution policies' "Command outcomes" treatment the file already cites: a modify rejection
  for a filled order has no effect, so the fill is reported as the outcome.

## What I ran, with its result

- The eleven named tests (13 cases) before the change: **13 failed**, as the task stated.
- The same eleven after the change: **13 passed**.
- `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q`:
  **114 passed**.
- Full offline suite `mise exec -- uv run --locked pytest -q`: **370 passed, 21 skipped** (the
  skips are the paper tests, which need keys and were not run, per the task).
- `mise exec -- uv run --locked ruff check .`: all checks passed.
- `mise exec -- uv run --locked ruff format --check .`: 98 files already formatted.
- `mise exec -- uv run --locked ty check`: all checks passed.
- `scripts/ci` was not run: the task states this checkout omits files it expects, so its failure
  here would not be evidence about this change.

## Anything I am unsure of

- **UNVERIFIED against paper.** The new orderings (fill before `replaced`, duplicate `replaced`,
  late `canceled` of a replaced order, a replacement that is itself `pending_replace`/`replaced`
  when read) are exercised only by the offline stand-ins in `tests/conftest.py`, built from
  Alpaca's docs and the 2026-11-10 observation. No paper run was made (no keys, per the task), so
  whether Alpaca's real stream delivers these orderings the way the stand-ins model them is
  UNVERIFIED.
- **`_replace_applied` grows by one entry per applied replace for the life of a connection**,
  like the existing `_accepted`/`_finished` sets; it is cleared on disconnect. That is bounded by
  the number of modifies in a session and matches the client's existing trade-off, but it is a
  deliberate choice, not something an upstream documents.
- A modify whose outcome never arrives by any route (no `replaced`, no rejection, no fill,
  unsuccessful reconciliation) stays in `_modifying` until disconnect; that is the same
  "unknown outcome, resolved by a trade update or reconciliation" stance the client already takes
  for unknown replace answers, now made explicit in one map.
