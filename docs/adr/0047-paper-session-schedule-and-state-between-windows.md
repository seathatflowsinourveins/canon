---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Paper session windows: what a window knows at start-up, and how it warms up

## Context and Problem Statement

ADR 0044 runs `SessionMaCross` on paper in bounded windows, one node per window. NautilusTrader's
reconciliation page advises: "Persist all execution events to the cache database." What can a node with
this repo's Python adapter keep between windows, what does a window therefore know when it starts, and
how does it start from the venue's truth without a phantom position and without a 20-minute warm-up? What
starts the windows through the weekday sessions is ADR 0050, split from this record on 2026-11-10.

## Decision Drivers

* Upstream's own features and patterns first (ADR 0001); a window starts from Alpaca's state, not ours.
* A window placing nothing until its averages warm up wastes two thirds of a 30-minute window.

## Considered Options

1. Cache database backing (Redis or PostgreSQL; strategy state needs Redis at the pin) to carry state.
2. No backing: each window rebuilds from the venue at start-up, and its averages from the adapter's bars.

## Decision Outcome

Chosen: **option 2**, the only one available. Upstream's adapter guide at the pin: "Redis/PostgreSQL cache
backing is unsupported with custom Python clients in either launch mode. Both modes drive asyncio, where
the backing's blocking worker calls would stall the event loop." Strategy state persistence
(`with_load_state`, `with_save_state`, `on_save` and `on_load`) "requires a Redis backing" at the pin
(configure_live_trading; later releases add Postgres). The guide's own exit, "Native-only nodes can use
database backing with `run()`", is closed to a node with a Python adapter.

**What a window knows at start-up**, from start-up reconciliation against Alpaca alone: every open order
however old; an explicit position report for the symbol, flat included, since the adapter reports each
loaded instrument with no position as flat ("An explicit position report is authoritative", reconciliation
page, "Bounded history safety"); and the order and fill history inside `reconciliation_lookback_mins`.

**The lookback stays at one minute**: every window and the `EmaCross` paper test start only after finding
the account flat, and the flat report is authoritative, so older history adds Alpaca calls and nothing
needed. Upstream's readings differ: the docs' tip leaves it unset (here the newest 500 orders and up to
1,000 fills), the Lighter tutorial uses 60 minutes, the adapter's paper tests one or two. Each should be
safe from ADR 0037's phantom with the flat report (the docs, the adapter's offline test); UNVERIFIED on paper.

**Lost between windows:** the averages, the cap's peak, the order and position history. What a window
leaves is not the next one's: the next refuses to start unless the account is flat.

**The warm-up.** `SessionMaCross(warm_up=True)` follows upstream's EMA cross `on_start` (concepts/strategies,
"Handler example": "Registered indicators receive the bars from a request response before
`on_historical_bars()` runs, so a request hydrates them"): before the live subscription it requests at
least an hour of the adapter's one-minute `EXTERNAL` bars from IEX; the live bars stay the `INTERNAL` ones
built from IEX trades. One departure: the averages are registered for the live type only, and
`on_historical_bars` feeds the history to them, oldest first, only while no live bar has reached them, since
a registered history type would also take an answer arriving after a live bar or the 60 s timeout; such an
answer is dropped and counted. Decisions wait for completion or the timeout; the report gives the counts.

**What would close the gap upstream** (this repo's reading; no issue or plan found): database backing for
custom Python clients; with it and Redis, `save_state` and "persist all execution events" would apply.

### Consequences

* Good: a window decides from its first live bar; every start is from Alpaca's state.
* Bad: history bars (Alpaca's aggregation) and live bars (the engine's, from trades) are built by two
  different aggregators from the same feed; a minute with no IEX trade has no live bar.
* Bad: an answer later than the first live bar is dropped, and that window warms up on live bars.

### Confirmation

Offline, in `scripts/ci` (`tests/test_session_window.py`): on NautilusTrader's `BacktestEngine`, with 30
`EXTERNAL` one-minute bars in a `ParquetDataCatalog` and live `INTERNAL` bars built from trades, the
history is fed and the averages are initialized at the first live bar; without the warm-up they are not.
An answer after a live bar or after the timeout is dropped, not fed; one in any order is fed oldest first.
The request's callback ran on failure too (no catalog), in a probe outside `scripts/ci`, as actors.md
documents.
On paper: UNVERIFIED until a regular window runs.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md, "Database cache backing";
  the same warning at the pin's `docs/developer_guide/python_adapters.md` (fetched, raw).
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: `with_load_state` and
  `with_save_state` need a Redis or Postgres backing (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/execution/reconciliation.md: "Persist all execution
  events to the cache database"; "Bounded history safety"; the lookback tip (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/strategies.md, "Handler example"; the same at the pin
  (fetched, raw).
* nautilus-trader 2.0.0rc8 in `.venv`: `LiveNodeBuilder.with_cache_database_factory` exists, which the
  guide rules out for Python clients; `Strategy.request_bars(..., callback=)` (`bundled`).
* `packages/canon-alpaca/src/canon_alpaca/execution.py` on `main`: a flat report for each loaded instrument
  with no position; its offline test `test_older_filled_orders_and_no_position_at_alpaca_end_start_up_flat`
  (read). The adapter's paper tests' lookbacks: `test_paper_matrix.py` 1, `test_paper_fill_reports.py` 2.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/actors.md,
  "Python callbacks and joins": a request callback "runs once on terminal success or failure" (fetched by
  the second reader, raw).
* The Lighter tutorial at the pin, `docs/tutorials/lighter_rwa_composite_mm.md`: 60 minutes (fetched, raw).
* Second reader (`evidence-verifier`), 2026-11-10: corrected the state backing (Redis at the pin), the
  default history, the lookbacks, who starts flat, the callback's source and the upstream-gap wording.
* ADR 0044: the windows, the lookback's first value and the phantom it removed (read).
* The offline tests above (run).
* The schedule, its install and status, their tests, `UNVERIFIED` items and evidence, moved to ADR 0050
  on 2026-11-10, with no change of decision.
