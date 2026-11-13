---
status: proposed
date: 2026-11-12
decision-makers: lead orchestrator (Claude) for the unit and its properties; repo owner ratifies
review-by: 2026-12-12
---

# A streamed trade print older than the newest one passed on is set aside by the Alpaca data client, behind `validate_trade_sequence`, off by default; open: how often the tape is out of order

## Context and Problem Statement

A stream delivers trade prints in the order they reach it, not in the order of their own times. NautilusTrader
takes each trade as it arrives: the data engine caches and publishes it, the cache's last trade is the one
added last, and the order emulator sets its last price from it. A sale the Mover holds on trade prints, and
its record, can therefore move on a print older than one already acted on.

## Decision Drivers

* A price that releases or moves a sale must not come from a print older than one already acted on (the
  lead's first property for this unit, 2026-11-12).
* Nothing is dropped silently: a print set aside is counted by name with its lateness, and a window's
  record carries the count (the second property).
* Use what upstream offers before writing anything (rule 3; the fourth property).

## Considered Options

1. Leave ticks as delivered and guard each reader in the strategy.
2. Turn on NautilusTrader's own sequence check, `DataEngineConfig.validate_data_sequence`.
3. Apply that check's shape to streamed prints in the Alpaca data client, behind a setting that is off.
4. Wait for upstream to extend its check to trades.

## Decision Outcome

Chosen: **option 3, with option 1 beside it**, because option 2 does not exist for trades and option 4
has no date. Upstream's check is for bars only: at the pin the setting is read in the bar paths alone, and
the release note that added it says "currently only for `Bar` data". That is the gap this record is
`proposed` over. Option 1 is the Mover's own guard, built in the same unit by the
lane that owns the Mover; it cannot reach the order emulator, which reads no time from a trade.

* **The rule.** With `validate_trade_sequence` on, a streamed print whose own time (`ts_event`) is before
  the newest one passed on for its symbol is set aside before the data engine sees it. A print with the
  newest's own time passes. This is the first comparison of upstream's `validate_bar_sequence`. Its second
  comparison, of receipt times, is left out: the client stamps a print's receipt itself, in arrival order.
* **Not the earlier of a print's own time and its receipt.** That rule was built first and corrected in
  review: a node clock behind Alpaca's stamps puts every own time ahead of its receipt, and prints ordered
  by receipt are never late, so the check would pass everything and say nothing.
* **A stamp far ahead of the clock.** A print stamped more than five seconds ahead of its receipt has no
  usable time. It is passed on as with the setting off, counted by name, and never becomes the newest, so
  it cannot hold back the prints after it. Five seconds is ours and UNCALIBRATED.
* **Counted.** Log lines that begin `Alpaca trade sequence on feed ` name the first of each kind per
  symbol under each subscription, then the count at each power of ten and at unsubscribe or disconnect. A process-wide tally gives
  per symbol the count, the most any was behind, their shares and the prints passed on unordered; a Mover
  window's record carries it as `late_prints_set_aside_by_the_adapter`.
* **Scope.** Streamed prints only. A request's trades are asked for in ascending time (`sort=asc`) and go
  out as one response. The newest time is kept for the client's life.

The request assumed a drop by "stamp"; the evidence corrects which stamp, as above.

### Consequences

* Good: with the setting on, a print older than the newest one passed on reaches neither the cache nor a
  strategy nor the order emulator, which all take what the data engine publishes. The exception is a print
  stamped far ahead of the clock: it is passed on unordered, and so is acted on. The tests read the
  strategy and the cache; the emulator is covered by reading upstream, not by a test here.
* Bad: a tick set aside is in no bar built from ticks, so such a bar can lack its shares and, if the print
  was the minute's high or low, that price. Alpaca's own minute bars are not affected. The tally's `shares`
  shows how much.
* Bad: prints of one symbol from different exchanges may reach the stream slightly out of time order as a
  matter of course. How many would be set aside is not measured (UNVERIFIED).
* Open, and what closes each: the rate of out-of-order prints on `sip` (windows' tallies with the
  setting on; the one window run so far, under Confirmation, set none aside); the bound of five
  seconds (a measured spread of own time against receipt) and its move to the reference Mover measures
  against;
  upstream extending `validate_data_sequence` to trades, which would replace this setting.

### Confirmation

* `packages/canon-alpaca/tests/test_data_client.py`, the cases under "late streamed prints set aside": the
  rule, the equal time, a clock that runs behind Alpaca's stamps, the far-ahead stamp, the per-symbol
  order, the count across subscriptions, the trade-condition filter's left-out prints, and the kept tally. Each rule was reverted alone and a named
  case failed.
* `tests/test_mover_window.py`: the record carries the tally, and has no such field when it is empty.
* The setting remains off by default (`AlpacaDataClientConfig().validate_trade_sequence is False`);
  2026-11-13, the paper and sandbox node builders accept `validate_trade_sequence=True`, with blind
  tests for unchanged plain configs when off, adapter defaults when on and non-bool refusal before a
  build. A session window turns it on with `--validate-trade-sequence`, which is Mover's, since Mover's
  record carries the check's tally, and says so in the record (`validate_trade_sequence`), a refused
  window's record included (`tests/test_session_window.py`). No window runs it unless told; no task
  passes it.
* 2026-11-13, one paper window ran with it, from a tip that held the wiring before it landed: a
  regular-session Mover window of fifteen minutes on the default trigger, ended 16:52:11 UTC, result
  `ok`, 775.6 s, nothing left, flat. Its record says `validate_trade_sequence: true` and
  `trigger: bid_ask`, and carries no `late_prints_set_aside_by_the_adapter`, a field the record has
  only once a print was set aside or passed on unordered. So a live Mover node ran and ended flat with
  the setting on, and no print arrived out of time order in that window. Not shown by it, and still
  UNVERIFIED: a print set aside live (the offline cases cover that path), and any rate, which one
  window of fifteen minutes does not give.
* `--validate-trade-sequence` together with `--trigger last_price` is refused by name, by the window's
  config and when the arguments are read (the lead's ruling, 2026-11-13). Under a print trigger Mover
  judges a late print itself: one counts toward a held stop's low though it is never the newest price
  (`Mover.on_trade` in `src/canon_trading/mover.py`), so Mover must see every print, and this check
  sets a late one aside before any strategy sees it. Under the default trigger no print feeds a hold's
  low (`held_low` counts prints only under `LAST_PRICE`), which is why only a print trigger is refused.
  The check still changes three things Mover reads there, and a window run with it is read knowing so:
  its own tally of late prints reads without the prints set aside, which
  `late_prints_set_aside_by_the_adapter` counts; a print set aside is in no minute bar built from ticks,
  the bars Mover's entries, ATR and trail read; and the newest trade Mover reads where a name has no
  bid (a held stop in `_last_known`, a sale's anchor in `_last_print`) is the same after a print that is
  truly late, which is never the newest, but not under the limit below.
* A limit, not closed here: the two sides measure a print's time "ahead" differently. This check
  measures it against receipt on this host (`STAMP_AHEAD_MOST_NS`), and Mover against the venue's time
  as its name's quotes show it, which cancels this host's clock offset (`PRINT_AHEAD_MOST_NS`, whose
  text says this bound "is to move to this reference with it"). Until it moves, with this host's clock
  ahead by some seconds a print stamped up to five seconds and that offset ahead of the venue's time
  becomes the newest here, which Mover would not use, and the timely prints behind it are set aside:
  under the default trigger a held stop or a sale with no bid then reads an older price than it would
  with the check off. With the host more than five seconds behind, every print passes unordered and
  the check does nothing. Moving the bound to the quote reference is the next change.

## Evidence

Read 2026-11-12, at NautilusTrader's pin through `scripts/canon src nautilus-trader`: each path below is
under https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186 and
was read in the tree fetched at that commit (`resolved`), not on the web page.

* `crates/data/src/engine/dispatch.rs:117-128` — `handle_trade` adds the trade to the cache and publishes
  it and any synthetic trade made from it; no time is compared — resolved.
* `crates/data/src/engine/dispatch.rs:444-446` — "Live bars and aggregator emissions honor
  `validate_data_sequence`" — resolved.
* `crates/data/src/engine/dispatch.rs:471-496` — `validate_bar_sequence`: a bar whose `ts_event` or
  `ts_init` is below the last cached bar's is logged and not passed on — resolved.
* `crates/data/src/engine/config.rs:59-61` — `validate_data_sequence`, "If data objects timestamp
  sequencing will be validated and handled" — resolved.
* `RELEASES.md:7664`, under "NautilusTrader 1.165.0 Beta" (`:7622`, released 2023-01-14) — "Added
  `DataEngineConfig.validate_data_sequence` (default `False` and currently only for `Bar` data)" — resolved.
  An old note; the next line is what shows it still holds.
* `crates/data/src/engine/` — the setting is read at `dispatch.rs:132`, `:279` and `bar.rs:424`, `:786`,
  each a bar path, and nowhere else in the folder (searched by name) — resolved.
* `crates/common/src/cache/mod.rs:2418-2422` — a trade is pushed to the front of its deque — resolved.
* `crates/common/src/cache/mod.rs:6362-6368` — `trade` "Returns the most recently added trade" (the
  deque's front), so the cache's last trade is the one added last — resolved.
* `crates/execution/src/order_emulator/emulator.rs:1093-1105` — `on_trade_tick` sets the last price from
  the trade (and the bid and ask too when no quotes are subscribed) and iterates the orders; it reads no
  time — resolved.
* `crates/core/src/time.rs:313-316` — the clock's time never goes back ("No backward jumps: If the OS time
  moves backward, we ignore that shift"), which is why receipt times are in arrival order — resolved.
* `docs/concepts/orders/emulated.md:17-19`, `:35` — `LAST_PRICE` uses trades; "For trailing-stop orders, it
  updates the trailing trigger from that market data" — resolved.
* The order in which Alpaca's `sip` and `iex` streams deliver prints of one symbol — UNVERIFIED: no Alpaca
  page was read for it and no stream was measured.
* NautilusTrader after the pin — a copy on disk named for v2.0.0rc9, at commit `da6b57aefdd6`, has the same
  `handle_trade` with no time compared (`crates/data/src/engine/dispatch.rs:138-150`) and reads the setting
  in bar paths only (`dispatch.rs:153`, `:300`; `bar.rs:478`, `:862`) — read on disk; that this commit is
  the release's tag is UNVERIFIED.
* 2026-11-12, the evidence verifier's pass: every upstream line above held; it asked for the cache's
  `trade` lines, the release behind the note, the limit on the "Good" consequence and the wording on
  subscriptions and requests, all now in the text.
