---
status: proposed
date: 2026-11-12
decision-makers: the owner for the bus contract and the practices (2026-11-11); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2026-12-12
---

# The movers ranking's actor: a `DataActor` that publishes ADR 0087's score each minute as the signal `movers-rank`, and publishes nothing when an input is missing, stale or invalid

## Context and Problem Statement

Split from ADR 0087 on 2026-11-12, with its text moved unchanged. ADR 0087 decides the score, its inputs
and rows; this record decides the actor that computes it in the node and the bus contract between the
actor and the strategy that reads it (M1b, ADR 0088). The ranking must reach a strategy in the same node,
be computed from what the node's data client hands the engine, and never offer an entry from a guess.

## Decision Drivers

* The owner's requirement of 2026-11-11, restated. The ranking travels on the message bus through
  upstream's signal API at the pin: `DataActor.publish_signal(name, value, ts_event)`, read by
  `subscribe_signal(name)` and `on_signal(signal)` on a `Strategy` or a `DataActor`. The name is
  `movers-rank`; the value is one string of at most 20 entries, best first, each `ID.ALPACA=move`,
  joined by `;`; `ts_event` is the time the ranking was computed. The reader refuses entries older than
  90 s, which is M1b's row, UNCALIBRATED, and not enforced by the publisher.
* The owner's practices of 2026-11-11, restated in ADR 0087's drivers, bind the actor too: one
  generation per interval, and no entry from a missing, stale or invalid ranking.

## Considered Options

1. **Custom data on the bus** (`publish_data(DataType, CustomData)`), upstream's documented form for
   structured payloads.
2. **A signal** whose value is one string in a shared codec.
3. **Topic messaging** (`publish_message(topic, object)`), arbitrary in-process Python objects.
4. **The strategy ranks for itself**, from its own subscriptions.

## Decision Outcome

Chosen: **option 2**, the signal, by the owner's requirement. Custom data on the bus is the documented
form for structured values; the signal came first because it is the simplest form the pin supports from
a Python actor on a live node. Topic messaging carries no `ts_event` and is not market data. Option 4 puts
the whole market's state in every strategy. Revisit when the list needs more than one figure a name.

**The codec** is `canon_trading.movers_rank_signal`, shared by publisher and reader: `SIGNAL_NAME`,
`MAX_ENTRIES = 20`, `RankEntry(instrument_id, move)`, `encode_ranking` and `decode_ranking`; the empty
ranking is `""`. It refuses, by name, a venue other than `ALPACA`, a move that is not a finite float, a
repeated id, more than 20 entries, and whitespace or a separator in an id.

**The actor** is `canon_trading.movers_rank_actor.MoversRankActor`, a `DataActor`. It subscribes to
`<SYM>.ALPACA-1-MINUTE-LAST-EXTERNAL` and the status of every equity in the cache, and adds each bar to
its symbol's `Tally`. The bars subscription names `params={"feed": feed, "wildcard": True,
"updated_bars": True}`; the status subscription names `params={"feed": feed, "wildcard": True}`.
NautilusTrader preserves adapter params; `feed` is ADR 0055's and `wildcard`/`updated_bars` are the
adapter contract in proposed ADR 0090, read at `cx-c11-request-trades` commit `2e1de581e540` (Task M gap 3,
2026-11-11). This checkout's adapter has no streaming bars/status hooks yet, so wire delivery remains
UNVERIFIED until that adapter change is integrated. Alpaca's own bars are used, not node-built ones: at the pin a time bar is built for
every clock minute, pauses included, unless `time_bars_build_with_no_updates` is off. The I/O boundary
is the actor: it reads the reference closes (daily bars, `adjustment=split`, `sip`) and the forward and
reverse splits since them over GET only, in a worker thread, at start and again when the trade date rolls
at 20:00 New York (a failed or unusable read is retried on the next tick), and reads the baseline file
its config names once a trade date with `movers_rank.read_baselines`. Each `PUBLISH_INTERVAL_SECS`,
`PUBLISH_OFFSET_SECS` after the minute mark, it computes one generation with `computed_ns` the tick, so
no bar that ended later is used, and publishes the top 20 through
the codec with `ts_event` the generation time. Startup takes the first strictly future offset, passing
`fire_immediately=True` to the pinned clock so that the first event is at `start_time`, without adding
another interval: a 09:30:10 start first ticks at 09:31:05; a 09:30:01 start ticks at 09:30:05, publishing
only if usable data has arrived (Task M gap 9, 2026-11-11). It publishes nothing, and logs why, when a bar was
refused since the last tick (an updated bar repeats its minute; a bar out of order or from an earlier
trade date), when a read is missing or failed, when no bar is in the generation, or when the latest bar
used ended more than config `max_data_age_secs` before the tick (a gap). Each record also names that bound
and lists `stale_symbols`, while retaining every universe row before projecting. A signal is not replayed to a late
subscriber, so every generation is published and the reader takes "no signal yet" as no ranking. Each
tick logs one record with the feed and the latency labels `publication` (the data time),
`receipt`, `generation`, and `entry_eligible` and `submission`, which are the strategy's and stay null.
A reference or split read that returns no reference close or no split read counts as failed: it is read
again, and the ranking stays unpublished with the reason (ultra review of the composition, finding 12,
2026-11-12).

The data limits (the open, corrections and cancels, the recorder's catalog) are ADR 0087's.

### Consequences

* Good: the strategy reads one string through one codec; a missing or doubtful input costs a minute's
  ranking and never offers an entry.
* Bad: an updated bar costs that minute's ranking; a correction or cancel is not seen until the REST check.
* Bad: until the adapter's wildcard subscriptions are on `main`, the actor runs only in simulation.

### Confirmation

* `tests/test_movers_rank_actor.py` on a `BacktestEngine`, and `movers_rank`'s own tests.
* A simulation on the recorder's catalog before landing; a paper run once the adapter's wildcard bars and
  statuses are on `main`, with the first published value and both clocks added here.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records
(`nt:` is `nautilus-trader@d53849b216e2`, v2.0.0rc8; `ap:` is `alpaca-py@c42add7c0a70`, v0.45.0).

* The owner's requirement and practices of 2026-11-11 — restated above; an instruction, not evidence.
* `nt:python/nautilus_trader/common/__init__.pyi:688-886` — `DataActor` with `publish_signal(name, value,
  ts_event)`, `subscribe_signal`, `on_signal`, `subscribe_bars` and `subscribe_instrument_status` taking a
  list; `Signal(name, value: str, ts_event, ts_init)` at `:1642`. Resolved.
* `nt:docs/concepts/actors.md:176-196` — "Use custom data for structured payloads, signals for lightweight
  values, or topic messaging for arbitrary in-process Python objects". Resolved.
* `nt:docs/concepts/message_bus.md:55-110` — `publish_data`, `publish_signal`, and `publish_message`, whose
  handlers get the object as published. Resolved.
* `nt:examples/backtest/callback_borrow_reentrancy.py` — upstream's own `DataActor` pair publishing and
  reading a signal in a `BacktestEngine`. Resolved.
* `nt:crates/data/src/engine/config.rs:36-38` — `time_bars_build_with_no_updates` defaults to true.
  Resolved.
* `nt:python/nautilus_trader/common/__init__.pyi:654-674`,
  `nt:crates/common/src/clock/mod.rs`, `validate_and_prepare_schedule`, and
  `nt:crates/common/src/timer.rs`, `TimerInterval::next_time_after` — `fire_immediately` defaults to
  false, which adds one interval to the supplied start; true fires at the supplied start. Resolved,
  2026-11-11; first-tick boundary tests run in a `BacktestEngine`.
* `cx-c11-request-trades@2e1de581e540:docs/adr/0090-alpaca-wildcard-bars-and-trading-statuses.md` and
  `packages/canon-alpaca/src/canon_alpaca/data.py:1454-1482`, `:1600-1649` — the adapter's explicit
  `wildcard` and `updated_bars` params; read from that unlanded branch, 2026-11-11. The current checkout's
  adapter feed parser and the actor's real subscription API are exercised offline; streaming
  bars/statuses through that adapter and Alpaca are UNVERIFIED here.
* Measured here 2026-11-11 at the pin: a `DataActor` with a 60 s timer publishing to a second
  `DataActor` in a `BacktestEngine` delivered one `Signal` a tick with `ts_event` the tick; a NautilusTrader
  `Bar` takes about 304 bytes in a process (500,000 built, `ru_maxrss`).
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md — minute bars "are emitted right
  after each minute mark"; updated bars after a late trade, "`t` set to" the earlier minute; corrections
  and cancels arrive with the trade channel. Fetched.
* `ap:alpaca/data/requests.py:85-106`, `:556-581`; `ap:alpaca/data/historical/corporate_actions.py` and
  https://docs.alpaca.markets/us/reference/stockbars.md — the actor's reads of `adjustment=split` closes
  and splits; the lines are ADR 0087's Evidence, resolved and fetched 2026-11-11.
