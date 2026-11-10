# Defect Review of `12f19a0b5eac6da29a15bf8512a99d0ba0e4ee35` (`packages/canon-alpaca/`)

Reviewed `git diff 12f19a0b5eac6da29a15bf8512a99d0ba0e4ee35^ 12f19a0b5eac6da29a15bf8512a99d0ba0e4ee35` in `packages/canon-alpaca/` against the pinned upstream packages installed from `uv.lock` (`nautilus-trader==2.0.0rc8`, `alpaca-py==0.45.0`). Defects are listed most severe first.

---

## 1. Unimplemented `_query_order` causes NautilusTrader's in-flight reconciliation to synthesize false `OrderRejected` and `OrderCanceled` events without querying Alpaca (and makes the adapter's reconciliation claims false)

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient` (missing `_query_order`; see module docstring lines 62–63, `_submit_order` lines 624–630, `_cancel_order` lines 704–712, `_log_unknown_outcome` lines 792–796, and `_generate_order_status_report` lines 909–912, 945–965).
   * `packages/canon-alpaca/README.md` — lines 197, 199–201, and 225–226.

2. **What goes wrong:**
   * Both the code and documentation state that orders with an unknown submit or cancel outcome (network failure, HTTP 5xx, etc.) or orders whose trade updates were sent before `TradingStream` finished subscribing are left in `SUBMITTED` or `PENDING_CANCEL` so that NautilusTrader's reconciliation checks resolve them (`execution.py` lines 62–63 and 794–795; `README.md` lines 199–201 and 225–226).
   * In `nautilus-trader==2.0.0rc8`, `LiveExecutionEngineConfig` enables continuous in-flight checks by default (`inflight_check_interval_ms=2000`, `inflight_check_threshold_ms=5000`, `inflight_check_retries=5`, `reconciliation_startup_delay_secs=10.0`, `submission_recovery_policy=SubmissionRecoveryPolicy.RESOLVE_LOCALLY`, while `open_check_interval_secs=None`). When an order stays in `SUBMITTED` or `PENDING_CANCEL`, `nautilus_live::execution::manager` does **not** call `_generate_order_status_report`; it dispatches `QueryOrder` commands to `ExecutionClient._query_order`.
   * Because `AlpacaExecutionClient` does not override `_query_order` (`README.md` line 197 notes `queries` are not implemented), `ExecutionClient._query_order` (`nautilus_trader/live/clients.py:670-671`) raises `NotImplementedError` on every attempt (`[ERROR] ... Client ALPACA operation _query_order failed`), making **zero** REST calls to Alpaca. Once `inflight_check_retries` is exhausted, `SubmissionRecoveryPolicy.RESOLVE_LOCALLY` resolves the in-flight order locally without consulting Alpaca:
     * A `SUBMITTED` order that **actually accepted or filled on Alpaca** (for example, `ConnectionError` after Alpaca processed `POST /v2/orders`, and the stream missed the early update) is transitioned locally to `OrderRejected(..., reason='INFLIGHT_TIMEOUT')` (`status=REJECTED`, `filled_qty=0`), leaving an untracked live position or working order on Alpaca.
     * A `PENDING_CANCEL` order whose cancel **never reached Alpaca** (for example, `ConnectionError` or HTTP 500 in `_cancel_order`, where line 711 logs `"the order is left as it is"`) is transitioned locally to `OrderCanceled` (`status=CANCELED`), while the order remains open and working on Alpaca.
   * Consequently, the statements in `execution.py` (lines 62–63, 794–795) and `README.md` (lines 199–201, 225–226) that reconciliation's checks resolve these orders are also false under a default `LiveNode` configuration.

3. **Input / sequence of events and how it was shown:**
   * Ran an offline `LiveNode` (using `FakeOrdersClient` and `FakeTradeStream`, with `LiveExecutionEngineConfig(reconciliation_startup_delay_secs=0.05, inflight_check_interval_ms=100, inflight_check_threshold_ms=100)`) in two scenarios:
     1. **Unknown submit outcome after venue fill:** `submit_order` recorded the order on `FakeOrdersClient` as `status="filled", filled_qty="2"` (`script="silent"`) and raised `ConnectionError`. `AlpacaExecutionClient` logged `"the order stays SUBMITTED until a trade update or reconciliation resolves it"`. The engine logged four `_query_order` `NotImplementedError` tracebacks, made zero `get_order_by_client_id` / `get_order_by_id` calls (`order_queries` stayed at `1` from startup), and emitted `OrderRejected(..., reason='INFLIGHT_TIMEOUT')`, leaving the cached order `REJECTED` with `filled_qty == 0` while the venue order was `filled`.
     2. **Unknown cancel outcome while order stays working:** Strategy submitted a limit order (`script="accept"`), received `OrderAccepted`, and called `cancel_order` while `cancel_order_by_id` raised `ConnectionError` (leaving the venue stand-in order in `status="new"`). After four `_query_order` `NotImplementedError` failures, the node emitted `OrderCanceled`, leaving local `order.status == OrderStatus.CANCELED` while the venue order remained `"new"`.

---

## 2. `_accept` adds `client_order_id` to `self._accepted` even when skipping `generate_order_accepted`, permanently suppressing `OrderAccepted` if an in-flight cancel is rejected

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._accept` (lines 800–809).

2. **What goes wrong:**
   * `_accept` adds `client_order_id` to `self._accepted` unconditionally on line 805 *before* checking on lines 808–809 whether `current.status in (OrderStatus.INITIALIZED, OrderStatus.SUBMITTED, OrderStatus.RELEASED)`.
   * When a strategy cancels an order while `submit_order` is still in flight (`OrderSubmitted` -> `OrderPendingCancel`, so `current.status == OrderStatus.PENDING_CANCEL`), `_submit_order` (line 640) calls `self._accept(...)`. `_accept` records `client_order_id` in `self._accepted` and does **not** call `generate_order_accepted`.
   * If Alpaca then refuses the cancel (`OrderCancelRejected`, via HTTP 422 in `_cancel_refused` or `"order_cancel_rejected"` on the stream), NautilusTrader reverts the order from `PENDING_CANCEL` back to its prior status `OrderStatus.SUBMITTED` with `venue_order_id = None`.
   * Because `client_order_id` is already in `self._accepted`, every later call to `_accept` (including when `"new"` or `"accepted"` arrives on the trade stream) returns immediately at lines 803–804. `OrderAccepted` is never emitted for the order (violating `_accept`'s docstring `"Generate OrderAccepted once per order"`), and while the order rests open on Alpaca it remains stuck in `OrderStatus.SUBMITTED` with `venue_order_id = None` (requiring another `get_order_by_client_id` lookup if canceled again).

3. **Input / sequence of events and how it was shown:**
   * Ran an offline `LiveNode` with `trader(order_type="limit", cancel_on="OrderSubmitted")`, `venue.trading.script = "silent"`, and `venue.trading.cancel_error = api_error(422, 42210000, "order is not cancelable")`.
   * After `_submit_order` returned and `_cancel_refused` emitted `OrderCancelRejected`, `strategy.names()` was `['OrderSubmitted', 'OrderPendingCancel', 'OrderCancelRejected']`, `cache.order(client_order_id).status` was `OrderStatus.SUBMITTED`, `venue_order_id` was `None`, and `client._accepted` contained `client_order_id`.
   * Sending a `"new"` trade update on `venue.stream` produced no `OrderAccepted`; `order.status` stayed `OrderStatus.SUBMITTED` and `order.venue_order_id` stayed `None`.

---

## 3. `_refused` accepts orders that Alpaca already rejected (or closed) and does not re-check `_accepted` / `_finished` after `await`

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._refused` (lines 651–680, specifically lines 652 and 669–680).

2. **What goes wrong:**
   * First, unlike `_submit_order` (lines 633–639, which checks `if placed.status == AlpacaOrderStatus.REJECTED: self._reject(...)`), `_refused` (lines 669–675) checks only `if isinstance(held, AlpacaOrder):` and unconditionally calls `self._accept(order, VenueOrderId(str(held.id)), dt_to_unix_nanos(held.submitted_at))`. When `alpaca-py` retries a `submit_order` after HTTP 429/504, the retry fails with 403/422, and `get_order_by_client_id` returns the earlier order with `held.status == AlpacaOrderStatus.REJECTED` (while the stream missed the rejection before listening), `_refused` emits `OrderAccepted` instead of `OrderRejected`, leaving a rejected order open in `OrderStatus.ACCEPTED` (and similarly leaves an already `CANCELED` or `EXPIRED` order open in `ACCEPTED` if the stream missed the event).
   * Second, `_refused` checks `if client_order_id in self._accepted or client_order_id in self._finished: return` only *before* `await self._call(self._venue().get_order_by_client_id, ...)` (line 652), not after `await`. If `get_order_by_client_id` returns 404 while a trade update (`"new"`, `"accepted"`, `"partial_fill"`, etc.) arrives on the stream during the `await`, line 676 calls `self._reject(order, ...)`, transitioning an already `ACCEPTED` order in NautilusTrader to `REJECTED` and adding it to `self._finished`.

3. **Input / sequence of events and how it was shown:**
   * Ran an offline `LiveNode` where `submit_order` stored the order in `FakeOrdersClient._placed` with `status="rejected"` (`script="silent"`) and raised `api_error(422, 42210000, "duplicate client_order_id")`.
   * `_refused` retrieved `held` (`held.status == AlpacaOrderStatus.REJECTED`), logged `"Alpaca refused a submit ... (422) but holds the order: an earlier attempt reached it"`, and emitted `OrderAccepted`, leaving `cache.order(client_order_id).status == OrderStatus.ACCEPTED` and `is_open == True`.

---

## 4. `_apply` marks an order finished in `self._finished` on `"fill"` even when `_fill` refused to apply it

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._apply` (lines 859–866) and `_fill` (lines 868–882); see also lines 518–521.

2. **What goes wrong:**
   * When a `"fill"` trade update lacks `execution_id`, lacks `qty` or `price`, or has a fractional `qty`, `_fill` catches `ValueError`, logs `"Alpaca fill ... not applied: ..."`, and returns without emitting `OrderFilled`. The order in NautilusTrader remains open (`ACCEPTED` or `PARTIALLY_FILLED`).
   * Back in `_apply`, lines 865–866 still run unconditionally:
     ```python
     if event in TERMINAL_EVENTS:
         self._finished.add(client_order_id)
     ```
     Because `"fill"` is in `TERMINAL_EVENTS`, `client_order_id` is added to `self._finished` even though no terminal event was emitted (contradicting lines 518–519: `"orders it has seen reach a final state"`). Every subsequent stream event for that order (such as a restated/corrected `"fill"` with `execution_id`) is silently dropped at line 836 (`if client_order_id in self._finished: return`).

3. **Input / sequence of events and how it was shown:**
   * Ran an offline `LiveNode` with `venue.trading.script = "accept"` and `venue.trading.execution_ids = False`, waited for `OrderAccepted`, and sent a `"fill"` update (`execution_id` absent).
   * Observed `cache.order(client_order_id).status == OrderStatus.ACCEPTED` while `client_order_id in client._finished` was `True`. Setting `venue.trading.execution_ids = True` and sending a valid `"fill"` update produced no `OrderFilled` event, leaving the order stuck in `OrderStatus.ACCEPTED`.

---

## 5. `_order_status_reports` drops open orders submitted before `command.start` when `command.open_only=True`

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._order_status_reports` (lines 967–990; also contradicts module docstring lines 65–66 and `packages/canon-alpaca/README.md` lines 213–214).

2. **What goes wrong:**
   * Alpaca's `GetOrdersRequest.after` and `until` filter on **submission time** (`submitted_at`). When `command.open_only` is `False` and `command.start` or `command.end` is set, `_order_status_reports` issues a second unwindowed `GetOrdersRequest(status=QueryOrderStatus.OPEN, ...)` so working orders submitted before `command.start` are still reported.
   * When `command.open_only` is `True` and `command.start` (or `command.end`) is set, line 984 (`if not command.open_only and ...`) skips that unwindowed query while lines 976–982 pass `after=unix_nanos_to_dt(command.start)` and `until=unix_nanos_to_dt(command.end)` on `QueryOrderStatus.OPEN`.
   * In `nautilus-trader==2.0.0rc8`, `LiveExecutionEngineConfig`'s periodic open-order check defaults to `open_check_open_only=True` and `open_check_lookback_mins=60`, which sends `GenerateOrderStatusReports(open_only=True, start=<now - 60 mins>, end=None)`. `_order_status_reports` therefore fails to report any open GTC order submitted more than 60 minutes earlier (and contradicts `execution.py` lines 65–66 and `README.md` lines 213–214, which state without qualification that reports include `"every open order however old"`).

3. **Input / sequence of events and how it was shown:**
   * Ran `LiveNode` with `LiveExecutionEngineConfig(open_check_interval_secs=0.2)` and confirmed `FakeOrdersClient.order_queries` received `GetOrdersRequest(status=QueryOrderStatus.OPEN, after=<non-None>, until=None)`.
   * Ran `await client._generate_order_status_reports(...)` against `ORDER_AAPL` (an open GTC limit order with `submitted_at="2023-12-12T22:31:24.577215743Z"`) and `start = dt_to_unix_nanos(datetime(2024, 1, 1, tzinfo=UTC))`: with `open_only=False` it returned `[OrderStatusReport(venue_order_id='7b08df51-c1ac-453c-99f9-323a5f075f0d')]`, whereas with `open_only=True` it returned `[]`.

---

## 6. `order_status_report` treats unmapped closed/done Alpaca statuses (such as `done_for_day` and `calculated`) as open and aborts startup instead of skipping them

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `order_status_report` (lines 395–409, 445–448) and module docstring (lines 67–69).
   * `packages/canon-alpaca/README.md` — lines 216–219.

2. **What goes wrong:**
   * `execution.py` lines 67–69 state: *"An order Alpaca reports with a type, side, class, status, quantity or time in force this slice does not map fails the report when it is open and is skipped with a warning when it is closed."* (`README.md` lines 216–219 state the same.)
   * In `order_status_report` (line 402):
     ```python
     is_open = status not in CLOSED_STATUSES and status not in (AlpacaOrderStatus.REPLACED,)
     ```
     `CLOSED_STATUSES` contains only `{FILLED, CANCELED, EXPIRED, REJECTED}` — all four of which *are* mapped. Every unmapped `AlpacaOrderStatus` other than `REPLACED` — including non-open / terminal FIX statuses in `alpaca.trading.enums.OrderStatus` such as `DONE_FOR_DAY` (`"done_for_day"`) and `CALCULATED` (`"calculated"`) — sets `is_open = True`, causing `order_status_report` to raise `ValueError("Alpaca order ... is open with status ...")` and fail reconciliation instead of returning `None` with a warning.

3. **Input / sequence of events and how it was shown:**
   * Called `order_status_report(AlpacaOrder.model_validate(alpaca_order(status="done_for_day")), 1)` (and likewise for `"calculated"`, `"stopped"`, `"suspended"`, `"held"`). Instead of returning `None` for closed/done statuses, every status outside `{filled, canceled, expired, rejected, replaced}` raised `ValueError: Alpaca order 7b08df51-c1ac-453c-99f9-323a5f075f0d (AAPL) is open with status done_for_day`.

---

## 7. `order_status_report` does not catch `decimal.InvalidOperation` when `limit_price` is `None` (or unparseable), crashing reconciliation on a closed limit order

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `exact_report_price` (lines 363–368) and `order_status_report` (lines 440, 445–448).

2. **What goes wrong:**
   * In `alpaca.trading.models.Order` (`alpaca/trading/models.py:293`), `limit_price` is typed `Optional[Union[str, float]] = None`.
   * When `order_type == OrderType.LIMIT`, line 440 calls `exact_report_price(order.limit_price)`, which executes `Decimal(str(value))` (`Decimal("None")` when `limit_price is None`). `Decimal("None")` raises `decimal.InvalidOperation`, which is a subclass of `ArithmeticError`, **not** `ValueError`.
   * Because line 445 catches only `except ValueError as e:`, a closed limit order with `limit_price=None` is not skipped (`return None`) — it raises an unhandled `decimal.InvalidOperation` and aborts the entire order status report batch (and for an open order, escapes without the `"Alpaca order ... is open with ..."` wrapper).

3. **Input / sequence of events and how it was shown:**
   * Ran `order_status_report(AlpacaOrder.model_validate(alpaca_order(status="rejected", type="limit", order_type="limit", limit_price=None)), 1)` offline; it raised `decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>]` instead of returning `None`.

---

## 8. `_position_status_reports` converts every US equity position before filtering by `command.instrument_id`, failing single-instrument reports when another equity position is fractional

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._position_status_reports` (lines 1010–1030, specifically lines 1027–1029).

2. **What goes wrong:**
   * `_position_status_reports` calls `position_status_report(position, ts_init)` on **every** `US_EQUITY` position returned by `get_all_positions()` (line 1027) *before* checking `if command.instrument_id is None or report.instrument_id == command.instrument_id:` (line 1028).
   * If the account holds a fractional US equity position in an unrelated symbol (e.g. `TSLA` with `qty="0.5"`) and a `GeneratePositionStatusReports` command asks only for `AAPL.ALPACA` (`command.instrument_id = InstrumentId.from_str("AAPL.ALPACA")`), `position_status_report` raises `ValueError("TSLA position quantity 0.5 is fractional; Equity is whole-share only")` and fails the `AAPL.ALPACA` report.

3. **Input / sequence of events and how it was shown:**
   * Ran `await client._position_status_reports(SimpleNamespace(instrument_id=InstrumentId.from_str("AAPL.ALPACA")))` with `venue.trading.positions = [dict(POSITION_AAPL, symbol="TSLA", qty="0.5"), POSITION_AAPL]`. It raised `ValueError: TSLA position quantity 0.5 is fractional; Equity is whole-share only` instead of returning the `AAPL.ALPACA` report.

---

## 9. `test_startup_reconciliation_reports_orders_and_positions` passes even if filled-order reporting or position reporting is completely broken

1. **File and line / function:**
   * `packages/canon-alpaca/tests/test_execution_client.py` — `test_startup_reconciliation_reports_orders_and_positions` (lines 516–544; see also `packages/canon-alpaca/README.md` line 249).

2. **What goes wrong:**
   * This is the only test in the suite that exercises position reconciliation (`venue.trading.positions`) or filled-order startup reconciliation (`filled_market`), and `README.md` line 249 claims it covers `"start-up reconciliation of an open order, a filled order and a position"`.
   * However, `filled_market` (`client_order_id="filled-before-start"`, `filled_qty="2"`, `filled_avg_price="174.78"`) and `POSITION_AAPL` (`qty="2"`, `avg_entry_price="174.78"`) describe the exact same `LONG 2 AAPL @ 174.78` position, and the test only asserts `node.cache.orders_open()` (checking `open_limit`) and `node.cache.positions_open()` (checking `quantity == 2` and `avg_px_open == 174.78`). It never checks `node.cache.order(ClientOrderId("filled-before-start"))` / `orders_closed()`, nor that `get_all_positions()` was called or returned anything.
   * As a result:
     1. If closed/filled orders are never reported at all, NautilusTrader creates the `2 @ 174.78` position from `POSITION_AAPL` alone and **every assertion in the test passes**.
     2. Conversely, if `_generate_position_status_reports` returns `[]` without reporting any positions at all, NautilusTrader infers a `2 @ 174.78` fill from `filled_market` and opens the identical `2 @ 174.78` position, and **every assertion in the test (and the entire offline test suite) still passes**.

3. **Input / sequence of events and how it was shown:**
   * Ran the exact node setup from `test_startup_reconciliation_reports_orders_and_positions` twice:
     1. With `venue.trading.orders = [open_limit]` (no `filled_market`) and `venue.trading.positions = [POSITION_AAPL]`: `orders_open()` had `open_limit` and `positions_open()` had `LONG 2 AAPL.ALPACA` at `174.78`.
     2. With `venue.trading.orders = [open_limit, filled_market]` and `venue.trading.positions = []` (no positions): `orders_open()` had `open_limit` and `positions_open()` had `LONG 2 AAPL.ALPACA` at `174.78`.

---

## 10. `test_paper_limit_order_is_accepted_and_canceled` can pass when the cancel never took effect on Alpaca, and leaks an open paper order on failure or timeout

1. **File and line / function:**
   * `packages/canon-alpaca/tests/test_paper_execution.py` — `AcceptThenCancel` and `test_paper_limit_order_is_accepted_and_canceled` (lines 50–117).

2. **What goes wrong:**
   * **Can pass for the wrong reason:** `_connect_venue` (`execution.py` lines 552–558) starts the `TradingStream` thread without waiting for the WebSocket connection, authentication, or `listen` subscription to complete. `AcceptThenCancel.on_start()` immediately submits the limit order via REST, receives `OrderAccepted` from the `POST /v2/orders` response, and immediately calls `cancel_order`. If `cancel_order_by_id` fails with a transient error (left in `PENDING_CANCEL`), or if `TradingStream` misses the `"canceled"` trade update while still connecting, NautilusTrader's default `inflight_check` (~23 seconds into the test's 60-second timeout: `10s` startup delay + `5s` threshold + four `2s` retries hitting unimplemented `_query_order`, see Defect 1) synthesizes a local `OrderCanceled` event. `AcceptThenCancel` then records `["OrderSubmitted", "OrderAccepted", "OrderPendingCancel", "OrderCanceled"]` and **the test passes even if the order was never canceled on Alpaca**.
   * **Leaks a working order on failure:** If `AcceptThenCancel` receives `OrderCancelRejected` (line 74 shuts down the node) or `run_hosted` raises `TimeoutError` (for instance, outside regular market hours if a `DAY` order cancel stays `pending_cancel`), the `finally:` block only calls `node.dispose()` and never cancels the paper order via REST, leaving an open order on the paper account.

3. **Input / sequence of events and how it was shown:**
   * Ran `AcceptThenCancel` offline inside `LiveNode` (`test_paper_execution.py`'s exact configuration with shortened `inflight_check` timers) while `cancel_order_by_id` did not emit `"canceled"` on the stream (and likewise when `cancel_order_by_id` raised `ConnectionError`, leaving the venue stand-in order `"new"`). In both cases, NautilusTrader synthesized `OrderCanceled` after `_query_order` raised `NotImplementedError` four times, and `strategy.events` matched `["OrderSubmitted", "OrderAccepted", "OrderPendingCancel", "OrderCanceled"]`.

---

## 11. `_order_report` logs a false warning message when skipping a `replaced` (or non-`us_equity`) closed order

1. **File and line / function:**
   * `packages/canon-alpaca/src/canon_alpaca/execution.py` — `AlpacaExecutionClient._order_report` (lines 1032–1039).

2. **What goes wrong:**
   * Whenever `order_status_report` returns `None`, `_order_report` logs:
     ```text
     Alpaca order <id> (<symbol>, <status>) is not reported: its type, side, class, quantity or time in force is not in this slice
     ```
   * When `order` is a plain whole-share US equity `GTC` or `DAY` market/limit order with `status == AlpacaOrderStatus.REPLACED` (`execution.py` lines 404–405), its type, side, order class, quantity, and time in force *are* all in this slice — it was skipped solely because its **status** is `replaced` (and similarly, a closed option or crypto order can be skipped solely because its **asset class** is not `us_equity`, lines 409–412). The warning message omits `status` (and `asset class`) and falsely attributes the skip to fields that are valid.

3. **Input / sequence of events and how it was shown:**
   * Inspected `order_status_report` (lines 404–412, 445–448) alongside `_order_report` (lines 1033–1038): `alpaca_order(status="replaced")` has `type="limit"`, `side="buy"`, `order_class="simple"`, `qty="2"`, `time_in_force="gtc"`, and `asset_class="us_equity"`, returns `None` from line 405 (`ValueError("status replaced: modify is not in this slice")`), and triggers the warning naming only `"its type, side, class, quantity or time in force"`.
