# Report: Alpaca Order Replace Ordering, Deduplication, and Reconciliation Fixes

## 1. What Was Wrong

In `packages/canon-alpaca/src/canon_alpaca/execution.py`, `AlpacaExecutionClient` implements `_modify_order` via Alpaca's "Replace Order by ID" (`TradingClient.replace_order_by_id`). It assumed the PATCH HTTP answer arrives first, followed by one `replaced` trade update (or one rejection) in order while the replacement is still in `new`. When updates arrived late, twice, out of order, or mid-replace, ten related defects occurred:

1. **Missed `replaced` followed by a filled replacement lost the fill during reconciliation (`_order_report`)**:
   `_order_report` skipped any replacement with `order.status in CLOSED_STATUSES` whenever `held.venue_order_id != VenueOrderId(str(order.id))`. Meanwhile `order_status_report` skipped the old order because its status was `REPLACED`. Thus, if `replaced` was missed and the replacement reached `FILLED` (or `CANCELED`/`EXPIRED`), neither order was reported. Only a `REJECTED` replacement closed without taking over from the old order.
2. **Double `OrderModifyRejected` on HTTP 422 + stream `order_replace_rejected` (`_modify_order`, `_replace_refused`, `_apply`)**:
   `_replace_refused` emitted `OrderModifyRejected` on the HTTP 422 response, and `_apply` independently emitted `OrderModifyRejected` on `order_replace_rejected` without tracking whether the modify attempt had already been settled.
3. **Duplicate `OrderUpdated` on repeated `replaced` updates (`_replaced`)**:
   `_replaced` did not record which Alpaca order IDs had already been superseded, so a duplicate `replaced` update read the replacement from Alpaca (after `_answers.pop` missed on the second delivery) and emitted a second `OrderUpdated`.
4. **Replacement fill (or cancel/expiry) arriving before `replaced` applied against old terms (`_apply`)**:
   When a replacement's `fill` or `partial_fill` arrived before `replaced`, `_apply` sent `OrderFilled` directly while the node's order still held the old `venue_order_id`, `quantity`, and prices. A fill larger than the old quantity was dropped by NautilusTrader as an overfill, and `OrderUpdated` only arrived afterward (if at all).
5. **Late `canceled` or `expired` on a superseded order closed the live replacement (`_apply`)**:
   Once `OrderUpdated` moved the node's order to the replacement, a late `canceled` or `expired` trade update for the old order still matched `client_order_id` and called `generate_order_canceled` / `generate_order_expired`, closing the node's order while its replacement remained active at Alpaca.
6. **`pending_replace` unmapped in `OPEN_STATUSES` and `order_status_report`**:
   `OPEN_STATUSES` omitted `AlpacaOrderStatus.PENDING_REPLACE`, causing `order_status_report` to raise `ValueError("status pending_replace")` on open orders and abort startup reconciliation; nor did `order_status_report` promote an order in `pending_replace` with `filled_qty > 0` to `OrderStatus.PARTIALLY_FILLED`.
7. **`_replaced` failed when the replacement had already moved to `pending_replace` or `replaced` (`_replaced`)**:
   When `_replaced` read the replacement from Alpaca and found `status == pending_replace` or `status == replaced` (with `replaced_by` set because a subsequent modify had already replaced it again), `order_status_report` raised `ValueError`, `_apply_trade_update` caught and logged it, and no `OrderUpdated` was emitted.
8. **Second modify refused before the first's `replaced` mistaken for `alpaca-py`'s retry (`_replace_refused`, `replaces_as_asked`)**:
   When a command omitted `quantity` (`ReplaceOrderRequest.qty is None`), `replaces_as_asked` treated `request.qty is None` as matching any `replacement.qty` instead of verifying that `replacement.qty` still matched the node order's unchanged quantity; additionally, `_replace_refused` did not check whether `replacement.id` had already been returned as the HTTP 200 answer to an earlier `_modify_order` call.
9. **Late PATCH answer retained indefinitely (`_modify_order`, `_replaced`)**:
   When `replaced` arrived and was applied before `replace_order_by_id` returned on the worker thread, `_modify_order` unconditionally stored `self._answers[str(answer.id)] = answer`, which nothing ever popped afterward.
10. **Replacement `rejected` arriving after the old order filled returned early without settling or logging the modify (`_apply`)**:
    When the old order filled, `_apply` added `client_order_id` to `self._finished`. When Alpaca's subsequent `rejected` update for the replacement arrived, `_apply` returned at `if client_order_id in self._finished: return` before reaching the replacement-`rejected` handler, leaving the modify unsettled and logging nothing about its outcome.

## 2. What Was Changed and Why

* **`packages/canon-alpaca/src/canon_alpaca/execution.py`**:
  * **`OPEN_STATUSES` and `order_status_report`**:
    Mapped `AlpacaOrderStatus.PENDING_REPLACE` to `OrderStatus.ACCEPTED` in `OPEN_STATUSES`, and in `order_status_report` promoted `OrderStatus.ACCEPTED` with `filled_qty > 0` to `OrderStatus.PARTIALLY_FILLED`. `ACCEPTED` (rather than `PENDING_UPDATE`) is used for unfilled `pending_replace` orders because NautilusTrader v2.0.0rc8's `nautilus_execution::reconciliation::orders` drops external order reports with `OrderStatus.PENDING_UPDATE` as unhandled (`Unhandled order status PENDING_UPDATE for external order ...`), whereas `ACCEPTED` / `PARTIALLY_FILLED` reconciles the working order into `cache.orders_open()`.
  * **Explicit modify and replacement lifecycle state on `AlpacaExecutionClient`** (cleared on `_disconnect`):
    * `self._modifying` and `self._modify_targets`: track each unsettled modify from before `replace_order_by_id` is called (under a per-attempt key) through its HTTP 200 answer (re-keyed under `str(answer.id)`), settling each modify at most once across HTTP 422, connect-timeout, `order_replace_rejected`, replacement `rejected`, and replacement adoption (`_begin_modify`, `_settle_modify_key`, `_settle_modify_target`, `_settle_replacement`).
    * `self._answered`: records replacement order IDs already returned by `replace_order_by_id` (or matched on retry), so a 422 on a subsequent `_modify_order` call is never mistaken for `alpaca-py`'s internal retry of an earlier answered modify, even if both asked for identical terms.
    * `replaces_as_asked(replacement, request, order)`: compares omitted `request.qty` against `int(order.quantity.as_double())` so a modify that keeps the existing share quantity is not matched against a prior replacement that changed it, while keeping `ReplaceOrderRequest.qty = None` on the wire when the command does not change quantity.
    * `self._superseded` and `self._settled_replacements`: track Alpaca order IDs whose replacement has taken over and replacement IDs whose replace outcome (`replaced`, pre-event adoption, or `rejected`) has already been handled. Late PATCH answers whose `answer.id` is already in `_settled_replacements` or `_superseded` are neither added to `_answers` nor re-added to `_modifying`; terminal events on the active order also drop any unused `_answers` entries for that order.
  * **Stream event ordering in `_apply`, `_is_superseded`, `_adopt_order`, and `_replaced`**:
    * `order_replace_rejected` and replacement `rejected` are handled before the `client_order_id in self._finished` guard so a replacement rejected after the old order filled (tracked deterministically via `self._filled_orders` as well as `order.status == OrderStatus.FILLED`) settles `_modifying` and logs `"which had filled first; the fill is the outcome"` instead of emitting `OrderModifyRejected` on a closed order (which NautilusTrader's `Order.apply` rejects as an invalid state transition).
    * `ACCEPTING_EVENTS` (`new`, `accepted`, `pending_new`) on a replacement do not adopt it (since Alpaca may still reject the replacement if the old order filled first).
    * An active execution or terminal update (`fill`, `partial_fill`, `canceled`, `expired`) on a non-superseded replacement whose `venue_order_id` has not yet been adopted calls `_adopt_order` first — emitting `OrderUpdated` before `OrderFilled` / `OrderCanceled` / `OrderExpired` and marking predecessors in `_superseded` — so fills use the replacement's quantity and ID and a later `replaced` does not emit a second `OrderUpdated`.
    * `_is_superseded` identifies superseded legs (via `_superseded`, `AlpacaOrderStatus.REPLACED`, original orders after `order.venue_order_id` has moved on, or intermediate replacements not succeeding `order.venue_order_id`) so late `canceled`, `expired`, `rejected`, or accepting updates on superseded orders are ignored without closing the live replacement.
    * `_replaced` ignores already-superseded `old` orders (preventing duplicate `OrderUpdated`) and, when the replacement read from Alpaca has `status == REPLACED` and `replaced_by` set, follows `replaced_by` up to `MAX_REPLACE_CHAIN` links to the live order, settling intermediate legs along the way.
  * **Reconciliation in `_order_report`**:
    Restricted the un-adopted closed-replacement filter from `order.status in CLOSED_STATUSES` to `order.status == AlpacaOrderStatus.REJECTED`, so replacements that took over and reached `FILLED`, `CANCELED`, or `EXPIRED` while `replaced` was missed are still reported under the node's `client_order_id`.
* **`packages/canon-alpaca/README.md` and `docs/adr/0036-alpaca-adapter-order-management.md`**:
  Updated statements in `execution.py`'s module docstring, `packages/canon-alpaca/README.md`, and `docs/adr/0036-alpaca-adapter-order-management.md` that previously said `OrderUpdated` is generated *only* on `replaced`, omitted `pending_replace` from mapped report statuses, or listed `replaced` as unmapped in the stream section of `README.md`.
* **`packages/canon-alpaca/tests/test_replace_ordering.py`** (new file; no existing test files modified):
  Added offline `LiveNode` tests covering the symmetric arrival order (stream `order_replace_rejected` before delayed HTTP 422), a second modify with identical terms refused after an answered first modify, replacement `canceled`/`expired` and `partial_fill` arriving before `replaced` followed by a late `replaced`, late `expired` on a superseded order, and missed-`replaced` reconciliation of `canceled` and `expired` replacements.

## 3. Upstream APIs and Contracts Followed

* **NautilusTrader `v2.0.0rc8`** (`nautilus_trader.live.clients.ExecutionClient`, adapter guide and execution/reconciliation contracts cited in `execution.py`):
  * `ExecutionClient.generate_order_updated`, `generate_order_modify_rejected`, `generate_order_filled`, `generate_order_canceled`, `generate_order_expired`, and `OrderStatusReport` / `OrderStatus`.
  * Verified directly against the installed `v2.0.0rc8` binary that `LimitOrder.apply(OrderModifyRejected)` on a `FILLED` order raises `RuntimeError: Invalid order state transition` (logged and dropped by the engine), and that `nautilus_execution::reconciliation::orders` logs `Unhandled order status PENDING_UPDATE for external order` when an external `OrderStatusReport` carries `OrderStatus.PENDING_UPDATE`, whereas `OrderStatus.ACCEPTED` and `OrderStatus.PARTIALLY_FILLED` reconcile into `cache.orders_open()`.
* **Alpaca Trading API & `alpaca-py` `0.45.0`** (as documented and observed in ADR 0036):
  * "Replace Order by ID" (`PATCH /v2/orders/{order_id}`, `TradingClient.replace_order_by_id`, `ReplaceOrderRequest`): HTTP 200 returns the new `Order` with `replaces` set and does not guarantee replacement until confirmed; HTTP 422 refuses invalid/already-replaced requests; `RESTClient._request` retries 429/504 up to 3 times within a single call.
  * "Orders at Alpaca" & WebSocket `trade_updates` (`TradingStream`): `OrderStatus.PENDING_REPLACE`, `OrderStatus.REPLACED`, `replaced` carrying the old order with `replaced_by`, `order_replace_rejected`, and replacement `rejected` when the existing open order fills before the replacement reaches the execution venue.

## 4. Verification Run and Results

All offline tests and static checks were executed in the workspace without network access or credentials:

1. `mise exec -- uv run --locked pytest`
   * Result: **`378 passed, 21 skipped in 55.14s`** (0 failures; all 11 previously failing test functions / 13 parametrized items in `packages/canon-alpaca/tests/test_execution_client.py` pass, all 8 new items in `packages/canon-alpaca/tests/test_replace_ordering.py` pass, and all pre-existing offline tests pass; the 21 skipped tests are the paper-trading tests gated on credentials).
2. `mise exec -- uv run --locked ruff check .`
   * Result: **`All checks passed!`** (exit 0).
3. `mise exec -- uv run --locked ruff format --check .`
   * Result: **`99 files already formatted`** (exit 0).
4. `mise exec -- uv run --locked ty check`
   * Result: **`All checks passed!`** (exit 0).
5. `scripts/canon lint`
   * Result: **`canon: lint clean`** (exit 0).

## 5. Remaining Uncertainties (`UNVERIFIED`)

* **Live paper behavior of `pending_replace`, `order_replace_rejected`, and partial-fill replace semantics (`UNVERIFIED`)**:
  Per task rules and `AGENTS.md` rule 8, no paper tests or live Alpaca calls were run in this session. As noted in ADR 0036, exact live Alpaca payloads for `order_replace_rejected`, `pending_replace`, and how Alpaca interprets `ReplaceOrderRequest.qty` on an already partially filled order (total order quantity vs. remaining quantity) were not re-observed against Alpaca's paper server in this session and remain `UNVERIFIED` against live paper.
* **Late fill on a superseded order after `replaced` (`UNVERIFIED`)**:
  Whether Alpaca's stream can deliver an out-of-order `partial_fill` for a replaced order *after* its `replaced` update has already been delivered is `UNVERIFIED`. The client ignores `ACCEPTING_EVENTS` and terminal `canceled`/`expired`/`rejected` updates on superseded orders so they cannot close or revert the live replacement, while still allowing a fill update carrying a distinct `execution_id` to pass through duplicate-fill detection without re-marking the active replacement finished.
