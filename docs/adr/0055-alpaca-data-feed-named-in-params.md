---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# The Alpaca data feed is named in a subscription's or bars request's params; one stream per client, no fallback

## Context and Problem Statement

ADR 0038 left the data client on IEX (its option C) until the overnight session had been observed on
paper. It now has been: after 16:00 New York `iex` streamed nothing, and in the overnight session `boats`
streamed quotes, trades and minute bars while `overnight` streamed quotes only. How does a strategy ask
for a feed, what does the client do with two different asks, and what happens when Alpaca does not serve
the feed asked for?

## Decision Drivers

* ADR 0001, rule 3: upstream's channel for an adapter-specific option, not a config switch of this repo's.
* Nothing that exists changes: a subscription or request that names no feed gets IEX, as before.
* No silent fallback (AGENTS.md rule 5): a feed asked for and not served fails, naming the feed.
* Alpaca allows most users one stream connection per endpoint (406 "connection limit exceeded").

## Considered Options

From ADR 0038: (A) a subscription names its feed in params, one stream per feed in use; (B) the same
params, one feed per client, the feed of its first subscription; (C) IEX only, until observed. Also: (D) a
feed set in the data client's config.

## Decision Outcome

Chosen: **option B**, by the master orchestrator under the owner's delegation of 2026-11-10.

**The channel.** NautilusTrader's `subscribe_quotes`, `subscribe_trades` and `request_bars` take `params`,
the command a client hook receives carries them (`params`, a dict or `None`), and the adapter guide's data
hooks section says "Nested params are copied". Upstream's adapters choose a venue channel there:
Databento's `params={"schema": "mbp-1"}` on `subscribe_quotes`, OKX's `params={"rpi": True}` for its
`books-rpi` channel; the Data Testing Spec's TC-D71 passes "adapter-specific `subscribe_params`" through
opaquely. So a strategy calls `subscribe_quotes(id, params={"feed": "boats"})`, Alpaca's own parameter
name, and the same on `subscribe_trades` and `request_bars`. Only `feed` is read, as Databento's client
reads only its `schema` key: the dict is shared, since the engine's own keys ride in it (ADR 0040's
`update_catalog`; the engine added `start_ns` to a subscription's params, observed offline). A name Alpaca
does not document for the call is refused before anything is sent, as Databento refuses a schema outside
the call's list ("Invalid `schema` ... Must be one of"): for a subscription, the real-time page's and
alpaca-py's `iex`, `sip`, `delayed_sip`, `boats`, `overnight`; for bars, the bars reference's `iex`,
`otc`, `sip`, `boats`. A value that is not a string is refused.

**One stream.** The client builds its stream at its first subscription, on that subscription's feed, and
refuses a later subscription whose feed differs, naming both; naming no feed means IEX. A bars request is
a REST call and takes its own feed each time. A has not been taken because nothing needs it: a session
window starts a node per session (ADR 0044), so no window spans overnight into the regular session. Alpaca
limits connections per endpoint ("in many subscriptions ... this limit is 1", 406), and each feed is its
own endpoint, so a second feed's stream may well be allowed; that is UNVERIFIED and not relied on.
NautilusTrader's data engine passes the client only the first subscription to an instrument's quotes or
trades (observed offline: a second strategy's `subscribe_quotes` for the same instrument, naming another
feed, never reached the client), so the refusal covers a different instrument or data type; a second
strategy asking for the same one gets the first one's feed, unrefused.

**No fallback.** Alpaca refuses a feed outside the account's plan "during authentication" (409
"insufficient subscription"); alpaca-py 0.45.0 then ends the stream and reports why only to `logging`,
and on any other refusal retries without ending. So the client passes alpaca-py's stream warnings and
errors from its stream's thread to the node's log, naming the feed, with a `logging.Handler`, the way
Python's logging HOWTO has an application receive a library's records; when the stream ends by itself it
logs an error naming the feed and refuses later subscriptions until the client reconnects. It never
tries another feed. This departs from the request in one respect: the subscription has already returned
when authentication fails, since alpaca-py connects only after its first subscription and does not
expose the outcome, so the failure is loud in the log, not raised by the subscribe hook.

**Not an entitlement.** Alpaca's pages disagree with the observation. "Paper Trading": a Paper Only
account "is only entitled to receive and make use of IEX market data". The Trading API's 24/5 page gives
`boats` latest quotes and trades to Algo Trader Plus, and the free plan `overnight` latest data (quotes
"real-time indicative", trades "15-minute delayed") and `boats` history, "all 15-minute delayed"; the
general 24/5 page offers its Blue Ocean feed "for a monthly fee", trades "delayed by 15 minutes". Yet the
paper pair was served real-time `boats` quotes, trades and minute bars on the stream, current `boats`
latest quotes and bars over REST, and at 20:46 New York it authenticated on `v2/sip` too. Why is
UNVERIFIED; this is an observation of one night, not a right relied on, and a feed no longer served
fails as above.

**Pre-market and after-hours** (04:00-09:30 and 16:00-20:00 New York): at 16:46 the `iex`, `overnight`
and `boats` streams each sent no quote or trade in 45 s on four symbols. IEX did trade after 16:00 (REST
latest bars at 16:17 and 16:50), so this is one short window, not proof that no feed serves those hours;
`sip` and `delayed_sip` were not probed then. In pre-market on 2026-11-11 `iex` and `boats` delivered
nothing from 04:13 to 04:58; from 08:23 the sip feed delivered and `iex` barely (Confirmation), so
pre-market and after-hours name `sip`, one setting per part, with no fallback to another feed (ADR 0044,
its rows).

D was not taken: a per-call option belongs in params, and a config feed would be a field of this repo's.

### Consequences

* Good: overnight quotes reach a strategy through the node, in the form Alpaca takes; nothing existing
  changes.
* Bad: a node has one feed for its stream, so mixed feeds need two nodes; a refused feed shows only in
  the log, after the subscription returned.

### Confirmation

Offline, in `scripts/ci` (`test_data_client.py`): no params gives an IEX stream and IEX bars; `boats` named
through a real node reaches the stream and the bars request; a second subscription on another feed, or
none, is refused, naming both; a name not documented for the call or a value not a string is refused
before anything is sent, `None` included (only a missing key is the default); a stream refused at
authentication, as alpaca-py logs and ends it on 409, is logged naming the feed and the reason, and
later subscriptions are refused, from the moment alpaca-py's `run()` returns, while the stream's thread
still winds down; the engine passes on only the first subscription to an instrument, and passes an
unsubscribe the params its subscription had; an unsubscribe naming a feed Alpaca does not stream, or
another feed than the stream's, sends nothing and is logged, as the adapter guide has unsubscribe hooks
tolerate members whose subscription failed; each relayed log line names the stream's thread, by a
name unique in the process; a subscribe made from another thread while alpaca-py sends its first
subscribe message, which alpaca-py 0.45.0 records and does not send (a narrow start-up race: the three
statements run with no lock around them, as on upstream's master; reproduced offline by holding that
send, on alpaca-py's real stream on an in-memory socket, in a contract test that fails if upstream
changes the path; not observed on a live stream; the usual pattern, subscribe and then run, cannot meet
it), is sent once the stream's first quote or trade arrives, when
the client sends its subscriptions again, if a subscribe was made after the stream started; a start
whose sent subscriptions stay quiet and an unsubscribe lost in the same window (alpaca-py does not
send it either, and a subscribe does not undo it) are not covered; a reconnect alpaca-py makes by itself,
which it announces with a warning before connecting, is covered the same way (2026-11-11, below).

What a caller sees when the stream fails: a stream that ends by itself is not restarted (alpaca-py
reconnects and resubscribes by itself after a websocket error and after any error while connecting, and
keeps the connection after an error once connected; it ends a run only on 409, which a restart on the
same feed would meet again), its end is logged as an error naming the feed, and later
subscriptions are refused until the client reconnects. A stream alpaca-py never gets connected (406,
for one) is retried by alpaca-py without end: the node logs its retry warnings naming the feed and
thread, and no data arrives; no bound is set, since its connection state is private to alpaca-py.
NautilusTrader v2.0.0rc8 has no channel for a data client to tell the engine its data has stopped, so
neither reaches the engine as more than the log.

A stream that connects and then sends nothing (2026-11-11, `AlpacaDataClientConfig`). alpaca-py 0.45.0
takes `data_timeout`, in seconds, `None` (off) by default: only market data resets its clock, and on
expiry it logs "no data received ... reconnecting", closes the socket, waits a backoff of 0.5 to 1 s
that doubles to 30 s while no data comes, and connects again, sending every handler map whole, so each
subscription is sent again. The client sets it from `stream_data_timeout_secs`, 900 s by default, above
the longest silence seen on a feed that then delivered (513 s: `iex` with QQQ alone, pre-market
2026-11-11, one trade and no quote in 600 s); a window's parts are far busier (SPY on `iex` in the regular
session, hundreds of quotes a minute; on `boats` overnight, 1,552 to 3,700 quotes a window, ADR 0044). Its
reconnect reaches the log as a warning naming the feed, so a quiet feed fails nothing. As alpaca-py
reconnects without end, the client counts the silence too: no quote or trade for
`stream_mute_limit_secs` (3,600 s by default, and longer than the data timeout) while it holds a
subscription, and it stops the stream, logs an error beginning "Alpaca stream on feed <feed> went mute",
which a session window reads as its stream's error, and refuses later subscriptions until it reconnects,
the same say-so as a stream that ended, and as upstream's Databento feed handler gives up reconnecting
after `reconnect_timeout_mins` with an error. The limit counts from the stream's start, so a stream
alpaca-py never gets connected meets it too. It cannot tell a dead stream from a feed with nothing to
send, so a node spanning a part no feed serves sets it longer than that part, or `None`; it outlasts
every window this repository runs (an hour at most), so it matters to longer nodes. A subscribe made
after alpaca-py's reconnect warning and before the next message is sent again on that message, as at
the stream's start. A client
forgets its subscriptions at disconnect while the engine keeps its own record of them until a reset, but
a client belongs to one node run and a stopped node refuses to run again, so none is owed to a later
connect. Offline, in `scripts/ci` (`test_data_client.py`): the stream is built with the configured
`data_timeout`, or the default from a plain `DataClientConfig`, or none; the config refuses a bound
that is not a positive number of seconds, a limit not longer than the timeout and any other keyword;
on alpaca-py's real stream over an in-memory socket, a mute connection is closed and made again with
the subscription sent again, a later quote reaches the cache, and the reconnect is logged as a warning;
a mute stand-in past the limit is stopped, its error names the feed and the limit, and a later
subscription is refused; quotes inside the limit keep it running and the count starts from the last one;
silence with nothing subscribed, before a later subscription, and after a disconnect fails nothing; a
subscribe held in the window of a `data_timeout` reconnect on the real stream is sent on the next quote;
a bound too large for a float is refused by name; a stopped node cannot run
again (fails if upstream allows it). UNVERIFIED: how long a healthy feed can stay silent beyond the one
10-minute sample; a mute connection on a live stream.

Paper, `mise run paper:test -- -s -k "test_paper_boats_quotes_reach_the_strategy or
test_paper_default_feed_is_iex or test_paper_asset_and_daily_bars_reach_the_strategy"`, 2026-11-10 20:49
New York, overnight session, exit 0, 3 passed: through a node, `subscribe_quotes(AAPL, params={"feed":
"boats"})` authenticated on `v1beta1/boats` and 19 quotes reached the strategy in 30 s, none crossed; no
params authenticated on `v2/iex` (no quote after 16:00) (`test_paper_feed.py`); daily bars with no params,
as before (`test_paper_integration.py`). The same three passed again at 21:28 on the final code, exit 0,
with 2 `boats` quotes in 30 s. Shaped on TC-D20 and TC-D71 with a small strategy: v2.0.0rc8's
Python `DataTesterConfig` takes no `subscribe_params`.

Paper, 2026-11-11, pre-market, a read-only probe under `mise run paper:test` (account A, no order),
exit 0: the sip feed authenticated on paper on 2026-11-10, and authenticated and delivered on 2026-11-11. On `v2/sip`,
08:23:05 to 08:26:05 New York: SPY 1,167 quotes, 240 trades, 3 bars; QQQ 7,292 quotes, 796 trades, 3
bars. On `v2/iex`, 08:26 to 08:36: SPY about 2,010 quotes, 5 trades, 2 bars; QQQ 0 quotes, 1 trade, 0
bars; IEX's REST bars matched the stream. This replaces, for pre-market, the earlier observation that no
stream delivered there, which held for 04:13 to 04:58 (ADR 0044). The same day a pre-market window on `sip` (account B, 08:42 to 09:08)
was served 525 `sip` one-minute bars over REST, from the prior day's 15:35 to its start, and streamed
18,388 SPY quotes and 1,571 trades (ADR 0044, its pre-market row).

Prices finer than the instrument's precision (2026-11-11). The consolidated tape carries trade and bar
prices finer than the adapter's four decimals (the study's loader met a SPY bar high of 747.086488), and
sub-dollar names often do. Until this date the client refused them: a live trade or quote was dropped with
an error line per print that no session window read, and a bars or quotes request holding one failed
whole. Now a trade or bar price is rounded half to even, as upstream rounds a venue's value
(`Instrument.make_price`; the Deribit and Bybit parsers' `Price::from_decimal_dp`): 747.086488 becomes
747.0865 and 0.12345 becomes 0.1234. Each rounding is named on the stream (the first per symbol, the count
at each power of ten, the total at unsubscribe or disconnect) as a warning beginning `Alpaca stream on
feed `, which a session window keeps and does not fail on, and per bars request in one warning. A quote
that fine is dropped and counted the same way, not rounded, since a lit book quotes at the tick; a quotes
request drops it and keeps the rest. A price that is not a finite number, or a positive one that
rounds to zero, is still refused. This corrects
ADR 0030's "market data is never rounded". Offline, written failing first (`test_parsing.py`,
`test_data_client.py`): 747.086488, 0.12345, 0.98765 and two midpoints rounded half to even; a live trade
rounded and counted while the stream goes on; a live quote dropped and named; a bars request answered
with the bar rounded; a quotes request keeping the rest; each kind's count logged at its own powers
of ten, and a quote unsubscribe logging only the quotes' total; a price rounding to zero refused. `UNVERIFIED`: how often a sip quote is finer than
four decimals (none seen); the rounding on a live sip stream. Paper, account A, 2026-11-11, under
`mise run paper:test`, exit 0 each: at 10:20 New York `sip` streamed 2,297 AAPL trades and 1,387 quotes in
60 s through a node, none dropped and none finer than four decimals, so none rounded; at 10:28, with the
node's log shown to be captured, 2,036 trades, 1,002 quotes, none rounded; at 10:30 a request for three
days of AAPL one-minute `sip` bars was answered with 2,001 bars, none rounded. At 11:20
(`test_paper_sip_bars.py`) SPY `sip` bars, one-minute over three days and daily over a year, were answered
with 2,264 and 251 bars, none finer than four decimals; at 11:27 a one-off request for SPY `sip`
one-minute bars from 2025-11-28 to 2026-01-01 was answered with 20,146 bars, none rounded. So no paper run has yet met a print the client
rounds: where the study's 747.086488 came from (an adjusted bar, or another window) is `UNVERIFIED`.

`UNVERIFIED`: why the paper pair is served `boats` and `sip`; a second stream on another feed; a refused
feed's server message on paper (none tried was refused; 409 is documented); `sip` in after-hours, which
no stream has been probed for on it.

## Evidence

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised.

* 2026-11-11, an evidence refresh with no change of decision: two pre-market IEX windows and a read-only
  probe of `iex` and `boats` under `paper:test`, 04:53 New York, received no SPY quote, trade or bar;
  IEX's REST bars began at 08:15 in 2026-11-10's pre-market. The counts and sources are in ADR 0044's
  pre-market row and Evidence (run).

* NautilusTrader at the pin, `d53849b216e2`: `crates/common/src/python/actor.rs` (`params=None` on
  `subscribe_quotes`, `subscribe_trades`, `request_bars`), `crates/common/src/python/commands.rs` (the
  commands' `params` getter), `docs/developer_guide/python_adapters.md` ("Nested params are copied", of
  the data hooks), `docs/developer_guide/spec_data_testing.md` (TC-D71, TC-D72),
  `docs/integrations/databento.md` and `crates/adapters/databento/src/data.rs` (`params={"schema": ...}`,
  only `schema` read; "Invalid `schema` ... Must be one of"), `docs/integrations/okx.md` (`rpi`,
  `books-rpi`) (fetched, raw). The installed stubs: `params: dict | None` on `SubscribeQuotes`,
  `SubscribeTrades`, `RequestBars` (bundled).
* Alpaca, fetched raw: https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md (the five
  stream feeds; the error "during authentication"); https://docs.alpaca.markets/us/docs/streaming-market-data.md
  (connections "to a single endpoint", "in many subscriptions ... this limit is 1", 406; 409);
  https://docs.alpaca.markets/us/reference/stockbars.md (`feed` enum, default `sip`);
  https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md and
  https://docs.alpaca.markets/us/docs/245-trading.md (plans and feeds overnight);
  https://docs.alpaca.markets/us/docs/paper-trading.md (IEX only for a Paper Only account).
* https://docs.python.org/3/howto/logging.html, "Configuring Logging for a Library": an application's
  handlers receive a library's logging calls (fetched, raw).
* alpaca-py 0.45.0 as installed: `DataFeed`; `StockDataStream(feed=...)`, `_FEED_VERSIONS`; `_auth`
  raises the server's message; `_drive_run` ends the run on "insufficient subscription", else retries,
  logging through `logging` (bundled; read).
* Offline, 2026-11-10: two strategies subscribing AAPL quotes on one node, naming `boats` and `sip`; the
  client's hook was called once, with `{'feed': 'boats', 'start_ns': None}` (ran).
* Paper, 2026-11-10, a feed probe with alpaca-py, logs kept outside the repository: 15:59 ET (`iex`
  delivered; `overnight` and `boats` nothing), 16:46 ET (no feed delivered in 45 s), 20:01 ET (90 s:
  `overnight` quotes only; `boats` quotes, trades and minute bars, and current REST data) (ran). 20:46 ET
  through a node, a first `test_paper_feed.py` with a `sip` subscription: it authenticated, no quote (ran).
* NautilusTrader's Python adapter guide, read 2026-11-10 (https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md,
  fetched, raw): "Unsubscribe hooks must therefore tolerate members whose subscription failed".
* Offline, 2026-11-10: a strategy that subscribed AAPL quotes on `boats` and called `unsubscribe_quotes`
  with no params reached the client's hook with `{'feed': 'boats'}` (ran).
* NautilusTrader v2.0.0rc8, `nautilus_trader/live/clients.py` as installed: `is_connected` is a read-only
  property of the client runtime, and the client has no other connection or health method (bundled;
  read). `crates/adapters/databento/src/data.rs` at the tag: when its feed handler gives up reconnecting
  (`DatabentoMessage::Error`, "Reconnection timeout after N minutes" in `live.rs`), the client logs
  "Feed handler error" and does nothing more (fetched, raw).
* alpaca-py 0.45.0, the latest release (https://github.com/alpacahq/alpaca-py/releases, read
  2026-11-10): `_drive_run` sends the handler maps, then sets `_running`; `_subscribe` sends only while
  `_running`; a subscribe in between was recorded and never sent, run offline on the real
  `StockDataStream` (ran). No upstream issue found for it (searched 2026-11-10). 2026-11-11:
  `_drive_run` and `_subscribe` on alpaca-py's `master`
  (https://raw.githubusercontent.com/alpacahq/alpaca-py/master/alpaca/data/live/websocket.py) are the
  same as 0.45.0's (fetched, compared); described above as a narrow start-up race, which the offline run
  reaches only by holding the first send.
* 2026-11-11, the silent stream: alpaca-py at the pin, read in the local copy at the pin,
  https://github.com/alpacahq/alpaca-py/blob/c42add7c0a7026e2ead3ed64e76f4def2b99fda4/alpaca/data/live/websocket.py
  (`DataStream.__init__` `data_timeout` lines 91-118; `_is_market_data` 52-76; `_consume` 264-299;
  `_drive_run` 519-535; `_send_subscribe_msg` 412-424), `alpaca/data/live/stock.py` lines 35-72 and
  `alpaca/common/utils.py` `reconnect_delay` lines 47-76; the installed 0.45.0 files are the same (compared).
  NautilusTrader at the pin, read in the local copy:
  https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/adapters/databento/src/live.rs
  (`reconnect_timeout_mins`, "Giving up reconnection"), `crates/adapters/hyperliquid/src/config.rs`
  (`stale_stream_receive_timeout_secs`, "above the instrument's expected quiet period"),
  `crates/data/src/client.rs` (`has_active_subscription`, `clear_subscription_state`),
  `crates/data/src/engine/mod.rs` (`reset` clears it, `connect` and `disconnect` do not) and
  `docs/developer_guide/python_adapters.md` ("Define adapter configuration"; "A client instance and its
  bound provider belong to one node run"). Offline: a stopped `LiveNode` raised "LiveNode cannot be run
  from state Stopped; build a new node to run again" (ran). Paper, pre-market 2026-11-11 08:23 to 08:36
  New York, a read-only probe under `paper:test`: `sip` 180 s then `iex` 600 s on SPY and QQQ; `iex` QQQ
  one trade at 08:34:39 and no quote, `iex` SPY 245 to 1,104 quotes per five minutes, `sip` hundreds of
  quotes a minute on each (counts kept outside the repository; ran).
* 2026-11-11, prices finer than the precision: NautilusTrader at the pin, read in the local copy,
  https://github.com/nautechsystems/nautilus_trader/blob/d53849b216e20f2c122ca01b4dd0da377638b186/crates/model/src/instruments/mod.rs
  (`try_make_price_from_decimal` lines 372-377, `MidpointNearestEven`; `try_make_price` 391-395, from the
  float's decimal string; `try_normalize_price` 410, refusing a price off the grid, not in the Python
  bindings, searched), `crates/model/src/types/price.rs` (`from_decimal_dp` 508-519, "banker's
  rounding"), `crates/adapters/deribit/src/websocket/parse.rs` (`parse_trade_msg` line 86, quotes 508-509
  and 541-542, chart bars 741: `Price::from_decimal_dp(.., instrument.price_precision())`) and
  `crates/adapters/bybit/src/common/parse.rs` (`parse_trade_tick` 912, `parse_kline_bar` 1049-1052,
  `parse_price_with_precision` 1361-1369). Offline at the pin, `Equity.make_price` at precision 4:
  747.086488 to 747.0865, 1.00025 to 1.0002, 1.00015 to 1.0002, 0.12345 to 0.1234 (ran).
* Second reader, 2026-11-10 (`evidence-verifier`): B stands; corrected above: Databento's refusal cited
  from source, the data hooks' sentence quoted, the after-hours claim narrowed, the per-endpoint limit.
