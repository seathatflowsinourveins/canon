---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Extended hours for the Alpaca adapter: the submit's params, limit orders only; the overnight feed later

## Context and Problem Statement

The owner wants paper runs on weekdays around the clock ("24/5"). `AlpacaExecutionClient` (ADR 0030, ADR
0036) never sent Alpaca's `extended_hours`, so its orders could trade in the regular session only, and the
data client streams IEX only. How does a NautilusTrader order ask for the other sessions, what may such an
order be, and which feed carries each session's data?

## Decision Drivers

* ADR 0001, rule 3: upstream's way of carrying a venue order flag, not a config switch of this repo's own.
* Alpaca's own rules for an eligible order, checked before anything reaches Alpaca (ADR 0036's practice).
* Paper only (ADR 0009); what a page does not state is `UNVERIFIED` until a paper run shows it.

## Considered Options

How an order is marked: (1) the submit command's `params`; (2) order tags, as Interactive Brokers carries
`outsideRth`; (3) a setting on the client's config. The data feed: (A) a subscription names its feed in
params, one stream per feed in use; (B) the same params, one feed per client; (C) the execution part now,
the data feed once the overnight session has been observed on paper.

## Decision Outcome

### Execution: option 1

NautilusTrader orders have no field for it: Interactive Brokers' adapter at the pin calls `outsideRth` one
of the "attributes Nautilus orders cannot carry" and takes it from order tags. The guide says commands
"preserve native identifiers and params", and upstream's Bybit, Kraken and OKX adapters read their venue
order flags from the command's params (take-profit and stop-loss, `order_iv`; `leverage`, `use_ws_trade`;
`td_mode`, `px_usd`), as `ExecTesterConfig` sends `order_params`. So a strategy calls `submit_order(order,
params={"extended_hours": True})`, Alpaca's own field name. The params, not order tags as Interactive
Brokers uses for `outsideRth`, because the guide documents params as the channel and they reach the client
unparsed, where a tag is free text the strategy must encode. Alpaca: "Only limit orders with `time_in_force`
set to `day` or `gtc` orders are accepted as extended hours eligible. All other order types and TIF values
will be rejected with an error." So such a limit order goes with `extended_hours` true, any other order so
marked is denied before Alpaca with that reason, and so are a params key the client does not take and a
value that is not a boolean: this repo's choice, since upstream's adapters ignore keys they do not know and
fall back on a bad value, which here would trade in the wrong session without a word. An unmarked order is
sent as before. Alpaca's pages list no event peculiar to such an order, so its updates map as any order's.

Sessions, from Alpaca's "Orders at Alpaca" and 24/5 pages: overnight 8:00pm-4:00am ET Sunday to Friday,
pre-market 4:00am-9:30am, after-hours 4:00pm-8:00pm; the overnight session follows the NYSE holiday
calendar. Only `limit`, `day` or `gtc`, overnight. A `day` order placed overnight "will remain active
through the regular and after-hours sessions of the upcoming trading day. If unfilled, the order is canceled
at 8:00 PM ET"; an order not marked eligible is "queued for the next trading session". Assets tradable
overnight carry `overnight_tradable`, which alpaca-py 0.45.0's `Asset` model drops; a raw read of the assets
endpoint could show it, and none is built, so an order in an ineligible symbol goes to Alpaca as any other.

### Data: option (C)

Alpaca streams `v2/iex`, `v1beta1/overnight` and `v1beta1/boats`; its 24/5 page gives overnight data as
`boats` (Algo Trader Plus; the free plan's delayed history also uses it) or `overnight` (free plan), all REST
endpoints, and "Any attempt to access a data feed not available
for your subscription will result in an error during authentication." A probe with the paper pair at 15:59
ET on 2026-11-10 found: REST latest quotes answered on all three feeds, the overnight ones with the last
night's final quote; the `iex` stream delivered quotes; the `overnight` and `boats` streams authenticated
and accepted a quote subscription but, outside the overnight session, sent nothing. Which feed to build for
depends on what those streams deliver overnight, so the master orchestrator chose (C) under the owner's
delegation of 2026-11-10: this record lands the execution part, and the data client streams IEX as before.

### Consequences

* Good: an order trades outside the regular session only when its strategy asks, in the form Alpaca takes.
* Bad: no market data overnight through the node yet; the client does not check `overnight_tradable`.

### Confirmation

Offline, in `scripts/ci` (`test_execution_client.py`): an eligible limit order sent with `extended_hours`
true and filled; `extended_hours` false sent as an unmarked order; a market and a stop order so marked, a
non-boolean value and an unknown key, each denied before Alpaca. Paper, after-hours, 16:02 ET on 2026-11-10,
`mise run paper:test` (`test_paper_execution.py`): a DAY limit buy of 1 AAPL at $1.00 marked eligible,
accepted, held by Alpaca with `extended_hours` true, canceled; a buy priced 0.2% through the IEX ask filled
and its share was sold back 0.2% through the bid, both marked eligible, the account left flat. The fill test
runs only in pre-market or after-hours, from Alpaca's calendar, and skips without an IEX quote from the last
minute, as at 16:16 ET; the cleanup closes a position outside the regular session with an eligible limit
order. Each paper test has a 60 s limit, runs under the paper lock and places nothing while the account
holds an AAPL order or position.

The data part is decided in [ADR 0055](0055-alpaca-data-feed-named-in-params.md) (2026-11-10): ADR
0038's option (B), the feed named in a subscription's params, one stream per client; on paper that
night `boats` quotes reached a strategy through a node in the overnight session.

`UNVERIFIED`: an eligible order in pre-market and overnight (only after-hours ran); a modify of an eligible
order (whether the replacement keeps `extended_hours`); why the `boats` stream and latest quote answered a
paper pair whose plan the page does not say includes it; a `day` order across
8:00 PM ET and the update its cancel sends; an order in a symbol not `overnight_tradable`; what the
`overnight` and `boats` streams deliver in the overnight session, and how late.

## Evidence

Read 2026-11-10; "raw": fetched with `curl` and read, not summarised.

* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading": the sessions, the
  eligibility rule, `extended_hours` (fetched, raw).
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md: the overnight session, its order types
  and times in force, `day` orders across sessions, `overnight_tradable`, the `boats` and `overnight` feeds
  (fetched, raw, read whole).
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md: the stream URLs per feed, the
  authentication error for a feed not subscribed (fetched, raw).
* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: commands keep their params
  (fetched, raw). At NautilusTrader `d53849b216e2`, the pin:
  `crates/adapters/interactive_brokers/src/execution/commands.rs` and `transform/tags.rs` (`outsideRth`
  through `IBOrderTags`); `bybit/src/execution.rs`, `kraken/src/execution/spot.rs`, `okx/src/execution.rs`
  (params read) (fetched, raw).
* NautilusTrader `v2.0.0rc8` and alpaca-py 0.45.0 as installed: `Strategy.submit_order(..., params=None)`,
  `SubmitOrder.params`, `ExecTesterConfig.order_params`, `LimitOrderRequest.extended_hours`, `DataFeed`
  (`BOATS`, `OVERNIGHT`), `StockDataStream`'s per-feed URL, `Asset` without `overnight_tradable`, `Calendar`
  with `session_open` and `session_close` (bundled with the pinned packages; read).
* Paper, 2026-11-10, logs kept outside the repo: the feed probe at 15:59 ET (a temporary test, not kept);
  the two extended-hours tests at 16:02 ET and again at 16:16 ET (ran).
* Second reader, 2026-11-10 (`evidence-verifier`): the decision stands. Corrected above: what checking
  `overnight_tradable` would take; an unsourced consequence; refusing unknown keys as this repo's choice;
  why params and not tags; the `boats` plan question.