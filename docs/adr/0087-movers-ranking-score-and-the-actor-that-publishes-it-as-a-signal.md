---
status: proposed
date: 2026-11-11
decision-makers: the owner for the bus contract, the filters and the practices (2026-11-11); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# The movers ranking: pure scoring functions and a planned `DataActor` publishing the signal `movers-rank` each minute, withholding missing, stale or invalid input

## Context and Problem Statement

ADR 0083 left the ranking score as its gap 1, to be recorded before any code, and puts ranking at the head
of the mover node's building order: wildcard data into the node first, then the ranking, then the strategy
that reads it (M1b, ADR 0088). The ranking must reach a strategy in the same node, be computed from what
the node's data client hands the engine, and never offer an entry from a guess. This record decides the
score, its inputs and rows, the actor that computes it, and the bus contract between the two.

## Decision Drivers

* The owner's requirement of 2026-11-11, restated. The ranking travels on the message bus through
  upstream's signal API at the pin: `DataActor.publish_signal(name, value, ts_event)`, read by
  `subscribe_signal(name)` and `on_signal(signal)` on a `Strategy` or a `DataActor`. The name is
  `movers-rank`; the value is one string of at most 20 entries, best first, each `ID.ALPACA=move`,
  joined by `;`; `ts_event` is the time the ranking was computed. The reader refuses entries older than
  90 s, which is M1b's row, UNCALIBRATED, and not enforced by the publisher.
* The owner's practices of 2026-11-11, restated: at most one full-market generation per publish interval;
  a missing, stale or invalid ranking means no entry, with no fallback; the whole universe and every
  unknown row stay in the generation and the top 20 is only a projection of it; unknown is unknown, never
  zero; `sip` and `boats` figures stay apart; a correction, a cancel, a gap or an invalid input
  invalidates the generation; two clocks, when the market made a fact available and when it was
  received, with availability deciding what a generation may use; one stream an account, owned by the
  data client, never by the ranking.
* The filters are registered rows from the start, with no price or market-cap floor: tradability, halt
  state, asset class (common shares and depositary receipts in; warrants, units and rights out), a split
  or reverse split since the reference close, and one share fitting the order cap.
* Antigravity's review of the M0 probe: no corporate-action or warrant filter (item 1); updated bars,
  corrections and cancels ignored (item 2); a snapshot's previous daily bar taken as the reference close
  in pre-market and after hours (item 4b).

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

**The score** is `canon_trading.movers_rank`'s, pure functions over plain values that do no reads:

* `move` is the latest bar's close over the reference close, less one. The reference is the latest
  completed, split-adjusted official regular close of a session *before* the trade date:
  `movers_rank.rank` refuses a reference of the trade date or later, so the close that settled at 16:00
  New York is the next trade date's reference, read from 20:00 when the trade date rolls (item 4b).
  The move from the regular open is kept beside it.
* `ratio` is the dollar volume from 04:00 New York to each symbol's own data time over the baseline for
  the same part of the session. Dollar volume is bar volume times the typical price, an approximation:
  NautilusTrader's `Bar` has no trade count and no VWAP. The baseline is cumulative dollar volume per
  symbol per 15-minute segment from 04:00 to 20:00, 64 values a session, averaged over the last
  `BASELINE_SESSIONS` sessions; inside a segment the baseline interpolates between the previous
  segment's cumulative value and this segment's end by the elapsed share of the segment
  (`movers_rank.baseline_at`), an approximation. The baseline is a file written once a night through
  `movers_rank.write_baselines` and read by the actor with `movers_rank.read_baselines`, from the
  recorder's catalog once it holds the days and from Alpaca's 15-minute `sip` bars until then
  (UNCALIBRATED).
  *Decided by the lead on 2026-11-11 under the owner's delegation to fill an undefined case:* names
  with positive dollar volume over a zero baseline form their own tier, ordered by dollar volume.
  Such a name ranks above every finite ratio only when its dollar volume is at least the smallest
  dollar volume among the names qualifying by ratio at that moment; below it the name is not ranked.
  With no ratio qualifier, the tier ranks by dollars alone. Instrument id breaks ties and the existing
  long-only positive-move rule and other eligibility filters still hold. This rule is the registered
  row, with "zero baseline not ranked" as its paired walk-forward arm: `zero_baseline_arm` selects
  `relative-floor` (default) or `exclude`, so the tape can compare both. Zero over zero is not ranked
  (no activity); a missing baseline stays no entry, no substitute.
  *The coordinator's reading of the lead's rule, 2026-11-11:* "qualify by ratio" means this ranker's
  own eligible rows with a finite positive score, without importing ADR 0092/0083's listing condition.
  "Dollar volume in the segment" means the existing cumulative figure from 04:00 to the symbol's data
  clock, on both sides of the comparison; it does not introduce a separate 15-minute tally. The
  absolute `zero_baseline_dollar_floor` remains optional, with no numeric default and no registered
  value: unset (`None`) means not applied, so the relative rule alone governs. When configured by the
  owner or walk-forward, it is finite and nonnegative, and volume must be strictly above it; a name at or
  below it keeps its known ratio and score and is excluded as `zero_baseline:absolute_floor`. This
  reading replaces the earlier unset-means-unranked interpretation. Test-local amounts register no
  trading number. `volume_score` represents the active zero-baseline tier with an infinite ratio and
  signed infinite score (zero for a flat move); ordinary arithmetic overflow remains unknown and
  excludes the row. Configured floors are validated by both `rank` and standalone `volume_score`,
  including when no usable baseline exists. Booleans are not numeric amounts or measurements;
  nonfinite or negative volume/baseline measurements stay unknown, and an invalid move stays unknown.
  The zero-baseline exclusion reasons are recorded independently of other failed filters or the move's
  sign; only eligible finite positive-score rows qualify to set the relative floor.
* *Open limits, code review round 3 (2026-11-11), each answered by the lead's definitions of the same
  date, which a follow-up landing builds before the first composition firing.* (1) A baseline near zero
  but not zero still gives an unbounded ratio: one stray trade in the lookback puts a nearly flat name
  above a real mover. The lead's answer: a baseline is established only where the name traded in that
  segment in every session of the lookback; a name without one gets no ratio and joins the
  zero-baseline tier, with no denominator floor. (2) The relative floor, the least dollars of any
  eligible riser, is often tiny, so the tier can take the top before real movers. The lead's answer:
  ADR 0092 and 0083's listing move condition applies to both tiers, and the floor is the median dollar
  volume of the established names that pass it.
* The score is `move × ratio`; the tie-break, trade acceleration, is unknown for now and breaks no tie.
  The first ranking uses the wildcard minute bars only: no wildcard trades and no condition masking.
* Filters: `tradable` from the asset; a halt from `InstrumentStatus`, each status folded with
  `movers_rank.status_after` so a short-sale restriction change cannot erase a halt, unknown until the
  adapter publishes statuses, and until then eligible and recorded (`HALT_UNKNOWN_ELIGIBLE`); the
  security type, an input,
  for which alpaca-py 0.45.0's `Asset` has no field (its fields: `asset_class`, `exchange`, `symbol`,
  `name`, `status`, `tradable`, `marginable`, `shortable`, `fractionable`, `attributes`,
  `borrow_status`, `cusip`), so a registered proxy reads it from the asset's `name`, with unknown
  ineligible; a split since the reference close from Alpaca's corporate actions; one share within the
  order cap, whose figure is the strategy's and is enforced at the order by the strategy and its risk
  engine (M1b, ADR 0088), the ranking recording it only.
* Rows, all UNCALIBRATED: `TOP = 20` (the codec's `MAX_ENTRIES`), `BASELINE_SESSIONS = 5`,
  `PART_START = 04:00`, `SEGMENT_MINUTES = 15`, `HALT_UNKNOWN_ELIGIBLE`, the name proxy's words,
  `zero_baseline_arm = "relative-floor"` and its paired `exclude` arm. The optional absolute
  `zero_baseline_dollar_floor` has no registered numeric value or default.

**Planned freshness (UNBUILT, UNVERIFIED in this checkout)**: the imported M-b design proposes
`MAX_DATA_AGE_SECS = 180`, `rank(max_data_age_secs=...)` and `last_bar:stale` to exclude each symbol's
old retained price, moves, ratio and score independently (Task M gap 4). None of that constant,
parameter or age exclusion exists in this tree. Its functions report publication-to-compute latency
and reject future input, but enforce no maximum data age; a fresh symbol's own clock does not advance
another's baseline or regular-open gate. The proposed age rule and its tests remain M-b integration
work, not a verified property of these functions.

**Planned actor (UNBUILT, UNVERIFIED in this checkout)**: `src/canon_trading/movers_rank_actor.py` and
`tests/test_movers_rank_actor.py` do not exist here. The following is M-b's intended composition, not
an implemented or tested actor in this tree. The proposed `canon_trading.movers_rank_actor.MoversRankActor`
would be a `DataActor` subscribing to
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

**Limits of the planned composition (UNVERIFIED in this checkout).** The move from the open is unknown:
no source of the official regular open is named, so none
is passed. Corrections and cancels come only on Alpaca's trade channel, which the actor does not read, so
it sees a late trade only as an updated bar; the recorder's catalog keeps the original minute bars, and
updated bars, corrections and cancels go to its side events until the nightly REST check replaces a
corrected minute. At the pin `write_bars` refuses a write that overlaps a file of its bar type, so the
recorder holds a late or repeated bar out of the catalog and names it. Each recorder segment restart
loses about 11 to 15 seconds of bars, owed by the REST check, and a segment's first and last minutes are
partial and marked so. Whether Alpaca's daily close is the official closing price is UNVERIFIED. The
M0 probe (`m0_probe`) is retired; nothing builds on it.

*Producer timestamp clarification, 2026-11-11:* `canon_alpaca.data.parse_bar` supplies an EXTERNAL
open-stamped `ts_event` and synthesizes `ts_init = ts_event + interval`. NautilusTrader's live engine
defaults to INTERNAL close-stamped, left-open time bars; its `TimeBarAggregator` sets both timestamps
to the close. The rank functions derive bounds from these two conventions and require one full bar
type per tally or baseline; nondefault engine timestamp settings are outside this contract. The
per-symbol baseline and regular-open gate use those bounds. The legacy `received_ns` / `receipt`
field retains `ts_init`, which duplicates the close for both producers above and is not measured
arrival time; recording actual receipt remains UNVERIFIED until the adapter/actor supplies it.
Bars straddling 04:00 New York are refused for both a tally and a baseline: the bar cannot allocate
its dollars across that boundary. A bar ending exactly at 04:00 is wholly before the part, and one
starting exactly at 04:00 is wholly inside it. Booleans are unknown reference/open prices, not 0 or 1;
order caps must be numeric positive finite amounts, and compute time a nonnegative integer clock.

### Consequences

* Good: the strategy reads one string through one codec; a missing or doubtful input costs a minute's
  ranking and never offers an entry.
* Good: every figure is a function of plain inputs, so a replay on the catalog computes what paper did.
* Bad: an updated bar costs that minute's ranking; a correction or cancel is not seen until the REST check.
* Bad: the actor and its simulation tests are unbuilt in this checkout; its planned wire delivery also
  awaits the adapter's wildcard subscriptions.

### Confirmation

* Present: `tests/test_movers_rank.py` covers the functions and producer-shaped bars; it exercises no
  ranking actor, subscription API or actor timer.
* Planned: `tests/test_movers_rank_actor.py` on a `BacktestEngine` (UNBUILT, UNVERIFIED in this checkout).
* UNVERIFIED, not run here: an actor simulation on the recorder's catalog before actor landing; a
  paper run once the adapter's wildcard bars and statuses are integrated, with its first published
  value and clocks added to the record.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records
(`nt:` is `nautilus-trader@d53849b216e2`, v2.0.0rc8; `ap:` is `alpaca-py@c42add7c0a70`, v0.45.0).
Upstream-source references are evidence of an API, not of an actor running in this tree. The actor
composition and experimental notes imported with this record have not been reproduced here.

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
* `nt:crates/live/src/node/config.rs:2214-2219`, `nt:crates/data/src/aggregation.rs:1735-1752`,
  `:5361-5368`; this tree's `packages/canon-alpaca/src/canon_alpaca/data.py:825-837` — default live
  close timestamps and adapter open timestamps with synthetic initialization at close. Resolved,
  2026-11-11; producer-shaped rank regressions exercise both conventions.
* `nt:python/nautilus_trader/common/__init__.pyi:654-674`,
  `nt:crates/common/src/clock/mod.rs`, `validate_and_prepare_schedule`, and
  `nt:crates/common/src/timer.rs`, `TimerInterval::next_time_after` — `fire_immediately` defaults to
  false, which adds one interval to the supplied start; true fires at the supplied start. The imported
  source note is dated 2026-11-11; actor subscription and first-tick tests are UNVERIFIED in this checkout.
* `cx-c11-request-trades@2e1de581e540:docs/adr/0090-alpaca-wildcard-bars-and-trading-statuses.md` and
  `packages/canon-alpaca/src/canon_alpaca/data.py:1454-1482`, `:1600-1649` — the adapter's explicit
  `wildcard` and `updated_bars` params; read from that unlanded branch, 2026-11-11. The current checkout's
  adapter streaming integration and actor subscription API have not been exercised in this tree;
  streaming bars/statuses through that adapter and Alpaca are UNVERIFIED here.
* Imported branch measurement claim, UNVERIFIED and not reproduced in this checkout: a `DataActor`
  with a 60 s timer publishing to a second actor in a `BacktestEngine` reportedly delivered one
  `Signal` a tick; the same branch reported about 304 bytes per NautilusTrader `Bar` from 500,000 built
  with `ru_maxrss`. Neither is a measured result of this tree.
* `ap:alpaca/trading/models.py:37-66` — `Asset`'s fields; no security type. Resolved.
* `ap:alpaca/data/requests.py:85-106`, `:556-581`; `ap:alpaca/data/historical/corporate_actions.py` —
  `StockBarsRequest.adjustment` defaults to None; `CorporateActionsRequest(types, start, end, limit=1000)`,
  the limit a total. Resolved.
* https://docs.alpaca.markets/us/reference/stockbars.md — `adjustment`: "`split`: adjust price and volume
  for forward and reverse stock splits", "Default: `raw`"; `limit` at most 10,000, "the total number of
  data points, not per symbol". Fetched.
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md — minute bars "are emitted right
  after each minute mark"; updated bars after a late trade, "`t` set to" the earlier minute; corrections
  and cancels arrive with the trade channel. Fetched.
