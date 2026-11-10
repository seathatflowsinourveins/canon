# Review of f364348 (canon-alpaca: bar windows, stream lifecycle, data checks)

Scope: `git diff f364348^ f364348` under `packages/canon-alpaca/`. Baseline: `mise exec -- uv run --locked
pytest packages/canon-alpaca` gives 95 passed and 1 skipped (the paper test). I ran the experiments below as
scratch tests outside the checkout. They load `tests/conftest.py` and `tests/test_data_client.py` and use
the same stand-ins and the same real `LiveNode`. I checked the alpaca-py 0.45.0 and NautilusTrader sources
that `uv.lock` installs.

Defects are listed most severe first.

---

## 1. A bar request with no `start` and an `end` before today sends an inverted window and fails. The docs say this cannot happen.

**Where:** `data.py`, `alpaca_bars_window` (lines 294–308). The false statements are in its docstring ("supplying it
here keeps the window sent from ending before it starts"), the module docstring (lines 41–44), the README's
"Bar windows" bullet ("and the window sent never ends before it starts"), and the commit message ("so the
window sent never ends before it starts").

**What goes wrong:** With `start=None`, the start becomes `start_of_day(now)`, which is midnight New York
today. The end is `min(end, now)`. If the requested `end` is before today's midnight, the start sent is
after the end sent. Alpaca refuses the request, so it fails with no bars. The code only ever uses the
current day as the default start. It never uses the day the requested `end` falls in.

**How shown:** I ran a scratch test through a real node. The strategy called `request_bars(1-HOUR, start=None,
end=now-2 days)`. The adapter sent `start=2026-11-10 04:00` and `end=2026-11-08 20:59` (naive UTC). The
stand-in refused it ("start is after end"), and the strategy got `failure_reason` `{"code": 42210000, ...}`.
The real endpoint's answer to an inverted window is UNVERIFIED, but it cannot return the bars asked for.
None of the repository's tests combine an open start with a past end.

## 2. After a disconnect that times out and a reconnect, the old stream still feeds data into the client. A symbol can then be delivered twice.

**Where:** `data.py`, `_disconnect` (lines 461–481), `_emit_quote` and `_emit_trade` (lines 623–658), and the
`weak_coroutine` handlers registered in `_subscribe_*` and `_connect_venue`.

**What goes wrong:** If the stream thread does not stop within `STREAM_JOIN_TIMEOUT_SECS`, `_disconnect`
clears `_stream` and the symbol sets and raises. The old `StockDataStream` keeps running, connected, for up
to the ~15 s or more that the constant's own docstring describes, and its handlers still point at the same
live client. After `_connect` and a new subscribe on the new stream, `_quote_symbols` and `_trade_symbols`
hold the symbol again. Every message the old stream still receives then passes the filters in
`_emit_quote` and `_emit_trade` and reaches `_handle_data`, in addition to the new stream's copy. The same
holds for trade cancel and correction warnings. The commit guards the *exit report* against exactly this
case (`stream is self._stream` in `_on_stream_exit`), but not the data.

**How shown:** I ran a scratch test with `run_with_client` and `STREAM_JOIN_TIMEOUT_SECS=0.05`. The old stand-in
had `ignores_stop=True`. I then ran `_disconnect` (it raised "did not stop within 0.05 s"), `_connect` with a
new stand-in, and `_subscribe_quotes(AAPL)`. Next I invoked the old stream's AAPL quote handler, as a message
arriving on the old socket would. `_handle_data` received one more QuoteTick while the old stream was still
running. The handlers keep no record of which stream a message came from, so the client cannot tell the two
copies apart.

## 3. The default start uses wall-clock arithmetic, so a weekly request across the autumn clock change returns a bar outside its window

**Where:** `data.py`, `alpaca_bars_window` and `start_of_day` (lines 289–308). The affected expression is
`start - interval`, where `start` carries `ZoneInfo("America/New_York")`.

**What goes wrong:** `start_of_day` returns a datetime that carries the New York time zone. Python
subtracts a `timedelta` from such a datetime on the local clock, not in elapsed time. On a weekly request
with no `start`, made on Monday 2026-11-02, the code sends 2026-10-26 00:00 EDT, which is 04:00 UTC. The
start the docstring describes is today's midnight minus 168 hours, 2026-10-26 05:00 UTC. The previous
week's bar opens at 04:00 UTC and has `ts_init` 2026-11-01 23:00 EST, before today's midnight, yet it is
returned. That breaks the docstring's rule, "returns exactly the bars whose `ts_init` lies in its window",
with the window starting at "midnight New York of the current day". The same bar was already a valid
result on the Sunday after 23:00. A daily request is also off by an hour across the change, but no Sunday
bar exists for that hour to affect. Explicit starts are not affected: NautilusTrader passes them in as UTC
datetimes (checked with a spy: `request.start`, `request.end` and `clock.utc_now()` are all
`tzinfo=timezone.utc`).

**How shown:** I called `alpaca_bars_window(None, None, 2026-11-02 12:00 NY, interval)` directly. For a week
it sent a start of 2026-10-26 04:00 UTC, against 05:00 UTC in elapsed time. For a day it sent 2026-11-01
04:00 UTC, against 05:00. `test_bar_request_with_no_bounds_starts_at_the_beginning_of_the_day` computes its
expected value as `midnight - DAY` on a New York datetime, the same arithmetic, so it cannot catch this. The
README's "for WEEK, last week's bar on a Monday" describes the current behaviour, not the stated rule. This
depends on Alpaca opening weekly bars at Monday midnight New York, which the commit itself marks UNVERIFIED.

## 4. The `limit` test case never makes `limit` cut anything

**Where:** `tests/test_data_client.py`, `test_bar_window_holds_the_bars_that_close_inside_it`, second case
`(START, START + HOUR, 2, [START, START + HOUR])`. The README's test table lists "`limit`" as covered.

**What goes wrong:** The window sent is [08:00, 09:00], which holds exactly two of the stand-in's opens.
`limit=2` therefore removes nothing, and the case passes whether or not the limit reaches Alpaca or the
stand-in applies it.

**How shown:** I ran the same case with the stand-in's `_answer` patched to drop `limit`. It still got 2 bars and
passed.

## 5. A daily-window test case passes with or without the window shift it is meant to check

**Where:** `tests/test_data_client.py`, `test_daily_bar_window_selects_by_open_plus_24_hours`, second case,
"Inside Monday's session ... so nothing".

**What goes wrong:** Without the shift, the window [Mon 14:30, Mon 21:00 UTC] holds no open either, since the
bars open at 05:00 UTC. The expected `[]` holds under both implementations, so this case tests nothing.
The other two cases do catch a missing shift.

**How shown:** I ran the case with `alpaca_bars_window` patched to send the requested bounds unshifted. The
response had no error and no bars, and the case passed.

## 6. The cancelled-request test's `max_active == 1` assertion cannot fail

**Where:** `tests/test_data_client.py`, `test_cancelled_bar_request_keeps_the_next_waiting_until_its_call_returns`.

**What goes wrong:** `second` is created with `ensure_future`, and then the `finally` sets the gate in the
same step, before `second` runs. The first call is already unblocked when the second could start, so the
two calls never overlap and `max_active == 1` holds even if the lock were released on cancel. Only the
`client._historical_lock.locked()` assertion tests the property. Found by reading; I did not run a
variant.

## 7. The one-sided quote count is dropped without a log line on unsubscribe, and a comment misstates when the count resets

**Where:** `data.py`, `__init__` line 429 (`# dropped per symbol since connect`), and `_unsubscribe_quotes` lines
504–505. The README says "...and the total on disconnect. The count restarts when the symbol is
unsubscribed."

**What goes wrong:** Unsubscribing removes the symbol's count without logging it, so the comment's "since
connect" is false: the count runs from the subscribe. A drop count that has not reached a power of ten is
never reported. For example, 9 one-sided quotes followed by an unsubscribe log only the first warning. The
disconnect total covers only the current subscriptions. This follows from the code as written; I did not
run it separately. `test_one_sided_quote_count_restarts_after_resubscribing` shows the 2 drops before the
resubscribe never appear as a total.

## 8. A TICK (or other non-time) bar request now fails with NautilusTrader's error, not the adapter's

**Where:** `data.py`, `_request_bars` lines 538–542. `bar_interval_ns(bar_type)` now runs before
`bar_timeframe(bar_type)`.

**What goes wrong:** `BarSpecification.get_interval_ns()` raises `ValueError("Aggregation not time based, was
Tick")` for TICK, VOLUME and similar bar types. That error now surfaces before the adapter's own check
("Alpaca bars are MINUTE, HOUR, DAY or WEEK here"). The request still fails, but with a message that does
not say what Alpaca supports. MONTH still reaches `bar_timeframe`, because `get_interval_ns` returns 30
days for it. So the "Known limits" reason for refusing MONTH ("no fixed interval") no longer matches the
interval function the code now uses. Low severity.

**How shown:** For `AAPL.ALPACA-100-TICK-LAST-EXTERNAL`, `get_interval_ns()` raised `ValueError: Aggregation not
time based, was Tick`. For `...-1-MONTH-...` it returned `2592000000000000`.

## 9. (Unsure) `weak_coroutine` does not ensure the client is never dropped on the stream thread, though its docstring implies it

**Where:** `data.py`, `weak_coroutine` docstring (lines 196–198) and the module docstring ("holds the client only
weakly ... as does the thread itself").

**What goes wrong:** While `call` runs, `bound` and the bound methods passed to `post` hold the client strongly
on the stream thread. More importantly, `tests/conftest.py` itself states that the client and its
`ClientRuntime` form a reference cycle, freed by the cyclic collector "on whichever thread happens to
allocate". A stream thread left running after a timed-out disconnect can therefore still be the thread
that frees the client, and so its `Logger` and `Clock`. The weak references remove one reason the client
outlives the node, but not the off-thread drop that the docstring gives as the reason for them. I did not
reproduce an off-thread drop.

---

## Checked and found to hold

* alpaca-py 0.45.0 `_unsubscribe` sends first and deletes its handlers only after the send returns. When not
  running it only deletes, and raises `KeyError` for a symbol never subscribed. `register_trade_cancels` and
  `register_trade_corrections` set `{"*": handler}`. The `TradeCancel` and `TradeCorrection` field names
  used in the log lines exist.
* The `filter_callable` claims: the base `InstrumentProvider` in `nautilus_trader/live/providers.py` never
  reads it, and the native library contains "Binance v2 does not support instrument filter_callable".
  `AssetStatus` is a `str` enum, so `status="active"` is accepted.
* `requests.exceptions.RequestException` and `ConnectionError` subclass `OSError`.
* `exact_price`'s `round(value, 4) == value` criterion matched a decimal-count check of `repr(value)` on 200,000
  random floats.
* The NautilusTrader runtime runs subscribe and unsubscribe hooks one at a time per client. A strategy's
  unsubscribe followed by a subscribe came out in order, so the ordering change in `_unsubscribe_*` has no
  race through the engine. A bar request stalled in `get_stock_bars` did not hold up a later quote
  subscription, so the README's "Subscriptions ... are not held up by it" holds.
* The DST arithmetic for weekly bars in the README and the paper test's window and day choice (2025-03-12,
  a Wednesday) are consistent.

Not verified: anything against the live Alpaca API, including bar stamping, endpoint filtering and the answer
to an inverted window. I did not run the paper test.
