---
status: proposed
date: 2026-11-11
decision-makers: the owner for the bus contract, the filters and the practices (2026-11-11); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# The movers ranking: a score from pure functions over the whole market's minute bars, published each minute as the signal `movers-rank` by a `DataActor`, and nothing published when an input is missing, stale or invalid

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
* `ratio` is the dollar volume from 20:00 New York the evening before (the whole trade date) to each
  symbol's own data time over the baseline for the same part of the session. Dollar volume is bar volume times the typical price, an approximation:
  NautilusTrader's `Bar` has no trade count and no VWAP. The baseline is cumulative dollar volume per
  symbol per 15-minute segment from 20:00 the evening before to 20:00, 96 values a session (format
  `canon-movers-baseline/2`; version 1, 64 values from 04:00, is refused whole), averaged over the last
  `BASELINE_SESSIONS` sessions; inside a segment the baseline interpolates between the previous
  segment's cumulative value and this segment's end by the elapsed share of the segment
  (`movers_rank.baseline_at`), an approximation. The baseline is a file written once a night through
  `movers_rank.write_baselines` and read by the actor with `movers_rank.read_baselines`, from the
  recorder's catalog once it holds the days and from Alpaca's 15-minute `sip` bars until then
  (UNCALIBRATED). The file carries each symbol's `traded_sessions` (`movers_rank.segment_trade_counts`)
  and the lookback they count against (`lookback_sessions`); a writer may add header keys of its own,
  which the reader ignores and which may not replace the format's. A file without `lookback_sessions`,
  as the writer before trade counts left, is refused whole and says so.
  *Decided by the lead on 2026-11-11 under the owner's delegation to fill an undefined case, with the
  lead's two definitions of the same day:* a baseline is *established* only where the name traded in
  that segment in every session of the lookback (`traded_sessions[segment] == lookback_sessions` in the
  file, `movers_rank.Baseline.established`); names with positive dollar volume and no established
  baseline, a zero baseline among them, get no ratio and form their own tier, ordered by dollar volume,
  with no denominator floor. The listing move condition, a move of at least 3% (`MOVE_CONDITION`, the
  move only, not ADR 0092's ignition volume ratio), applies to every ranked name in both tiers. A tier
  name ranks above every finite ratio only when its dollar volume is at least the median dollar volume
  of the established names passing the move condition at that moment; below it the name is not ranked.
  With no ratio qualifier, the tier ranks by dollars alone. Instrument id breaks ties and the existing
  long-only positive-move rule and other eligibility filters still hold. This rule is the registered
  row, with "zero baseline not ranked" as its paired walk-forward arm: `zero_baseline_arm` selects
  `relative-floor` (default) or `exclude`, under which no name without an established baseline is
  ranked, so the tape can compare both. Zero over zero is not ranked (no activity); a missing baseline,
  a hole in a file that exists (see the part with no file below), stays no entry, no substitute, and a baseline whose file carries no trade counts leaves
  establishment unknown and the name ineligible (`established:unknown`).
  The names setting the median are those with an established baseline, a finite ratio and a move past
  the condition, whatever their other filters: a big mover over the order cap or halted still measures
  what a mover trades. A move equal to 3% within `math.isclose`'s default tolerance passes.
  "Dollar volume in the segment" means the existing cumulative figure from 20:00 the evening before to the symbol's data
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
* *Code review round 3 (2026-11-11), findings 1 and 2, built by the lead's definitions above:* a
  baseline near zero but not zero no longer gives an unbounded ratio, since one stray trade in the
  lookback leaves the baseline unestablished; and the relative floor is the median, not the least,
  dollar volume of the established risers past the move condition, so a thin tier name no longer takes
  the top before real movers. Kept as the definitions' own consequences (UNCALIBRATED, for the
  walk-forward to weigh): a liquid name that missed one lookback session's print in its clock's segment
  joins the tier, ranked by dollars above every finite ratio once it clears the median, even below its
  own baseline; and a session with no trade in some segments for every name, as an early close
  (UNVERIFIED for Alpaca's extended hours) or a short catalog would leave, keeps those segments
  unestablished for the next five sessions. The tier's reasons keep the `zero_baseline:` prefix,
  the lead's name for it; `Row.established` and `Row.baseline` tell the two kinds apart.
* *The overnight part, the lead's definition of 2026-11-11:* 20:00 to 04:00 New York, on the overnight
  feed (`boats`), uses the same 15-minute segments and the same definitions as the other parts, so one
  rule serves every part and the baseline writer needs no second scheme. Few names trade in every
  session of an overnight segment, so most fall into the not-established tier there and are ranked on
  dollars under the move and median conditions: expected, not a defect. Built: the part runs from 20:00
  the evening before to 20:00 (`SEGMENTS = 96`, segment 32 opening at 04:00), a tally counts every bar
  of the trade date, and the file is `canon-movers-baseline/2`, agreed with the baseline writer on
  2026-11-11 (`segments` is the format's key; the feeds are the writer's header keys). A bar across
  04:00 is refused by a tally, since a day ratio's span starts there, and by a baseline, as any bar
  across a segment's end is. The
  overnight baseline needs a `boats` history loader, which does not exist (the extended history load
  refuses `boats`): until then a file built from `sip` has no trades in segments 0 to 31, so overnight
  every name is unestablished and ranks in the tier.
  *The lead's rule of 2026-11-11: "A ratio's numerator counts exactly the clock minutes its baseline
  covers."* While the baseline file's header lists no overnight feed (`feeds` without `boats`,
  `BaselineRead.overnight` false), a generation whose data time is past 04:00 (not at it) counts
  every row's dollars from 04:00, against the baseline from 04:00, and the night's dollars are kept
  apart (`Row.dollars_before_day`); a baseline built with the overnight feed counts both sides from
  20:00, and nothing else changes. The span is the generation's, not each name's, so the tier, its
  median and the absolute floor all compare the same span's dollars; a name last seen by 04:00 has no
  day minutes and no ratio. Each row shows the span (`Row.ratio_span`, "04:00-now" or "20:00-now").
  The caller passes `overnight_baseline` true only when its tallies hold the overnight feed too, and
  a `feeds` header that is not a list of names is refused (code review of the span, 2026-11-11). A second, from the same review: a name that
  traded only overnight keeps its overnight clock and dollars into the day, so with no established
  name it can rank on hours-old data until the actor's maximum data age (M-b) excludes stale names.
  A part across a clock change, a Sunday's, is no trade date's and is refused.
* *No baseline file for a part, the lead's ruling of 2026-11-11 (21:49):* every name in that part is
  not established and ranks in the dollar tier under the move condition, on dollars alone when no
  name is established. The preflight passes by name ("no baseline for this part: ranked on dollars and
  move"), recorded in each window, and never passes overnight on the `sip` file. "Missing baseline, no
  entry" now means only a hole: a name or segment absent from a file that exists and should cover it.
  **Built overnight only** (the lead's decision (A) of 2026-11-12, on the owner's review of the
  composition, finding 2): with no `boats` file, an overnight window starts and its actor ranks every
  name in the dollar tier under the move condition, and the window's record states "no baseline:
  dollar tier under the move condition" as each row's basis until Wave 2's overnight baseline exists.
  Regular and pre-market still refuse to start with no file; a file that exists and cannot be used
  still refuses in every part, and in a file that exists a name absent from it is a hole.
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
  `PART_START = 20:00` the evening before, `SEGMENT_MINUTES = 15`, `MOVE_CONDITION = 0.03`,
  `HALT_UNKNOWN_ELIGIBLE`, the name proxy's words, `zero_baseline_arm = "relative-floor"` and its paired
  `exclude` arm, and `MAX_DATA_AGE_SECS = 180`. The optional absolute `zero_baseline_dollar_floor` has no
  registered numeric value or default. `MAX_DATA_AGE_SECS` is the default for `rank(max_data_age_secs=...)`
  and the actor config's setting of the same name: each symbol's last bar must have ended within that
  many seconds of `computed_ns`, including the exact bound. An older symbol stays in the generation with
  its bar timestamp and `last_bar:stale` exclusion, but its current price, moves, dollar-volume ratio and
  score are unknown and it cannot enter the top 20. A fresh symbol never refreshes another symbol's
  retained price (Task M gap 4, 2026-11-11).

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

**Limits.** The move from the open is unknown: no source of the official regular open is named, so none
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
A bar across a segment's end is refused for a baseline: the bar cannot allocate its dollars across that
boundary. 04:00 is a segment's end inside the part and where a day span starts, so a tally refuses a bar across
it too; a bar
ending exactly at 04:00 is in segment 31, and one starting exactly at 04:00 in segment 32. Booleans are unknown reference/open prices, not 0 or 1;
order caps must be numeric positive finite amounts, and compute time a nonnegative integer clock.

### Consequences

* Good: the strategy reads one string through one codec; a missing or doubtful input costs a minute's
  ranking and never offers an entry.
* Good: every figure is a function of plain inputs, so a replay on the catalog computes what paper did.
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
