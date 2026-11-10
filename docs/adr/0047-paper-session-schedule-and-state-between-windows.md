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
how does it start from the venue's truth without a phantom position and without a 20-minute warm-up?

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

**The lookback stays at one minute** (`paper_node.RECONCILIATION_LOOKBACK_MINS`). Every window and the
`EmaCross` paper test start only after finding the account flat in the symbol, and the flat report makes
the position authoritative, so older history adds Alpaca calls and nothing they need. Upstream offers
three readings: the docs' tip leaves it unset for the adapter's default (here the newest 500 orders and up
to 1,000 of the newest fills), the Lighter tutorial uses 60 minutes, and the adapter's paper tests use one
or two. With the flat report each should be safe from ADR 0037's phantom: so the docs and the adapter's
offline test say; on paper it is UNVERIFIED until a window runs.

**Lost between windows:** the strategy's averages, the cap's peak, and the cache's order and position
history. A position or order a window leaves is not the next window's: the next refuses to start unless
the account is flat.

**The warm-up.** `SessionMaCross(warm_up=True)` follows upstream's EMA cross `on_start` (concepts/strategies,
"Handler example": register the indicators, `request_bars`, subscribe; "Registered indicators receive the
bars from a request response before `on_historical_bars()` runs, so a request hydrates them"). It requests the
`EXTERNAL` form of the live bar type, the one-minute bars the adapter serves from IEX, at least an hour of
them, before the live subscription, as the example orders it; the live bars stay the `INTERNAL` ones the
data engine builds from IEX trades. One departure: the averages are registered for the live bar type only,
and `on_historical_bars` feeds the history to them, oldest first, only while no live bar has reached them.
A registered history type would also take an answer that lands after a live bar, or after the 60 s
timeout, and so put older bars after newer ones; such an answer is dropped and counted instead. Decisions
wait for the request's completion or the timeout. A window's report gives the bars fed and dropped.

**What would close the gap upstream** (this repo's reading; no upstream issue or plan was found): database
cache backing for custom Python clients, which the guide rules out because the backing's calls block the
asyncio loop; with it and Redis, `save_state` and the docs' "persist all execution events" would apply.

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
