# Review of `3a00f617fd20c97d647ee51f7c2c4091aac3a035` in `packages/canon-alpaca/`

Defects in what `git diff 3a00f617fd20c97d647ee51f7c2c4091aac3a035^ 3a00f617fd20c97d647ee51f7c2c4091aac3a035 -- packages/canon-alpaca/` adds or alters, ordered most severe first.

---

## 1. Reconciliation drops a closed replacement whenever `OrderUpdated` was not applied on the node, leaving the order unsettled

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._order_report` (lines 1515–1524), interacting with `order_status_report` (lines 506–507) and `AlpacaExecutionClient._order_status_report` (lines 1422–1433); module docstring (lines 20–21) and `AlpacaExecutionClient._replaced` docstring and log message (lines 1284–1285, 1298–1299).

2. **What goes wrong:**
   `_order_report` returns `None` whenever a replacement order has `order.status in CLOSED_STATUSES` (`FILLED`, `CANCELED`, `EXPIRED`, `REJECTED`) and `held.venue_order_id != VenueOrderId(str(order.id))`:
   ```python
   if (
       held is not None
       and node_id is not None
       and node_id.value != order.client_order_id
       and order.status in CLOSED_STATUSES
       and held.venue_order_id != VenueOrderId(str(order.id))
   ):
       # A replacement that closed without taking over (rejected when the old order filled first):
       # the node's order is reported by the order it still names.
       return None
   ```
   That condition assumes `held.venue_order_id != VenueOrderId(str(order.id))` means the replacement never took over. In fact, whenever `OrderUpdated` was not applied on the node (for example, `_replaced` failed to read `old.replaced_by`, the stream missed `replaced`, a transport error prevented the PATCH answer from being stored and the subsequent `get_order_by_id` failed, or a second replace closed before its `OrderUpdated` ran), `held.venue_order_id` in `self.cache` still holds the *prior* order's venue ID even though on Alpaca the prior order has `status == REPLACED` and the replacement *did* take over and then filled, canceled, or expired.
   - `order_status_report` skips the `REPLACED` prior order (`None`), and `_order_report` also returns `None` for the closed replacement, so `_generate_order_status_reports(open_only=False)` omits the order altogether.
   - Worse, `_generate_order_status_report` explicitly walks `found.replaced_by` forward from the `REPLACED` old order to the live replacement (`found = replacement`, lines 1422–1430) and then calls `self._order_report(found)`, which immediately returns `None` at lines 1515–1524.
   - Consequently, the statements at lines 20–21 (`"if neither yields them, nothing is generated and reconciliation settles the order"`), 1284–1285, and 1298–1299 (`"no OrderUpdated, reconciliation settles it"`) are false whenever the replacement has closed (`filled`, `canceled`, `expired`). The node's order stays stuck in `PENDING_UPDATE` with missing fills/terminal status.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence:
     1. Strategy submits a limit buy of 2 AAPL at `$150.00`; Alpaca accepts it (`old`, `venue_order_id = old_id`).
     2. Strategy calls `modify_order(..., price=Price.from_str("150.50"))`. Alpaca replaces `old` (`old.status = "replaced"`, `old.replaced_by = new_id`) with `new`, but `OrderUpdated` is not generated (e.g., `replaced` update is missed on the stream, or `get_order_by_id(new_id)` transiently fails inside `_replaced` as tested in `test_replacement_that_cannot_be_read_gives_no_update`). `held.venue_order_id` on the node remains `old_id`.
     3. `new` fills (or cancels/expires) on Alpaca (`new.status = "filled"`, `filled_qty = "2"`, `filled_avg_price = "150.50"`), with the fill update also missed or rejected (see Defect 2).
     4. Reconciliation calls `_generate_order_status_reports(open_only=False)` or `_generate_order_status_report` by `client_order_id` or `venue_order_id`.
   - Shown by running an offline `LiveNode` script against `FakeOrdersClient` (`venue.trading.script = "accept"`, `venue.trading.replace_script = "silent"`), modifying the order, marking `new` `"filled"` (`filled_qty="2"`, `filled_avg_price="150.50"`), setting `venue.trading.orders = list(venue.trading._placed.values())`, and calling:
     - `await client._generate_order_status_reports(SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None))` → returned `[]`
     - `await client._generate_order_status_report(SimpleNamespace(venue_order_id=None, client_order_id=strategy.client_order_id))` → returned `None`
     - `await client._generate_order_status_report(SimpleNamespace(venue_order_id=VenueOrderId(old["id"]), client_order_id=None))` → returned `None`
     while `node.cache.order(strategy.client_order_id)` remained in `OrderStatus.PENDING_UPDATE` with `filled_qty == 0`.

---

## 2. A replacement `fill` arriving before `OrderUpdated` when quantity was increased is rejected as an overfill by NautilusTrader, yet still marks the order `_finished` so `replaced` is dropped too

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._apply` (lines 1223–1224, 1270–1279).

2. **What goes wrong:**
   When a modify increases `quantity` (for example from `2` to `5` shares) and a `"fill"` (or `"partial_fill"` exceeding the old quantity) trade update for the replacement arrives while `order.venue_order_id != venue_order_id` — either because Alpaca's stream delivers the replacement's `"fill"` before `old`'s `"replaced"`, or because `_replaced` failed to read `old.replaced_by` — `_apply` calls `self._fill(order, update, venue_order_id, ts_event)` directly without first emitting `OrderUpdated` with `alpaca_order`'s new quantity and venue order ID.
   - Inside NautilusTrader's `ExecutionEngine`, `order.quantity` is still `2` while `OrderFilled.last_qty` is `5`. `ExecutionEngine` rejects and drops the fill event (`Order overfill rejected: ... potential_overfill=3, current_filled=0, last_qty=5, quantity=2`), leaving `order.filled_qty == 0` and `order.status == OrderStatus.PENDING_UPDATE`.
   - Immediately afterward, line 1277–1278 runs:
     ```python
     if event in TERMINAL_EVENTS:
         self._finished.add(client_order_id)
     ```
     Because `"fill"` is in `TERMINAL_EVENTS`, `client_order_id` is added to `self._finished` even though the engine rejected the fill. When `old`'s `"replaced"` update arrives next, line 1223 (`if client_order_id in self._finished: return`) drops it (and leaves `self._answers[str(new.id)]` leaked in memory). Because `held.venue_order_id` is still `old`'s ID and `new` has `status == FILLED`, reconciliation also drops `new` (Defect 1). The 5-share fill is lost in both stream processing and reconciliation.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence:
     1. Submit limit buy of `2` AAPL at `$150.00`; accepted (`old`).
     2. Modify `quantity` to `5` (`ModifyOrder` with `quantity=Quantity.from_str("5")`). Alpaca creates replacement `new` (`qty="5"`).
     3. Stream delivers `update(new["id"], "fill", 5, status="filled", filled_qty="5")` before `confirm_replace(old["id"])`.
     4. Stream delivers `confirm_replace(old["id"])`.
   - Shown by running `Modifier(field="quantity", prices=["5"])` on an offline `LiveNode` with `venue.trading.replace_script = "silent"`, sending `venue.trading.update(new["id"], "fill", 5, status="filled", filled_qty="5")` followed by `venue.trading.confirm_replace(old["id"])`:
     - NautilusTrader logged `[ERROR] CANON-TEST.ExecEngine: Order overfill rejected: client_order_id=O-..., last_qty=5, quantity=2, current_filled=0, potential_overfill=3`.
     - `strategy.names()` remained `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate']`.
     - `node.cache.order(strategy.client_order_id)` had `filled_qty == 0`, `quantity == 2`, `status == OrderStatus.PENDING_UPDATE`, while `strategy.client_order_id in client._finished` was `True`.

---

## 3. `AlpacaOrderStatus.PENDING_REPLACE` is missing from `OPEN_STATUSES`, raising `ValueError` in `order_status_report`, startup reconciliation, runtime status reports, and `_replaced`

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `OPEN_STATUSES` (lines 271–277), `order_status_report` (lines 503–556), `AlpacaExecutionClient._replaced` (lines 1305–1312), and module docstring (lines 120–122).

2. **What goes wrong:**
   `OPEN_STATUSES` maps `PENDING_CANCEL` (`AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL`) alongside `NEW`, `ACCEPTED`, `PENDING_NEW`, and `PARTIALLY_FILLED`, but omits `AlpacaOrderStatus.PENDING_REPLACE` (`OrderStatus.PENDING_UPDATE`), even though `packages/canon-alpaca/tests/test_paper_replace.py` line 53 recognizes `"pending_replace"` in `OPEN_STATUSES`.
   - In `order_status_report(order, ...)`:
     - `status` is `AlpacaOrderStatus.PENDING_REPLACE`.
     - `is_open` (line 504) is `True` because `PENDING_REPLACE` is neither in `CLOSED_STATUSES` nor `(AlpacaOrderStatus.REPLACED,)`.
     - `nautilus_status` (line 508) is `None`, raising `ValueError("status pending_replace")`, which lines 553–555 re-raise as `ValueError("Alpaca order ... is open with status pending_replace")`.
   - Effects:
     1. Startup reconciliation (`_generate_order_status_reports`) crashes and prevents the node from starting if a valid whole-share US equity order happens to be in `pending_replace` on Alpaca — contradicting lines 120–122 (`"Start-up fails, by design, on an open order or position this slice cannot represent: a fractional position, or an open order that is not a simple whole-share US equity market, limit, stop or stop-limit order"`).
     2. Runtime `_generate_order_status_report` / `_generate_order_status_reports` fail with `ValueError` whenever an order is queried while in `pending_replace`.
     3. If a replacement order itself is already in `pending_replace` (from a second modify) when `_replaced` calls `order_status_report(replacement, ...)`, `_replaced` raises `ValueError` and drops `OrderUpdated`.

3. **Input, state, or sequence of events, and how shown:**
   - Input: `Order(**alpaca_order(status="pending_replace"))` passed to `order_status_report(..., 0)` (or returned by `get_orders` / `get_order_by_id`).
   - Shown by running `mise exec -- uv run --locked python -c 'from alpaca.trading.models import Order; from canon_alpaca.execution import order_status_report; from conftest import alpaca_order; order_status_report(Order(**alpaca_order(status="pending_replace")), 0)'` (with `packages/canon-alpaca/tests` on `PYTHONPATH`), which raised:
     `ValueError: Alpaca order 7b08df51-c1ac-453c-99f9-323a5f075f0d (AAPL) is open with status pending_replace`.

---

## 4. Modifying an order to `quantity=0` (or `trigger_price=0` when risk checks are bypassed) raises an unhandled `ValidationError` in `_modify_order`, leaving the order stuck in `PENDING_UPDATE` with no `OrderModifyRejected`

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._modify_denial` (lines 1029–1048) and `AlpacaExecutionClient._modify_order` (lines 950–960).

2. **What goes wrong:**
   - For quantity: `_modify_denial` checks `if command.quantity is not None and not command.quantity.as_double().is_integer():` (line 1036), without checking `command.quantity.as_double() <= 0` (unlike `_denial` on submit, which checks `if order.quantity.as_double() <= 0:`). `Quantity.from_str("0").as_double()` is `0.0`, and `(0.0).is_integer()` is `True`, so `_modify_denial` returns `None`. NautilusTrader's default `RiskEngine` does *not* block `modify_order(..., quantity=Quantity.from_str("0"))` either: it sets the order to `OrderStatus.PENDING_UPDATE`, emits `OrderPendingUpdate`, and dispatches `ModifyOrder` to `_modify_order`.
   - For stop/limit prices: `sub_penny_denial(Price.from_str("0.00"), "stop price")` returns `None` (`Decimal("0.00")` has ≤ 4 decimals). When `LiveRiskEngineConfig(bypass=True)` is configured (as in `test_paper_execution.py` line 213), `ModifyOrder` with `trigger_price=Price.from_str("0.00")` reaches `_modify_order` as well.
   - Line 950 calls `ReplaceOrderRequest(...)` outside any `try` block. In `alpaca-py` 0.45.0, `ReplaceOrderRequest` validates `qty: Optional[int] = Field(gt=0)` and `stop_price: Optional[float] = Field(gt=0)`, raising `pydantic_core.ValidationError` synchronously. `ExecutionClient._modify_order_wrapper` catches the unhandled exception and logs an error, never emitting `OrderModifyRejected`; the order stays stuck in `OrderStatus.PENDING_UPDATE`.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence: Submit a 2-share limit buy on a `LiveNode` (default `RiskEngine` enabled, not bypassed); on `OrderAccepted`, call `strategy.modify_order(client_order_id, quantity=Quantity.from_str("0"))`.
   - Shown by running `Modifier(field="quantity", prices=["0"])` through `build_node` in an offline `LiveNode` run (and likewise `Modifier(order_type="stop", field="trigger_price", prices=["0.00"])` with `LiveRiskEngineConfig(bypass=True)`):
     - Logged `[ERROR] CANON-TEST.ALPACA: Client ALPACA operation _modify_order failed: 1 validation error for ReplaceOrderRequest\nqty\n  Input should be greater than 0 [type=greater_than, input_value=0, input_type=int]`.
     - `strategy.names()` stopped at `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate']` with no `OrderModifyRejected`.

---

## 5. `_replaced` raises `ValueError` and drops `OrderUpdated` when a replacement fetched from Alpaca has already been replaced (`status == REPLACED`)

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._replaced` (lines 1292–1313) and `AlpacaExecutionClient._replace_refused` (lines 997–1014).

2. **What goes wrong:**
   To extract `quantity`, `price`, and `trigger_price` for `generate_order_updated`, `_replaced` calls `order_status_report(replacement, ...)` (lines 1305–1312). Whenever `self._answers.pop(str(old.replaced_by), None)` misses, `_replaced` fetches `replacement` via `get_order_by_id(str(old.replaced_by))`. That cache miss happens in several normal flows:
   - alpaca-py retried a 504/429 PATCH and got `422 "order already replaced"`, so `_modify_order` took the `except APIError` path into `_replace_refused` (lines 965–968): `_replace_refused` fetched `replacement` on lines 997–1003 and verified `replaces_as_asked`, **but discarded `replacement` without storing it in `self._answers` or `self._replacements`**;
   - the PATCH call hit a transport error (`OSError`) or HTTP 5xx after Alpaca accepted it; or
   - the client reconnected, clearing `self._answers`.
   If `replacement` (`R1`) has itself already been replaced by a second modify (`R2`) by the time `_replaced` calls `get_order_by_id(R1.id)`, Alpaca returns `R1` with `R1.status == AlpacaOrderStatus.REPLACED` (even though `R1.qty`, `R1.limit_price`, and `R1.stop_price` are still populated on `R1`). `order_status_report(R1, ...)` returns `None` at lines 506–507 / 556 because `status == REPLACED`, causing line 1311–1312 to raise:
   `ValueError(f"replacement {replacement.id} is not an order this slice maps")`,
   which `_consume_updates` catches and logs as an error, dropping `OrderUpdated` for `R1`.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence:
     1. Submit limit order `O0`; modify it to `R1`, where `self._answers` has no entry for `R1.id` (e.g., 504→422 retry handled by `_replace_refused`, or reconnect).
     2. Before `O0`'s `"replaced"` update calls `get_order_by_id(R1.id)`, `R1` transitions to `status = "replaced"` on Alpaca (from a subsequent replace `R2`).
     3. `O0`'s `"replaced"` update runs `_replaced`.
   - Shown by running an offline `LiveNode` with `venue.trading.replace_script = "silent"`, clearing `client._answers`, setting `new["status"] = "replaced"`, and calling `venue.trading.confirm_replace(old["id"])`:
     - Logged `[ERROR] CANON-TEST.ALPACA: Failed to apply Alpaca trade update TradeEvent.REPLACED: ValueError('replacement ... is not an order this slice maps')`.
     - `OrderUpdated` was never emitted.

---

## 6. `_modify_order` prefers a stale `command.venue_order_id` over `order.venue_order_id`, and `replaces_as_asked` falsely suppresses `OrderModifyRejected` when `request.qty is None`

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._modify_order` (line 943), `AlpacaExecutionClient._replace_refused` (lines 995–1020), and `replaces_as_asked` (lines 469–479).

2. **What goes wrong:**
   Two defects in `_modify_order` / `_replace_refused` affect back-to-back modifies:
   1. **Stale `venue_order_id` selection (line 943):** `_modify_order` computes `venue_order_id = command.venue_order_id or order.venue_order_id`. `Strategy.modify_order` snapshots `order.venue_order_id` onto `ModifyOrder.venue_order_id` when the command is constructed. If a strategy (or two components) submits a first modify `M1` (`old_id` → `R1`) and submits a second modify `M2` before `M1`'s `OrderUpdated` updates `self.cache`, `M2.venue_order_id` holds `old_id`. Even if `M1`'s `OrderUpdated` updates `order.venue_order_id` to `R1.id` *before* `_modify_order` executes `M2`, `command.venue_order_id or order.venue_order_id` still picks `M2`'s stale `old_id` instead of `order.venue_order_id` (`R1.id`) — unlike `_cancel_all_orders` (line 854, which reads `order.venue_order_id`) and unlike `_cancel_refused` (lines 911–918, which follows `held.replaced_by`). Alpaca answers `422 "order already replaced"` on `old_id`.
   2. **False match in `replaces_as_asked` (lines 469–479, 1009–1014):** When `422` hits `_replace_refused`, `replaces_as_asked(replacement, request)` checks:
      ```python
      asked is None or (held is not None and Decimal(str(held)) == Decimal(str(asked)))
      ```
      Because `_modify_order` sets `request.qty = None` whenever `command.quantity is None` (while always populating `limit_price` / `stop_price` from `command.price or order.price`), if `M1` modified **both** `quantity=5` and `price=150.50` on a 2-share order (`R1` has `qty="5", limit_price="150.5"`), and `M2` (sent while `order` still had `venue_order_id = old_id` and `quantity = 2`) modified **only** `price=150.50` (`command.quantity = None`, intending to keep `qty = 2`), `M2` hits 422 on `old_id`, fetches `R1`, sees `request.qty is None` and `request.limit_price == 150.5 == R1.limit_price`, and `replaces_as_asked` returns `True` despite `R1.qty == "5" != "2"`. `_replace_refused` logs `"shows the order replaced as asked: an earlier attempt reached it"` and suppresses `OrderModifyRejected`.

3. **Input, state, or sequence of events, and how shown:**
   - Shown by running both two-modify sequences on an offline `LiveNode` against `FakeOrdersClient`:
     1. Sending `modify_order(cid, price=150.50)` and `modify_order(cid, price=151.00)` on `OrderAccepted` (`venue.trading.replace_script = "replaced"`): `venue.trading.replace_requests` sent **both** PATCH requests to `old_id`, and the second modify was rejected with `OrderModifyRejected("Alpaca refused the replace (422): order already replaced")` even though `OrderUpdated` for `150.50` had arrived.
     2. Sending `modify_order(cid, quantity=5, price=150.50)` then `modify_order(cid, price=150.50)` on a 2-share limit buy (`venue.trading.replace_script = "silent"`): the second modify hit 422 on `old_id`, logged `[WARN] ... Alpaca refused a replace of ... (422) but shows the order replaced as asked: an earlier attempt reached it`, and emitted **no** `OrderModifyRejected` even though the second command had not changed `quantity` from `2` while `R1` had `qty="5"`.

---

## 7. `test_rejected_replacement_is_modify_rejected_not_order_rejected` never executes the rejected-replacement branch or emits `OrderModifyRejected`, and the comments and branch misstate and mishandle that race

1. **File and location:**
   - `packages/canon-alpaca/tests/test_execution_client.py`: `test_rejected_replacement_is_modify_rejected_not_order_rejected` (lines 1030–1050).
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: module docstring (lines 25–27) and `AlpacaExecutionClient._apply` (lines 1223–1224, 1243–1252).

2. **What goes wrong:**
   1. **Test passes without executing the code under test:** In `test_rejected_replacement_is_modify_rejected_not_order_rejected`, line 1040 sends a full `"fill"` for `old` (`2` of `2` shares) *before* line 1041 sends `"rejected"` for `new`, and lines 1048–1049 assert only:
      ```python
      assert "OrderRejected" not in strategy.names()
      assert "OrderFilled" in strategy.names()
      ```
      It never asserts `"OrderModifyRejected" in strategy.names()`. When `_apply` processes `old`'s `"fill"`, line 1278 adds `client_order_id` to `self._finished`. When `_apply` next processes `new`'s `"rejected"`, line 1223 (`if client_order_id in self._finished: return`) returns immediately. Lines 1243–1252 (`if event == "rejected" and is_replacement and order.venue_order_id != venue_order_id:`) are **never reached**, and `OrderModifyRejected` is **never emitted** (`strategy.names()` is `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderFilled']`). Deleting lines 1243–1252 entirely would still leave this test passing.
   2. **False comment and docstring claims:** Module docstring lines 25–27 state `"`order_replace_rejected` and a replacement's `rejected` are `OrderModifyRejected` too, the second because Alpaca rejects the replacement when the old order fills first"`, and lines 1244–1246 state `"# Alpaca: if the old order 'is filled before the replacing (new) order reaches the execution venue, the replacing (new) order is rejected'. The node's order is the old one, filled. self.generate_order_modify_rejected(...)"`. Whenever the old order has already filled on the node, `self._finished` drops `new`'s `"rejected"` before line 1243 (and even without `self._finished`, NautilusTrader's `ExecutionEngine` ignores `OrderModifyRejected` on an already-`FILLED` order). Lines 1243–1252 only ever emit `OrderModifyRejected` if `new`'s `"rejected"` arrives *before* `old`'s `"fill"` (or after a `partial_fill` while `old` is still open).
   3. **Wrong `venue_order_id` and leaked `self._answers` when lines 1243–1252 *do* run:** When `new`'s `"rejected"` arrives while `old` is still open (`order.venue_order_id == old_id`), line 1248 passes `venue_order_id` (`new.id`, the replacement that never took over) instead of `order.venue_order_id` (`old.id`) to `generate_order_modify_rejected`, and unlike `order_replace_rejected` (lines 1233–1236) never deletes `str(alpaca_order.id)` from `self._answers` (nor do terminal events on `old`).

3. **Input, state, or sequence of events, and how shown:**
   - Shown by running `test_rejected_replacement_is_modify_rejected_not_order_rejected`'s exact sequence in an offline `LiveNode`:
     - When `old` `"fill"` ran before `new` `"rejected"` (as in the test): `strategy.names()` was `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderFilled']` — no `OrderModifyRejected`.
     - When `new` `"rejected"` ran before `old` `"fill"`: `strategy.names()` was `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderModifyRejected', 'OrderFilled']`, `OrderModifyRejected.venue_order_id` was `new["id"]` while `order.venue_order_id` was `old["id"]`, and `new["id"] in client._answers` remained `True`.

---

## 8. `FakeOrdersClient` and `test_replace_rejected_on_the_stream_is_modify_rejected` raise HTTP 422 alongside `"order_replace_rejected"`, emitting two `OrderModifyRejected` events and allowing the test to pass if the stream handler is deleted

1. **File and location:**
   - `packages/canon-alpaca/tests/conftest.py`: `FakeOrdersClient.replace_order_by_id` (lines 482–484).
   - `packages/canon-alpaca/tests/test_execution_client.py`: `test_replace_rejected_on_the_stream_is_modify_rejected` (lines 1019–1028).

2. **What goes wrong:**
   In `FakeOrdersClient.replace_order_by_id`, the `"reject"` script does both:
   ```python
   if self.replace_script == "reject":
       self._send("order_replace_rejected", old)
       raise api_error(422, 42210000, "replace rejected (stand-in)")
   ```
   1. Raising `422` from `replace_order_by_id` causes `_modify_order` → `_replace_refused` to emit `OrderModifyRejected("Alpaca refused the replace (422): replace rejected (stand-in)")` on its own, while the stream consumer *also* emits `OrderModifyRejected("Alpaca rejected the replace (trade update)")`. `strategy.names()` receives **two** `OrderModifyRejected` events for a single modify.
   2. Because `test_replace_rejected_on_the_stream_is_modify_rejected` only checks `assert "OrderModifyRejected" in strategy.names()` (without checking the count or `reason`), the test passes even if the entire `if event == "order_replace_rejected":` block in `execution.py` (lines 1232–1240) is deleted.
   3. Because `replace_order_by_id` raises instead of returning `answer` (unlike real asynchronous venue rejection after HTTP 200), `self._answers[str(answer.id)] = answer` (line 978) never runs, so the `self._answers` cleanup loop at lines 1233–1236 is never exercised against an actual stored answer.

3. **Input, state, or sequence of events, and how shown:**
   - Shown by running `test_replace_rejected_on_the_stream_is_modify_rejected`'s setup on an offline `LiveNode` and inspecting `strategy.events`:
     - `strategy.names()` was `['OrderSubmitted', 'OrderAccepted', 'OrderPendingUpdate', 'OrderModifyRejected', 'OrderModifyRejected']` with reasons `'Alpaca rejected the replace (trade update)'` and `'Alpaca refused the replace (422): replace rejected (stand-in)'`.

---

## 9. `_order_status_report` does not catch HTTP 404 when walking `replaced_by` links, and `_order_report` logs a false "not in this slice" warning for every `replaced` order

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._order_status_report` (lines 1407–1430) and `AlpacaExecutionClient._order_report` (lines 1500–1539).

2. **What goes wrong:**
   1. **Unhandled 404 while walking `replaced_by` (line 1430):** In `_order_status_report`, lines 1407–1421 wrap only the initial `get_order_by_id` / `get_order_by_client_id` call in `try ... except APIError as e: if e.status_code == 404: return None`. The `for _ in range(MAX_REPLACE_CHAIN):` loop at lines 1422–1430 calls `await self._call(self._venue().get_order_by_id, str(found.replaced_by))` *outside* that `try` block (contrast `_node_order_id` lines 1188–1195, which catches 404 on every hop in the chain). If `get_order_by_id(str(found.replaced_by))` returns HTTP 404, `_order_status_report` raises an unhandled `APIError` instead of returning `None`.
   2. **False warning for every `replaced` order (lines 1533–1538):** In `_order_report`, when `_generate_order_status_reports` runs with `open_only=False` (including startup reconciliation and periodic closed-order reconciliation), `found` includes every `replaced` order in the window. For each `replaced` order, `order_status_report` raises `ValueError("status replaced: its replacement is reported instead")` internally and returns `None` (lines 506–507, 556). `_order_report` then unconditionally logs:
      ```
      Alpaca order <id> (<symbol>, replaced) is not reported: its type, side, class, quantity or time in force is not in this slice
      ```
      That statement in the warning is false: the order *is* a valid US equity order in this slice and was skipped only because its status is `replaced`.

3. **Input, state, or sequence of events, and how shown:**
   - For (1): `found` returned by `get_order_by_client_id` has `status=AlpacaOrderStatus.REPLACED` and `replaced_by=<uuid>` whose `get_order_by_id` raises `api_error(404, 40410000, "order not found")`; line 1430 is outside `try` and propagates `APIError`.
   - For (2): Ran `await client._generate_order_status_reports(SimpleNamespace(open_only=False, instrument_id=None, start=None, end=None))` after one replace of a valid 2-share AAPL DAY limit order; the client logged:
     `[WARN] CANON-TEST.ALPACA: Alpaca order 63512a5d-... (AAPL, replaced) is not reported: its type, side, class, quantity or time in force is not in this slice`.

---

## 10. `_node_order_id` permanently caches a replacement in `self._foreign` if a stream update arrives before startup reconciliation populates `self.cache`

1. **File and location:**
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: `AlpacaExecutionClient._connect_venue` (lines 671–680) and `AlpacaExecutionClient._node_order_id` (lines 1180–1206).

2. **What goes wrong:**
   `_connect_venue` starts the trade-updates stream and `_consume_updates` task (lines 672–680) *before* `LiveNode` runs startup reconciliation to populate `self.cache`. If an open order `O0` existed before connect and is replaced externally (or by another client/before shutdown) by `R1` while startup reconciliation is in flight, and `R1` delivers a trade update before `ExecutionEngine` adds `O0` to `self.cache`:
   - `_node_order_id(R1)` walks `R1.replaces` → `O0` → `O0.replaces is None`.
   - Because `self.cache.order(ClientOrderId(O0.client_order_id))` is still `None`, the loop reaches `replaces is None` on the next iteration and executes `self._foreign.add(R1.client_order_id)` (line 1185), returning `None`.
   - `self._foreign` is never invalidated during the session (cleared only in `_disconnect`, line 701). Even after startup reconciliation finishes and inserts `O0` into `self.cache`, line 1180 (`if alpaca_order.client_order_id in self._foreign: return None`) causes every subsequent stream update and reconciliation report for `R1` (and any `R2` whose walk stops before `O0`) to fail to trace back to `O0` for the lifetime of the connection.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence:
     1. `_connect_venue` starts `_stream_thread` and `_consume_updates` while `self.cache` is still empty.
     2. Startup reconciliation's `get_orders` captures `O0` (open), and concurrently `O0` is replaced by `R1`, which emits `"new"` on the trade stream before `ExecutionEngine` registers `O0` in `self.cache`.
     3. `_apply` calls `_node_order_id(R1)` → adds `R1.client_order_id` to `self._foreign`.
     4. Startup reconciliation adds `O0` to `self.cache`; later `R1` emits `"fill"` on the stream → `_node_order_id(R1)` hits `R1.client_order_id in self._foreign` at line 1180 and drops the fill as belonging to an order `"which this node does not hold"`.
   - Shown by code inspection of `_connect_venue` (lines 671–680), `_node_order_id` (lines 1174–1206), and `_disconnect` (line 701).

---

## 11. Defects in the new paper tests (`test_paper_replace.py` and `test_paper_execution.py`)

1. **File and location:**
   - `packages/canon-alpaca/tests/test_paper_replace.py`: `observe` (lines 118–130) and `test_paper_replace_answers_as_observed` (lines 211–216).
   - `packages/canon-alpaca/tests/test_paper_execution.py`: `AcceptModifyCancel.on_order_event` (lines 125–136) and `test_paper_limit_order_is_modified_through_a_node` (lines 166–190).

2. **What goes wrong:**
   1. **`KeyError` before assertion #2 in `test_paper_replace.py`:** In `observe` (lines 118–129), `seen["replace"] = {"answer": answer}` is assigned *only* inside `except APIError:`. If Alpaca ever accepts a replace that keeps `client_order_id` (the exact regression that assertion `# 2` at lines 214–216 is written to catch), `observe` succeeds without populating `seen["replace"]`, and line 212 (`old, new = seen["placed"], seen["replace"]["answer"]`) crashes with `KeyError: 'replace'` before reaching lines 214–216.
   2. **`AcceptModifyCancel` in `test_paper_execution.py` modifies immediately on `OrderAccepted` (while Alpaca can still hold `accepted`), omits `OrderCancelRejected` from its stop condition, and masks `OrderModifyRejected` behind cleanup failure:**
      - `_submit_order` (in `execution.py` line 769) emits `OrderAccepted` immediately from the `POST /v2/orders` response, when Alpaca's order status is frequently `accepted` or `pending_new` rather than `new`. As `execution.py` line 28 notes (`an order in accepted "cannot be replaced"`) — and why `test_paper_replace.py` line 116 explicitly polls `wait_for(lambda: status(trading, old_id) == "new", ...)` before replacing — calling `self.modify_order(...)` synchronously inside `on_order_event` on `OrderAccepted` (line 130–131) races Alpaca's routing from `accepted` to `new` and can fail with HTTP 422 (`OrderModifyRejected`) for timing reasons rather than an adapter bug.
      - When `OrderModifyRejected` does occur, line 134–135 calls `self.shutdown_system(...)` without canceling the still-open `$1.00` order on Alpaca, so `aapl_left_as_found()` sets `cleaned["orders"] = 1`, and line 178 (`assert cleaned == {"orders": 0, "positions": 0}`) fails first, hiding `strategy.names()` on lines 179–186.
      - Unlike `AcceptThenCancel` at line 104 (which includes `"OrderCancelRejected"` in its shutdown set), `AcceptModifyCancel` at line 134 omits `"OrderCancelRejected"`, so if the post-modify cancel is rejected, the node hangs until the 60-second `asyncio.timeout` instead of shutting down cleanly.

3. **Input, state, or sequence of events, and how shown:**
   - Shown by code inspection of `test_paper_replace.py` (lines 116–130, 211–216), `test_paper_execution.py` (lines 104, 125–136, 178–186), and `execution.py` (lines 28–29, 769). (Paper tests were not executed, per task rules.)

---

## 12. Statements that `replaces` is "walked again after a restart" are false for a node restart (uncertain only if "restart" meant an in-process client reconnect)

1. **File and location:**
   - `packages/canon-alpaca/README.md`: lines 207–209.
   - `packages/canon-alpaca/src/canon_alpaca/execution.py`: lines 22–23, 631–633, and 1170–1172.
   - `packages/canon-alpaca/tests/test_execution_client.py`: lines 923–926 (`test_fill_on_the_replacement_reaches_the_nodes_order[after-restart]`).

2. **What goes wrong:**
   `README.md` lines 207–209 and `execution.py` lines 22–23 state:
   > "The replacement gets a client order ID Alpaca makes, so its updates and reports are traced back to the node's order through its `replaces` links, cached and walked again after a restart."

   And `execution.py` lines 631–632 / 1172 refer to `"a new client after a restart"` / `"the walk repeats after a restart, when the cache is empty"`.
   - As `packages/canon-alpaca/README.md` line 105 states, custom Python clients in NautilusTrader v2 have **"no Redis or PostgreSQL cache backing"**. Across an actual node/process restart, `self.cache` starts empty.
   - When the new node starts up, startup reconciliation fetches Alpaca orders: the original order `O0` has `status == REPLACED` and is skipped (`order_status_report` returns `None`), so `O0` is **never** added to `self.cache`. When `_node_order_id(R1)` walks `R1.replaces` → `O0`, `self.cache.order(ClientOrderId(O0.client_order_id))` is `None` (line 1201), `self._replacements` is empty, so `_node_order_id(R1)` adds `R1.client_order_id` to `self._foreign` and returns `None`. `R1` is therefore reconciled under Alpaca's generated UUID `client_order_id`, **not** traced back to `O0`'s `client_order_id`.
   - `test_fill_on_the_replacement_reaches_the_nodes_order` (lines 923–939) parametrizes `cold=True` with ID `"after-restart"`, but only calls `client._replacements.clear()` on the **same running node** whose in-memory `self.cache` still holds `O0`.
   - **Uncertainty note:** If by "restart" the author meant only an in-process `_disconnect()` followed by `_connect()` on the same `LiveNode` instance (where `self.cache` remains populated in memory while `self._replacements` was cleared in `_disconnect`), the code works for that case; for an actual node/process restart (which is how a reader normally reads "after a restart" and "a new client after a restart"), the statement is false.

3. **Input, state, or sequence of events, and how shown:**
   - Sequence: Node 1 submits `O0` (`client_order_id = "O-node1"`) and modifies it to `R1` (`client_order_id = "<alpaca-uuid>"`, `replaces = O0.id`; `O0.status = "replaced"`), then stops. A fresh Node 2 starts against the same Alpaca state (`venue.trading.orders = [O0, R1]`).
   - Shown by code inspection of `README.md` line 105, `order_status_report` lines 506–507, `_node_order_id` lines 1174–1206, `_order_report` lines 1506–1533, and `test_execution_client.py` lines 923–943: on Node 2, `self.cache` does not contain `"O-node1"`, so `_node_order_id(R1)` returns `None` and `R1` is reported under `"<alpaca-uuid>"` instead of `"O-node1"`.
