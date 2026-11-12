---
status: accepted
date: 2026-11-11
decision-makers: the owner for the focus (2026-11-11); master orchestrator (Claude) for the composition and the order of work, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# Strategy focus: extreme movers, built from upstream parts in four steps; the ranking score, the adapter's part of a changing universe, the strategy class and halts each wait on a record of their own

## Context and Problem Statement

The owner decided on 2026-11-11 that strategy work in this repository concerns extreme movers only. The
practice is the owner's, restated here on 2026-11-11 from the owner's own records, which are not part of
this repository: it is the requirement, not evidence. Rule 3 allows glue to an upstream, not invention. Which upstream part carries each need,
what has no upstream, and in what order does the work start? Nothing here recommends a trade or a strategy.

## Decision Drivers

* The requirement, restated:
  1. **Rank.** Scan the whole market and trade only the most promising movers, found as they start. The
     score is the move from the latest completed official regular-session close times unusual volume:
     a ratio against the symbol's own normal for that time of day, not an amount of dollar volume. A
     stale ranking means no entry.
  2. **Enter early.** Buy the first rising run of the first leg with a limit order, in every session;
     an extended move only on a pullback and reclaim. Paper size follows the startup account allocation
     (Paper sizing below, replacing risk sizing on paper 2026-11-11); the original risk-sizing row remains a
     legacy simulation setting.
  3. **Protect.** A sell trigger below the entry, outside the tape's noise, that only rises with the
     high; a gain lock once the gain covers its costs; a breach must persist before the fast sell, a
     limit that chases within a cap. A broker stop is the backstop for an outage only.
  4. **Re-enter or rotate.** After a fast sell and a cooldown, buy back on a reclaim while the name
     still ranks, or rotate into the new top mover.
* Its process: research, replay, sandbox, paper, then live, which is the owner's alone, long only at 1x
  (ADR 0009). Paper is never gated by profit and loss, and every threshold is an uncalibrated row until
  a measurement calibrates it.
* Long only, in every step and on every account: an order here buys to open or sells what is held. The
  practice refuses short selling and options trading without a further decision, and no such decision
  is recorded here.
* The evidence rule (the owner, 2026-11-11): every factor, definition, threshold and method in this
  record and in each gap's record is sourced to a maintained upstream read at source, to the vendor's
  documentation or to a measurement made here; anything else is a registered row marked UNCALIBRATED.
* What is not known, and not measured here: whether early entry with a tight trigger and re-entry
  beats holding flat, or holding to the close, after costs. M3 measures it.
* Upstream parts as shipped; a missing part is a stated gap and the work stops there (rule 3).

## Considered Options

1. Keep ADR 0068's direction: upstream's shipped strategies as shipped, the quote study (ADR 0077) next.
2. Write a scanner framework, a pacer, a custody layer and an exit engine here.
3. A thin composition of upstream parts, in four steps, with each missing part a gap that stops the work.

## Decision Outcome

Chosen: **option 3**. Option 1 is no longer the focus the owner set. Option 2 is invention under rule 3:
no upstream ships those parts, and none is written here.

**The parts**, each read at the pin (`nt:` is `nautilus-trader@d53849b216e2`, v2.0.0rc8; `ap:` is
`alpaca-py@c42add7c0a70`, v0.45.0) or in Alpaca's pages (Evidence):

| Need | Upstream part, as shipped | State |
|---|---|---|
| Full-market data | The stock stream's `*` wildcard on trades, quotes, bars and statuses (`ap:alpaca/data/live/stock.py:75-145`; the streaming page), on the `sip` and `boats` feeds (`:13-19`); snapshots of many symbols (`ap:alpaca/data/historical/stock.py:205`) | Not through the adapter, which serves loaded instruments only (`canon_alpaca/data.py:1374`). M0 probes it with alpaca-py directly (`canon_trading.movers_stream`, `movers_screener`) |
| A first candidate list | `ScreenerClient.get_market_movers` and `get_most_actives` (`ap:alpaca/data/historical/screener.py:50-72`) | A cross-check until M0 measures it: "resets at market open", no date parameter (`ap:alpaca/data/requests.py:487-520`), so no history |
| Ranking score | None | **Gap 1** |
| A universe that changes in a session | Subscriptions take one id or a list (`nt:python/nautilus_trader/common/__init__.pyi:818-835`, `docs/concepts/actors.md:559`); `request_instrument` (`:989-997`); `Controller` creates and removes strategies at run time, in a live node and a backtest (`nt:python/nautilus_trader/trading/__init__.pyi:97-118`, `live/__init__.pyi:1164`; `BacktestEngineConfig(controller=…)`, `backtest/__init__.pyi:159`) | **Gap 2** |
| Entry | `OrderFactory.limit` (`nt:…/common/__init__.pyi:1388`); the adapter's `extended_hours` param (ADR 0038) | Exists |
| Paper size | Alpaca TradeAccount.buying_power and cash, divided by configured slots, whole shares through instrument.make_qty | ADR 0091; cash funding limit reproduced against pinned rc8; no margin model |
| Legacy size from risk | `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, …, hard_limit)` (`nt:python/nautilus_trader/risk/__init__.pyi:16-29`, `crates/risk/src/sizing.rs:87-89`) | Explicit simulations only |
| Rising trigger, fast exit | The order emulator: `TRAILING_STOP_LIMIT` on `BID_ASK` or `LAST_PRICE`, released as a `LIMIT` through the risk engine (`nt:docs/concepts/orders/emulated.md:15-19`, `:71-73`, `:129`; `…/common/__init__.pyi:1523-1541`) | UNVERIFIED on paper: M1 |
| Backstop | Alpaca's stop and trailing stop (`ap:alpaca/trading/requests.py:496`, `:624`) | Regular session only: a trailing stop "will not trigger outside of the regular market hours", and extended hours take limit orders only |
| Halts | Trading-status messages (`ap:…/live/stock.py:135-145`) into `InstrumentStatus` (`nt:python/nautilus_trader/live/clients.py:267`, `docs/developer_guide/python_adapters.md:246`) | **Gap 4** |
| Indicators | `AverageTrueRange`, `VolumeWeightedAveragePrice`, the moving averages (`nt:python/nautilus_trader/indicators/__init__.pyi:155`, `:379`, `:1013`) | To adopt; none written here |
| Replay over many symbols | `BacktestRunConfig(chunk_size=…)` (`nt:docs/concepts/backtesting/apis-and-runs.md:155`) | Not used by `canon_trading.backtest` yet |
| Adoption test | skfolio's `deflated_sharpe_ratio`, in the gate (ADR 0031) | Built for the Sharpe term only; see below |

**The gaps.** Each stops the work it names until a `proposed` record of its own lands, before any code.

1. **The ranking score.** ADR 0087 records the score, its pure inputs and the actor that publishes
   `movers-rank`; ADR 0091 composes it with a fixed startup roster. Paper delivery is UNVERIFIED until
   the baseline artifact and adapter's external data are present.
2. **A changing universe.** The mechanism is upstream and this adapter's part is unbuilt. Upstream's
   Polymarket adapter loads a missing instrument at run time on a subscription or `request_instrument`,
   and its Hyperliquid adapter passes new definitions to the execution client without a restart
   (`nt:docs/integrations/polymarket.md:2480-2496`, `hyperliquid.md:643-649`). This adapter lacks
   `_request_instrument`, which the base class raises on (`nt:…/live/clients.py:327-328`), and each
   client loads its own provider, so an order for an unloaded instrument is denied
   (`canon_alpaca/factories.py:59`, `:82`; `execution.py:1875`). Leaning: one strategy an instrument,
   created by `Controller`, UNVERIFIED in a live node; an adapter change of its own adds the request as
   Polymarket's does, and the engine's position check stops naming one instrument (`paper_node.py:98`).
   Two parts have no upstream. Nothing upstream scans a market or chooses a universe. And the notional
   cap is a map fixed at node build, an instrument outside it has no limit, and its setter has no Python
   binding (`nt:crates/risk/src/engine/mod.rs:1448-1454`, `:441-446`): an instrument without an entry is
   not traded, and a late one has an entry only if the map is filled from the asset list at build.
   **Composition update, 2026-11-11 (ADR 0091):** the startup sample supplies several qualified names,
   loaded into both providers and the reconciliation/risk maps before the node starts. One Mover
   rotates within that fixed roster; each name has its own entry share cap and position monitor.
   This enables several slots without Controller; runtime admission of a new name remains unbuilt.
3. **The strategy class.** The pin ships five example strategies, none of this kind
   (`nt:crates/trading/src/examples/strategies`). It is the one piece written here, a composition of the
   rows above, in a record of its own before any code.
4. **Halts.** The adapter implements no `_subscribe_instrument_status`. alpaca-py has no subscription
   for LULD bands (the channel key alone, `ap:alpaca/data/live/websocket.py:62`). In the pages read,
   Alpaca states what happens to an order in a halt for the overnight session only.

**Starting rows, all UNCALIBRATED**, registered and not argued, with no source claimed for a number
beyond the owner's practice: every NMS common stock and ADR at any price; entries from 04:00 New York;
move at least 3%, volume ratio at least 3, trade acceleration at least 1.5; five names at once, two for
movers; planned risk 1% a trade (on paper, replaced 2026-11-11; Paper sizing below), capped at 5% of the prior five minutes' dollar volume; limit orders
only outside the regular session; the broker stop under 3%.
The lead's later 2026-11-11 paper instruction supersedes that size row: read reported buying_power at
each startup and divide by actual configured slots. The adapter models CASH, so after reproducing its
free-balance denial the instructed conditional engine limit makes the figure min(buying_power, cash)/slots,
named in the record. This uses no numeric monetary policy constant, extra multiplier, DTBP or PDT field.
Paper Mover does not use FixedRiskSizer, risk or volume clamps to size; the older rule remains explicit
in simulations. ADR 0091 carries the shared figure through selection, entries, caps and the rank actor.

**The adoption test.** A rule is adopted only when it beats both the incumbent and holding flat at
doubled costs, with a deflated Sharpe ratio at 0.95 or above; merit on paper has a floor of at least 30
round trips in at least 5 sessions. The Sharpe term is ADR 0010's; the doubled costs, the round trips
and the sessions are the requirement's, UNCALIBRATED. The practice also names a probability of backtest
overfitting of at most 0.05. The lead ruled on 2026-11-11 that it is carried here as an UNCALIBRATED row and a reported
diagnostic, not a pass condition: the method's own paper scores the example it calls a valid backtest
at 13%, so 0.05 would reject it (ADR 0027). This departs from the practice's figure, and the owner may
set a level once a study here gives values to calibrate it on. Only the Sharpe term is built (ADR 0010,
ADR 0031). The diagnostic is decided and not yet built: skfolio's `CombinatorialPurgedCV` makes the
splits and its Sharpe ratio the ratios, and the last two steps are code here (ADR 0032). It is built
before M3. The backtest's fees are zero (`backtest.py:172`, ADR 0067), so a cost model enters the trial
key first. A trial that never trades stops a ranking (ADR 0010), no incumbent has passed (ADR 0052) and
the gate counts no round trips, so M3's pre-registration fixes what each term means.

**Order of work.**

* **M0, no orders.** Under the data-only task, admitted by ADR 0070's dated line of 2026-11-11. In the
  regular session and after hours: one `sip` connection on `*` minute bars, then trades, with rates and
  memory for 15 minutes; the screener polled each minute beside it; the score for the top 50; how many
  of the screener's gainers it finds, and how early.
* **M1.** One mover a window on paper through the window runner's `--strategy` (ADR 0044), its name
  fixed before the node is built, so gap 2 does not bind it. Sized by `FixedRiskSizer` (on paper, replaced 2026-11-11;
  Paper sizing below), protected by the
  emulated trailing stop-limit, ending flat, in every session. It needs gaps 1 and 3 recorded, the
  window's SPY-only refusal and $1,000 cap replaced (`session_window.py:531`, `paper_node.py:58`), and
  flat checked across the account.
* **M2.** A fixed startup roster, re-entry and rotation in one Mover (ADRs 0088/0091). The current
  paper rows are ten held slots in regular/after-hours and one in pre-market/overnight, configurable by `--slots`,
  with `--spares` additional names (default 2, UNCALIBRATED). Entries have no count bound; a sale
  frees its slot for a reclaim after the cooldown or another ranked name until wind-down. ADR 0087's
  actor re-ranks the loaded roster; missing/stale/invalid ranking means no entry. A paper run of this
  composition is UNVERIFIED; Controller is reserved for future runtime admission outside the roster.
* **M3.** A pre-registered replay study through the gate: the starting rows as configurations, against
  holding flat at 1x and 2x costs. A replay cannot read the screener, so it rebuilds the universe from
  bars of every symbol, and the provider loads active assets only, so the delisted are missing.

M1 and M2 are checks of the stack, as ADR 0068 reads a paper window, not trials: nothing they earn
enters a ranking.

**Paper sizing, dated 2026-11-11.** By the owner's decision, 2026-11-11, as the lead (canon-fa) recorded it
under ADR 0074 rule 1, with the lead's figures of the same evening: the paper accounts carry no risk limit.
The entry size is the smaller of the account's buying power and its cash at the node's start, divided by the
slots in use, read again at each start. Cash keeps a margin account's buying power, up to 2x overnight and 4x
intraday at Alpaca, from sizing an entry above 1x until the margin-account build lands (a Codex build the
coordinator started on 2026-11-11). The engine's per-order notional cap is the entry times the existing outer
multiple (`mover_window.OUTER_BOUND_MULTIPLE`, 10), so the sale of a position that rose is never denied (ADR
0088). The slots are 7 in the regular session and 7 after hours (the lead, 2026-11-11), raised only after two
paper firings show room, and 7 in pre-market and overnight too, by the same worst-minute count (the lead,
2026-11-11); going above that count needs two records that agree. One limit: a paper fill takes any size at
the quote (Alpaca's paper page: "Your order quantity is not checked against the NBBO quantities"), so a size far beyond a name's traded
volume teaches less than it seems to. The comparison of fills against a replay is where that shows. It is a
learning note, not a cap. Live is untouched and stays the owner's. This replaces the window's former sizing
(`mover.py`) on paper; ADR 0091 carries the same text for the window. Built with the composition
(2026-11-12): each Mover node's entry is `min(buying_power, cash) / slots` from one startup account read
(`session_window`), taken in whole shares by `Mover` with `size_from_budget=True`, and the engine's per-order
cap is that entry times `OUTER_BOUND_MULTIPLE`. `FixedRiskSizer` at `risk` 0.01 remains only for explicit
simulations.

**Slots measured, dated 2026-11-11.** A reader of the night's instrumented paper windows found the worst count
of trading-API requests in any 60 seconds of one window at 14 to 15, in the regular session and after hours
alike, and the two windows of each part agree within one request. The after-hours windows ran
`SessionMaCross`, not `Mover`, and the runner runs one window at a time under its locks. With 40 of the
account's 200 a minute held in reserve, floor(160 / 15) = 10. The lead set **7 slots in the regular session
and 7 after hours** the same evening, a reserve under ADR 0091's 180 and not a risk limit, from a worst-minute
count per slot this record states separately. Both are conditional: they count one window's peak once per
slot, no window has run with several names at once, and the first firing that composes several names measures
it. Under `Mover`'s soft line of 140 in one process the same arithmetic gives 9. Pre-market and overnight had
no instrumented round trip, so their slots are not measured; the per-order model below gives 3 names outside
the regular session at 160 (1 at the old 100), and the lead set them at 7 by the same count.

**The request budget, measured 2026-11-11 (Task M gap 7).** The earlier 19/47-per-order planning
coefficients were UNVERIFIED; ADR 0076 is absent, and they are replaced by the following offline
measurement, not promoted to a worst-case guarantee. Alpaca's own archived Trading API documentation
(Evidence, "Rate Limit") states 200 requests a minute per key, including paper. That is the default
`request_limit_per_minute` setting, never a value obtained from an account. ADR 0088's 180-in-60-s
failure ceiling and Mover's existing entry soft line stay in force. Current paper/live enforcement
and per-key versus per-account scope are UNVERIFIED, as ADRs 0049 and 0069 record.

`mise exec -- uv run --locked pytest tests/test_request_budget.py` runs a scripted workload on the
pinned NautilusTrader `BacktestEngine`, cash/netting with quotes and the native 50 submits/minute
setting. In each slot a limit buy fills, a limit sell fills, a buy-back fills, and a fourth limit
ends flat, all within one minute. With chasing enabled, the first sale rests, is canceled after five
seconds and resubmitted on the following quote, eight times before filling; every replacement's
price comes down. The generated submit/cancel commands are then replayed through the actual
`AlpacaExecutionClient` and alpaca-py to an in-memory `HTTPAdapter.send` boundary. The backtest checks
fills and final positions; the second stage counts HTTP attempts, including the SDK's retries, with
regular and extended-hours flags. A fresh instance of the normal request counter on the simulation
clock independently confirms totals and both peaks without adding synthetic traffic to other tests.
It does not replay Mover's ranking, cooldown or entry decisions.

For this fixture's HTTP 200 submits and HTTP 204 cancels with a known venue order ID, a submit costs
one POST and a cancel/resubmit pair one DELETE plus one POST, in either session.
Entry/sale/re-entry therefore costs three requests per slot; the flat ending adds one. Six open-order
checks and one position check add seven GETs. For N slots and C chase pairs per slot this fixture
measures **7 + N × (4 + 2C)** requests in its minute, including the flat ending. Injecting three 504
responses before success on every check measures **28 + N × (4 + 2C)**: each of the seven GETs really
makes four transport attempts. Order commands are sent once. The fixture counts its own minute
buckets; it also verifies that every fill and attempt is in that minute, so its rolling peak agrees.

| Measured workload (either order flag) | 1 slot | 2 slots | 3 slots | 4 slots |
|---|---:|---:|---:|---:|
| Cycle and flat ending, no chase | 11 | 15 | 19 | 23 |
| Same, three retries per check | 32 | 36 | 40 | 44 |
| Eight chase pairs per slot | 27 | 47 | 67 | 87 |
| Same, three retries per check | 48 | 68 | 88 | 108 |

Four slots use 48 submits in the chase fixture, below the unchanged native 50; the measured peak of
108 is below both 180 and the 200 setting, so this measurement requires no slot reduction. If a
broader workload exceeds its configured budget, reduce the slot setting and repeat it; never raise
the checks to admit it. These rows exclude startup, post-stop account checks, missing-fill catch-up,
reconciliation beyond the periodic open/position checks, replace-chain reads, partial fills, submit
403/422 refusal lookups (`_refused`), cancel 422/fill-race lookups (`_cancel_refused`), unknown-venue-ID
cancel lookups (`_cancel`), all retries of those GETs, and other processes using the same account.
Their aggregate maximum and a reservation for exit headroom before
entry remain UNVERIFIED; the sampled soft line and post-run failure check are not such a reservation.

Every window record now names its budget setting, average requests/minute over the process observation
(module import before preflight through cleanup), UTC-minute peak and its timestamp, rolling 60-s peak,
and remaining rolling headroom, per sanitized account number and API. Market-data requests are retained
without applying the Trading API limit to them. Running and refused records name the setting too.
The provider keeps peaks but **no minute buckets**: the record says `minute_buckets_kept: false`, and
the average is not a minute-by-minute reconstruction. Adding sanitized buckets later belongs to the
Claude lane; this change neither wraps nor edits the credential-handling provider.

**Interrupted measurements (budget reader P2-2, 2026-11-11).** A record write normally refreshes both
request representations from the sanitized counter. Shutdown captures one final snapshot after
cleanup and log preparation, checks that snapshot and persists it without re-sampling. A preparation
error still attempts one final snapshot before writing the failure. `request_budget.measurement_status`
distinguishes running, complete, interrupted, failed,
refused and unfinalized snapshots. `calibration_eligible` is true only for an explicitly completed
measurement with a known positive observation interval; the window additionally needs an `ok` result
and an end timestamp. Ctrl-C, SystemExit and an abandoned/timed-out node mark it interrupted even
after cleanup takes a final snapshot. A process killed without running Python finalizers cannot
produce a final count; its last running snapshot already carries `calibration_eligible: false` and
must not be treated as a full-window observation. No minute history or missing final traffic is
reconstructed from it.
An unavailable counter leaves prior counts explicitly marked with `snapshot_error` and excluded
from calibration, while the primary window failure is preserved. A bad observation clock retains
the counted attempts with an unknown average, `observation_error`, and no calibration eligibility.

**Seven slots in every part, and the backstop (the lead's decisions of 2026-11-11).** `Mover`'s own chase
cadence is `chase_secs` 5 plus the one-second step: about ten cancel/resubmit pairs a minute for a slot
whose sale rests the whole minute, so C = 10 and a slot costs 4 + 2 × 10 = 24 requests in its worst
minute. Seven slots give 7 + 7 × 24 = **175** in the measured fixture's terms, under the 180 failure
ceiling and the 200 setting; with three retries per check, 28 + 168 = 196, still under 200 but over 180,
so a minute of retries at full chase is the failure the record names, not a budget. The lead set seven
slots in every part, overnight and pre-market included (L3 carries the numbers); a raise comes only
from two firings' measured peaks. NautilusTrader's own throttle is raised to the same 180 a minute for
submits and modifies on paper nodes (ADR 0049's dated line), since at 50 its failure handler denies an
over-limit submit, an exit included (`crates/common/src/throttler.rs`, `crates/risk/src/engine/mod.rs` at
the pin). No separate Mover submit gate is added. The backstop: at `soft_line` (140 in the last 60 s)
`Mover` sends no new entry and no chase; exits and cancels still go. **Limit:** failing a window only
when an exit is refused for rate is not built; `request_failure` (a rolling peak of 180 or any 429)
stays the failure for the first firings.

**Earlier records.** This supersedes ADR 0068's direction, which strategies run; its lanes, bounds and
landed steps stand, and it carries a dated pointer. ADR 0077's quote study is held unrun.

### Consequences

* Good: one focus, every part named at its pin, and four places where the work stops and nothing is
  invented.
* Bad: nothing trades until gaps 1 and 3 are recorded, and whether the practice beats holding is not
  known until M3.
* Bad: seven slots in every part (2026-11-11) rest on single-name windows, the measured fixture and
  `Mover`'s chase cadence, not on a firing's measured peak; pre-market and overnight have no measured figure
  of their own, and two firings' peaks come before any raise.
* Bad: the measured four-slot workload is bounded, but does not establish a maximum for missing-fill
  catch-up, startup, shutdown or all traffic on the account; admission with reserved exit headroom is
  still unbuilt.

### Confirmation

* Composition update, 2026-11-11: ADR 0091 replaces the single-name/one-entry window with a fixed
  qualified roster and actor generations, per-name share/position bounds and all-symbol account
  checking. The registered listing arm is the default in its 04:00-16:00 scope with explicit Day
  history; outside it the window records its bridge entry arm and reason. Simulations exercise
  several held names, re-entry, rotation and failed/missing input refusal. Paper delivery of the
  composition is UNVERIFIED; no performance or calibration claim follows from these checks.

* No strategy code lands under this record; each gap's record or change names it.
* When a gap's record lands, that gap's paragraph here shrinks to one line and a pointer.
* M0's figures and each paper run are added here, with command, exit and log.
* Accepted 2026-11-11 on the lead's read, after one evidence-verifier pass.
* 2026-11-11, the mover window (`session_window --mover`): a one-day bridge and a check of the stack on a
  mover (spreads, halts, the feed), not the practice's strategy and not M1. It runs the existing
  long-only `SessionMaCross`, one share, its marked DAY limit through the touch for every order (never a
  market entry, the practice's rule restated by the lead on 2026-11-11), regular session only, on `sip`, on one name
  chosen before the node is built (`choose_mover`). The name is the top of `movers_stream.rank` over a
  three-minute sample of the whole market's sip minute bars on the window's own pair (`stream_mover`:
  `subscribe_bars(handler, "*")`, then `movers_stream.refresh`); if the sample fails or qualifies no name,
  over Alpaca's 50 gainers (`get_market_movers`, `get_stock_snapshot`, `get_stock_bars`, `read_normals`),
  and the record says so. The pick is the first that
  `TradingClient.get_asset` gives `status` active and `tradable` (`ap:alpaca/trading/models.py:37-97`,
  `client.py:399`), whose name does not mark a warrant, unit or right (a name match, a proxy: an ETF is
  not told apart, UNVERIFIED), and that has no corporate action since the last session
  (`CorporateActionsClient.get_corporate_actions`). Halted has no REST read at the pin and Alpaca's assets page has `overnight_halted`
  only; the statuses channel (`ap:…/live/stock.py:135`) is not used, since Alpaca's page does not say a
  subscription is sent the current status, so not-halted is approximated by a latest sip trade at most
  120 s old (UNCALIBRATED, a proxy with no upstream behind it). No price floor. The
  SPY-only refusal is now "one share must fit the cap" for any symbol: for a mover at the entry limit it
  would send (the snapshot's ask through the touch, since the risk engine prices a limit at its limit;
  corrected on a second reader's finding, 2026-11-11), otherwise at the latest sip trade
  (`get_stock_latest_trade`); the instrument loads through NautilusTrader's
  `InstrumentProviderConfig(load_ids=...)`; the start check and the check after the stop read the whole
  account (`get_orders` with no symbol, `get_all_positions`) and act on the window's symbol only. The
  record names the symbol, its rank inputs, the ranked and passed-over names and the screener's list.
  A limit found on its first run (YMT, account A, 2026-11-11: no order, no cross in 25 live bars, the
  10-bar average below the 20 on every bar as YMT fell): the node's time bars are built for every clock
  minute, trades or not, since NautilusTrader's `time_bars_build_with_no_updates` defaults to true at the
  pin (`nt:crates/data/src/engine/config.rs:36-38`), so a volatility pause adds flat bars that Alpaca's
  sip bars do not have (25 against 9; by count, the bars were not kept). For a mover's averages and
  ranking Alpaca's bars are the right ones; a mover node sets it false, as upstream's data testing spec
  does (`nt:docs/developer_guide/spec_data_testing.md:42`). Not changed in the bridge.
  The same reader's other findings, fixed the same day: the fallback ranks on completed minutes only, the
  stream path measures trade age on Alpaca's clock, and a symbol is ranked on the sample only with a bar
  in every one of its minutes. Recorded, not fixed: a close the account check sends has its status in the
  record and not its fill price, and an order denied before Alpaca accepted it has no submit delay.
* 2026-11-11, the mover window outside the regular session (the lead's earlier decision of that day,
  superseded by the later decision in ADR 0088 and ADR 0091, below, in place of
  "regular session only" above): under the schedule's `mover` value the regular part runs `Mover`
  (ADR 0091), and pre-market and after-hours run this bridge as an extended window: `SessionMaCross` on
  the name `choose_mover` gives, one share, every order its marked DAY limit with `extended_hours`
  (ADR 0038), on `sip`, which carries both parts (`session_window.MOVER_PARTS`). It has no overnight form:
  the choice's sample, latest trades and quotes are read on `sip`, and Alpaca's 24/5 page puts the
  overnight session's data on `boats` and `overnight`; a scheduled firing there has nothing to run and
  says why, and a run by hand is refused by name. The screener fallback ranks the regular session's
  minutes only: before the open it has none, and after the close today's close would leave it the
  after-hours drift against the whole day's volume, so outside the regular session the stream sample is
  the only source, and a window whose sample fails is refused naming both causes (a refused firing, exit
  1, not a skip). UNVERIFIED until a paper firing in each part shows it: that a sample outside the
  regular session ranks a name, that Alpaca takes an `extended_hours` limit on the name chosen, and that
  the bridge's limit exit fills there. A position it leaves is named by the account check after the
  stop (ADR 0060), which cannot close at market outside the regular session, and every later window
  is then refused as not flat until `mise run paper:close windows` (recorded, not fixed).
* Later 2026-11-11, the lead lifted the regular-only limit for `Mover` itself (ADR 0088, ADR 0091):
  `--mover` runs it in pre-market and after-hours on SIP and overnight on BOATS, each with one slot
  as a run setting and every venue order a marked DAY limit. A part's explicit `bridge_reason`
  records why `SessionMaCross` runs there instead. The regular part keeps the strategy its arguments
  name. Overnight normals use the preceding evening of each prior weekday trade date; action checks
  include the overnight trade date, and candidates need an affordable ask and overnight eligibility.
  The screener fallback remains regular-only. Its overnight form ran on paper that night (Evidence);
  `Mover` in pre-market and after-hours has not yet run on paper (UNVERIFIED).
  Recorded, not fixed (code review of that day, finding 1; the lead kept it): outside the regular part
  `Mover` enters as ADR 0091 sizes it, up to 500 USD under the engine's cap, not the bridge's one
  share, and its only exit there is the marked-limit wind-down, after which the stop cancels. What is
  left goes to the account check after the stop, which outside the regular session sends marked DAY
  limits through the latest quote on the part's feed and cancels the last as it ends (ADR 0060's
  paragraph of that day); on a thin book those can go unfilled, the window is then not flat, and every
  later window is refused. What covers that until account B's repair makes the close automatic: on any
  window that ends not flat, or a firing refused for what is left, the coordinator runs
  `mise run paper:close windows` at once. A per-part entry size, its default unchanged, is the next
  round's.
  Recorded with it, not fixed (a builder's regression, the lead's ruling of that day): above $1 the
  emulator can release the trailing exit as a limit with fractional cents, since NautilusTrader's native
  emulator rounds the trailing price only to the instrument's 0.0001 increment
  (`crates/execution/src/order_emulator/emulator.rs`, `crates/execution/src/trailing.rs`), and the
  adapter's `sub_penny_denial` refuses it. On such a name the protection is refused at release and the
  wind-down is the exit, until the adapter rounds to the venue's tick at submit (ADR 0030). Not yet seen
  on paper (UNVERIFIED there). Answered later on 2026-11-11: the adapter now sends such a limit on
  Alpaca's cent, a sell rounded down (ADR 0030), and the regression above passes through it.

## Evidence

Read 2026-11-11. Fetched means read in this session as raw text; resolved means read in a local copy at
the commit `canon/upstreams.lock.json` records.

* https://raw.githubusercontent.com/alpacahq/alpaca-docs/master/content/api-references/trading-api/_index.md
  — Alpaca's archived documentation, "Paper Trading" and "Rate Limit": 200 requests every minute per
  API key, 429 on excess. Fetched for Task M gap 7; current paper/live enforcement and per-account
  versus per-key scope are UNVERIFIED, not measured here (ADRs 0049/0069).
* `nautilus-trader@d53849b216e2`: `python/nautilus_trader/backtest/__init__.pyi` (`BacktestEngine`,
  `add_venue`, `add_data`), `trading/__init__.pyi` (`Strategy`), `live/clients.py` (execution hooks),
  `live/providers.py` (`InstrumentProvider.add`) and `common/__init__.pyi` (`Clock.new_test`). Resolved
  for the fast-cycle simulation; no upstream code was run from the read-only source copy.
* `alpaca-py@c42add7c0a70`: `alpaca/trading/client.py` (`submit_order`, `cancel_order_by_id`,
  `get_orders`, `get_all_positions`), `alpaca/common/rest.py` (`_request`) and `common/constants.py`
  (three retries). Resolved; the installed pinned packages run in `tests/test_request_budget.py`.

* The owner's practice — the requirement, the starting rows and the adoption terms, restated in this
  record on 2026-11-11 from the owner's own records, which are not part of this repository. An
  instruction, not evidence: no claim here rests on it, and each number of it is UNCALIBRATED.
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — "every symbol using the `*` wildcard";
  405 "symbol limit exceeded", 406 "connection limit exceeded"; "in many subscriptions (or without one)
  this limit is 1". Fetched. Whether `*` counts against a symbol limit is not stated: UNVERIFIED.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — "Websocket subscriptions | 30 symbols |
  Unlimited" on the two equity plans. Fetched.
* https://docs.alpaca.markets/us/reference/movers-1.md — `top` at most 50; "based on real time SIP
  data"; "For stocks, the endpoint resets at market open. Until then, it shows the previous market day's
  movers." Fetched. No plan, refresh interval or numeric rate limit on the page.
* https://docs.alpaca.markets/us/reference/mostactives-1.md — by volume or trades, `top` at most 100.
  Fetched.
* https://docs.alpaca.markets/us/reference/stocksnapshots-1.md — latest trade, quote, minute bar, daily
  bar and previous daily bar for a list of symbols; no stated maximum. Fetched.
* https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md — the feeds; "Trading Status"
  ("reason for the trading halt if any") and "LULDs" messages. Fetched.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Only limit orders with `time_in_force` set
  to `day` or `gtc` orders are accepted as extended hours eligible"; "Trailing stop will not trigger
  outside of the regular market hours"; brackets: "Extended hours are not supported". Fetched.
* https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id.md — `status` "active or
  inactive", `tradable`, and `attributes` whose one halt value is `overnight_halted`; no general halt
  field. Fetched 2026-11-11 for the mover window's line (Confirmation).
* https://docs.alpaca.markets/us/docs/245-trading.md — the four sessions; an order for a security halted
  overnight is "accepted" and "held in a pending state". Fetched.
* NautilusTrader at the pin
  (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186) —
  every `nt:` line above. Resolved. `scanner` and `universe` appear in no example or Python module,
  and in the docs only on venue pages (Hyperliquid, Polymarket, Derive, the blockchain adapter), where
  they mean a venue's instrument list. The release candidate rc9 adds a sixth example strategy,
  `orderbook_imbalance`; it is not pinned.
* alpaca-py at the pin (https://github.com/alpacahq/alpaca-py/tree/c42add7c0a7026e2ead3ed64e76f4def2b99fda4)
  — every `ap:` line above. Resolved.
* `skfolio@772109053763:src/skfolio/measures/_measures.py:1411`, `model_selection/_combinatorial.py:71`
  — `deflated_sharpe_ratio`, `CombinatorialPurgedCV`. Resolved.
* `packages/canon-alpaca/src/canon_alpaca/` (`data.py`, `execution.py`, `factories.py`),
  `src/canon_trading/` (`session_window.py`, `paper_node.py`, `backtest.py`, `movers_stream.py`) — as
  cited. Read.
* Corrections to the request, found on reading: the pin ships five example strategies, not six; list
  subscriptions are at the pin, not new in rc9; the overfitting figure is a reported diagnostic here,
  by the lead's ruling of 2026-11-11 on ADR 0027's evidence (§3.1 and §6 of the paper, as that record
  reads them).
* `nautilus-alpaca-gmatrunich@c899426ca2d6:nautilus_alpaca/data.py:300-312` — a community adapter, role
  `watch`, answers `_request_instrument` from its provider's `load_async`. Resolved; read, not adopted.
* Second reader (`evidence-verifier`), 2026-11-11: gap 2 restated, since upstream's Polymarket and
  Hyperliquid adapters admit instruments at run time; three line ranges; the adoption terms; M0 under
  ADR 0070; five names over the line in the regular session too; the Evidence labels. Applied above.
  Not applied: the split the length rule asks for (Confirmation). Not checked by it: ADR 0076's figures.
* The lead's read, 2026-11-11: the requirement is stated here in full and carries no pointer outside
  this repository; where a term had two readings, the practice's is the one kept. Re-read the same day
  against the owner's evidence rule: no result of the practice's own research is stated, and each of
  its terms without a source here is marked UNCALIBRATED.
* Second reader of the mover window's code, 2026-11-11: the cap was checked at the last trade, not at
  the entry limit the risk engine prices (`nt:crates/risk/src/engine/mod.rs`, `order_price`), plus three
  ranking findings and one record gap. Applied in the mover window's dated line (Confirmation).
* Code review of the mover window outside the regular session, 2026-11-11: the fallback ranked an
  after-hours move against the whole day's volume, and the part's other windows' feed decided whether
  the bridge ran; both fixed with failing tests first. The leftover position after 20:00, a sample-only
  pre-market and an `extended_hours` limit on the chosen name are recorded in that dated line.
* `Mover` overnight on paper account B, by hand, 2026-11-11, 20:20 to 20:47 New York, on `boats`:
  the stream sample ranked 10 names and chose PLCE; one entry of 694 shares by a marked DAY limit,
  filled at 0.7202; the wind-down canceled the emulated stop and sold 694 by a marked DAY limit, filled
  at 0.7212; record `ok`, nothing left, no denial, no error, the trading API's peak 16 requests in any
  60 seconds; `paper:account windows` afterwards flat.
* Code review of `Mover` in every part, 2026-11-11: nine findings. The first is recorded in the later
  dated line above; the other eight, and a per-part entry size, are the next round's.
* A reader's budget report on the night's paper windows, 2026-11-11, its table of slots: the worst
  rolling 60-second trading count 15 in the regular session and after hours, floor(160 / 15) = 10 with a
  40-request reserve (the lead set 7 on 2026-11-11), pre-market and overnight not measured; and its
  finding that the 100-request line here was stale against `REQUEST_CEILING = 180`. Read; its raw window
  records were not re-read here.
* https://alpaca.markets/support/usage-limit-api-calls — "currently 200 requests per minute, per account".
  https://docs.alpaca.markets/us/docs/paper-trading.md — the random partial fills, no market impact or
  latency slippage, and quantity not checked against the NBBO. https://docs.alpaca.markets/us/docs/margin-and-short-selling.md
  — buying power up to 2x overnight and 4x intraday on a margin account. Fetched by the evidence-verifier
  on the sizing edits, 2026-11-11.
* evidence-verifier on the sizing and slot edits, 2026-11-11: the cap at the entry contradicted ADR 0088,
  the window sizes by `FixedRiskSizer` under the 500, the after-hours peaks came from `SessionMaCross`,
  and four lines were stale. Corrected above, with the lead's figures of the same evening.
