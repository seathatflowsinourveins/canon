# Review of `4afa6a7c62f0f186761a402cec9df5fb5374f78a` (`packages/canon-alpaca/`)

Defects are listed most severe first.

---

## 1. `_cancel_all_orders` skips `PENDING_CANCEL` orders whose single-order cancel failed without reaching Alpaca, leaving live orders open

1. **File and location:** `packages/canon-alpaca/src/canon_alpaca/execution.py`, `AlpacaExecutionClient._cancel_all_orders` (lines 715–740, specifically lines 722–723 and 738); also `execution.py` module docstring (lines 62–63) and `packages/canon-alpaca/README.md` (line 201).
2. **What goes wrong:** `_cancel_all_orders` skips any cached order with `order.status == OrderStatus.PENDING_CANCEL` (`execution.py:738`), and its docstring asserts (`execution.py:722–723`): *"An order already `PENDING_CANCEL` has a cancel in flight and is left to it."* That statement is false under this client's own failure handling, and skipping `PENDING_CANCEL` leaves live orders open on Alpaca. When a strategy sends a single-order `CancelOrder` (including via `strategy.cancel_all_orders(...)` with the default `strategy_only=True`), NautilusTrader transitions the cached order to `OrderStatus.PENDING_CANCEL` *before* `_cancel_order` sends any HTTP request. If `_cancel` / `_cancel_logged` then hits an unknown or unexpected error (`OSError` at `execution.py:777–780`, a non-422 `APIError` such as HTTP 500/502/503/429 at `execution.py:771–775`, or `ValueError` / `TypeError` / `RuntimeError` in `_cancel_logged` at `execution.py:706–712`), it logs the error and emits no `OrderCancelRejected` (*"the order is left as it is"*). The order stays `OrderStatus.PENDING_CANCEL` in cache while **no cancel is in flight** and Alpaca still holds the order open (`new` / `accepted`). Moreover, `Strategy.cancel_order` subsequently refuses to retry canceling a `PENDING_CANCEL` order (`Cannot cancel order: state is PendingCancel`), making `CancelAllOrders(..., strategy_only=False)` (such as on strategy/node stop) the only cancel command that reaches the execution client—and `_cancel_all_orders` skips it at line 738, leaving the live order working on Alpaca after shutdown. (By contrast, if a cancel *were* genuinely `pending_cancel` or `canceled` at Alpaca, calling `_cancel_logged` and receiving HTTP 422 is already handled quietly by `_cancel_refused` at lines 792–796, which suppresses `OrderCancelRejected` when Alpaca reports `canceled` or `pending_cancel`.)
3. **Input, state, or sequence of events, and verification:**
   - Sequence:
     1. A strategy submits a DAY limit buy of 1 AAPL; Alpaca accepts it (`OrderAccepted`, cached status `ACCEPTED`, Alpaca status `new`).
     2. The strategy calls `self.cancel_order(order)` (or `self.cancel_all_orders(AAPL)` with default `strategy_only=True`). NautilusTrader moves the cached order to `OrderStatus.PENDING_CANCEL`.
     3. `TradingClient.cancel_order_by_id` raises `ConnectionError("network blip")` before reaching Alpaca; `_cancel` logs the `OSError` and leaves the order as-is (`PENDING_CANCEL` locally, `new` on Alpaca).
     4. Connectivity recovers, and on shutdown the strategy or `ExecTester` calls `self.cancel_all_orders(AAPL, strategy_only=False)`.
     5. `_cancel_all_orders` finds the order via `orders_open` / `orders_inflight`, hits `order.status == OrderStatus.PENDING_CANCEL` at line 738, and `continue`s without calling Alpaca.
   - Verification: Ran a `LiveNode` offline against `FakeOrdersClient` with that exact sequence (`submit_order` -> `OrderAccepted` -> `cancel_error = ConnectionError("network blip")` -> `cancel_order` -> `cancel_error = None` -> both `cancel_order` and `cancel_all_orders(AAPL, strategy_only=False)`). `len(trading.cancels)` stayed `1` (only the failed attempt), the cached order stayed `OrderStatus.PENDING_CANCEL`, and Alpaca's order remained `"new"`.

---

## 2. `aapl_left_as_found` calls `cancel_order_by_id` unguarded in `finally` (and queries `open_aapl_orders` without `limit`), aborting cleanup and masking test failures when an order is `pending_cancel` or closes concurrently

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, `open_aapl_orders` (lines 133–136) and `aapl_left_as_found` (lines 150–178, specifically lines 172–174).
2. **What goes wrong:** In `aapl_left_as_found`'s `finally` block (`lines 172–177`):
   ```python
   for order in open_aapl_orders(trading):
       trading.cancel_order_by_id(order.id)
       cleaned["orders"] += 1
   if holds_aapl(trading):
       trading.close_position("AAPL")
       cleaned["positions"] += 1
   ```
   `open_aapl_orders` requests `GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=["AAPL"])`. On Alpaca, `status=open` includes orders in `pending_cancel` (for example, an order whose cancel the node sent right before timing out or disconnecting) as well as orders that fill, finish canceling, or expire between `get_orders` and `cancel_order_by_id`. On such an order, `TradingClient.cancel_order_by_id` (`DELETE /v2/orders/{order_id}`) raises `alpaca.common.exceptions.APIError` (HTTP 422 `"order is not cancelable"`, or HTTP 404). Because `cancel_order_by_id` is unguarded inside `finally`:
   - the loop aborts immediately, leaving any remaining open AAPL orders in the list uncanceled;
   - `if holds_aapl(trading): trading.close_position("AAPL")` is skipped, leaving any AAPL position open; and
   - the `APIError` replaces and masks whatever assertion failure or exception occurred inside the `with` block, and leaves the paper account dirty so subsequent paper tests fail `aapl_left_as_found`'s pre-check.
   In addition, `GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=["AAPL"])` at line 134 omits `limit=500`, defaulting to Alpaca's page size of 50 (`execution.py:191`), so if more than 50 open AAPL orders were placed, `finally` cancels at most 50.
3. **Input, state, or sequence of events, and verification:**
   - Sequence: A test raises an assertion/timeout while two AAPL orders are returned by `open_aapl_orders(trading)`—the first already `pending_cancel` (or filled/canceled between `get_orders` and `cancel_order_by_id`) and the second `new`—and an AAPL position is held.
   - Verification: Checked `TradingClient.cancel_order_by_id` in pinned `alpaca-py` (`alpaca/trading/client.py`, which calls `self.delete(f"/orders/{order_id}")` and raises `APIError` on non-2xx) and executed `aapl_left_as_found()` offline with a stand-in `TradingClient` returning `[ord-1-pending-cancel, ord-2-still-open]` and an open AAPL position when `AssertionError("original test failure")` was raised in the `with` block. `aapl_left_as_found()` raised `APIError: {"code": 42210000, "message": "order is not cancelable"}` instead of `AssertionError`, never called `cancel_order_by_id` on `ord-2-still-open`, never called `close_position("AAPL")`, and sent `GetOrdersRequest` with `limit=None`.

---

## 3. `aapl_left_as_found` does not wait for asynchronous cancels or `close_position("AAPL")` to settle, so `close_position` can fail with HTTP 403 or leave an open closing order and position behind

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, `aapl_left_as_found` (lines 150–178, specifically lines 172–177).
2. **What goes wrong:** `aapl_left_as_found`'s docstring states (`lines 151–152`): *"Refuse to start while the paper account holds an open AAPL order or an AAPL position, and leave it so: afterwards, whatever happened, cancel every open AAPL order and close any AAPL position."* Both cleanup operations in `finally` are asynchronous on Alpaca, and `aapl_left_as_found` returns without waiting for either to complete:
   - `trading.cancel_order_by_id(order.id)` returns HTTP 204 as soon as Alpaca accepts the cancel request (`pending_cancel`), before shares reserved by an open order are released. Immediately calling `trading.close_position("AAPL")` (`DELETE /v2/positions/AAPL` with no `cancel_orders` parameter in `TradingClient.close_position`) fails with `APIError` (HTTP 403, insufficient available qty / opposing open order) when a just-canceled AAPL order has not yet transitioned to `canceled`.
   - Even when `trading.close_position("AAPL")` succeeds, Alpaca's `DELETE /v2/positions/AAPL` merely submits a closing market `Order` and returns that `Order` while it is still open (`accepted` / `new` / `pending_new`; outside regular trading hours it remains queued and open until the next session). Returning immediately from `aapl_left_as_found` leaves that closing AAPL order open (and the AAPL position open until the order fills), violating *"and leave it so"* and failing the next paper test's `assert held == []` / `assert not holds_aapl(trading)` pre-check.
3. **Input, state, or sequence of events, and verification:**
   - Sequence: During `test_paper_limit_order_is_accepted_and_canceled`, an AAPL order fills (creating a 1-share AAPL position; if another open AAPL order is also being canceled, its shares are still locked in `pending_cancel`). `aapl_left_as_found`'s `finally` calls `cancel_order_by_id` and immediately calls `trading.close_position("AAPL")`, then `test_paper_cancel_all_on_stop_tc_e41` starts and calls `aapl_left_as_found()`.
   - Verification: Inspected `TradingClient.close_position` in pinned `alpaca-py==0.45.0` (`alpaca/trading/client.py`: sends `DELETE /positions/{symbol_or_asset_id}` accepting only `ClosePositionRequest(qty=..., percentage=...)` and returns an `Order` model for the newly submitted liquidation order without waiting for fill).

---

## 4. `node.dispose()` in `test_paper_execution.py` does not release NautilusTrader's thread-local `LiveNode` guard when a test fails, causing the next paper test to fail with `RuntimeError` and making line 189's comment false

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, `test_paper_limit_order_is_accepted_and_canceled` (lines 180–199, specifically lines 189–190) and `run_exec_tester` (lines 234–281, specifically lines 238–254 and 279–280).
2. **What goes wrong:** Commit `4afa6a7` wraps `test_paper_limit_order_is_accepted_and_canceled` in `with aapl_left_as_found() as cleaned:`—retaining the comment `# Disposed here, so a failure's traceback does not keep the node and fail the next paper test.` above `node.dispose()` at lines 189–190—and adds a second `LiveNode` test (`test_paper_cancel_all_on_stop_tc_e41` -> `run_exec_tester`, which likewise binds `node` as a local variable and only calls `node.dispose()` in `finally` at lines 279–280).
   In pinned `nautilus_trader==2.0.0rc8`, `node.dispose()` does **not** release NautilusTrader's thread-local single-`LiveNode` guard; only dropping all references to the Python `LiveNode` object (`Drop` in Rust, e.g. `del node` / `node = None`) releases it. When `test_paper_limit_order_is_accepted_and_canceled` fails (whether inside `run_hosted(node)`, inside `aapl_left_as_found`'s `finally`, or on `assert cleaned == ...` / `assert has_account` / `assert strategy.events == ...`), `pytest` stores the failing test's traceback frames until the end of the session, keeping the local `node` object alive despite `node.dispose()`. When `test_paper_cancel_all_on_stop_tc_e41` then executes `LiveNode.builder(...).build()` at line 253 on the same thread, it fails immediately for the wrong reason with `RuntimeError: A LiveNode already exists or is being built on this thread; drop it before building another`. (Likewise, if `run_exec_tester` raises inside `asyncio.run(main())`, its traceback retains `node` and fails any subsequent `LiveNode` test on the thread, such as `test_paper_integration.py`.)
3. **Input, state, or sequence of events, and verification:**
   - Sequence: Run `pytest` on both tests in `test_paper_execution.py` when `test_paper_limit_order_is_accepted_and_canceled` fails (e.g., `run_hosted` raises or times out, or `assert cleaned == ...` / `assert strategy.events == ...` fails).
   - Verification: Ran `pytest.main` offline with stand-ins executing `test_paper_limit_order_is_accepted_and_canceled` (configured to raise inside `run_hosted`, and separately verified after cancelling `node.run_async()` and calling `node.dispose()`) followed by `test_paper_cancel_all_on_stop_tc_e41`. Although `node.dispose()` ran and logged `Disposed`, `test_paper_cancel_all_on_stop_tc_e41` failed at `test_paper_execution.py:253` (`.build()`) with `RuntimeError: A LiveNode already exists or is being built on this thread; drop it before building another`.

---

## 5. `run_exec_tester` waits out the full 60-second deadline when an order is `DENIED` or `REJECTED` (including on any sub-penny quote bid >= $51.00)

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, `TOB_OFFSET_TICKS` (lines 69–71) and `run_exec_tester` (lines 264–269).
2. **What goes wrong:**
   - `AlpacaInstrumentProvider` configures US equities with `price_precision=4` (`0.0001`), and `AlpacaDataClient` forwards any quote representable at 4 decimal places. When `ExecTester` receives a quote whose bid has 3 or 4 non-zero decimal places at or above `$51.00` (e.g., `$174.5550`), subtracting `TOB_OFFSET_TICKS = 500_000` (`$50.0000`) yields a limit buy price `>= $1.00` with more than 2 decimal places (`$124.5550`), which `AlpacaExecutionClient._submit_order` immediately denies (`OrderDenied`: `"limit price 124.555 has more than 2 decimals, which Alpaca rejects for a price at or above $1.00"`).
   - In `run_exec_tester` (`lines 265–269`), the polling loop only exits early when `len(orders) == count and all(o.status == OrderStatus.ACCEPTED for o in orders)`. If `ExecTester`'s order reaches a terminal non-accepted status (`OrderStatus.DENIED` or `OrderStatus.REJECTED`), that condition can never become true—even if a subsequent order were accepted (`len(orders)` would exceed `count` and `all(...)` would still see the closed order)—so `run_exec_tester` needlessly stalls for the full 60-second deadline before stopping the node (keeping any other accepted order live on the paper account for up to 60 seconds).
3. **Input, state, or sequence of events, and verification:**
   - Sequence: `ExecTester` starts on AAPL (`price_increment=0.0001`) with `tob_offset_ticks=500_000` and receives a quote with `bp=174.555, ap=174.60` (or Alpaca rejects the submitted order).
   - Verification: Ran `ExecTester` on a `LiveNode` offline with `FakeStream` emitting `quote(bp=174.555, ap=174.60)`. `ExecTester-000` submitted a limit buy at `124.5550`, `AlpacaExecutionClient` emitted `OrderDenied`, `cache.orders(instrument_id=AAPL, strategy_id=TESTER)` held 1 order in `OrderStatus.DENIED` (`['OrderInitialized', 'OrderDenied']`), no replacement order was submitted on a subsequent quote, and the `len(orders) == count and all(o.status == OrderStatus.ACCEPTED ...)` loop condition stayed `False` until the deadline.

---

## 6. Two cancel-all continuation tests in `test_execution_client.py` depend on unspecified cache order and pass even when `_cancel_all_orders` aborts on the first refusal or failure

1. **File and location:** `packages/canon-alpaca/tests/test_execution_client.py`, `test_cancel_all_rejects_the_cancel_alpaca_refuses_and_cancels_the_rest` (lines 648–665) and `test_cancel_all_goes_on_after_an_unexpected_failure` (lines 667–679).
2. **What goes wrong:** `execution.py:62` explicitly specifies that cancel-all *"sends one cancel per order, one at a time, in no specified order"*, and `_cancel_all_orders` iterates over `self.cache.orders_open(...)` / `self.cache.orders_inflight(...)` without ordering. Both tests exist to verify that `_cancel_all_orders` **continues canceling the remaining orders** after one order's cancel is refused (`422`) or raises an unexpected `ValueError`. However, instead of failing/refusing the **first cancel call attempted** (e.g., `len(venue.trading.cancels) == 1`), both tests attach the failure to a specific order's price (`if order["limit_price"] == "140.0"`), and `test_cancel_all_goes_on_after_an_unexpected_failure`'s `run_until` condition only waits for `"OrderCanceled" in strategy.final().values()`.
   Whenever `orders_open` yields the `141.00` order (`second`) before the `140.00` order (`first`)—which the documented contract allows—`second` is canceled *before* `first` is attempted, so `_cancel_all_orders` never has to continue past a refusal or exception for every assertion in both tests to pass.
3. **Input, state, or sequence of events, and verification:**
   - Sequence: `ClientCache.orders_open` yields `[second, first]` (`141.00`, then `140.00`), and `_cancel_all_orders` has a bug that `return`s immediately upon the first `OrderCancelRejected` or caught exception instead of continuing the loop.
   - Verification: Replaced `AlpacaExecutionClient._cancel_all_orders` in memory with a broken implementation that reverses `orders_open` and `return`s immediately on the first refusal or exception, then ran `pytest -k "test_cancel_all_rejects_the_cancel_alpaca_refuses_and_cancels_the_rest or test_cancel_all_goes_on_after_an_unexpected_failure"`: **both tests passed (`2 passed`)** even though `_cancel_all_orders` aborted on error.

---

## 7. False statement in `test_paper_execution.py` module docstring: claims two orders are canceled by `CancelAllOrders`

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, module docstring (line 2).
2. **What goes wrong:** Line 2 states:
   > `accepted and then canceled, one by CancelOrder and two by one CancelAllOrders.`

   In fact, `test_paper_cancel_all_on_stop_tc_e41` places and cancels **one** order by `CancelAllOrders`, not two: `enable_limit_buys=True`, `enable_limit_sells=False`, `count=1`, and `assert len(events) == 1` (`lines 217–226`), because the limit sell was omitted (`lines 12, 208–210`).
3. **Input, state, or sequence of events, and verification:**
   - Verified by reading `packages/canon-alpaca/tests/test_paper_execution.py` lines 1–2, 11–13, and 204–226.

---

## 8. False statements in `test_paper_execution.py` module docstring and `packages/canon-alpaca/README.md` about pre-checks and `finally` cleanup scope

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, module docstring (lines 13–15), and `packages/canon-alpaca/README.md` (line 263).
2. **What goes wrong:**
   - `test_paper_execution.py` lines 13–15 state (of the second test, TC-E41): *"so the test places nothing, and fails, if the paper account already holds an open AAPL order; it needs the regular session for quotes, and in a `finally` cancels any order of its own left open."*
   - `packages/canon-alpaca/README.md` line 263 states: *"the TC-E41 test places nothing and fails if the paper account already holds an open AAPL order"*.
   - Three parts of these statements are false against `test_paper_execution.py`:
     1. **Both** tests in `test_paper_execution.py` (`test_paper_limit_order_is_accepted_and_canceled` at line 181 and `test_paper_cancel_all_on_stop_tc_e41` at line 211), not just the TC-E41 test, wrap execution in `with aapl_left_as_found():` and refuse to place anything if pre-existing AAPL state is found.
     2. `aapl_left_as_found()` (`lines 156–167`) refuses to start and fails not only when the paper account holds an open AAPL order, but also when it holds an **AAPL position** (`assert not holds_aapl(trading)` at lines 165–167).
     3. In `finally` (`lines 172–177`), `aapl_left_as_found()` does **not** cancel only *"any order of its own left open"*: `open_aapl_orders(trading)` fetches and cancels **every** open AAPL order on the paper account regardless of `client_order_id` or ownership, and closes any AAPL position.
3. **Input, state, or sequence of events, and verification:**
   - Verified by reading `aapl_left_as_found` (`test_paper_execution.py:150–178`), both test functions (`test_paper_execution.py:180–231`), the module docstring (`test_paper_execution.py:10–15`), and `packages/canon-alpaca/README.md:263`.

---

## 9. False parameter name in `run_exec_tester` docstring (`orders` instead of `count`)

1. **File and location:** `packages/canon-alpaca/tests/test_paper_execution.py`, `run_exec_tester` docstring (lines 234–236).
2. **What goes wrong:** The docstring states:
   > `"""Run upstream's ExecTester on a node with the spec's setup until it has orders orders accepted, then stop it (its stop cancels them), and return each of its orders' event names."""`

   `run_exec_tester(config: ExecTesterConfig, count: int)` has no parameter named `orders`; the parameter is named `count` (`orders` is an internal local variable inside `main()` at line 266).
3. **Input, state, or sequence of events, and verification:**
   - Verified by reading `run_exec_tester`'s signature and docstring at `packages/canon-alpaca/tests/test_paper_execution.py:234–236`.

---

## 10. False statement in `execution.py` module docstring: says the `CancelAllOrders` command is read from the cache

1. **File and location:** `packages/canon-alpaca/src/canon_alpaca/execution.py`, module docstring (lines 62–63).
2. **What goes wrong:** Lines 62–63 state:
   > `* Cancel-all sends one cancel per order, one at a time, in no specified order, skipping an order already pending cancel; an order placed after the command was read from the cache is not canceled by it.`

   The `CancelAllOrders` **command** is not read from the cache—it is passed directly to `_cancel_all_orders(self, command: live.CancelAllOrders)`, which reads the **open and in-flight orders** from `self.cache` (`orders_open` and `orders_inflight` at `execution.py:728–730`). An order placed after those orders are read from the cache is not canceled by the command.
3. **Input, state, or sequence of events, and verification:**
   - Verified by reading `packages/canon-alpaca/src/canon_alpaca/execution.py:62–63` and `715–740` (compare `docs/adr/0036-alpaca-adapter-order-management.md:287`, *"placed after the cache was read"*).
