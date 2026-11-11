---
status: proposed
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation (ADR 0074); repo owner ratifies
review-by: 2026-12-11
---

# Alpaca's whole-market minute bars and trading statuses reach NautilusTrader through the data client: the wildcard is a subscription param, each status maps to an `InstrumentStatus`, and LULD bands stay out

## Context and Problem Statement

The mover node (ADR 0083) ranks the whole market from its own stream: minute bars, the bars Alpaca
updates after a late trade, and trading statuses for every loaded instrument, through the adapter's data
client into NautilusTrader's engine. The client streams quotes and trades only. ADR 0083's gap 4 says the
adapter implements no `_subscribe_instrument_status`, and that gap stops the work until a record states
how halts reach the engine. Alpaca subscribes the whole market with `"*"`; NautilusTrader's subscribe
commands name instruments or bar types, one or a list, and carry adapter keys in `params`.

## Decision Drivers

* NautilusTrader's contract is what a strategy subscribed to: the engine caches and publishes every bar
  and status a client hands it, so the client hands it only what was subscribed.
* Alpaca's contract for the whole market is `"*"`, shown for `bars` and `statuses` on its page.
  alpaca-py takes `"*"` for `subscribe_bars`, `subscribe_updated_bars` and `subscribe_trading_statuses`,
  dispatches a symbol with no handler of its own to the `"*"` handler, and sends every handler map whole
  on each subscribe, so thousands of listed symbols would be sent again on every later subscribe.
* Nothing invented: the status mapping is a lookup from Alpaca's published codes to NautilusTrader's own
  `MarketStatusAction` members, named by their upstream docstrings; no adapter at the pin maps CTA or UTP
  codes, so there is none to take.

## Considered Options

1. The command lists every bar type or instrument; the client sends those symbols.
2. The command lists what the strategy wants and adds `params={"wildcard": True}`: the client subscribes
   `"*"` once per channel and hands the engine data only for the listed symbols.
3. The client hands the engine data for every loaded instrument whatever the command lists.

## Decision Outcome

Chosen: **option 2**. It keeps NautilusTrader's meaning (only what was subscribed reaches the engine)
and Alpaca's (one `"*"` per channel). Option 1 resends the whole list with each subscribe; option 3
delivers data nobody subscribed to.

* `_subscribe_bars` takes `<SYMBOL>.ALPACA-1-MINUTE-LAST-EXTERNAL`, Alpaca's stream bar, and refuses
  any other bar type with `Cannot subscribe:`. A bar is parsed as a requested one is (timestamped at its
  open, prices rounded as `Instrument.make_price` rounds, ADR 0055), stamped on arrival.
* `params={"updated_bars": True}` also subscribes Alpaca's `updatedBars` and hands each to the engine
  as a bar of the same type. Off by default: a strategy then sees two bars for one minute, which
  NautilusTrader's sequence check lets through, as it refuses only an earlier `ts_event` or `ts_init`.
* `_subscribe_instrument_status` hands the engine one `InstrumentStatus` per Alpaca status, with
  `trading_event` the status code and message, and `reason` the reason code and message:

  | Alpaca status code | `MarketStatusAction` | Flags |
  |---|---|---|
  | `2` (CTA), `H` (UTP): trading halt | `HALT` | not trading |
  | `P` (UTP): volatility trading pause | `PAUSE` | not trading |
  | `3` (CTA), `T` (UTP): resume | `TRADING` | trading |
  | `Q` (UTP): quotation resumption | `QUOTING` | quoting, not trading |
  | `5` (CTA): price indication | `NEW_PRICE_INDICATION` | none |
  | `E` (CTA): short sale restriction | `SHORT_SELL_RESTRICTION_CHANGE` | restricted |
  | any other (imbalances, range indications, `F`) | `NONE` | none |

* An unsubscribe removes its symbols; the `"*"` subscription is unsubscribed when no listed symbol is
  left on its channel. Bars and statuses join quotes and trades in everything the client already does
  per channel: one stream on one feed, the resend after a connect or reconnect, the mute limit.
* Gap 4's other half, Limit Up-Limit Down bands, stays open: alpaca-py has no subscription for them.
* Trades are not taken by wildcard. The first ranking reads wildcard minute bars, whose volume Alpaca
  builds by the tape's rules, so nothing here masks prints. Trades are subscribed per symbol, for the
  top names and holdings, and a later change filters them before a `TradeTick` exists: a data-client
  field chooses prints eligible to update the last price or all prints, by a table read at start from
  Alpaca's conditions endpoint, never written from memory, with the dropped prints counted and logged.
  Volume comes from bars, never from filtered ticks. Today `parse_trade` drops the condition codes.

### Consequences

* Good: a strategy or actor gets the whole market's minute bars and halts as NautilusTrader types,
  through its usual subscribe calls, on one connection.
* Bad: `wildcard` and `updated_bars` are this adapter's keys; another adapter does not read them.
* Limit, until the ranking record needs more: NautilusTrader's `Bar` carries no trade count and no VWAP,
  so a bar's dollar volume is its volume times its typical price, an approximation; trade acceleration,
  the ranking's tie-break, is unknown and by the practice breaks no tie. Alpaca's trade count and VWAP
  beside each bar, as custom data, is a later item.
* Bad: whether the paper data subscription serves `statuses` is `UNVERIFIED` (Alpaca's page says
  production statuses need sales); a `"*"` bars stream's volume and memory are measured on paper, not
  argued.

### Confirmation

Offline tests in `scripts/ci` (`test_data_client.py`): bars reach a strategy from a stream subscription
and from a `"*"` one, only for listed symbols; a bar type other than one-minute last is refused; updated
bars only when asked; each status code row above; an unsubscribe of the last listed symbol unsubscribes
`"*"`; a reconnect sends the wildcard again. Paper, `mise run paper:test`: a `"*"` bars subscription on
`sip` reaches a strategy, with the bars per minute, the instruments seen and the statuses received named.

## Evidence

Read 2026-11-11.

* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md: the `bars`, `updatedBars` and
  `statuses` channels and schemas; `"*"` shown for `bars` and `statuses`; the CTA and UTP status and reason
  code tables; production statuses through sales (fetched, summarised by the fetch tool).
* alpaca-py `c42add7c0a70`, `alpaca/data/live/stock.py` 99-145 (`subscribe_bars`,
  `subscribe_updated_bars`, `subscribe_trading_statuses`, each taking `"*"`) and
  `alpaca/data/live/websocket.py` 34-66 (bars, updated bars and statuses count as market data) and 385-391
  (`"*"` handler for a symbol with none of its own); `alpaca/data/models/trades.py` 82-101 (`TradingStatus`)
  (resolved, read in the pinned source cache).
* NautilusTrader `d53849b216e2`: `docs/developer_guide/python_adapters.md` (the `bars` and
  `instrument_status` hooks, command lists, `InstrumentStatus` through `_handle_data`);
  `crates/data/src/engine/dispatch.rs` 131 and 446-495 (bars cached and published without a subscription
  check; the sequence check); `crates/model/src/enums.rs` 1035-1068 (`MarketStatusAction`) (resolved,
  read in the pinned source cache).
