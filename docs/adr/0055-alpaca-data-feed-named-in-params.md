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
`sip` and `delayed_sip` were not probed then, and pre-market not at all (UNVERIFIED). Nothing is built to
work around it.

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
name unique in the process; a subscribe made while alpaca-py connects, which alpaca-py 0.45.0 records
and never sends (the contract test runs this on alpaca-py's real stream, on an in-memory socket, and
fails once upstream closes the window), is sent once the stream's first quote or trade arrives, when
the client sends its subscriptions again, if a subscribe was made after the stream started; a start
whose sent subscriptions stay quiet, a reconnect alpaca-py makes by itself, and an unsubscribe lost in
the same window (alpaca-py does not send it either, and a subscribe does not undo it) are not covered.

What a caller sees when the stream fails: a stream that ends by itself is not restarted (alpaca-py
reconnects and resubscribes by itself after a websocket error and after any error while connecting, and
keeps the connection after an error once connected; it ends a run only on 409, which a restart on the
same feed would meet again), its end is logged as an error naming the feed, and later
subscriptions are refused until the client reconnects. A stream alpaca-py never gets connected (406,
for one) is retried by alpaca-py without end: the node logs its retry warnings naming the feed and
thread, and no data arrives; no bound is set, since its connection state is private to alpaca-py.
NautilusTrader v2.0.0rc8 has no channel for a data client to tell the engine its data has stopped, so
neither reaches the engine as more than the log.

Paper, `mise run paper:test -- -s -k "test_paper_boats_quotes_reach_the_strategy or
test_paper_default_feed_is_iex or test_paper_asset_and_daily_bars_reach_the_strategy"`, 2026-11-10 20:49
New York, overnight session, exit 0, 3 passed: through a node, `subscribe_quotes(AAPL, params={"feed":
"boats"})` authenticated on `v1beta1/boats` and 19 quotes reached the strategy in 30 s, none crossed; no
params authenticated on `v2/iex` (no quote after 16:00) (`test_paper_feed.py`); daily bars with no params,
as before (`test_paper_integration.py`). The same three passed again at 21:28 on the final code, exit 0,
with 2 `boats` quotes in 30 s. Shaped on TC-D20 and TC-D71 with a small strategy: v2.0.0rc8's
Python `DataTesterConfig` takes no `subscribe_params`.

`UNVERIFIED`: why the paper pair is served `boats` and `sip`; a second stream on another feed; a refused
feed's server message on paper (none tried was refused; 409 is documented); pre-market data on any feed.

## Evidence

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised.

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
  `StockDataStream` (ran). No upstream issue found for it (searched 2026-11-10).
* Second reader, 2026-11-10 (`evidence-verifier`): B stands; corrected above: Databento's refusal cited
  from source, the data hooks' sentence quoted, the after-hours claim narrowed, the per-endpoint limit.
