# Order-Replace Defect Fix in `packages/canon-alpaca`

## What Was Wrong

`AlpacaExecutionClient` modifies a working order via Alpaca's `PATCH /v2/orders/{order_id}` (`TradingClient.replace_order_by_id`), which creates a new Alpaca order linked by `replaces` / `replaced_by`. Ten defects occurred when Alpaca's HTTP responses and WebSocket `trade_updates` for a replace arrived late, twice, or out of order:

1. **Missed `replaced` dropped filled replacements during reconciliation**: `_order_report` skipped *every* closed replacement (`order.status in CLOSED_STATUSES`) whenever `held.venue_order_id != VenueOrderId(str(order.id))`. That condition was intended only for replacements rejected when the old order filled first, but it also matched replacements that took over and filled (or canceled/expired) after a missed `replaced` update — while `order_status_report` skipped the `replaced` original too, leaving neither order reported.
2. **Double `OrderModifyRejected` for one refused replace**: `_modify_order` emitted `OrderModifyRejected` on a 422 HTTP refusal and `_apply` emitted a second `OrderModifyRejected` on the stream's `order_replace_rejected` without tracking whether the modify on that venue order ID was already settled.
3. **Duplicate `replaced` updates emitted two `OrderUpdated` events**: `_replaced` did not record which venue order IDs had already been processed as replaced or which replacement IDs had already been adopted.
4. **Replacement fill before `replaced` applied under old order terms**: `_apply` forwarded `fill` / `partial_fill` on a replacement directly to `_fill` without first adopting the replacement via `generate_order_updated`, leaving the node's cached order at the old `venue_order_id` and old `quantity` (causing NautilusTrader's execution engine to drop fills larger than the old quantity as overfills, and applying `OrderUpdated` only after the fill).
5. **Late `canceled` or `expired` for a replaced order closed the live replacement**: `_node_order_id` mapped updates for the old order to the same `ClientOrderId`, and `_apply` emitted `OrderCanceled` / `OrderExpired` without checking whether that Alpaca order ID had already been superseded.
6. **`pending_replace` was unmapped**: `OPEN_STATUSES` omitted `AlpacaOrderStatus.PENDING_REPLACE`, so `order_status_report` raised `ValueError` on an open order mid-replace at start-up, and did not map `pending_replace` with `filled_qty > 0` to `OrderStatus.PARTIALLY_FILLED`.
7. **Replacement already `pending_replace` or `replaced` when read on `replaced` raised**: When `_answers` missed and `_replaced` fetched `old.replaced_by` from Alpaca, an order already in `pending_replace` failed `order_status_report` (unmapped status) and an order already `replaced` again was neither followed along `replaced_by` nor mappable directly, raising `ValueError` and emitting no `OrderUpdated`.
8. **Second modify refused before the first's `replaced` mistaken for `alpaca-py` retry**: `replaces_as_asked` skipped `request.qty` whenever `request.qty is None` (instead of comparing against the order's unchanged quantity), and `_replace_refused` checked only `replaces_as_asked` without verifying that the call owned the first unsettled modify on that `venue_order_id` and that the replacement had not already been answered on an earlier modify call.
9. **Late PATCH answers leaked in `self._answers`**: `_modify_order` wrote `self._answers[str(answer.id)] = answer` whenever `order.client_order_id not in self._finished`, even if `replaced` (or an earlier fill/rejection) had already settled that modify while the HTTP response was in flight.
10. **Replacement `rejected` after the old order filled returned early without settling the modify**: `_apply` returned at `if client_order_id in self._finished: return` before reaching the `order_replace_rejected` and replacement `rejected` handlers, leaving `self._modifying` unsettled and logging nothing about what became of the modify (while NautilusTrader's `ExecutionEngine` silently drops `OrderModifyRejected` on an already `FILLED` order).

---

## What Changed, and Why

### 1. `packages/canon-alpaca/src/canon_alpaca/execution.py`

* **`OPEN_STATUSES` and `order_status_report` (Defect 6, part of Defect 7)**:
  * Mapped `AlpacaOrderStatus.PENDING_REPLACE` to `OrderStatus.ACCEPTED`, and upgraded it in `order_status_report` to `OrderStatus.PARTIALLY_FILLED` when `filled_qty > 0`.
  * Why `ACCEPTED` / `PARTIALLY_FILLED` rather than `PENDING_UPDATE`: verified against the installed NautilusTrader `v2.0.0rc8` (`nautilus_execution::reconciliation::orders`) that start-up reconciliation rejects external orders in `OrderStatus.PENDING_UPDATE` (`Unhandled order status PENDING_UPDATE for external order`), whereas `ACCEPTED` (no fills) and `PARTIALLY_FILLED` (with fills) reconcile cleanly as open working orders.
* **Per-connection replace lifecycle tracking (Defects 2, 3, 4, 5, 8, 9, 10)**:
  * Added `_filled`, `_modifying` (`dict[str, ClientOrderId]`, unsettled replace per venue order ID), `_answered`, `_adopted`, `_replaced_ids`, `_superseded`, and `_rejected_replacements` to `AlpacaExecutionClient`, all cleared in `_disconnect`.
  * Why tracked explicitly on the client rather than inferred from `alpaca_order.replaced_by` or `self.cache.order(...)` alone: `ExecutionClient` emits events over an asynchronous FIFO channel to the Rust engine, so `self.cache.order(...)` does not reflect `generate_order_updated` or `generate_order_filled` synchronously inside the same `_apply` turn; tracking the per-venue-order lifecycle locally keeps stream updates and HTTP answers race-free and idempotent.
* **`replaces_as_asked`, `_modify_order`, and `_replace_refused` (Defects 2, 8, 9)**:
  * `replaces_as_asked(replacement, request, quantity)` compares `replacement.qty` against `request.qty` when set and against `order.quantity` when `request.qty is None`, and checks `None` prices stay `None`.
  * `_modify_order` claims `self._modifying[venue_order_id.value] = order.client_order_id` only when `venue_order_id.value` is neither superseded nor already in `_modifying` (`owns_modify`).
  * `_replace_refused` treats a 422 `"order already replaced"` as `alpaca-py`'s in-call retry only when `owns_modify` is true, `str(replacement.id) not in self._answered`, and `replaces_as_asked(...)` matches; otherwise it pops `_modifying` (if `owns_modify`) and emits `OrderModifyRejected` at most once.
  * `_modify_order` records `answer_id` in `_answered`, maps `answer.client_order_id` in `_replacements`, and stores `self._answers[answer_id] = answer` *only* when the order is not finished, `venue_order_id.value` is still in `_modifying`, and `answer_id` has not already been adopted, superseded, or rejected.
* **`_apply`, `_replaced`, `_adopt`, `_supersede`, `_reject_modify`, and `_fill` (Defects 2, 3, 4, 5, 7, 9, 10)**:
  * Handled `order_replace_rejected` and un-adopted replacement `rejected` *before* `if client_order_id in self._finished: return`, cleaning up `_answers` and `_modifying` and routing through `_reject_modify`. When the order has already filled (`client_order_id in self._filled` or `current.status == OrderStatus.FILLED`), `_reject_modify` logs `"Alpaca rejected ..., which had filled first; the fill is the outcome"` instead of emitting a `OrderModifyRejected` event that NautilusTrader drops.
  * `_replaced` marks `old_id` superseded, pops `_modifying`, skips if `old_id in self._replaced_ids` (popping any stale `_answers` entry), and otherwise walks `replaced_by` up to `MAX_REPLACE_CHAIN` when a fetched replacement is already `AlpacaOrderStatus.REPLACED`, popping `_answers` both before and after awaiting `_call(get_order_by_id, ...)` so an answer arriving mid-lookup cannot linger. It then calls `_adopt` once if the terminal live replacement has not yet been adopted.
  * In `_apply`, `ACCEPTING_EVENTS` (`new`, `accepted`, `pending_new`) still never adopt a replacement (since a replacement in `new` can still be rejected if the old order fills first). When `fill`, `partial_fill`, `canceled`, or `expired` arrives for an un-adopted, non-superseded replacement before `replaced`, `_apply` calls `_adopt` first — queueing `OrderUpdated` ahead of `OrderFilled` / `OrderCanceled` / `OrderExpired` on the engine's FIFO channel so the order takes the replacement's venue order ID, quantity, and prices before the fill is applied.
  * Late `canceled` or `expired` updates for an order in `_superseded` (or a non-replacement whose `venue_order_id` no longer matches `order.venue_order_id`) are logged and ignored.
* **`_order_status_report` and `_order_report` (Defects 1, 6)**:
  * Recorded `REPLACED` orders in `_superseded` and passed `walked` into `_order_report`.
  * Replaced the overly broad `order.status in CLOSED_STATUSES` skip in `_order_report`: when `prior` (`order.replaces`) is present in `known` and `prior.status != AlpacaOrderStatus.REPLACED`, the predecessor has not been replaced (it is still `pending_replace` or closed first) and represents the chain, so the replacement is skipped; when `prior` is outside the batch (`prior is None`), only a `REJECTED` replacement that never took over `held` is skipped. Replacements that took over and filled, canceled, or expired after a missed `replaced` update are reported under `node_id`.
  * Recorded `self._modifying.setdefault(str(order.id), report.client_order_id)` when reporting an open `PENDING_REPLACE` order so a subsequent rejection settles cleanly.

### 2. Documentation & Regression Tests

* **`packages/canon-alpaca/README.md`**: Updated the execution **Modify** bullet, **Stream** unmapped-events note (`pending_replace` ignored on the stream), and **Tests** table (`test_execution_client.py` description and new `test_execution_replace_races.py` row).
* **`docs/adr/0036-alpaca-adapter-order-management.md`**: Updated the Modify decision and `UNVERIFIED` race note so nothing in ADR 0036 contradicts the updated behaviour.
* **`packages/canon-alpaca/tests/test_execution_replace_races.py`** (new file; no existing test file modified): Added 5 offline `LiveNode` regression tests covering a second modify with identical terms refused before `replaced`, a late `expired` on a replaced order, `partial_fill` before `replaced` followed by `replaced` and `fill`, start-up reconciliation when both `pending_replace` and its `pending_new` replacement are returned, and two missed replaces reconciled when the final replacement fills.

---

## Upstream APIs and Contracts Followed

* **NautilusTrader `v2.0.0rc8` (`d53849b216e2`)**:
  * `nautilus_trader.live.clients.ExecutionClient` (`_modify_order`, `_generate_order_status_report`, `_generate_order_status_reports`, `generate_order_updated`, `generate_order_modify_rejected`, `generate_order_filled`, `generate_order_canceled`, `generate_order_expired`), Python adapter guide (`developer_guide/python_adapters.md`).
  * Execution policies (`concepts/execution/policies.md`, "Command outcomes") and reconciliation (`concepts/execution/reconciliation.md`), including external order status handling in `nautilus_execution::reconciliation::orders` and `ExecutionEngine`'s handling of `OrderModifyRejected` on `FILLED` orders.
* **Alpaca Trading API & `alpaca-py` `v0.45.0`**:
  * "Replace Order by ID" (`PATCH /v2/orders/{order_id}`, `TradingClient.replace_order_by_id`, `ReplaceOrderRequest`), "Orders at Alpaca" (replacement rejection when the existing open order fills first; order lifecycle statuses including `pending_replace` and `replaced`), and "Websocket Streaming" (`trade_updates`: `replaced`, `pending_replace`, `order_replace_rejected`, `fill`, `partial_fill`, `canceled`, `expired`, `rejected`).
  * `alpaca-py` `0.45.0` `OrderStatus.PENDING_REPLACE`, `OrderStatus.REPLACED`, `Order.replaces`, `Order.replaced_by`, and `RESTClient._request` retry behaviour on 429/504 (ADR 0030, ADR 0036, ADR 0039).

---

## Verification Run and Results

All offline tests and static checks were run in this checkout with `mise exec -- uv run --locked`:

1. **`mise exec -- uv run --locked pytest`**
   * Result: **Exited 0** — `375 passed, 21 skipped in 47.02s` (all 11 previously failing test functions / 13 items in `packages/canon-alpaca/tests/test_execution_client.py` pass, all 5 new tests in `packages/canon-alpaca/tests/test_execution_replace_races.py` pass, all other offline tests pass, and the 21 paper tests skip cleanly without credentials).
2. **`mise exec -- uv run --locked ruff check .`**
   * Result: **Exited 0** — `All checks passed!`
3. **`mise exec -- uv run --locked ruff format --check .`**
   * Result: **Exited 0** — `99 files already formatted`
4. **`mise exec -- uv run --locked ty check`**
   * Result: **Exited 0** — `All checks passed!`
5. **`scripts/canon lint`**
   * Result: **Exited 0** — `canon: lint clean`

---

## Uncertainties (`UNVERIFIED`)

Per `AGENTS.md` rule 4 and task rules (no paper tests or network calls were run):

* **`UNVERIFIED` on live paper**: The exact stream ordering and payloads Alpaca's paper server emits when a fill on the original order races a replace (`rejected` on the replacement vs `order_replace_rejected` on the original, or both), when `pending_replace` appears on `GET /v2/orders` alongside a `pending_new` replacement, and when a partially filled order is replaced (`qty` semantics on partial fills) were exercised against the documented contracts and offline stand-ins, not against live paper in this session.
* **`UNVERIFIED` across multi-page order histories**: If an original order is older than Alpaca's 500-order `get_orders` cap (`ORDERS_PAGE_LIMIT`) *and* closed without being replaced while its rejected replacement remains inside the newest 500 orders, and the node restarts with an empty cache (`held is None`), `_order_report` has neither `prior` in `known` nor `held` in cache to distinguish that rejected replacement from a standalone rejected order without an extra per-order HTTP read. Closed `REJECTED` external orders do not create open orders or positions, but whether that edge case can arise on paper is `UNVERIFIED`.
