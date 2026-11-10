# Review of f364348445fb1be2ba6ca09a3d4b6e934d08dc5d

1. **Abandoned streams can publish into a new connection.** `packages/canon-alpaca/src/canon_alpaca/data.py`, `_disconnect` (lines 467–480), `_on_quote` / `_on_trade` (lines 688–694).

   A timed-out disconnect now discards the old stream and permits a fresh connection, but the old stream's data handlers have no stream identity or connection generation. After reconnecting and subscribing to the same symbol, `_stopping` is false and the symbol is present again, so old quotes and trades pass the current subscription checks. The identity check added to `_on_stream_exit` protects only the exit notification. This can deliver stale or duplicate market data, and old trade corrections can likewise produce misleading warnings about the new connection.

   **Shown offline:** used the existing `run_with_client` harness and stream stand-in, disabled automatic quotes, subscribed to AAPL, set `ignores_stop=True` and the join timeout to 0.02 seconds, caught the disconnect timeout, connected a fresh stream, and subscribed to AAPL again. Invoking the old stream's retained AAPL quote handler with bid 11.11 / ask 11.12 put a quote with bid `11.1100` into the real node cache. Both threads were released and the node disposed afterward.

2. **The weak handler still permits client destruction on the stream thread (native-object consequence uncertain).** `packages/canon-alpaca/src/canon_alpaca/data.py`, `weak_coroutine` (lines 193–205), and the “never holding the client” comment above `_on_quote`.

   `bound = ref()` creates a strong bound-method reference on the stream thread and retains it throughout `await bound(item)`. If the owning loop releases its last references while that callback is running, the callback can release the final reference on the stream thread. Posting `self._emit_quote` also temporarily creates and queues a strong bound method; it does not guarantee that the stream-side reference will be released before the queued callback runs. Thus the stated guarantee that nothing on the stream thread can keep the client alive is false, and the change does not fully enforce its stated requirement that native Logger/Clock objects be dropped on their owning thread. The new garbage-collection test exercises an idle handler after disconnect, not an in-flight callback during teardown.

   **Shown offline:** wrapped a receiver's async bound method with the actual `weak_coroutine`, invoked it on a thread named `stream-proof`, and blocked inside the method using two `threading.Event`s. Deleted the owning-thread reference and ran `gc.collect()`: the weak reference was still alive. Releasing the callback caused the receiver's `__del__` to run on `stream-proof`. **Uncertainty:** this proves the lifetime/claim defect, but I did not reproduce a native NautilusTrader Logger/Clock teardown failure with the complete client.

3. **Bar bounds subtract wall-clock time instead of the fixed elapsed interval across DST.** `packages/canon-alpaca/src/canon_alpaca/data.py`, `alpaca_bars_window`, line 308.

   Subtracting a `timedelta` directly from a `ZoneInfo` datetime uses local clock arithmetic. That is inconsistent with `parse_bar`, which adds a fixed nanosecond interval to an epoch timestamp. A bound spanning a clock change therefore shifts by 167 or 169 hours for a weekly bar instead of 168. Explicit strategy bounds may arrive normalized to UTC, but the implicit start always comes from `start_of_day(now)` in New York, so the default-start path is affected regardless.

   **Shown offline:** with start `2025-03-10 00:00 America/New_York` and a seven-day interval, the actual helper returns a start of `2025-03-03 05:00Z`; fixed elapsed subtraction requires `2025-03-03 04:00Z`. For a concrete default-start failure after the autumn change, use `now=2026-11-02 12:00Z`, no start, and a weekly interval: the helper starts at `2026-10-26 04:00Z` rather than `05:00Z`. It consequently admits the weekly bar opening at `2026-10-26 00:00` New York, whose fixed `ts_init` is Sunday November 1 at 23:00 New York, before the promised Monday-midnight default window. With no requested start, the response has no start bound to remove that extra bar. The existing weekly DST tests supply explicit bounds through the node and do not exercise this implicit New York bound.

4. **An end-only historical request contradicts the new guarantee against inverted windows.** `packages/canon-alpaca/src/canon_alpaca/data.py`, `alpaca_bars_window` (lines 304–308); `packages/canon-alpaca/README.md`, “Bar windows”.

   When start is absent, the helper chooses today's midnight even if the supplied end precedes today. It then sends start after end. This is particularly relevant to an end-plus-limit request for historical data. At minimum the new docstring and README assertion that the window sent never ends before it starts is false; the helper neither handles nor explicitly rejects this case before passing it to Alpaca.

   **Shown offline:** `alpaca_bars_window(None, datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), timedelta(hours=1))` returns start `2025-12-30 23:00-05:00` and end `2024-12-31 23:00Z`, with `start > end`. No network was used. Whether the live endpoint rejects this exact request is UNVERIFIED; the changed offline stand-in explicitly rejects it.

5. **Failure to submit the HTTP worker permanently holds the historical lock.** `packages/canon-alpaca/src/canon_alpaca/data.py`, `_one_historical_call`, lines 577–583.

   The exception handler surrounds construction of a task containing `asyncio.to_thread`, but executor submission happens later, when that coroutine runs. If submission fails, `release_after` never executes, the exception escapes through `asyncio.shield` outside the handler, and the acquired lock is never released. Subsequent bar calls then wait forever instead of reporting the same executor failure. This requires an executor that refuses work, such as a default executor already shut down during lifecycle handling; ordinary successful submission is unaffected.

   **Shown offline:** on a fresh asyncio loop, awaited `loop.shutdown_default_executor()` and then called the actual `_one_historical_call` as an unbound method with a minimal receiver containing an `asyncio.Lock`. It raised `RuntimeError: Executor shutdown has been called`; afterward `_historical_lock.locked()` was `True`. The HTTP function was never entered.

## Verification and limits

Reviewed the parent-to-HEAD diff within `packages/canon-alpaca/`, related fixtures, and installed pinned upstream Python sources/stubs. Ran `mise exec -- uv run --locked pytest packages/canon-alpaca/tests --ignore=packages/canon-alpaca/tests/test_paper_integration.py`: **95 passed in 5.16s**. The additional reproductions above used the pinned environment and offline stand-ins only.

No paper tests, credentials, remote repository access, later history, or `evals/` were used. No tracked checkout files were changed (`git status --porcelain` was empty after verification); the permitted locked test command created the ignored `.venv`. Live API behavior and the native teardown consequence identified in finding 2 remain unverified. Fixes and regression tests remain to be implemented.
