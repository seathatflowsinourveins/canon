# Report: Alpaca Execution Client Order Replace Defects

## What Was Wrong

`AlpacaExecutionClient` in `packages/canon-alpaca/src/canon_alpaca/execution.py` modifies open orders via Alpaca's `PATCH /v2/orders/{order_id}` (`TradingClient.replace_order_by_id`). Ten defects occurred when Alpaca's HTTP responses and WebSocket trade updates arrived late, twice, or out of order:

1. **Missed `replaced` + filled replacement dropped in reconciliation:** `_order_report` skipped any replacement (`node_id.value != order.client_order_id`) whose status was in `CLOSED_STATUSES` whenever `held.venue_order_id != VenueOrderId(str(order.id))`. When `replaced` was missed, the old order had status `replaced` (skipped by `order_status_report`) while `held.venue_order_id` still held the old venue order ID, so a `filled` (or `canceled` / `expired`) replacement was also skipped and the node never saw the fill.
2. **Duplicate `OrderModifyRejected` on HTTP 422 + `order_replace_rejected`:** `_replace_refused` and `_apply` (`event == "order_replace_rejected"`) each emitted `OrderModifyRejected` independently without tracking whether that modify had already been rejected on the other path.
3. **Duplicate `OrderUpdated` on repeated `replaced`:** `_replaced` did not record which venue order IDs had already been superseded or which replacement IDs had already been adopted, so a second `replaced` update re-read the replacement from Alpaca and emitted a second `OrderUpdated`.
4. **Fill on replacement before `replaced`:** `_apply` applied `fill` and `partial_fill` updates directly to the cached order without first adopting an unadopted replacement. Fills larger than the old order's quantity were dropped by NautilusTrader as overfills, and `OrderUpdated` was never emitted ahead of the fill.
5. **Late `canceled` or `expired` on a replaced order:** `_apply` treated `canceled` and `expired` updates for an already-superseded order ID as closing the node's live order while its replacement remained open at Alpaca.
6. **`pending_replace` unmapped in `OPEN_STATUSES` and `order_status_report`:** `AlpacaOrderStatus.PENDING_REPLACE` was absent from `OPEN_STATUSES`, causing `order_status_report` to raise `ValueError` on open `pending_replace` orders during startup reconciliation (and not reporting `OrderStatus.PARTIALLY_FILLED` when `filled_qty > 0`).
7. **`replaced` whose replacement had moved on:** When `_replaced` fetched `old.replaced_by` from Alpaca and that replacement was already in `pending_replace` or `replaced` (with its own `replaced_by`), `order_status_report` raised or returned `None`, causing `_replaced` to fail instead of following `replaced_by` to the current order.
8. **Second modify before first's `replaced` mistaken for retry:** `_replace_refused` checked `replaces_as_asked(replacement, request)`, which treated `None` fields in `ReplaceOrderRequest` (unchanged fields) as wildcards rather than comparing them against the replaced order's fields, and did not track whether the replacement had already been claimed by an earlier modify call.
9. **Late PATCH answer leaked in `_answers`:** When `replaced` ran before `replace_order_by_id` returned on the REST worker thread, `_replaced` popped `_answers` (finding nothing), read the order from Alpaca, and finished or awaited the lock while `_modify_order` subsequently wrote `self._answers[str(answer.id)] = answer`, leaving the entry in `_answers` for the life of the session.
10. **Old order fills before replacement `rejected`:** `_apply` returned early at `if client_order_id in self._finished: return` before reaching the replacement-`rejected` branch, leaving the modify unsettled in `self._modifying` and logging nothing about the modify outcome after the fill.

---

## What Changed and Why

### 1. Code (`packages/canon-alpaca/src/canon_alpaca/execution.py`)

* **`pending_replace` status mapping (`OPEN_STATUSES`, `order_status_report`):**
  Mapped `AlpacaOrderStatus.PENDING_REPLACE` to `OrderStatus.ACCEPTED` in `OPEN_STATUSES`, and in `order_status_report` upgraded `nautilus_status` to `OrderStatus.PARTIALLY_FILLED` when `status == AlpacaOrderStatus.PENDING_REPLACE` and `filled_qty > 0`. Verified against `nautilus_trader` `v2.0.0rc8` (`LiveNode` startup reconciliation logs `Unhandled order status PENDING_UPDATE for external order` and drops the order if `PENDING_UPDATE` is reported, whereas `ACCEPTED` and `PARTIALLY_FILLED` reconcile cleanly).
* **Reconciliation of non-rejected replacements (`_order_report`):**
  Narrowed the closed-replacement filter from `order.status in CLOSED_STATUSES` to `order.status == AlpacaOrderStatus.REJECTED`. Only a `rejected` replacement closed without taking over (e.g., when the old order filled before the replacement reached the venue); replacements in `filled`, `canceled`, or `expired` took over from the `replaced` order and must be reported under the node's `ClientOrderId` even when the `replaced` stream event was missed.
* **Exact term comparison + claimed replacement tracking (`replaces_as_asked`, `_claimed_replaces`, `_replace_refused`):**
  Changed `replaces_as_asked(original, replacement, request)` to compare each omitted field (`None` in `ReplaceOrderRequest`) against `original`'s value (`qty`, `limit_price`, `stop_price`), matching Alpaca's replace semantics where omitted parameters retain the prior order's terms. Added `self._claimed_replaces` (`replacement_id -> call_key`) so a replacement already created/claimed by an earlier `_modify_order` call is never accepted as `alpaca-py`'s retry of a subsequent modify call, even if both asked for identical terms.
* **Single-settlement tracking per modify (`_modifying`, `_modify_parent`, `_settled_replacements`, `_stream_rejected_calls`, `_rejected_replace_count`):**
  * `_modify_order` registers an in-flight `call_key` in `self._modifying` before calling `replace_order_by_id`, re-keys it to `str(answer.id)` on HTTP 200 only if `new_id` is not already in `self._settled_replacements`, and stores `_answers[new_id]` only while unsettled and unfinished.
  * `_settle_modify_by_replacement` and `_settle_modify_for_parent` mark replacements in `self._settled_replacements`, pop `_answers`, and settle the corresponding entry in `self._modifying`. `_replaced` also pops `next_id` in a `finally` block after `get_order_by_id`, ensuring a PATCH answer arriving while `_replaced` awaits `_rest_lock` is never retained in `_answers`.
  * `_replace_refused`, `_on_replace_rejected`, and `_on_replacement_rejected` coordinate via `_stream_rejected_calls` and `_rejected_replace_count` so a modify refused both on HTTP 422 and on the stream (`order_replace_rejected`) emits `OrderModifyRejected` once, while preserving the retry-lookup warning when `took_before_error` succeeded before a 422 on retry.
* **Settling a rejected modify after the old order filled (`_apply`, `_on_replace_rejected`, `_on_replacement_rejected`, `_filled`):**
  Moved `order_replace_rejected` and unadopted-replacement `rejected` handling in `_apply` ahead of `if client_order_id in self._finished: return`. When the order has already filled (`client_order_id in self._filled` or `order.status == OrderStatus.FILLED`), the modify is removed from `self._modifying` and logged (`"... which had filled first; the fill is the outcome"`) instead of emitting `OrderModifyRejected` (which NautilusTrader drops on a `FILLED` order) or `OrderRejected`.
* **Replacement adoption, chain walking, idempotent `replaced`, and superseded order filtering (`_replaced`, `_adopt_replacement`, `_is_superseded`, `_superseded`, `_adopted`, `_current_venue_id`, `_replace_parent`):**
  * `_replaced` ignores `old.id` if already in `self._superseded`, follows `replaced_by` up to `MAX_REPLACE_CHAIN` (16) links when the replacement has itself already been `replaced`, marks intermediate and prior venue order IDs in `self._superseded`, and skips emitting `OrderUpdated` if the final replacement is already in `self._adopted`.
  * In `_apply`, when a `fill` or `partial_fill` arrives for an unadopted replacement (`is_replacement`, not in `_adopted`, and `_active_venue_id(order) != str(alpaca_order.id)`), `_adopt_replacement` emits `OrderUpdated` with the replacement's terms first and marks the old order in `_superseded` before `_fill` emits `OrderFilled`. A `new` event on a replacement still does not adopt it, since Alpaca can still reject it if the old order fills first.
  * `_apply` ignores late `canceled` and `expired` updates when `_is_superseded(order, alpaca_order)` is true (checked via `_superseded`, root-order advancement, `replaced_by`, and `_replace_parent` ancestry).
  * All new per-connection tracking collections are cleared in `_disconnect`.

### 2. Documentation and Records

* Updated module and method docstrings in `packages/canon-alpaca/src/canon_alpaca/execution.py`.
* Updated `packages/canon-alpaca/README.md` (`Modify`, `Stream`, and the `Tests` table) to reflect replacement adoption before early fills, `pending_replace` reporting, single-settlement modify rejections, and the added offline test file.
* Updated `docs/adr/0036-alpaca-adapter-order-management.md` (`Modify: option (b)` and `Confirmation`) where its statements ("`OrderUpdated` comes only with `replaced`" and replacement `rejected` after old-order fill becoming `OrderModifyRejected`) were superseded by this fix.

### 3. Additional Offline Tests

* Added `packages/canon-alpaca/tests/test_execution_replace_edges.py` (without modifying any existing test file) covering:
  * late `expired` on a replaced order;
  * `partial_fill` on a replacement before `replaced` followed by a late `replaced` and final `fill`;
  * a second modify with identical terms refused 422 after the first modify answered;
  * a replacement rejected on the stream while the old order stays open (verifying `OrderModifyRejected` keeps the old `venue_order_id`);
  * `replaces_as_asked` comparing omitted request fields against the replaced order.

---

## Upstream APIs and Contracts Followed

* **NautilusTrader `v2.0.0rc8` (`d53849b216e2`):**
  * `nautilus_trader.live.clients.ExecutionClient` ("Execution client hooks", Python adapter guide): `generate_order_updated`, `generate_order_modify_rejected`, `generate_order_filled`, `generate_order_canceled`, `generate_order_expired`, and reconciliation report hooks (`OrderStatusReport`).
  * Execution command outcomes & reconciliation (`docs/concepts/execution/policies.md`, `docs/concepts/execution/reconciliation.md`): single outcome per modify command; `OrderModifyRejected` dropped by NautilusTrader once an order is `FILLED`; external open orders reported as `OrderStatus.ACCEPTED` or `OrderStatus.PARTIALLY_FILLED` (not `PENDING_UPDATE`).
* **Alpaca Trading API & `alpaca-py` `0.45.0`:**
  * "Replace Order by ID" (`PATCH /v2/orders/{order_id}`, `TradingClient.replace_order_by_id`, `ReplaceOrderRequest`): HTTP 200 returns the new `Order` without guaranteeing replacement has occurred; omitted fields retain existing order terms; if the existing open order fills before the replacement reaches the execution venue, the replacement is `rejected`.
  * WebSocket `trade_updates` stream (`TradingStream`, `TradeUpdate`) & `OrderStatus` (`pending_replace`, `replaced`, `order_replace_rejected`, `replaces`, `replaced_by`), plus paper observations recorded in ADR 0036 (`test_paper_replace.py`).

---

## Verification Run and Results

All offline checks and tests were executed in this checkout with `uv --locked` via `mise exec`:

1. `mise exec -- uv run --locked pytest`
   * **Result:** `375 passed, 21 skipped in 124.93s` (all 11 previously failing test functions / 13 parametrized cases in `packages/canon-alpaca/tests/test_execution_client.py` pass, all 5 new tests in `packages/canon-alpaca/tests/test_execution_replace_edges.py` pass, and all other offline tests pass; the 21 paper tests skipped as expected with no credentials set).
2. `mise exec -- uv run --locked ruff check .`
   * **Result:** `All checks passed!` (exit 0)
3. `mise exec -- uv run --locked ruff format --check .`
   * **Result:** `99 files already formatted` (exit 0)
4. `mise exec -- uv run --locked ty check`
   * **Result:** `All checks passed!` (exit 0)
5. `scripts/canon lint`
   * **Result:** `canon: lint clean` (exit 0)

---

## Uncertainties and Unverified Items

* **Live paper Trading API behavior (`UNVERIFIED` in this session):** Per task rules and ADR 0009/0033, no paper tests or live network calls were run. Whether Alpaca's live paper stream ever emits `order_replace_rejected` after a 422 `"order already replaced"`, how `qty` behaves on a replace of a partially filled order on paper, and the exact timing of `pending_replace` and late `canceled`/`expired` events on paper remain `UNVERIFIED` against the live venue beyond what ADR 0036 recorded on 2026-11-10.
* **`scripts/ci`:** Not run in full because this task copy omits files outside the graded checks, as noted in the task instructions.
