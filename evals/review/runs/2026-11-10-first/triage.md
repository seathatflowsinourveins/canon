# Triage: the unconfirmed findings of run 2026-11-10-first

This run's 115 unconfirmed findings ([`scores.md`](scores.md)) are checked here against `main` at
`665b7bf`. Each finding is counted by its report's own top-level numbering, as the score sheet counts it.
Findings that make the same claim are merged across reviewers first. Each merged item then falls into
one category:

* **Confirmed:** a later commit on `main` fixes it, with a regression test whose commit message reports
  it failing first ([README](../../README.md), "A case"). An item that is a new defect is entered in its
  case and counts from the next scoring. One that makes the claim of a defect already in its case adds
  nothing. This category judges the claim, not the mark: a finding the score sheet settled as missed
  because it named another location stays missed there, and `scores.md` stands as scored.
* **Live:** the code still behaves that way at `665b7bf`. This was shown by reading the code or by an
  offline probe outside the repository; each row says which. Nothing here fixes a live item. They are
  listed for the owning lanes, under [Live, by area](#live-by-area).
* **Gone:** later code removed or changed the behaviour, but no commit message reports a failing test
  for it, so it cannot be entered.
* **Not a defect:** a limit the commit states, a misreading of the code, or wording only. The row gives
  the reason.

An item whose parts fall into different categories is counted once, under its main claim. The other
part is noted in the row. Anything not checked against a source is marked `UNVERIFIED`.

## Counts

Findings per reviewer and category, summed over the five cases:

| Reviewer | Confirmed | Live | Gone | Not a defect | Total |
|---|---|---|---|---|---|
| `claude-opus-high` | 2 | 17 | 4 | 4 | 27 |
| `codex` | 1 | 7 | 2 | 0 | 10 |
| `muse` | 1 | 15 | 5 | 9 | 30 |
| `antigravity` | 4 | 23 | 12 | 9 | 48 |
| **Total** | 8 | 62 | 23 | 22 | 115 |

Per case, after merging:

| Case | Findings | Merged items | Confirmed | Live | Gone | Not a defect |
|---|---|---|---|---|---|---|
| `cancel-all` | 24 | 16 | 0 | 4 | 5 | 7 |
| `stop-orders` | 16 | 10 | 1 | 3 | 0 | 6 |
| `data-client-hardening` | 29 | 17 | 0 | 13 | 0 | 4 |
| `exec-client-first-slice` | 25 | 20 | 0 | 18 | 1 | 1 |
| `trial-ledger-gate` | 21 | 16 | 4 | 2 | 9 | 1 |

### New `[[defects]]` entries

| Case | Defect | Fix | Test | Reported by |
|---|---|---|---|---|
| `stop-orders` | `unholdable-client-id-aborts-startup` | `f8505c4` (#77) | `test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped` | `claude-opus-high` #1, `antigravity` #3 |
| `trial-ledger-gate` | `session-day-without-return-dropped` | `ba44c25` (#79) | `test_a_session_day_with_no_return_is_refused` | `muse` #3, `antigravity` #5 |

`stop-orders` is no longer clean. The confirmed defects in the cases go from 21 to 23.

Three fixes with a failing-first test are not entered:

* **`cancel-all` 1, the order skipped as pending cancel (fixed in `f8505c4`).** At `4afa6a7`, the README's
  cancel-all limit and the commit message both state the skip ("skipping one already pending cancel").
  The README leaves a stated limit out of a case, so a reviewer who accepted it, as muse #7 did, is not
  marked down. It is counted as not a defect here.
* **Four fixes in `d5134ec`:** N-hat below 1, the writes outside `record_trial`'s `try`, a record not in
  the writer's shape, and a bar stamped at 00:00 UTC (`trial-ledger-gate` 1, 2, 4, 5). Its message
  reports thirteen reproductions as a group, eleven failing and two passing. It names neither of the two
  that passed, and says of one only that it "needed another case (affine copies) to reach the bare
  `ValueError`". So it does not report any one of these tests failing first, and all four are counted as
  gone. The fixing lane can settle them by naming each reproduction's test and result.

## `cancel-all` (`4afa6a7`)

| # | Findings | Claim | Category | Evidence |
|---|---|---|---|---|
| 1 | opus #1, antigravity #1 | Cancel-all skips an order left `PENDING_CANCEL` by a cancel that got no answer, so it stays working at Alpaca | Not a defect (stated limit) | The README's cancel-all limit at the commit says it skips "one already pending cancel", and so does the commit message. `f8505c4` later fixes it with a failing-first test, but a stated limit is left out of the case (see above) |
| 2 | opus #2 | `aapl_left_as_found` says the paper lock keeps other sessions out | Not a defect | Stated precondition: paper tests run through `mise run paper:test`, which holds the lock (`.claude/rules/trading-safety.md` at the commit) |
| 3 | opus #3, codex #3, muse #6, antigravity #7 | The module docstring says one `CancelAllOrders` cancels two orders; the test places one | Gone | `724246e` adds the second order and rewrites the docstring |
| 4 | opus #4, antigravity #9 | The `run_exec_tester` docstring names a parameter `orders` | Gone | `724246e` names it `count` |
| 5 | codex #1, antigravity #3 | The cleanup does not wait for cancels or the close to settle | Gone | `25dc74a` adds `wait_until` after both. Its message reports no failing test |
| 6 | muse #1, antigravity #2 | `cancel_order_by_id` in the cleanup's `finally` is unguarded. A 422 for an order that closed meanwhile aborts the cleanup and hides the test's own failure | Live (read) | `test_paper_execution.py: aapl_left_as_found`, the bare cancel loop in `finally`. How often Alpaca hits this race is `UNVERIFIED` |
| 7 | muse #2 | The cleanup's REST listing may still show an order the stream already reported cancelled | Live (read) | Nothing makes the two views agree before the cleanup lists orders. The lag itself is `UNVERIFIED`. The effect is item 6 |
| 8 | codex #2, muse #3 | `run_exec_tester`'s 60 s deadline runs out silently, so an order that never reached `ACCEPTED` and was cancelled on stop still passes TC-E41 | Live (read) | `test_paper_execution.py: run_exec_tester`: the loop falls through to `finally`, and no assertion records the accept |
| 9 | muse #4 | The batch contract test runs the base class's default on a recorder | Not a defect | The commit message says so. The client does not override the hook |
| 10 | muse #5 | The external-order assertion reads the fake, not the node's cache | Not a defect | A point about test strength. The behaviour is right, as muse's own probe shows |
| 11 | muse #7 | No test covers the `PENDING_CANCEL` skip | Gone | `f8505c4` removes the skip and tests the repeat |
| 12 | antigravity #4 | `node.dispose()` does not release the one-`LiveNode`-per-thread guard, so a failed paper test fails the next one, and the comment says otherwise | Live (probe) | Offline probe: after `dispose()`, with a reference held, a second build raises "A LiveNode already exists". After `del`, it builds. `test_paper_execution.py: test_paper_limit_order_is_accepted_and_canceled` keeps that pattern and comment |
| 13 | antigravity #5 | `run_exec_tester` waits the full 60 s when an order is denied | Not a defect | The test still fails, only slower. The sub-penny trigger it names is not a price a US stock above $1 is quoted at |
| 14 | antigravity #6 | The continuation tests depend on cache order | Not a defect | Offline probe: an abort-on-first-failure mutant fails both tests in the cache's own order |
| 15 | antigravity #8 | The docstring and README misstate the pre-checks and the cleanup's scope | Gone | `724246e` rewrites both |
| 16 | antigravity #10 | The header says the command is read from the cache | Not a defect | Wording only |

## `stop-orders` (`724246e`)

| # | Findings | Claim | Category | Evidence |
|---|---|---|---|---|
| 1 | opus #1, antigravity #3 | `_order_report` builds `ClientOrderId(...)` for its cache lookup outside `order_status_report`'s `except ValueError`. A closed order with an empty or non-ASCII client ID then aborts start-up reconciliation | Confirmed, new | Read at `724246e`: the lookup is at the top of `_order_report`. `f8505c4` fixes it. Its message: "each reproduced with a failing test before the fix", and the test "failed before the fixes" |
| 2 | muse #1, antigravity #1 | A stop-market order with `display_qty` is not denied, and the full quantity goes to Alpaca | Live (probe) | Offline probe at `665b7bf`: stop-market gets no denial and sends a `StopOrderRequest` for 2 shares, while limit and stop-limit are denied. `_denial` checks `display_qty` only for `LIMIT_PRICED`, against the module docstring |
| 3 | muse #2, antigravity #2 | A stop naming another `trigger_instrument_id` is sent as a plain stop on its own symbol | Not a defect | No adapter of NautilusTrader's at the pin reads the field (installed package searched). Its order and emulated-order pages do not define it. Nothing in this repository promises a denial |
| 4 | opus #3, muse #4, antigravity #6 | "An open stop is reported with trigger type `DEFAULT`" is false for an order the node holds, and that path has no test | Live (read, docs) | `3a00f61` corrects the docstring and ADR 0036, with no failing test. `packages/canon-alpaca/README.md`'s known limits still say `DEFAULT` without the condition, and no test reports a held stop's trigger type |
| 5 | opus #2, muse #5 | The paper stop test's trigger-type check cannot fail and bypasses the client's report | Live (read) | `test_paper_execution.py: test_paper_stop_buy_is_accepted_with_its_prices` calls `order_status_report` with no trigger type, so `DEFAULT` holds by construction, and its docstring names the client's report |
| 6 | opus #4 | Alpaca may convert buy stops to stop-limits, and the limits do not say so | Not a defect | Alpaca's "Orders at Alpaca" page (read 2026-11-10): "Once the order is elected, the stop order becomes a market order". It documents no conversion of a buy stop |
| 7 | muse #3 | An open stop in a status outside `OPEN_STATUSES` fails start-up | Not a defect | A stated limit: an open order with a status this slice does not map fails the report (module docstring) |
| 8 | antigravity #4 | The fake records a stop price of `0.0` as none | Not a defect | A stand-in in the tests only, and no test sends a zero stop |
| 9 | antigravity #5 | The README states that Alpaca sends no update on election, which ADR 0036 marks unverified | Not a defect | Wording. The behaviour (no `OrderTriggered`) is the same either way |
| 10 | antigravity #7 | The README's test row describes only limit buys | Not a defect | Wording. The contradicting sentence was removed in `273ff52` |

## `data-client-hardening` (`f364348`)

`data.py` changed after this commit only in `5575460` (the HTTP timeout) and `d477737` (bounded waits at
disconnect). Neither touches the code behind these findings.

| # | Findings | Claim | Category | Evidence |
|---|---|---|---|---|
| 1 | opus #1, codex #4, antigravity #1 | With no `start` and an `end` before today, the window sent is inverted | Live (probe) | `data.py: alpaca_bars_window`. Probe: start 2026-11-10 04:00Z, end 2024-12-31 23:00Z |
| 2 | opus #2, codex #1, muse #1, antigravity #4 | After a timed-out disconnect and a reconnect, the old stream's handlers still deliver data | Live (read) | `_on_quote`, `_on_trade`, `_on_trade_cancel` and `_on_trade_correction` check only `_stopping` and the symbol sets. Only `_on_stream_exit` checks `stream is self._stream` |
| 3 | opus #3, codex #3, muse #2, antigravity #2 | Subtracting the interval from a New York time does wall-clock arithmetic, so a DST change shifts or inverts the window | Live (probe) | `alpaca_bars_window`, `start_of_day`. Probe: a weekly request on 2026-11-02 starts 04:00Z, not 05:00Z. Daily and weekly requests just after the March change are inverted |
| 4 | opus #9, codex #2 | `weak_coroutine` holds the client strongly on the stream thread during a call | Live (read) | `bound = ref()` is held across the `await`. The native teardown crash it could cause is `UNVERIFIED` |
| 5 | opus #4 | The `limit=2` case of the bar-window test cuts nothing | Live (read) | `test_bar_window_holds_the_bars_that_close_inside_it`, second case |
| 6 | opus #5 | The "inside Monday's session" daily case passes with or without the shift | Live (read) | `test_daily_bar_window_selects_by_open_plus_24_hours`, second case |
| 7 | opus #6 | The `max_active == 1` assertion cannot fail | Live (read) | `test_cancelled_bar_request_keeps_the_next_waiting_until_its_call_returns`: the gate is set in `finally` before the second request runs |
| 8 | opus #7, muse #7 | The one-sided count's comment says "since connect", and an unsubscribe drops the count unlogged | Not a defect | The README at the commit states that the count restarts on unsubscribe. The comment is wording only |
| 9 | opus #8, antigravity #8 | A tick bar request fails with NautilusTrader's error, not the adapter's | Live (probe), low | `_request_bars` calls `bar_interval_ns` before `bar_timeframe`. The request is refused either way |
| 10 | codex #5, antigravity #5 | If the executor refuses work, `_one_historical_call` keeps the request lock held for good | Live (probe) | The `try` covers only `ensure_future`. Probe: "Executor shutdown has been called", lock still held |
| 11 | muse #3 | `_disconnect` drops clients passed to the constructor | Not a defect | Stated design: "a later connect builds a fresh stream" (the docstring and README at the commit) |
| 12 | muse #5 | The fake stream counts as running before it connects, unlike alpaca-py | Live (read), test stand-in | `conftest.py: FakeStream.run`. alpaca-py 0.45.0 sets `_running` after connecting |
| 13 | muse #6 | `is_power_of_ten(1)` is true | Not a defect | No caller reaches it: `_emit_quote` handles a count of 1 first |
| 14 | muse #8 | "Ended while subscribed" is logged when nothing is subscribed | Not a defect | Wording. The error is stated behaviour at the commit |
| 15 | antigravity #3 | If the node's disconnect timeout cancels `_disconnect`, its cleanup and summary are skipped | Live (read) | `_disconnect_stream` has no `finally` around `join_within`. This is reachable only with a node timeout of at most 10 s; the repository's nodes use 15 s. That NautilusTrader cancels the hook is `UNVERIFIED` |
| 16 | antigravity #7 | A NaN bid or ask is taken as a one-sided quote | Live (probe), low | `parse_quote`: `not price > 0` is true for NaN. The quote is dropped either way, and only the log level differs |
| 17 | antigravity #9 | The fake stream never clears what it sent, so a resubscribe delivers nothing | Live (read), test stand-in | `conftest.py: FakeStream._serve` |

## `exec-client-first-slice` (`12f19a0`)

| # | Findings | Claim | Category | Evidence |
|---|---|---|---|---|
| 1 | opus #1 | A position in an instrument that is not loaded fails start-up | Live (read) | `execution.py: _position_status_reports` reports every US-equity position. The engine side rests on the reviewer's probe (`UNVERIFIED`) |
| 2 | codex #1, antigravity #8 | A fractional position in another symbol makes a single-instrument report raise | Live (probe) | `_position_status_reports` converts each position before the instrument filter. Probe: a 0.5-share MSFT position raises |
| 3 | opus #2 | With the default config, start-up queries only the newest 500 orders, and the test accepts either query shape | Live (read) | `_order_status_reports` adds the `OPEN` query only for a windowed request. The paper node sets a lookback, which mitigates it |
| 4 | muse #1, antigravity #5 | A windowed request with `open_only` drops open orders submitted before the window | Live (read) | The same condition. The README promises "every open order however old". The paper node's open check (since `2844c01`) takes this path |
| 5 | opus #3 | After start-up nothing re-checks an `ACCEPTED` order whose update was lost, yet the docs say reconciliation resolves it | Live (read), docs and default node | `2844c01` turns on the open check for the paper node, with no failing test. The README claim stands, and the default config has no open check |
| 6 | antigravity #1 | `_query_order` is not implemented, so NautilusTrader's in-flight check resolves orders locally as rejected or cancelled | Live (read) | The adapter defines no `_query_order`. The installed base raises `NotImplementedError`, and the default policy is `RESOLVE_LOCALLY`. That the in-flight path sends `QueryOrder` rests on the reviewer's probe (`UNVERIFIED`) |
| 7 | opus #4 | alpaca-py's stream reconnects by itself, so "no restart" is false and a drop goes unseen | Live (read) | alpaca-py 0.45.0 `_drive_run` logs "restarting connection" and reconnects. The header and README are unchanged |
| 8 | opus #5, muse #2 | Terminal events are generated without re-checking state after an `await` | Live (read) | `_submit_order` and `_reject` (double reject), `_cancel_refused` (cancel-rejected after a fill), `_refused` |
| 9 | opus #6 | `test_api_error_status_comes_from_the_http_error` checks the stand-in against itself | Live (read) | `test_upstream_contract.py`, unchanged |
| 10 | opus #7 | The README says each client has all three kinds of test; the data client has no fill test | Live (read), docs | README "Tests" |
| 11 | muse #3, antigravity #7 | `Decimal` raises `InvalidOperation`, not `ValueError`, out of `order_status_report` for a closed order | Live (probe) | Probe: a non-numeric `qty` or `filled_avg_price` still raises. The `limit_price=None` case is gone: `724246e` turns it into a `ValueError`, with no failing test |
| 12 | muse #4, antigravity #4 | A terminal fill that cannot be applied still marks the order finished, so later updates are dropped | Live (read) | `_apply`: `_finish` runs after `_fill` returns early |
| 13 | muse #5 | `names_live_host` misses a trailing-dot host | Live (probe), low | It fails closed anyway; only the error message is wrong |
| 14 | muse #6 | A trade update that is not a `TradeUpdate` is dropped silently | Live (read), low | `_on_trade_update` has no `else` |
| 15 | antigravity #2 | `_accept` records the order before its status check, so after a refused cancel it never gets `OrderAccepted` | Live (read) | `_accept`: `self._accepted.add` comes before the status test. The revert to `SUBMITTED` rests on the reviewer's probe (`UNVERIFIED`) |
| 16 | antigravity #3 | `_refused` accepts an order Alpaca holds as rejected, cancelled or expired | Live (read) | `_refused` never reads the held order's status. Its point about no re-check after the `await` is item 8 |
| 17 | antigravity #6 | `done_for_day`, `calculated` and other unmapped statuses fail start-up as open | Not a defect | Stated: an open order with a status this slice does not map fails start-up (docstring and README) |
| 18 | antigravity #9 | The start-up reconciliation test passes with filled-order or position reports broken | Live (read) | `test_startup_reconciliation_reports_orders_and_positions` asserts only the counts |
| 19 | antigravity #10 | The paper cancel test can pass when the cancel never took effect, and leaks an order | Gone | `4afa6a7` adds `aapl_left_as_found`, which cancels what is left and fails the test on it |
| 20 | antigravity #11 | `_order_report`'s skip warning names the wrong causes | Live (read), log text | Unchanged |

## `trial-ledger-gate` (`e21935c`)

| # | Findings | Claim | Category | Evidence |
|---|---|---|---|---|
| 1 | opus #2, antigravity #4 | The mean correlation can round above 1, so N-hat falls below 1 and skfolio's `ValueError` crashes `rank` | Gone | Fixed in `d5134ec` ("N-hat below 1 is raised to 1"), but its message does not report the test failing first (see above) |
| 2 | opus #4, antigravity #10 | The key, outcome and `tell` writes sit outside `record_trial`'s `try`, so a storage error leaves the trial `RUNNING` | Gone | Fixed in `d5134ec`, but whether its test failed first cannot be attributed (see above) |
| 3 | opus #3 | The only test of the DSR at N-hat cannot fail | Confirmed, entered | `n-hat-dsr-test-cannot-fail` (`6e6fed7`). No other reviewer's finding matches it |
| 4 | muse #2 | A record not in the writer's shape escapes `rank` as a bare `ValueError` or `KeyError` | Gone | Fixed in `d5134ec`, not attributable (see above) |
| 5 | antigravity #1 | A bar stamped at exactly 00:00 UTC is put on the next day, while NautilusTrader books it on the day ending there | Gone | Fixed in `d5134ec`, not attributable (see above) |
| 6 | muse #3, antigravity #5 | `daily_series` drops a session day that has no return, so T shrinks | Confirmed, new | `ba44c25`: "New tests, each failing before its fix", including `test_a_session_day_with_no_return_is_refused`. The score sheet settled antigravity #5 as a miss of `common-range-trim`; it is this defect |
| 7 | codex #2, antigravity #3 | The agreement check between records of one key leaves out `session_days`, so the result depends on record order | Confirmed, entered | Both name `ranking.py: rank` and say a rerun over other data (codex: the catalog left out of the key) gets the same key with a different calendar; the score sheet summarises them by that catalog half. This is `key-ignores-the-data`, seen from `rank`: `828c082` adds the session days and data digest to the check. The score sheet missed it on location (`ranking.py` is named, not `ledger.py`). antigravity #3's other half, `outcome` left out of the check, is live but minor: a probe mislabels an idle and a traded record of one key, the gate never passes wrongly, and `record_trial` cannot produce the pair |
| 8 | antigravity #2 | A traded record told `FAIL` wins as the pick once the trim drops its overflowing days | Confirmed, entered | This is `common-range-trim`: `828c082` removes the trim. Probe: the full sample now stops on the non-finite Sharpe ratio |
| 9 | muse #4, antigravity #12 | `run_backtest`'s docstring says `record_trial` calls it | Gone | Corrected in `828c082` |
| 10 | muse #5 | `backtest_run_config`'s error names `run_backtest` | Not a defect | Wording. The behaviour is right |
| 11 | muse #6 | `returns_matrix` reads its range from the first and last index entries, so an unsorted series is misread | Gone | `828c082` removes the trim. Probe: unsorted input now joins correctly |
| 12 | antigravity #9 | The distinct-names check passes series named `1` and `"1"` | Live (probe), minor | `returns_matrix.py: returns_matrix` gives columns `1` and `1_1`. Only a direct call can pass such names, since `rank` passes text keys. Its sorting half is item 11 |
| 13 | antigravity #6 | `_canonical`'s dict keys collide or depend on order, and NaN keys pass the NaN guard | Gone | `828c082` refuses keys that share a text form. The NaN-key half is live but minor (probe: `{nan: 1}` is hashed as `{"nan":1}`), and no configuration `trial_config` hashes holds float keys |
| 14 | antigravity #7 | `IntEnum` and `StrEnum` are keyed by value, against "enums by name" | Live (probe), minor | `ledger.py: _canonical` gives `1` and `"live"`. A plain `Enum` is refused, as stated. Only an extra attribute set on a config can reach this |
| 15 | antigravity #8 | A class-level callable config field is skipped, not refused | Gone | `828c082` removes `_is_method` and `_properties`; `trial_config` reads `vars(config)` |
| 16 | antigravity #11 | The test assertion `len(series) == len(sessions & set(returns))` cannot fail | Gone | `828c082` tightens it to `len(sessions)` |

muse #1, scored as found for `key-ignores-the-data`, makes item 7's claim as its main point.

## Live, by area

For the owning lanes. Each row gives the file, the function and the scenario.

### Adapter (`packages/canon-alpaca/`)

Order and connection paths, in `execution.py`:

* `AlpacaExecutionClient._denial`: a stop-market order with `display_qty` is sent with its full quantity, not denied (`stop-orders` 2).
* `_query_order` is missing: NautilusTrader's in-flight check resolves a `SUBMITTED` order locally as rejected or cancelled, without asking Alpaca (`exec-client-first-slice` 6).
* `_accept`: after a refused cancel, an order recorded as accepted before its status check never gets `OrderAccepted` (`exec-client-first-slice` 15).
* `_refused`: an order Alpaca holds as rejected or closed is accepted (`exec-client-first-slice` 16).
* `_submit_order`, `_reject`, `_cancel_refused`, `_refused`: a stream event that lands during an `await` is followed by a second, contradictory terminal event (`exec-client-first-slice` 8).
* `_apply`: a terminal fill that cannot be applied still finishes the order, so later updates are dropped (`exec-client-first-slice` 12).
* `_position_status_reports`: a fractional position in any other symbol makes a single-instrument report raise (`exec-client-first-slice` 2). A position in an instrument that is not loaded fails start-up (`exec-client-first-slice` 1).
* `_order_status_reports`: with no lookback, only the newest 500 orders are queried; with `open_only` and a window, an open order older than the window is dropped (`exec-client-first-slice` 3, 4).
* `order_status_report`: a non-numeric quantity or price raises `InvalidOperation` instead of skipping a closed order (`exec-client-first-slice` 11).
* The header and README say the stream is not restarted, but alpaca-py reconnects by itself; they also say reconciliation resolves a lost update, but a default node never re-checks (`exec-client-first-slice` 5, 7).
* Low: `names_live_host` misses a trailing-dot host, though it fails closed; a raw trade update is dropped silently; the skip warning names the wrong cause (`exec-client-first-slice` 13, 14, 20).

Data client, in `data.py`:

* `alpaca_bars_window`: with no start and an end before today, the window is inverted; across a DST change, a window with no start is shifted an hour or inverted (`data-client-hardening` 1, 3).
* `_on_quote`, `_on_trade`, `_on_trade_cancel`, `_on_trade_correction`: after a timed-out disconnect and a reconnect, the old stream still delivers data (`data-client-hardening` 2).
* `_one_historical_call`: an executor that refuses work leaves the request lock held for good (`data-client-hardening` 10).
* `weak_coroutine`: the client is held strongly on the stream thread during a call (`data-client-hardening` 4).
* `_disconnect_stream`: a node timeout of at most 10 s cancels the join and skips the cleanup and summary (`data-client-hardening` 15).
* Low: a tick bar request gets NautilusTrader's error text; a NaN price is logged as a one-sided quote (`data-client-hardening` 9, 16).

Tests and docs:

* `test_paper_execution.py: aapl_left_as_found`: an unguarded cancel in `finally` aborts the cleanup on a 422 and hides the test's own failure (`cancel-all` 6, 7).
* `test_paper_execution.py: run_exec_tester`: the deadline runs out silently, so TC-E41 passes for an order never accepted (`cancel-all` 8).
* `test_paper_execution.py`: `node.dispose()` does not release the one-node guard, so one failed paper test fails the next (`cancel-all` 12).
* `test_paper_execution.py: test_paper_stop_buy_is_accepted_with_its_prices`: the trigger-type check cannot fail, and the client's report is never called (`stop-orders` 5).
* `README.md`, known limits: an open stop's trigger type is stated as `DEFAULT` for every order, and no test covers a held stop (`stop-orders` 4).
* `test_data_client.py`: three assertions that cannot fail (`data-client-hardening` 5, 6, 7).
* `conftest.py: FakeStream`: it counts as running before connecting, and a resubscribe delivers nothing (`data-client-hardening` 12, 17).
* `test_execution_client.py: test_startup_reconciliation_reports_orders_and_positions`: it asserts only counts (`exec-client-first-slice` 18).
* `test_upstream_contract.py: test_api_error_status_comes_from_the_http_error`: it checks the stand-in against itself (`exec-client-first-slice` 9).
* README "Tests": the data client has no fill test (`exec-client-first-slice` 10).

### Gate (`src/canon_trading/` ledger, returns matrix, ranking)

All are minor, and none lets the gate pass a study it should reject:

* `ranking.py: rank`: `outcome` is not in the agreement check, so an idle and a traded record of one key are mislabeled, a pair `record_trial` cannot produce (`trial-ledger-gate` 7).
* `returns_matrix.py: returns_matrix`: series named `1` and `"1"` pass the distinct-names check, reachable only through a direct call (`trial-ledger-gate` 12).
* `ledger.py: _canonical`: a NaN dict key passes the NaN guard, and `IntEnum` and `StrEnum` values are keyed by value, reachable only through an extra config attribute (`trial-ledger-gate` 13, 14).

### Session and paper node (`src/canon_trading/paper_node.py`)

No defect is owned here. Two adapter items reach it: the paper node's open check takes the `open_only` windowed path, which drops older open orders (`exec-client-first-slice` 4), and the open check is what resolves a lost update during a session, which the README credits to reconciliation in general (`exec-client-first-slice` 5).

## How this was checked

* Each report was read against the case commit's code first, then against `665b7bf`, and every fix
  commit's message was read in full.
* Each entered test was checked as defined in its file at the fix commit, in a file that commit
  changes, and `tests/test_review_cases.py` passes on the changed cases.
* Probes ran offline at `665b7bf` against the locked environment (nautilus-trader 2.0.0rc8, alpaca-py
  0.45.0), outside the repository.
* No paper or live account was used.
* `UNVERIFIED` throughout: engine-side behaviours that rest on a reviewer's own probe (marked in the
  rows), and how often Alpaca produces the races in `cancel-all` 6 and 7.
* No fixed test was re-run against the code before its fix. The new entries rest on the fix messages,
  as the README's rule sets.
