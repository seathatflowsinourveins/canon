# Defect Report: Commit `f364348445fb1be2ba6ca09a3d4b6e934d08dc5d` (`packages/canon-alpaca/`)

Review of `git diff f364348445fb1be2ba6ca09a3d4b6e934d08dc5d^ f364348445fb1be2ba6ca09a3d4b6e934d08dc5d -- packages/canon-alpaca/`, ordered most severe first.

---

### 1. `alpaca_bars_window` sends an inverted window (`start > end`) when `start` is `None` with a historical `end` (or when `start > now`), breaking historical bar requests and falsifying its documented invariant

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `alpaca_bars_window` (lines 294–308, specifically lines 301–308) and `AlpacaDataClient._request_bars` (lines 539–547).
   - `packages/canon-alpaca/README.md`: lines 127–129.
2. **What goes wrong:**
   `alpaca_bars_window` clamps `end` with `end = now if end is None else min(end, now)` (line 306), then defaults `start` from `now` (`start = start_of_day(now) if start is None else start`, line 307) without checking `start <= end`.
   - When a strategy calls `self.request_bars(bar_type, start=None, end=past_end)` with `past_end` before midnight New York of the current day (a call NautilusTrader's `Strategy.request_bars` accepts), `alpaca_bars_window` sets `start` to today's New York midnight while leaving `end` at `past_end`, sending `start > end` to Alpaca.
   - Likewise, if `_request_bars` is called directly with `request.start > now` (a direct-hook caller scenario explicitly contemplated by `test_bar_request_ending_in_the_future_is_cut_at_now` at `tests/test_data_client.py:476–478`), `end` is cut to `now` while `start` stays in the future, again sending `start > end`.
   - Consequently, the statements in `data.py:304` (*"supplying it here keeps the window sent from ending before it starts"*) and `README.md:128–129` (*"so the adapter supplies the start itself, and the window sent never ends before it starts"*) are false.
3. **How it shows / verification:**
   - Input/sequence: Configure `Consumer` in a `LiveNode` with `bars_start=None`, `bars_end=datetime(2022, 1, 3, 10, tzinfo=UTC)`, `bar_type=BarType.from_str("AAPL.ALPACA-1-HOUR-LAST-EXTERNAL")` when `now` is in 2026 (or call `alpaca_bars_window(None, datetime(2022, 1, 3, 10, tzinfo=UTC), datetime(2026, 11, 10, 17, tzinfo=UTC), timedelta(hours=1))`).
   - Verified offline with `mise exec -- uv run --locked python`: `alpaca_bars_window` returned `start = 2026-11-09 23:00:00-05:00` (`2026-11-10 04:00:00Z`) and `end = 2022-01-03 09:00:00Z` (`start > end`), and in a real hosted `LiveNode` against `FakeHistoricalClient` the strategy received `failure_reason` containing `APIError: {"code": 42210000, "message": "start is after end (stand-in)"}` and zero bars. Calling `_request_bars` directly with `start = now + 1h, end = now + 3h` failed with the same inverted window.

---

### 2. Wall-clock `ZoneInfo` subtraction in `alpaca_bars_window` produces inverted windows (`start > end`) and wrong bar bounds across US DST changes, is masked by a tautological test assertion, and makes `README.md`'s `WEEK` default-window statement false

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `start_of_day` (lines 289–291) and `alpaca_bars_window` (lines 294–308, specifically lines 307–308).
   - `packages/canon-alpaca/tests/test_data_client.py`: `test_bar_request_with_no_bounds_starts_at_the_beginning_of_the_day` (lines 492–516, specifically line 511).
   - `packages/canon-alpaca/README.md`: lines 135–138.
2. **What goes wrong:**
   `parse_bar` defines `ts_init = ts_event + bar_interval_ns(bar_type)`, a fixed elapsed duration (`24 h` for `1-DAY`, `168 h` for `1-WEEK`), and `StockBarsRequest` converts `start` and `end` to UTC via `.astimezone(timezone.utc)`. However, `start_of_day(now)` returns a `ZoneInfo("America/New_York")`-aware `datetime` (and callers such as `test_weekly_bar_across_a_clock_change` also pass `ZoneInfo("America/New_York")` `start`/`end` bounds). In Python, `dt - interval` on a `ZoneInfo` datetime performs **wall-clock** arithmetic in that time zone and re-evaluates the UTC offset at the resulting local timestamp rather than subtracting elapsed UTC time:
   - **Inverted window (`start > end`) with `start=None, end=None` after spring-forward:** On Monday `2026-03-09` (the day after US clocks spring forward on Sunday `2026-03-08`), `start_of_day(now)` is `2026-03-09 00:00 EDT` (`04:00 UTC`), so `start_of_day(now) - timedelta(days=1)` is `2026-03-08 00:00 EST` (`05:00 UTC` — **23 elapsed hours** back), whereas `now - timedelta(days=1)` (`now` is UTC) subtracts **24 elapsed hours**. Between `00:00` and `01:00 EDT` (`04:00`–`05:00 UTC`) on `2026-03-09`, `alpaca_bars_window(None, None, now, timedelta(days=1))` sends `start > end` (`05:00 UTC > 04:30 UTC`). For `1-WEEK`, `start_of_day(now) - timedelta(weeks=1)` subtracts **167 elapsed hours** while `now - timedelta(weeks=1)` subtracts **168 elapsed hours**, so `alpaca_bars_window(None, None, now, timedelta(weeks=1))` sends `start > end` between `00:00` and `01:00 EDT` on **every day of the week `2026-03-09` through `2026-03-15`**.
   - **Wrong elapsed shift on explicit `ZoneInfo` `start`/`end` inputs:** When a caller passes `ZoneInfo("America/New_York")` bounds spanning a DST transition, `start - interval` / `end - interval` shifts by the wrong elapsed UTC duration. For `start = datetime(2026, 3, 8, 3, 0, tzinfo=NEW_YORK)` (`07:00 UTC`) and `interval = timedelta(hours=1)`, `start - interval` yields `2026-03-08 02:00 EST` = **`07:00 UTC`** — a **0-second** UTC shift instead of 1 hour, omitting the hourly bar that opened at `06:00 UTC` (`ts_init = 07:00 UTC == start`). Conversely, across the autumn fallback on Monday `2026-11-02`, `start_of_day(now) - timedelta(weeks=1)` subtracts **169 elapsed hours** (`2026-10-26 00:00 EDT` = `04:00 UTC` instead of `05:00 UTC`); because `request.start_ns` is `None` when `start=None`, NautilusTrader applies no lower-bound filter to `BarsResponse`, so a Monday `2026-11-02` `1-WEEK` request with `start=None` returns the `2026-10-26` weekly bar whose `ts_init` is `Sunday 2026-11-01 23:00 EST` (before Monday's midnight).
   - **Defective test (`test_data_client.py:492–516`):** `test_bar_request_with_no_bounds_starts_at_the_beginning_of_the_day` reads the live wall clock (`datetime.now(NEW_YORK)`), so it fails if run between `00:00` and `01:00 EDT` on spring-forward Monday (`FakeHistoricalClient` raises `APIError("start is after end")`), and at all other times line 511 (`assert start_sent == midnight - DAY`) computes `midnight - DAY` on a `NEW_YORK` `ZoneInfo` datetime — repeating the exact wall-clock bug under test instead of checking `midnight.astimezone(UTC) - DAY`.
   - **False statement in `README.md` (`lines 135–138`):** `README.md` states: *"With no `start`, the window is today in New York so far: ... for WEEK, last week's bar on a Monday (from 01:00 after the spring change) and none on other days."* On the Sunday of the autumn clock change (`2026-11-01`), the weekly bar that opened `2026-10-26 00:00 EDT` (`04:00 UTC`) has `ts_init = open + 168 h = 2026-11-02 04:00 UTC = Sunday 2026-11-01 23:00 EST`. Thus between `23:00` and `24:00 EST` on that **Sunday** (not Monday), a `WEEK` request with `start=None` includes last week's bar, falsifying *"and none on other days"*.
3. **How it shows / verification:**
   - Verified offline with `mise exec -- uv run --locked python` calling `alpaca_bars_window`:
     - `alpaca_bars_window(None, None, datetime(2026, 3, 9, 4, 30, tzinfo=UTC), timedelta(days=1))` → `start = 2026-03-08 05:00Z`, `end = 2026-03-08 04:30Z` (`start > end`).
     - `alpaca_bars_window(None, None, datetime(2026, 3, 11, 4, 30, tzinfo=UTC), timedelta(weeks=1))` → `start = 2026-03-04 05:00Z`, `end = 2026-03-04 04:30Z` (`start > end`).
     - `alpaca_bars_window(datetime(2026, 3, 8, 3, 0, tzinfo=NEW_YORK), datetime(2026, 3, 8, 4, 0, tzinfo=NEW_YORK), datetime(2026, 3, 9, tzinfo=UTC), timedelta(hours=1))` → `start = 2026-03-08 07:00Z` (0 s shift from `07:00Z`).
     - `alpaca_bars_window(None, None, datetime(2026, 11, 2, 17, 0, tzinfo=UTC), timedelta(weeks=1))` → `start = 2026-10-26 04:00Z`, selecting the `2026-10-26` bar whose `ts_init` is `2026-11-01 23:00-05:00` (before `2026-11-02 00:00-05:00`).
     - `alpaca_bars_window(None, None, datetime(2026, 11, 2, 4, 30, tzinfo=UTC), timedelta(weeks=1))` (Sunday `2026-11-01 23:30 EST`) → `[2026-10-25 04:00Z, 2026-10-26 04:30Z]`, which contains `2026-10-26 04:00Z` on Sunday.

---

### 3. `AlpacaDataClient._disconnect` skips all state cleanup and one-sided quote summary logging when cancelled by `LiveNode`'s disconnection timeout

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `STREAM_JOIN_TIMEOUT_SECS` (line 136), `AlpacaDataClient._disconnect` (lines 461–481), and `AlpacaDataClient._connect_venue` (lines 448–455).
   - `packages/canon-alpaca/README.md`: lines 112–118.
   - `packages/canon-alpaca/tests/test_data_client.py`: `build_node` (lines 203, 214) and `test_disconnect_clears_state_when_the_stream_thread_does_not_stop` (lines 734–754).
2. **What goes wrong:**
   In `_disconnect`, clearing `self._stream_thread`, `self._stream`, `self._historical`, `self._quote_symbols`, `self._trade_symbols`, and logging/clearing `self._one_sided_quotes` (lines 468–477) happens **after** `await asyncio.to_thread(thread.join, STREAM_JOIN_TIMEOUT_SECS)` (line 466) **without a `try ... finally` block**.
   Meanwhile, `STREAM_JOIN_TIMEOUT_SECS` is `10.0` s — equal to `LiveNodeConfig().timeout_disconnection_secs` (`10.0` s) and **greater** than the `timeout_disconnection_secs=5` configured in `build_node()` (`test_data_client.py:203, 214`) or any node with `timeout_disconnection_secs <= 10`.
   When the stream thread does not exit before the node's disconnection timeout, NautilusTrader cancels `_disconnect()` while it is awaiting `asyncio.to_thread(thread.join, ...)`:
   - `self._stream`, `self._stream_thread`, `self._historical`, `self._quote_symbols`, `self._trade_symbols`, and `self._one_sided_quotes` are **not** cleared;
   - the per-symbol `"Dropped N one-sided Alpaca quotes..."` summary warnings are **not** logged; and
   - if `_connect()` is called afterward, `_connect_venue` checks `if self._historical is None or self._stream is None:` (line 448) and `if self._stream_thread is None:` (line 613), reusing the stuck stream and thread instead of building a fresh stream — contradicting `data.py:467` (*"Cleared whether or not the thread stopped, so a later connect builds a fresh stream"*) and `README.md:115–117`.
   `test_disconnect_clears_state_when_the_stream_thread_does_not_stop` missed this because it monkeypatched `STREAM_JOIN_TIMEOUT_SECS` to `0.05` and called `await client._disconnect()` directly instead of letting `LiveNode` run its disconnect timeout against `STREAM_JOIN_TIMEOUT_SECS`.
3. **How it shows / verification:**
   - Input/sequence: Build a `LiveNode` with `timeout_disconnection_secs < STREAM_JOIN_TIMEOUT_SECS` (e.g. `timeout_disconnection_secs=1` and `STREAM_JOIN_TIMEOUT_SECS=2.0`, matching the 5 s vs 10.0 s ratio in `build_node`), set `stream.ignores_stop = True`, subscribe to `AAPL.ALPACA` quotes, and trigger `shutdown_system`.
   - Verified offline with `mise exec -- uv run --locked python`: `node.run_async()` raised `RuntimeError: disconnect timeout while disconnecting clients...`, and afterward `client._stream is not None`, `client._stream_thread is not None`, `client._historical is not None`, and `client._quote_symbols == {'AAPL'}` were all still `True`/non-empty.

---

### 4. An abandoned stream thread from a timed-out `_disconnect` still delivers stale/duplicate quotes, trades, and cancel/correction warnings after a reconnect

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `_emit_quote` / `_on_quote` (lines 623–647, 688–690), `_emit_trade` / `_on_trade` (lines 649–658, 692–694), `_warn_trade_cancel` / `_on_trade_cancel` (lines 660–667, 696–698), and `_warn_trade_correction` / `_on_trade_correction` (lines 669–677, 700–702); contrast `_on_stream_exit` (lines 679–684).
2. **What goes wrong:**
   Commit `f364348` changed `run_stream` and `_on_stream_exit` to tag exit notifications with `stream` and check `if stream is self._stream and not self._stopping:` (line 683), noting that *"a thread left behind by a timed-out disconnect can end after a reconnect, when `_stopping` is False again."*
   However, the handlers registered on the abandoned `StockDataStream` (`weak_coroutine(self._on_quote)`, `_on_trade`, `_on_trade_cancel`, `_on_trade_correction`) remain bound to the same `AlpacaDataClient` instance and do **not** check `stream is self._stream`. Their loop callbacks (`_emit_quote`, `_emit_trade`, `_warn_trade_cancel`, `_warn_trade_correction`) check only `self._stopping` and `symbol in self._quote_symbols` / `self._trade_symbols`.
   Once the client reconnects (`self._stopping = False`) and subscribes to the same symbol on the new stream, any message received by the abandoned stream before its websocket closes is emitted into the cache and strategy via `_handle_data` (or increments `_one_sided_quotes`, or logs duplicate cancel/correction warnings).
3. **How it shows / verification:**
   - Input/sequence: In a hosted `LiveNode`, subscribe `AAPL.ALPACA` quotes on `old_stream`, set `old_stream.ignores_stop = True` and `STREAM_JOIN_TIMEOUT_SECS = 0.05`, let `await client._disconnect()` time out, swap `StockDataStream` to an idle `new_stream` (`quotes = []`), call `await client._connect()` and `await client._subscribe_quotes(command(AAPL))`, and then invoke `await old_stream.quote_handlers["AAPL"](quote(bp=99.99, ap=100.01))`.
   - Verified offline with `mise exec -- uv run --locked python`: `node.cache.quote(AAPL).bid_price` changed from `87.6600` to `99.9900` solely from the abandoned `old_stream` handler firing after reconnect.

---

### 5. `AlpacaDataClient._one_historical_call` permanently deadlocks `self._historical_lock` if `asyncio.to_thread` fails to schedule or is cancelled before its first step, and leaks `'Task exception was never retrieved'` when a cancelled request's HTTP call fails

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `AlpacaDataClient._one_historical_call` (lines 566–584, specifically lines 578–583).
2. **What goes wrong:**
   ```python
   await lock.acquire()
   try:
       call = asyncio.ensure_future(asyncio.to_thread(release_after, loop, lock, fn, arg))
   except BaseException:
       lock.release()
       raise
   return await asyncio.shield(call)
   ```
   Two bugs occur here:
   - **Permanent lock deadlock:** `asyncio.to_thread(...)` is an `async def` coroutine function, not a synchronous `loop.run_in_executor(...)` call. `asyncio.ensure_future(asyncio.to_thread(...))` only schedules a `Task` wrapping the unstarted coroutine; `loop.run_in_executor` is not called until `call` executes its first step on the loop (inside `await asyncio.shield(call)`). If `loop.run_in_executor` raises inside `call` (e.g. `RuntimeError: cannot schedule new futures after shutdown` on a shut-down executor) or if `call` itself is cancelled via `asyncio.all_tasks()` before its first step runs, `release_after` never runs, the `except BaseException:` block at lines 580–582 has already been exited, and `self._historical_lock` stays locked forever.
   - **`Task exception was never retrieved` on cancelled requests:** When the caller awaiting `await asyncio.shield(call)` is cancelled while `fn(arg)` is running in the worker thread (the shutdown/cancellation path described at lines 51–53 and 571–572), `asyncio.shield` detaches its callback from `call` and `_one_historical_call` drops its reference to `call` without registering a done-callback to consume `call.exception()`. If `fn(arg)` subsequently raises (e.g. `APIError` or `ConnectionError`), `release_after` releases the lock and re-raises into `call`, causing Python's event loop to emit `Task exception was never retrieved` when `call` is garbage-collected.
3. **How it shows / verification:**
   - Verified both offline with `mise exec -- uv run --locked python`:
     1. Shutting down a `ThreadPoolExecutor` (`ex.shutdown(wait=False)`), setting `loop.set_default_executor(ex)`, and awaiting `client._one_historical_call(fn, params)` raised `RuntimeError: cannot schedule new futures after shutdown` and left `client._historical_lock.locked() == True` permanently (likewise when cancelling `call` before its first step).
     2. Starting `task = asyncio.create_task(client._one_historical_call(failing_fn, params))`, cancelling `task` once `failing_fn` entered the worker thread, letting `failing_fn` raise `RuntimeError("boom")`, and running `gc.collect()` triggered the loop exception handler with `'Task exception was never retrieved'`.

---

### 6. `BUILT_NODES` in `test_data_client.py` keeps `LiveNode` undisposed during test bodies, causing `logged(capfd)` assertions to race NautilusTrader's async logger (both flaky false failures and false passes) and breaking multiple `build_node` calls in one test

1. **Where:**
   - `packages/canon-alpaca/tests/test_data_client.py`: `BUILT_NODES`, `dispose_nodes`, `logged`, and `build_node` (lines 167–185, 219), affecting log assertions in `test_trade_cancels_and_corrections_are_logged` (lines 634–646), `test_one_sided_quotes_are_dropped_and_logged_once_then_counted` (lines 648–663), `test_one_sided_quote_count_restarts_after_resubscribing` (lines 665–681), `test_old_stream_ending_after_a_reconnect_is_not_reported` (lines 709–732), `test_stream_ending_while_subscribed_is_logged` (lines 756–764), and `test_missing_credentials_fail_the_connect_and_build_no_client` (lines 801–817).
2. **What goes wrong:**
   NautilusTrader writes logs asynchronously from a Rust logging thread and flushes synchronously when the node's `LogGuard` is disposed/dropped. Commit `f364348` added `BUILT_NODES.append(node)` in `build_node()` (line 219) and only disposes/pops nodes in the `dispose_nodes` fixture teardown (lines 177–178) — *after* the test body and `logged(capfd)` (`capfd.readouterr()`, line 183) have already run.
   - **False test failures:** Positive assertions on `logged(capfd)` race the Rust logging thread and fail when log lines have not yet been written to fd 1/2 at the instant `capfd.readouterr()` runs.
   - **False test passes:** Negative assertions on `logged(capfd)` — specifically `test_old_stream_ending_after_a_reconnect_is_not_reported` (`line 731`: `assert "Alpaca stream ended while subscribed" not in logged(capfd)`) — can **pass even if `_on_stream_exit` logged that exact error** just before `run_with_client` returned, because `logged(capfd)` reads before `dispose_nodes` flushes the logger.
   - **Single-node-per-test side effect:** Because `BUILT_NODES` holds a strong reference to every `LiveNode` until teardown and NautilusTrader allows only one live `LiveNode` object on a thread until dropped, calling `build_node(...)` twice in a single test raises `RuntimeError: A LiveNode already exists or is being built on this thread; drop it before building another`.
3. **How it shows / verification:**
   - Observed directly when running `mise exec -- uv run --locked pytest packages/canon-alpaca/tests`: `test_trade_cancels_and_corrections_are_logged` failed on the first full-suite run (and 2 of 4 repeated runs) with `AssertionError: assert 'Alpaca trade cancel for AAPL trade 465' in ''`, while both expected warning lines appeared in pytest's `Captured stdout call` milliseconds later during teardown.

---

### 7. `parse_quote` misclassifies `NaN` bid/ask prices as `OneSidedQuoteError` instead of unrepresentable prices, suppressing `ERROR` logs

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `parse_quote` (lines 354–373, specifically lines 360–364) and `AlpacaDataClient._emit_quote` (lines 629–646); docstring at line 357 and `packages/canon-alpaca/README.md` line 89.
2. **What goes wrong:**
   `parse_quote` evaluates `if not price > 0: raise OneSidedQuoteError(...)` (lines 361–364) *before* calling `exact_price` (which checks `not math.isfinite(value)` and raises `ValueError`). Because `float("nan") > 0` is `False` in IEEE 754 / Python, `not (float("nan") > 0)` is `True` (whereas `float("inf") > 0` is `True` and reaches `exact_price`). `alpaca-py`'s `Quote` model accepts `float("nan")` without raising.
   Consequently, a corrupt `NaN` quote price raises `OneSidedQuoteError` instead of `ValueError`: `_emit_quote` treats it as a routine one-sided quote (logging only a `WARNING` on count 1, and logging **nothing at all** on counts 2–9 if a genuine `0.0` one-sided quote was already seen for that symbol) instead of logging an `ERROR` (`"Dropped Alpaca quote: ..."`) as it does for `inf` or excess-precision prices, and contradicting `data.py:357` (*"A side priced at zero or less is refused"*).
3. **How it shows / verification:**
   - Verified offline with `mise exec -- uv run --locked python`: `parse_quote(quote(bp=float("nan")), equity, 1_700_000_000_000_000_000)` raised `OneSidedQuoteError("AAPL.ALPACA quote at 2021-02-22 15:51:45.335689+00:00 has bid price nan")` instead of `ValueError("... is not representable at price precision 4")`.

---

### 8. `AlpacaDataClient._request_bars` calls `bar_interval_ns(bar_type)` before `bar_timeframe(bar_type)`, bypassing the adapter's bar-type validation error messages on non-time bars

1. **Where:**
   - `packages/canon-alpaca/src/canon_alpaca/data.py`: `bar_interval_ns` (lines 277–279) and `AlpacaDataClient._request_bars` (lines 538–542); cf. `bar_timeframe` (lines 264–274) and `packages/canon-alpaca/tests/test_parsing.py` (lines 182–196).
2. **What goes wrong:**
   Commit `f364348` changed `bar_interval_ns` to call `bar_type.spec.get_interval_ns()` and placed `interval = timedelta(microseconds=bar_interval_ns(bar_type) // 1_000)` at line 538 **before** `timeframe=bar_timeframe(bar_type)` at line 542.
   `bar_timeframe` is the function that validates `EXTERNAL` source, `LAST` price type, and supported aggregations (`MINUTE, HOUR, DAY or WEEK`), with explicit error messages tested in `test_parsing.py:182–196` (including `"AAPL.ALPACA-100-TICK-LAST-EXTERNAL"` → `"MINUTE, HOUR, DAY or WEEK"` and `INTERNAL` → `"EXTERNAL bars only"`). Because `bar_interval_ns` runs first in `_request_bars`, any non-time bar type (such as `AAPL.ALPACA-100-TICK-LAST-EXTERNAL` or `AAPL.ALPACA-100-TICK-LAST-INTERNAL`) fails inside `BarSpecification.get_interval_ns()` with `ValueError: Aggregation not time based, was Tick` before `bar_timeframe` is ever reached.
3. **How it shows / verification:**
   - Verified offline with `mise exec -- uv run --locked python`: for `BarType.from_str("AAPL.ALPACA-100-TICK-LAST-INTERNAL")` and `BarType.from_str("AAPL.ALPACA-100-TICK-LAST-EXTERNAL")`, `bar_timeframe` raises `ValueError("...: Alpaca serves EXTERNAL bars only")` and `ValueError("...: Alpaca bars are MINUTE, HOUR, DAY or WEEK here")`, whereas `bar_interval_ns` (called first at line 538) raises `ValueError("Aggregation not time based, was Tick")`.

---

### 9. `FakeStream` never clears `sent` on `_unsubscribe`, so resubscribing on a running stand-in stream never delivers messages (contrary to its docstring)

1. **Where:**
   - `packages/canon-alpaca/tests/conftest.py`: `FakeStream` docstring (lines 252–255), `_unsubscribe` (lines 297–301), and `_serve` (lines 344–360).
2. **What goes wrong:**
   `FakeStream`'s docstring states that while running it sends each configured quote/trade message *"once per subscription"* (line 254). However, `_serve` tracks delivered `(channel, symbol)` pairs in a local `sent: set[tuple[str, str]] = set()` (lines 345, 352–354) that `_unsubscribe` never clears when a symbol is removed from `handlers`. If a test subscribes to `"AAPL"`, unsubscribes `"AAPL"`, and subscribes to `"AAPL"` again while `FakeStream` is running, `("quotes", "AAPL")` / `("trades", "AAPL")` remains in `sent` and the new subscription receives nothing (which forced `test_one_sided_quote_count_restarts_after_resubscribing` at `test_data_client.py:666–675` to bypass `FakeStream` and call `client._emit_quote(...)` directly).
3. **How it shows / verification:**
   - Sequence: Start `FakeStream.run()` on a thread, call `subscribe_quotes(handler, "AAPL")` (1 quote delivered), call `unsubscribe_quotes("AAPL")`, then call `subscribe_quotes(handler, "AAPL")` again. Because `("quotes", "AAPL")` is still in `sent` at `conftest.py:352`, `_serve` skips `symbol` on every loop iteration and never calls `handler` for the second subscription.
