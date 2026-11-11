---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude); the lead, under ADR 0074
review-by: 2026-12-06
---

# Upstream's strategies end to end around the clock: a backtest queue, the sandbox on live data, paper windows, bounded by the account and the stream

## Context and Problem Statement

The owner's instruction of 2026-11-10: keep the paper and simulation lanes in use around the clock, five
days a week for paper and seven for simulation, running current strategies end to end, since the paper
account's request limit (200 a minute) is not the constraint. Here current strategies means strategies a maintained upstream ships, through ADR
0010's protocol; this recommends no strategy, trade or position. Which of upstream's strategies can run end
to end, in which lane, what bounds how many run at once, and in what order can it start?

## Decision Drivers

* Upstream's strategies as shipped, configured only; one the adapter cannot carry is listed with what is
  missing, not adapted (rule 3). Live stays untouched (ADR 0009).
* Every run that is a trial is in the ledger and raises N (ADR 0010); "unlimited" never means uncounted.

## Considered Options

1. Paper only, around the clock, on the one account.
2. Backtests around the clock, paper in bounded windows.
3. Three lanes: a backtest queue at any hour, the sandbox execution client on Alpaca's live data while a
   feed streams, paper windows while a session is open; the account and the stream are the bounds.

## Decision Outcome

Chosen: **option 3**. Option 1 runs no quote-driven strategy at night and holds the one account; option 2
leaves the sandbox unused (ADR 0070). The review-by date is the 23/5 week's target (below).

**The candidates** (`crates/trading/src/examples/strategies` at the pin). All five are reachable from
Python: `add_builtin_strategy` took each type name and refused an unknown one (run). The adapter carries
market, limit, stop and stop-limit, DAY or GTC, whole shares; it denies reduce-only, post-only, IOC,
contingent orders, and has no options (`canon_alpaca.execution`).

| Strategy | Data | Orders | On paper through the adapter |
|---|---|---|---|
| `EmaCross` | quotes (EMAs of the mid) | market, buy or sell on each cross; no position check, no flatten on stop | regular session only (ADR 0037): market orders, and its unnamed subscription is IEX |
| `GridMarketMaker` | quotes | post-only limits both sides; `close_all_positions` (reduce-only) on stop | no: post-only and reduce-only denied |
| `CompositeMarketMaker` | quotes of a target and a signal instrument | as `GridMarketMaker` | no: as `GridMarketMaker` |
| `HurstVpinDirectional` | value bars, quotes, trades with an aggressor side | IOC market entries, reduce-only IOC closes | no: IOC and reduce-only denied; Alpaca's trades carry no aggressor side (`NO_AGGRESSOR`), so VPIN never warms and it never enters |
| `DeltaNeutralVol` | option Greeks and quotes | option limits, hedge market orders | no: no options |

Upstream's TWAP execution algorithm (`TwapAlgorithm`, NautilusTrader's execution-algorithms page) is
NautilusTrader's own client-side algorithm, which splits a primary order into smaller spawned orders. It is
a different thing from Alpaca's TWAP order type, which only the Elite Smart Router route offers (ADR 0036);
the two are not to be conflated.

**Orders in flight**, each candidate read against it at the pin, since `MaCross` checks its position but
not its working orders and so sends a second order under delay (`session_ma_cross.py`'s guard exists for
that). `EmaCross` has no guard: it sends one order on each change of `fast > slow`, so a delay adds no
second order for one cross, but crosses during a delay each send one and positions stack (why ADR 0044
puts `PositionCap` beside it). `GridMarketMaker` and `CompositeMarketMaker` guard: they requote only when
none of their orders is open or in flight or the price they quote from has moved (for
`CompositeMarketMaker`, also its signal residual), and size each side by
the position plus every open and in-flight order ("cancels are async and pending orders may still fill",
`grid_mm`). `HurstVpinDirectional`
guards: no entry while its entry or exit order is pending or any of its orders is open or in flight.
`DeltaNeutralVol` guards: entries wait for no open or in-flight entry order, and a pending hedge blocks
the next.

Upstream's examples run `GridMarketMaker` through `BacktestEngine.add_builtin_strategy` on quotes
(`examples/backtest/fx_market_maker_gbpusd_bars.py`) and the tutorial's bar-based `EMACross` (ADR 0048
option 2). In simulation `EmaCross` and `GridMarketMaker` ran on quotes (run); the other three were not
run. A built-in reaching `boats` overnight would need a feed in the data client's config, which ADR 0055
considered and did not take (its option D).

**Simulation without the paper account.** Three native forms, all from Python at the pin:

* `BacktestNode`, one per run (`apis-and-runs.md`), as `canon_trading.backtest` uses it. On generated quotes
  in a scratch catalog, `EmaCross` built in, this repo's venue: a million quotes built and ran in 0.91 s
  (1.3 to 2.3 s in a second reader's reruns under load), 28,655 orders all filled; five million in 4.7 s;
  3.07 MB of catalog per million quotes; the process peaked at 0.8 and 4 GB, generating and writing the
  quotes included. Quotes at two decimals against the adapter's 0.0001 increment were dropped and every
  order rejected "No market": a quote load must write the instrument's precision (run).
* `BacktestEngine` with `reset` and `clear_strategies` (same page): a rerun over the same million quotes
  took 0.77 s against 0.75 s (`EmaCross`); no saving at this size beyond the load (run). That rerun is
  sound at rc8 only because `clear_strategies` puts in a new instance: rc9 lists "Fixed `EmaCross` and
  `DeltaNeutralVol` retaining runtime state on engine reset".
* The sandbox execution client on a `LiveNode`, matching locally on the Alpaca data client's live data:
  proved once on paper, and with the data-only task that would run it without the paper lock, decided in
  ADR 0070. Many strategies go in one node on one stream.

**What "unlimited" may not mean.** The queue runs as much as the machine carries, one study at a time on
the shared ledger, each pre-registered before its first run (ADR 0048). N is per study (ADR 0054 term 5),
so nothing in code stops a new study of the same configurations from starting at zero: a pre-registration
repeating an earlier study's configurations says so and counts them, and a re-run on a moved sample stays
in its study. The ledger has no kind for a run that is not a trial; a run outside `record_trial` is
recorded nowhere. So a smoke run uses generated data or data outside every study's sample, a rule of this
record (gap). `record_trial` builds `MaCross` only and the backtest replays bars only, so trials of
upstream's strategies on quotes need both extended and the data digest to cover quotes: the sim lane's
quotes in the catalog (ADR 0066) and what a backtest is read against (ADR 0067), both accepted.

**Paper around the clock.** A paper window is a check of the stack, not a trial: ADR 0010 sends to paper
only a pick that passed the gate, and no `MaCross` variant has (ADR 0052). The window harness (start flat,
one share and `PositionCap`, the engine's limits, ADR 0044; the account check after the stop, ADR 0060) runs
`SessionMaCross` and, through `--strategy`, the built-in `EmaCross` (step 3, Confirmation): `WindowConfig`
takes a strategy name from a fixed table, `window_node` adds it beside the cap, `--strategy` reaches
`session_window`, `paper:session` and `paper-session.sh`, and the name goes in the record. `EmaCross`
does not flatten on stop; the window's account check closes at market in the regular session only, so it
runs in regular windows. The week ("Orders at Alpaca", the 24/5 page): overnight 20:00 to 04:00 from
Sunday evening to Friday morning, pre-market 04:00 to 09:30, regular 09:30 to 16:00, after-hours 16:00 to
20:00. Windows run in the regular session and, with `EXTENDED_READY` on, overnight, on `boats` and only
for an asset marked `overnight_tradable`; pre-market and after-hours have no feed and are skipped
(`session_window.py`, `SESSION_FEEDS`). The sandbox runs while a feed streams: IEX in the regular session;
`boats` overnight on one night's observation, not an entitlement; after-hours, nothing in one 45-second
check, not proof (ADR 0055); pre-market, neither `iex` nor `boats` sent a quote or trade in 120 s at 04:53
New York on 2026-11-11 (`session_window.py`). From Friday 20:00 to Sunday 20:00 only the backtest queue
runs. This week may change: the 24/5 page plans 23/5 trading, targeted for 2026-12-06 and subject to SEC
approval, from Sunday 21:00 to Friday 20:00 with a pause from 20:00 to 21:00 Monday to Thursday.

**The bound, with numbers.** Alpaca: "200 requests per minute, per account", 429 beyond (support page,
dated 2022); a staff reply gives the default as 200 a minute for each paper and live account alike
(forum, 2025-03-24), so no higher live figure is taken. A paper node is capped at 50 submits and 25 modifies a
minute (ADR 0049) and checks open orders every 10 s and positions every 60 s, about 7 calls a minute;
cancels and queries skip the risk engine and are uncapped, and the adapter's `_query_order` reads Alpaca.
The binding bounds are the locks: each paper account has its own, account A's for the tests and account
B's for the windows (ADR 0069 term 3), and the schedule holds B's for about 28 of every 38 minutes a
session is open (a 25-minute window and about three of start and stop, then 600 s after each firing
ends, ADR 0050). Every run that streams shares the login's stream lock. Streams are the bound behind it: "The number of connections to a single endpoint from
a user is limited based on the user's subscription, but in many subscriptions (or without one) this limit
is 1", and "most users can have only 1 active stream connection", 406 beyond. Whether it binds per
endpoint or per user across endpoints is UNVERIFIED.

### More paper accounts, and which runs stream

A second paper account for windows, beside the one the tests use, with a lock per account and a stream
lock, is ADR 0069's decision, accepted and built: account A for the tests, account B for the windows. What this plan takes from Alpaca: paper accounts are
created and deleted in the dashboard "rather than resetting them", with "new API keys for any newly
created account", and the initial one starts at $100k ("Paper Trading"). One staff post (forum, 2025-11-07) says "Each owner is currently allowed 3 paper accounts
and 1 live account" and "You can actually have as many paper accounts as you wish", and reconciles them
in the same post: "sign up and create an account under a different password. Each password creates a
unique owner"; it adds "The rate limits for Alpaca data plans is enforced per owner and not per
account". The customer agreement could not be decoded (UNVERIFIED). Requests are per account, paper
included (staff reply, forum, 2025-03-24); the data plan is the user's ("It's your user ID that's tied to your data
subscription", a staff reply comparing paper and live keys), so a second account of one login adds no
data budget, and no stream if the limit binds per user (UNVERIFIED, above). No Trading API plan gives a
connection count; Broker API plans list 5 or 10. Nothing here recommends buying or opening anything.

Runs that open a market-data stream (the adapter opens it at a node's first quote or trade subscription; on
`main`): every window; `test_paper_feed.py` (2); `test_paper_matrix.py` (6); `tests/test_paper_ema_cross.py`;
`test_paper_overnight_fill.py` (its `boats` quote subscription);
and in `test_paper_execution.py` the four `ExecTester` tests, TC-E30, TC-E41 and the TC-E20 and TC-E22
stop rows (`ExecTesterConfig` subscribes quotes and trades by default; run). The rest open none. Each node
with the Alpaca execution client also opens the trade-updates stream, whose limit no page read states.

**The plan, in the order it can start.** (1) Tonight, on `main`: the sandbox on `boats` with a strategy that
names its feed, under `paper:test` (done once); built-ins in a sandbox node and in the backtest node on
generated quotes, for sizing only. (2) After ADR 0066: a study of `EmaCross` and
`GridMarketMaker` on quotes, pre-registered in ADR 0077; its trials wait for `record_trial` and the
backtest to take a built-in and quotes. (3) Done 2026-11-11: `--strategy` in the window runner, `EmaCross`
in regular windows (Confirmation). (4) After rc9 is adopted: the sandbox lane unattended (ADR 0070).
(5) Done: windows on account B while lanes test on account A (ADR 0069), and `paper:data` (ADR 0070). **Compounding:** each run ends as trials and a line in a record; a
run that exposed a defect also ends as a test and a case in `evals/` (ADR 0046, ADR 0057).

### Consequences

* Good: live-data runs with no account, many strategies on one stream.
* Bad: on paper only `EmaCross` runs as shipped; one stream connection, on many plans, bounds every
  live-data lane.

### Confirmation

* The sandbox proof run, its command, exit and log: ADR 0070's Confirmation.
* The sizing runs above: scripts kept outside the repository, run offline on generated quotes.
* Step 3, 2026-11-11: `--strategy` in the window runner, `EmaCross` in regular windows only, its name in
  the record (ADR 0044, Confirmation). Offline in `tests/test_session_window.py` (run). Under latency, from
  `ema_cross/strategy.rs` at the pin: it keeps only the last quote's `fast > slow` and sends one market
  order at each change, reading neither its position nor its working orders, so one cross sends one order
  however slow the venue, crosses while one is in flight each send another, and an order refused after
  `submit_order` returns leaves its side flipped (an error `submit_order` itself returns leaves it as
  it was). That is why its window's submit budget is one order (ADR 0044, Confirmation), with
  the cap behind it and the account check closing the share. On paper: pending, one regular window,
  `mise run paper:session --session regular --strategy EmaCross --minutes 10`.
* Accepted 2026-11-11 by the lead under ADR 0074, after one evidence-verifier pass.

## Evidence

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed here.

* https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186/crates/trading/src/examples/strategies
  — the five `strategy.rs` and READMEs, as tabled. Fetched (raw, at the commit).
* `…/examples/backtest/fx_market_maker_gbpusd_bars.py` at the commit — `GridMarketMaker` on quotes. Fetched.
  The sandbox's sources are ADR 0070's.
* https://nautilustrader.io/docs/md/latest/concepts/execution/algorithms.md — `TwapAlgorithm`, an
  `ExecutionAlgorithm` that spreads a primary order across intervals. Fetched 2026-11-11.
* https://nautilustrader.io/docs/md/latest/concepts/backtesting/apis-and-runs.md — one node per run; `reset`
  and `clear_strategies`. Fetched by the second reader.
* NautilusTrader v2.0.0rc9 release notes (published 2026-11-10) — the sandbox open-order and GTD fixes.
  Fetched by the second reader; the pin is rc8 (`scripts/canon outdated`).
* nautilus-trader 2.0.0rc8 in `.venv` — the built-in names, `ExecTesterConfig`'s defaults, the
  timings. Run; the scripts are kept outside the repository.
* `canon_alpaca` `execution.py`, `data.py`, `providers.py`; `src/canon_trading/session_window.py`;
  `packages/canon-alpaca/tests/test_paper_*.py`, `tests/test_paper_*.py` — as cited. Read.
* https://docs.alpaca.markets/us/docs/paper-trading.md — accounts, keys, the initial $100k. Fetched.
* https://docs.alpaca.markets/us/docs/streaming-market-data.md — the two connection sentences, 406. Fetched.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md — 30 symbols on Basic; Broker API 5 or 10
  connections. Fetched.
* https://alpaca.markets/support/usage-limit-api-calls — 200 a minute per account, 429. Fetched.
* https://forum.alpaca.markets/t/feature-request-more-paper-trading-accounts/18125,
  https://forum.alpaca.markets/t/key-limitations-on-paper-trading-keys/17789 — the staff posts quoted.
  Fetched by a scout, quotes corrected by the second reader.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, https://docs.alpaca.markets/us/docs/245-trading.md
  — session hours. Fetched by a scout, summarised.
* Second reader (`evidence-verifier`), 2026-11-10: 17 findings, all applied: the streaming test list, the
  stream limit's wording, `boats`'s and after-hours' caveats, the probe's holder check, the inferred wait,
  the forum quotes, the Broker API scope (now in ADR 0070), the request bound, the lock's 28 of
  35 minutes, the runs not made, rc9, ADR 0069 not yet written (since written and accepted), and paper windows as checks, not trials (ADR 0010, ADR 0052).
* Split, 2026-11-10: the sandbox, the data-only task and the proof run moved to ADR 0070 with no change
  to what they say; the accounts design (keychain items, which task reads which pair, the locks, the
  rows, the files) is left to ADR 0069, then not yet written; Alpaca's account facts, the stream facts and
  the streaming-test list stay here, since the plan rests on them.
* Second reader (`evidence-verifier`) of the split, 2026-11-10: the accounts facts had lost their home,
  A and B were undefined, ADR 0066 had reached `main` as `proposed`, "one stream per user" had come
  back, and an Evidence line cited sources the text no longer used. Applied above.
* Follow-up, 2026-11-11: the owner's instruction restated rather than quoted; the window runner's
  sessions brought up to date with `session_window.py` on `main` (extended and overnight windows).
* https://forum.alpaca.markets/t/what-constitutes-an-api-call/16502 — staff, 2025-03-24: "default API
  rate limit of 200/min per account (ie each paper and live account is separate)". Fetched by the
  second reader. https://docs.alpaca.markets/us/docs/245-trading.md — the 23/5 plan, targeted for
  2026-12-06 and subject to SEC approval. Fetched by the second reader.
* Second reader (`evidence-verifier`), 2026-11-11, before acceptance: ADR 0066, 0067 and 0069 stale as
  unaccepted or unwritten; one lock for every paper run, now one per account; pre-market and
  after-hours windows, now skipped; plan steps 3 and 5, now done; `test_paper_overnight_fill.py` missing
  from the streaming list; `PositionCap` placed by ADR 0044, not ADR 0037; `CompositeMarketMaker`'s
  signal requote; the forum post's two account sentences reconciled in that post; the 1,000 for live
  answered by a staff reply; rc9's `EmaCross` reset fix; the schedule's 38-minute cycle; and the 23/5
  week, now the review-by date. Each checked against the file or record named and applied above. It
  also found that `EmaCross` keeps its side unflipped when `submit_order` itself returns an error
  (`ema_cross/strategy.rs`), so "a refused order leaves its side flipped" holds for a refusal after
  the submit returns; applied in the Confirmation.
