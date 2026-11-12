---
status: proposed
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation (ADR 0074); the lead reads each paper window
review-by: 2026-12-11
---

# The mover window: a loaded roster re-ranked on the bus, configurable slots and per-name caps, and unbounded re-entry until wind-down

## Context and Problem Statement

ADR 0088's strategy needs a node, a ranking on its bus and the rows of a run. The first bridge ran one
chosen name and one trade. The lead's decisions of 2026-11-11 enable several names, rotation and
re-entry until the part ends, in every session part. This composition loads a fixed startup roster and
uses ADR 0107's actor to re-rank those loaded names. Its paper delivery remains UNVERIFIED until the
Claude lane reads a firing of this version.

## Decision Drivers

* Paper is the proving ground (ADR 0083); a window starts flat, ends flat or fails, and acts only on its
  window symbols. Other holdings and orders are named and left alone.
* The strategy reads the ranking only from the bus. A missing, stale or invalid actor generation
  means no entry; the startup sample's order is never substituted (ADR 0087).
* Entries fit their name's share and notional bounds. The engine's larger per-order bound applies to
  every instrument and either side, allowing a sale after a rise (ADR 0088).

## Considered Options

1. Wait for the ranking actor.
2. Publish one startup choice repeatedly as the ranking (the original bridge).
3. Let the strategy read the vendor's movers list.
4. Load several qualified startup names and re-rank them in the node through ADR 0107's actor.

## Decision Outcome

Proposed: **option 4**, in `mover_window`, `session_window` and `paper_node`, on the lead's Task M
composition instruction of 2026-11-11. Option 2 remains an explicit compatibility simulation through
`FixedRank`/`mover_parts`. A live `--strategy Mover --mover` window uses `window_rank` and
`mover_universe_parts`. Option 3 remains refused by ADR 0088.

On paper nodes, every name outside the start-up roster may enter: with the data client's `load_all` that
is the whole market, and a name missing from the cache is requested on demand. Such a name carries no
per-instrument max-notional cap (upstream's default), no start-time share cap and no position monitor;
its order is bounded by the Mover's size (the entry notional and free balance at the current limit, and
the flow share) and the request ceiling, and a name without registered history is passed over by name
until the asynchronous context task lands.

* **The roster** comes from `choose_mover`'s startup ranking sample, under the same freshness, asset,
  action and price filters as the original bridge. The requested count is the part's slots plus
  `spares` (default 2, UNCALIBRATED), bounded by the actor's 20-name ranking interface. The old 10-name
  checking row could not carry ten slots plus two spares, so it is raised to 20. Fewer qualifying
  names remain fewer. Each chosen name, its inputs and its entry share bound are recorded under
  `mover.roster`; the primary name remains the report's `symbol`. The startup screener fallback,
  available only during the regular session, supplies candidate names; it never supplies a live
  actor generation. This roster stays fixed for the window. New instruments outside it are not traded.
* **The ranking** is ADR 0107's `MoversRankActor`, through `window_rank`, on the same node and feed.
  It re-ranks the cached roster and publishes `movers-rank`. The ordinary 90-second reader age row
  applies. Every actor tick, including a reason for publishing nothing, is kept in the run's
  `ranking_records`; every received valid signal keeps its value, `ts_event` and `ts_init` in
  `rankings`. The baseline setting is `baseline_path` (`--baseline-path`), default
  `catalog/movers-baseline/<feed>.json`, written separately through `movers_rank.write_baselines`.
  The path resolves against the repository root, independent of process cwd. This window does not
  bootstrap it: read_baselines(path, trade_date).problem refuses startup before node construction,
  naming the path and reason. A window with no published actor generation fails by name, even if
  quotes/trades arrived; a base-class NotImplementedError also fails it. A current baseline and
  external-bar delivery are prerequisites for publication. Paper delivery,
  including trading statuses, is UNVERIFIED here.
* **Slots and entries**: seven held names in every part (`PART_SLOTS`, the lead's ruling of 2026-11-12;
  ten and one before it), from `MOVER_PARTS`; `WindowConfig.slots`/`--slots` overrides the row. Spares add watched names, not
  held slots. The lead's 2026-11-11 setting uses its reported measurement of 15 requests per rolling
  minute per slot, with 20% reserve under 200; this builder did not measure that order cost.
  Pre-market and overnight are not measured, and hold seven by the same count (ADR 0083). `entries=None` imposes no count of completed entries: a sale frees the slot, a reclaim
  after the cooldown may buy the same name again, or the strategy rotates into another ranked name,
  until wind-down. Completion of one trade does not end the node.
* **Account-derived size**, superseding the 500/5,000 paper row on the lead's 2026-11-11 instruction:
  read Alpaca TradeAccount.buying_power once at every startup and divide by the configured slots in
  use, without applying another multiplier. Missing, invalid, non-finite or nonpositive buying_power
  refuses startup by name. The adapter models CASH with free funds equal to cash: a real engine
  simulation denied 197.79 with cash=100 and buying_power=200. Under the lead's conditional decision,
  the implemented entry figure is therefore min(buying_power, cash) / slots; the record names the
  engine_cash_limit when it binds. No margin model or deprecated DTBP/PDT field is inferred.
  Paper Mover uses whole shares, entry figure // current entry limit, through instrument.make_qty;
  FixedRiskSizer and its risk/flow clamps do not size this path. Available balance_free, when present,
  bounds each subsequent entry so losses or cash rounding cannot make an order exceed native funds.
  An explicit global max_shares is a read-only cap; the budget composition leaves it unset instead
  of installing a redundant initial-price cap. The same startup figure reaches the
  initial quote filter, actor order_cap, MoverConfig.max_notional, roster share caps and position
  monitors. Each name's share cap and independent PositionCap update at each entry's current limit,
  before submission, so a falling ask does not retain an initial-price ceiling. The strategy's
  share_caps map also refuses an unlisted name. One node loads all roster IDs into both providers,
  reconciles every ID, and gives each the engine order bound of entry figure x OUTER_BOUND_MULTIPLE
  (10, UNCALIBRATED), because the engine also caps equity sells. The submit/modify budgets remain
  shared by the node. Each cap's checks, peak and trip are
  recorded by instrument; the legacy aggregate peak keeps the largest observed share count.
* **Paper sizing, the owner's decision of 2026-11-11**, as the lead (canon-fa) recorded it under ADR 0074
  rule 1, with the lead's figures of the same evening: the paper accounts carry no risk limit, and the
  account-derived size above is that decision, built. Cash keeps a margin account's buying power, up to 2x
  overnight and 4x intraday at Alpaca, from sizing an entry above 1x until the margin-account build lands (a
  Codex build the coordinator started on 2026-11-11). The lead set 7 slots in every part (ADR 0083), raised
  only after two paper firings show room; going above that count needs two records that agree. Since
  2026-11-12 `mover_window.PART_SLOTS` holds 7 for every part, so a window that passes no `--slots`, a
  scheduled one included, holds seven names. One limit: a paper fill takes any size at the quote
  (Alpaca's paper page: "Your order quantity is not checked against the NBBO quantities"), so a size far
  beyond a name's traded volume teaches less than it seems to; the comparison of fills against a replay is
  where that shows, a learning note, not a cap. Alpaca's paper page says eligible orders "receive partial
  fills for a random size 10% of the time", and that paper trading does not account for the "market impact
  of your orders" or "price slippage due to latency" (https://docs.alpaca.markets/us/docs/paper-trading.md,
  read 2026-11-11). Live is untouched and stays the owner's. The 500 and 5,000 dollars stand as the first
  windows' record.
* **Entry context**: the default `MoverConfig.entry_arm` is `registered`, using ADR 0092's shared
  listing/run/extension rules. Before the node starts, `mover_entry_contexts` reads split-adjusted
  minute history on the part's feed, since 04:00 New York and over up to five earlier calendar
  sessions, latest first; the prior regular daily reference is read split-adjusted on SIP. Partial and
  future minutes are excluded. Each name receives its own `Day`. A failed read propagates; a missing
  calendar or reference is named in `Day.missing`. Per-name native context actors follow external bars
  while the name is unranked. Every timer tick compares the completed clock minute to Day.covered_to,
  even with no external bars. A startup/stream gap requests the missing prefix through the existing
  data client, one request outstanding per name; only successful completion grants range coverage,
  including no-trade minutes. A failure marks the context missing. Their one-second check interval is
  an UNCALIBRATED row. A continuously unranked name costs at most one completed minute-history request
  per minute, plus startup catch-up; all roster names, including spares, contribute. The first partial
  INTERNAL minute after subscription preserves a complete history minute if present and otherwise
  contributes nothing to known[minute]. The strategy itself requests no data. The final context status stays in its run
  record. Missing or gapped context inside the arm's scope means no entry by name.
* **Sessions**: `Mover` runs in every extended part too. SIP serves pre-market and after-hours;
  BOATS serves overnight, while the consolidated daily reference stays SIP. The registered arm's
  documented window is 04:00-16:00 New York, so pre-market and regular use it. After-hours and
  overnight explicitly use `entry_arm=bridge`, with `entry_arm_reason` recorded, on the coordinator's
  instruction of 2026-11-11. This changes the entry rule only. Each extended venue order remains a
  marked DAY limit, including the released trailing sell, chase and wind-down; stop cancels. The
  regular part retains its managed market exit. Overnight eligibility is checked for every roster
  name. A table `bridge_reason` may still select the whole `SessionMaCross` compatibility strategy,
  recorded separately under `mover.fallback`; no shipped row needs that strategy fallback.
* **The account check** reads the whole account, cancels working orders across all window symbols
  under one shared cancel budget, and sends at most one fresh-clock close per poll. A name with a
  working order, or with no price for a close outside the session, is passed for that poll, and the
  names tried least come first, so one name that cannot finish never holds the others' closes. An
  unsettled request retains its symbol before cache disposal. A missing sale blocks another sale of
  that name; an unknown symbol blocks every name it might close. The request bound stays independent
  of roster size. Other symbols remain named and untouched (ADRs 0044/0060).
* **The engine bound and request budget** retain their prior limits: an equity position worth ten
  times its entry may not fit in one engine-capped sale; the strategy retries, and the account check
  handles what remains. A window fails at 180 trading-API requests in 60 seconds or a 429; entries
  yield at the strategy's soft line of 140. No new bound is applied to the sale path.
* **Diagnostics** retain L6's qualifying-bar receipt to submit delay and the rise/ahead measure from
  the window's own fills and bars, now using each roster name's own reference. Registered-arm entries
  retain the qualifying receipt too. These fields change no rule and enter no gate.
* **The schedule** retains `--session auto --mover --regular-strategy Mover` on paper account B;
  extended parts choose Mover through their rows. No installer, schedule, paper run or credential
  operation was performed by the non-Claude composition builder.
* **Startup candidates, from the extended-part review merged from `main`**: a `Mover` candidate
  needs a positive ask in its latest quote on the part's feed, and an overnight candidate must be
  eligible and not halted there; one that fails is passed over by name before the next is tried.
  Overnight's trade date is the following day from 20:00 New York, Sunday before a Monday trading
  day included, and corporate actions are read through that trade date. The same-clock normal windows
  use the preceding evening of each prior weekday trade date from 20:00 New York, open Sunday evenings
  included and closed Friday evenings excluded.

**The replay before paper** (`tests/test_mover_day.py`, `tests/test_paper_mover_day.py`) runs the
strategy on one recorded day with the original simulation notional, move and unbounded entry-count rows,
explicitly selecting the bridge entry arm because this protection replay has no Day history: minute bars on SIP,
the feed a node's bars are built
from, and quotes on IEX, the one feed the adapter's quote request serves (ADR 0066). Its data limit:
IEX's top of book is not the national best bid and offer, which on 2026-11-11 decided the result, below.

**Update, 2026-11-11:** the replay's `stage_one` added the check across remade sells that Muse's sixth finding called
for, comparing recorded triggers within the same holding and resetting after flat.
The evidence is
`tests/test_mover_day.py::test_stage_one_checks_a_remade_sells_trigger_until_the_position_is_flat`:
the lower remake failed before the fix; lower, equal, higher and new-entry cases pass after it.

**Follow-up, 2026-11-11 (Antigravity findings 1 to 4):** the original check used the released limit's
new initialization timestamp and sampled after each step. It now uses `init_event.ts_init`, which the
pinned emulator preserves when transforming an order, and reads the cached order's initialization and
update events instead of timer samples. The real-engine tests in `tests/test_mover_day.py` cover an
immediate-fill release, two holdings, a lower remake after a partial entry fill, a sub-second trigger
peak before a remake, and a seeded stop released during submit; the offline checks live there, outside
the paper module. The replay still covers one instrument. It explicitly passes `max_shares=None`,
overridable through `replay`'s changes, and adds no `PositionCap`: unlike the window it has no starting
entry ask from which to derive the share bound. The later account-derived paper allocation is not
supplied by this protection replay; it retains explicit legacy risk sizing. That sizing difference
remains an open question for a study, and these replay results do not measure the new paper allocation.

**Review of the composition as fired, 2026-11-12 (`/code-review` high over the integrated diff):**
fixed, each with a test that failed first: a `--mover` window running `SessionMaCross` checked its
configured symbol, not the chosen name, after the stop; one name's unfilled extended close or missing
bid held every other name's close; entries sent in one step were each sized from the same free cash,
since the account locks an entry's cash only once the venue takes it, so the second was denied; a
malformed ranking counted as a fresh empty one and unwatched every idle slot. In a following commit, on
the lead's ruling: the part rows, which read 10/10/1/1 slots, hold 7 in every part, since ten would pass
the 180 throttle at the measured worst minute (ADR 0083); and the window's `flat` and `realized_usd`
count the account check's own closes, which they left out. A name whose fills hold more shares sold
than bought (a close of shares bought by an order whose answer the node never saw) is named
`incomplete` and books nothing, so a sale is never counted whole as profit. Recorded, not fixed:
* the actor is not seeded with the part's earlier minutes, so a window started after the part's start
  compares partial dollars with the part's full baseline and can invert the order (ADR 0087; the
  plan's mid-part seeding);
* `catalog/` is not tracked, so a scheduled clone has no baseline file unless one is written there, and
  a scheduled Mover window is refused for it: UNVERIFIED on a scheduled run;
* a late-updated bar on any roster name voids that minute's publish (`updated_bars`), an empty
  reference read is not retried, and the soft line counts neither the startup reads nor the context
  feeds' history requests: rates UNVERIFIED;
* a released sale may be repriced one second after it starts working (on `main` before this change).

### Consequences

* Good: the strategy runs on paper today through the same signal the ranking actor will publish.
* Bad: the roster is fixed at startup; a new mover outside it waits for another window.
* Open: a fractional-cent bid can make the native emulator release a sub-penny limit above one
  dollar, because it uses the instrument's 0.0001 increment rather than the strategy tick scheme.
  The real-engine regression and adapter price-validator refusal are recorded in the M-c builder
  report; on 2026-11-11 the coordinator handed the release-price contract to the lead for a Claude
  lane. It remains red here, without changing adapter/emulator behavior or claiming a paper fix.
* Bad: the replay cannot exercise a fill on a name IEX quotes widely; only paper does.

### Confirmation

* Composition checks, 2026-11-11: `tests/test_mover.py` runs the actual ranking actor on a real
  BacktestEngine with a known startup prefix: AAA is bought/sold, the actor re-ranks BBB, and the freed
  slot buys BBB, ending flat. Missing/wrong-session baselines send no order or substitute ranking.
  Separate cases hold two names under independent caps, repeat an entry after a sale/cooldown, require
  registered listing context, and repair/refuse native context gaps. L6's reclaim test failed against
  main `232e5c5` with one filled buy instead of two and passed on the supplied L6 work.
* `tests/test_paper_node.py` builds providers and checks multi-ID reconciliation/risk configuration
  offline. `tests/test_session_window.py` checks roster/context/actor wiring, each name's quote bound,
  all-symbol account clearing and missing-request protection, CLI settings and per-name diagnostics.
  Paper delivery of this multi-name composition and full CI remain UNVERIFIED for the Claude lane.

* `tests/test_mover_window.py` (each extended part builds `Mover` with its configurable slots and no entry-count bound),
  `tests/test_mover.py` (the window's parts on trades and quotes, the outer bound, marked DAY limit
  entry and released sale for each extended part) and
  `tests/test_session_window.py` (the node's build, the choice and the end handed to it, the request
  ceiling, `--mover` required, selection in every part, the named bridge fallback, overnight feed
  routing and Sunday's next trade date). These are offline builds and simulations; each extended
  part on account B remains UNVERIFIED until a Claude lane runs it on paper.
* The replay, 2026-11-11, `mise run paper:test -- -s -k test_paper_mover_day` on paper account A, data
  reads only. On IEX bars alone no name had enough bars (NXXT 43, NCT 61, SCKT 99 against a floor of
  120), so the bars moved to SIP with the floor unchanged. First SIP run: NXXT (270 bars) and NCT (134)
  passed over for quote gaps of five minutes or more, read as halts; SCKT (121 bars, 2,592 IEX quotes,
  prior close 0.2511 with no corporate action) replayed, and showed two defects: the entry kept
  filling after its sale had released, and it was sent at an ask of 0.3857 against a bid near 0.28.
  Both were fixed with failing tests first (ADR 0088). Rerun, exit 0: seven rising runs, each refused
  because IEX's bid was under the planned stop; no entry, no fill.
* **Window 1**, 2026-11-11, 13:20:49 to 14:04:06 New York, paper account A, the regular session; from
  an earlier form of the strategy's first commit (`660b37e`, rewritten since and not on `main`) with
  uncommitted changes, before the fixes it led to. The name was CENN, from the stream sample: up 128.3% from a close of 0.1227, a
  dollar-volume ratio of 1,031; GOVX and SLXN ranked above it and were passed over, for a reverse
  split and for no fresh trade. No rising run of three closes came for 37 minutes. At 13:58:00 a DAY
  limit to buy 1,686 at 0.2141 reached the node as accepted 44 ms after its submit and filled in six parts in ten seconds,
  358.24 dollars. The emulated trailing stop-limit was released at 13:59:05 at a bid of 0.2085, as a
  limit whose acceptance came back in 23 ms; 1,044 filled at 0.2084, and the 642 left were re-priced by a cancel
  and a new limit and filled by 13:59:16. Result: 349.12 dollars back, 9.11 lost, 2.54% under holding
  flat. The window ended `ok` and flat without its account check closing anything: 307 trading-API
  requests, at most 15 in 60 seconds, no 429. What it showed, each fixed since (ADR 0088): every
  further fill of the entry remade the trailing sell a second later and from a lower trigger (0.2058
  down to 0.2039); the log said nothing of the 37 minutes; the record's notes were empty. Its record
  is `window-20261111T182049Z.json`, kept outside the repository.
* **Window 2**, 2026-11-11, from 14:13:46 New York, account A, the regular session; from the commit
  that remakes the trailing sell in the same step, before this change's last fixes. The name was ELOG,
  from the stream sample, nothing passed over: up 84.1% from a close of 0.5922 to 1.09, a dollar-volume
  ratio of 10,058; the entry limit at the start was 1.10, so at most 454 shares. It then fell under a
  dollar. Four minutes at an unchanged 0.9100 restarted the run each time; closes of 0.7540, 0.7690,
  0.7833 and 0.8292 qualified. At 14:26:00.30 a DAY limit to buy 454 at 0.8318, on the four-decimal
  band, reached the node as accepted 30 ms later and filled whole at 14:26:02.12, 377.64 dollars. The
  trailing sell was made 0.18 s after the fill, its trigger 0.8099. At 14:26:07.19 the emulator lifted
  the trigger to 0.8340, which a bid of 0.8507 gives (the quote is not in the log), and released
  within the same millisecond at a bid of 0.8258; the limit's acceptance came back in 25 ms and it
  filled in two parts at 0.8258 by 14:26:10.40, 374.91 dollars. Result: 2.72 lost, 0.72%, 8 seconds
  held. The window's one entry was used. The machine then slept from 14:40 to 15:49, and the window
  waits for its end on the event loop's monotonic clock (`session_window._run_until`), which a sleep
  stops, so it ran past its planned end of 14:56. It was ended by an interrupt at 15:53:38, seven
  minutes before the close: flat, nothing for its account check to cancel or close, 228 trading-API
  requests, at most 14 in 60 seconds, no 429. Its record reads `failed: KeyboardInterrupt`, so it
  does not count as a window that ended `ok`; the wait is every window strategy's, and is not fixed
  here. Its record is `window-20261111T191346Z.json`.

## Evidence

Read 2026-11-11. Resolved means read in a local copy at the commit `canon/upstreams.lock.json` records.

* NautilusTrader at the pin
  (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186) —
  `python/nautilus_trader/common/__init__.pyi:688`, `:720`, `DataActor` and its `publish_signal(name,
  value, ts_event)`; `python/nautilus_trader/live/__init__.pyi:1126`, `LiveNode.add_actor`;
  `crates/risk/src/engine/mod.rs:2491-2507`, the per-order notional cap, whose one exemption
  (`:706-720`, a full exit of a crypto derivative) no equity order meets. Resolved.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca — extended eligibility requires the boolean
  `extended_hours` mark and a limit with DAY or GTC time in force; pre-market 04:00–09:30 and
  after-hours 16:00–20:00 New York. https://docs.alpaca.markets/us/docs/245-trading-for-trading-api —
  overnight 20:00–04:00 before the trade date, following the NYSE calendar; limits only, DAY or GTC;
  BOATS supports quotes, trades, snapshots and historical bars for the account's plan. Read 2026-11-11
  from the HTML pages linked by https://docs.alpaca.markets/llms.txt; paper support for the new mover
  windows is UNVERIFIED here.
* alpaca-py at the pin
  (https://github.com/alpacahq/alpaca-py/tree/c42add7c0a7026e2ead3ed64e76f4def2b99fda4) — `alpaca/data/live/stock.py`, the constructor's
  `feed` and BOATS stream endpoint; `alpaca/data/requests.py`, `StockBarsRequest.feed`,
  `StockSnapshotRequest.feed` and `StockLatestQuoteRequest`; `alpaca/trading/requests.py`,
  `OrderRequest.extended_hours` and `LimitOrderRequest`. NautilusTrader at the pin,
  `python/nautilus_trader/common/__init__.pyi`, `OrderFactory.limit` and `trailing_stop_limit`, and
  `crates/execution/src/order_emulator/emulator.rs`, `submit_released_order`, which retains submit
  params when transforming the order. Read locally 2026-11-11; resolved.
* That a signal is not replayed to a later subscriber: the ranking lane's finding at the pin on
  2026-11-11, not reproduced here. UNVERIFIED in this record; the actor repeats its ranking either way.
* https://alpaca.markets/support/usage-limit-api-calls — "currently 200 requests per minute, per
  account", the figure the ceiling sits under. Fetched by the evidence-verifier, 2026-11-11.
* `src/canon_trading/mover_window.py`, `src/canon_trading/session_window.py` and their tests — as
  described, read in this tree.
* The three replay runs and each window's record and node log (`window-<start>.json`, `.log`), read
  here on the day; all kept outside the repository, so UNVERIFIED for a later reader.
* An `evidence-verifier` pass, 2026-11-11: it corrected the two acceptance times (now both the
  node's receive time), the record that refuses option 3, the price the shares are counted at, the
  choice's bound, window 1's commit, and this section's labels.
* The owner's requirement of 2026-11-11 and the lead's decisions of that day, including the later
  startup account allocation, conditional cash-engine limit and per-part slot settings — instructions,
  not evidence of an edge. The earlier fixed 500/5,000 paper row is superseded.
* Antigravity's read of the extended-part change, 2026-11-11: the pre-midnight action end date, the
  candidate's affordable ask, candidate-level overnight eligibility and Sunday-evening normals were
  reproduced by failing tests before their fixes in `tests/test_session_window.py`; inherited comments
  in ADR 0083 and the scheduler were corrected. The final quote and eligibility rechecks remain in
  place. All of this evidence is offline; each extended-part firing on account B is still UNVERIFIED.
* evidence-verifier on the sizing edits, 2026-11-11: the cap at the entry contradicted this record's
  driver and ADR 0088, and the window sizes by `FixedRiskSizer` under the 500. Corrected above, with the
  lead's figures of the same evening.
