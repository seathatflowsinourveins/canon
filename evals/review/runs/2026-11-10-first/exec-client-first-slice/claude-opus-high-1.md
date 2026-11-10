# Review of 12f19a0 (paper-only execution client), `packages/canon-alpaca/`

Reviewer: Claude Opus 5.5, high effort. HEAD = `12f19a0b5eac6da29a15bf8512a99d0ba0e4ee35`.

How I checked: I read the whole diff. I read the pinned alpaca-py 0.45.0 source in `.venv`
(`alpaca/common/rest.py`, `alpaca/trading/stream.py`, `alpaca/trading/models.py`) and checked NautilusTrader
2.0.0rc8 defaults by introspection. `mise exec -- uv run --locked pytest packages/canon-alpaca` gives
154 passed, 2 skipped (the paper tests). I ran each probe below as a scratch pytest module outside the
checkout. It reuses the package's `conftest.py` stand-ins and `test_execution_client.py` helpers through
`PYTHONPATH=packages/canon-alpaca/tests pytest -p conftest --import-mode=importlib <probe>`, and runs a real
`LiveNode`. Nothing in the checkout was edited, and `git status` is clean.

---

## 1. An instrument that isn't loaded: a position stops start-up, and an open order is dropped without notice

**Where:** `execution.py`, `_position_status_reports` (l. 1010–1030) and `_order_status_reports` /
`order_status_report` (l. 395–448, 967–1008). Also the README's "Reports" limit and the header's "Start-up
fails, by design, on …" list.

**What goes wrong:** Both hooks report every US-equity order and position in the Alpaca account, whatever
the provider loaded. NautilusTrader treats the two differently, and neither outcome is documented:

* **Position** in an instrument that isn't loaded: start-up fails with `RuntimeError: Unresolved positions
  during startup reconciliation for ALPACA: … instrument=MSFT.ALPACA … instrument missing from cache`.
  A paper account holding anything other than the loaded symbols cannot start a node. The README's own
  example loads only `AAPL.ALPACA`, and so does `test_paper_execution.py`. The documented failures list only
  a fractional position.
* **Open order** in an instrument that isn't loaded: NautilusTrader logs `1 orders skipped (instrument not in
  cache)` and start-up carries on. A working Alpaca order stays unknown to the node. This contradicts
  `order_status_report`'s docstring ("An open order this slice does not map raises, so reconciliation fails
  rather than leaving a working order unknown to the node") and the README's statement that an unmapped open
  order fails start-up.

**Shown by:** probe with `venue.trading.positions = [POSITION_AAPL with symbol MSFT]` → `run(node)` raised
the RuntimeError above. A second probe set `venue.trading.orders = [alpaca_order(symbol="MSFT")]` (status
`accepted`). `_order_status_reports` returned `['MSFT.ALPACA']`. The log showed `1 orders skipped
(instrument not in cache)` and `Reconciliation complete … open=0`, and `node.cache.orders_open()` was `[]`.
The same probe with `AAPL` gives `open=1`.

## 2. With the engine's default config, start-up reconciliation doesn't fetch "every open order however old", and the test accepts that

**Where:** `execution.py`, `_order_status_reports` (l. 983–989). README "Reports" ("the orders submitted in
the engine's window and every open order however old"). Header l. 65. Test
`test_execution_client.py::test_startup_reconciliation_reports_orders_and_positions` (l. 532–534).

**What goes wrong:** The second `GetOrdersRequest(status=OPEN)` is added only when `command.start` or
`command.end` is set. NautilusTrader's default `LiveExecutionEngineConfig.reconciliation_lookback_mins` is
`None`, so at start-up the engine sends no window. The client then makes a single
`GetOrdersRequest(status=ALL, limit=500)` and nothing else. On an account with more than 500 orders, an
open order older than the newest 500 is never reported, and the node doesn't know about it. All that
appears is the "possibly truncated" warning. That the newest come first is alpaca-py's documented default
direction; it is UNVERIFIED against Alpaca this session. The fix is to send the `OPEN` request whenever
`not command.open_only`.

The test asserts `statuses in ([ALL], [ALL, OPEN])` under the comment "Every order in the engine's window,
and every open order however old". So it passes whether or not the open-orders request is sent: it passes
for the wrong reason. The stand-in `get_orders` also ignores `limit`, so no offline test could catch the
truncation.

**Shown by:** a probe ran a default node and recorded the queries: `QueryOrderStatus.ALL None None 500`,
one query only. With `LiveExecutionEngineConfig(reconciliation_lookback_mins=60)` the same probe sent
`[ALL after=…, OPEN]` and reported the 2023 open order. So the windowed branch works, and the default
branch is the gap.

## 3. Trade updates that are missed don't get resolved while the node runs; the docs say reconciliation resolves them

**Where:** header l. 55–58 and 62–63, the README "Stream" and "Fills" limits, and the log text in `_apply`
(l. 833) and `_fill` (l. 879).

**What goes wrong:** The docs say updates sent before the stream is listening "are missed and reconciliation
resolves those orders", that a fill without an `execution_id` can be inferred by reconciliation, and that
updates for orders the node does not hold are ones "reconciliation reports". With NautilusTrader's defaults
(`open_check_interval_secs=None`, `position_check_interval_secs=None`, checked by introspection), nothing
looks at an `ACCEPTED` order again after start-up. The in-flight check covers submitted and pending
orders; which statuses exactly is UNVERIFIED and from memory. So an order the REST answer accepted, whose
`fill`, `canceled` or `expired` update is lost, stays `ACCEPTED` in the node until the next restart.
Updates get lost in the window before the stream is listening, during alpaca-py's silent reconnects (see 4),
and when a fill has no `execution_id`. A strategy that submits in `on_start`, as `test_paper_execution.py`
does, can do so before the trade stream has finished connecting, authenticating and sending `listen`. The
stream thread is only started in `_connect`, and `_connect` doesn't wait for it.

**Shown by:** probe with `script="silent"`, where the REST answer is `accepted` and no updates are sent.
After `OrderAccepted` it marked the stand-in's order `filled` and waited 13.5 s, past the default
`reconciliation_startup_delay_secs=10` and the in-flight interval and threshold. The result was events
`['OrderSubmitted', 'OrderAccepted']`, cached status `ACCEPTED`, and zero `get_order_by_id` or
`get_order_by_client_id` calls.

## 4. "No restart" and "the client logs an error if the stream ends" are false: alpaca-py 0.45.0 reconnects by itself, so a dropped connection goes unseen

**Where:** header l. 62–64 ("If the stream ends by itself the client logs an error and does not restart
it"), README "Stream" ("there is no restart"), `_on_stream_exit` (l. 896–899).

**What goes wrong:** In the pinned `alpaca/trading/stream.py`, `TradingStream._drive_run` loops until
`stop()`. On `websockets.WebSocketException`, on `OSError` or a timeout before the session is up, or on any
other exception, it closes, waits with backoff and reconnects. `run()` therefore returns only after
`stop()`, so `_on_stream_exit`'s "ended while connected" error never fires when the connection drops. The
drop shows up only as alpaca-py's own `log.warning`. Every update sent during the gap is lost, and per 3
nothing resolves those orders. The same loop also swallows a `TradeUpdate(**data)` validation error with
`log.exception` (the `except Exception` branch while `_running`), so a trade update alpaca-py cannot parse
is dropped without reaching the adapter. The stand-in `FakeTradeStream` models none of this: it queues
messages until `run()` and never reconnects. The README's claim that the stand-ins "keep what … `TradingStream`
do[es]" covers only the handler refusal.

**Shown by:** reading the pinned source (`_drive_run`, `_consume`, `_dispatch`, `_cast`, `stop`). Not
exercised against a socket.

## 5. A submit Alpaca answers with status `rejected` can be rejected twice (low; no visible effect seen)

**Where:** `execution.py`, `_submit_order` l. 633–639.

**What goes wrong:** `_refused` and `_apply` both check `_finished`, but this branch doesn't. When the
stream's `rejected` update is applied before the REST answer returns, `_reject` and so
`generate_order_rejected` run a second time for an order that is already `REJECTED`. NautilusTrader
2.0.0rc8 seems to drop the second event without any message (the strategy saw one `OrderRejected`), so the
harm is only that the code relies on the engine to ignore an invalid transition.

**Shown by:** probe that wrapped `AlpacaExecutionClient._reject` to count calls, with `script="reject"`,
`submit_delay=0.3`, and the REST answer changed to status `rejected`. `_reject` was called twice
(`'… (trade update, status rejected)'`, then `'… on submission'`), and the events were `['OrderSubmitted',
'OrderAccepted', 'OrderRejected']`.

## 6. `test_api_error_status_comes_from_the_http_error` tests the stand-in against itself (low)

**Where:** `tests/test_upstream_contract.py`, the last new test.

**What goes wrong:** Its docstring says the stand-in builds `APIError` "the way alpaca-py's REST client
does". But it only builds an error with conftest's `api_error` and reads it back. It would still pass if
alpaca-py's `_one_request` stopped passing the `HTTPError`, which is what makes `status_code` non-None, and
in that case every status-based branch in `_submit_order` and `_cancel_order` would break. It never touches
alpaca-py's construction path, so the contract it claims is unchecked. The current source does build it
that way: `raise APIError(error, http_error)` in `_one_request`.

**Shown by:** reading the test and `alpaca/common/rest.py::_one_request`.

## 7. README "Tests": "This package has all three for each client" is false for the data client (low, doc)

**Where:** `packages/canon-alpaca/README.md`, the Tests section.

**What goes wrong:** The three are factory registration, a fill and shutdown. The data client has no fill
test. The changed docstring of `test_data_client.py` now points to the execution client's fill test. The
old wording ("the two that apply to a data client") was accurate.

---

## Not defects, checked

* The config refuses `paper`≠True, any `url_override`, and unknown keywords. The client takes only
  `AlpacaExecutionClientConfig`. The clients are built with `paper=True` (tests and source).
* `TradingStream.stop()` doesn't block in 0.45.0.
* alpaca-py's retries: 3 attempts on 429/504, and `TradingClient` exposes no retry setting, so "cannot be told
  not to" holds at the public API. When retries run out, `_one_request` raises `APIError(429/504)`, which the
  client handles as an unknown outcome.
* Fill prices finer than the instrument's 4 decimals are accepted by the engine (`105.8988475`, and up to 11
  decimals in a probe).
* The windowed reconciliation path works through a real node when a lookback is configured.

## Not verified

Alpaca's own behaviour: order ordering and limits, `qty`/`price` semantics on fill events, the 128-character
`client_order_id` limit, the TIF table, and what Alpaca answers a duplicate `client_order_id`. These need the
network or a paper key, which this review was not allowed to use. Exactly which order statuses
NautilusTrader's in-flight check covers is UNVERIFIED.
