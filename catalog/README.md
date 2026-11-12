# catalog

The local NautilusTrader `ParquetDataCatalog` lives here. Everything in this folder except this file is
ignored by git: market data is never committed.

Upstream's catalog guide builds the path as `Path.cwd() / "catalog"` and passes it to
`ParquetDataCatalog(...)`, so code run from the repo root finds this folder with no configuration. Under
it, upstream lays data out by type and identifier: bars in `catalog/data/bars/<bar type>/`, instrument
definitions in `catalog/data/equity/<instrument>/`, each file named for the `ts_init` range it covers.

What fills it: `canon_trading.bars_to_catalog.load_bars`, which requests a bar type's bars for a range
from Alpaca through the adapter's data client with NautilusTrader's catalog write-back, and writes the
instrument's definition if none is here (ADR 0040). It fetches only what the catalog does not already
cover. It needs the paper key pair, so it runs inside `mise run paper:test`;
`tests/test_paper_bars_to_catalog.py` loads AAPL's daily bars for the first quarter of 2025 here.

One feed a root. The catalog keys bars by bar type, not by Alpaca's feed, so `load_bars` writes the feed
it loaded into `alpaca-feed` in the root and refuses another feed there. This folder holds IEX; SIP bars
go in `catalog/sip/` (ADR 0077).

`mise run paper:history <tests|windows>` loads a bounded list of symbols' `sip` minute/day bars, GET
only, through `canon_trading.history_load` (ADR 0093). It requires the asset definitions already in
the chosen root, paces at ADR 0093's row, and takes one catalog lock without taking an account or
stream lock. A refused feed or rate limit stops by name. Give it `--catalog catalog/sip` for SIP.

The nightly movers baseline writer (ADR 0087) reads every ALPACA equity definition in the chosen
root and its last five sessions' minute bars. Load the whole asset list and history before calling
this a whole-market baseline; this job cannot establish completeness of the asset list. Run, for
example:

```sh
mise run paper:history tests baseline --session 2026-11-12 \
  --sessions-file /path/to/exchange-sessions.txt --feed sip
```

`--catalog` defaults to `catalog/<feed>`. The calendar file is an operator-supplied exchange-session
calendar, one ISO date per line in ascending order, including the coming trade date and at least five
prior sessions. Blank lines and lines beginning with `#` are ignored. Extra earlier dates are allowed:
only the latest five before `--session` are used. Verify the calendar against the exchange; weekdays
and missing bars cannot establish holidays. No calendar provider is fetched by this command. An
early-close date must still be covered through 20:00 New York, so build after its 20:00 bars are
published.

This mode runs `python -m canon_trading.movers_baseline` before any account pair is read, and refuses
a load flag (`--symbols`, `--start`, `--bars` and the like); load mode refuses a baseline flag
(`--feed`, `--session` and the like). It uses the same catalog lock as `history_load`. A name is
written when the root covers its minute ends from 04:01 through 20:00 New York on each selected
session; a covered session with no trades counts zero. A name missing a session is left out and listed
in the file's `excluded` with the feed, the session and the missing range: `history_load` marks no
coverage for an empty answer, so a name with no trade in a loaded range can never be covered. The
write stops by name, keeping the previous file, only when the share of distinct equities left out is
above `--max-excluded-share` (default 0.5, UNCALIBRATED). The file's header carries `source` (writer,
feed, catalog, sessions, equities, time written), `feeds`, `excluded_share`, `excluded`
and `lookback_sessions`, and each name its `traded_sessions` per segment. Legacy writers that do not take
the history lock must run separately; a root the recorder writes is best kept apart from the history
load's.

The arithmetic and atomic JSON replacement are `movers_rank.segment_baseline` and
`movers_rank.write_baselines`: typical-price dollar volume, cumulative over 96 15-minute segments
from 20:00 New York the evening before to 20:00 on the trade date, averaged over five sessions. SIP
has no overnight session, so a SIP baseline counts and checks only 04:01 through 20:00: its segments
0-31 (20:00-04:00) are zero, a night with no SIP bars never makes a session read as uncovered, and its
header's `feeds` is `["sip"]`, so `read_baselines` reports `overnight` false and the ranking counts
its day ratio from 04:00. The actor reads the result for exactly the coming date with
`read_baselines`. The default output is `catalog/movers-baseline/sip.json`; `--baseline-dir` sets its
directory, and the actor's `baseline_path` should name that file. `--feed boats` is refused by name:
the overnight part's baseline waits on a BOATS history loader, and ADR 0093's loader supports SIP only.

This is an explicit job invocation; it installs no schedule. The coordinator performs the first
credential-backed load and paper verification.

`mise run paper:history <tests|windows> --data trades --symbols AAPL --start <ISO time with zone>
--end <ISO time with zone> --catalog <existing SIP root>` loads individual SIP prints through the
adapter's historical trades fetch and parser, then NautilusTrader's `write_trade_ticks(ticks, start,
end)`. `--symbols-file` accepts one symbol per line with blank lines and `#` comments; `--bars` applies
only to bar loads. The root must already hold the instrument definitions. Trades go under
`data/trades/<instrument>/`, keeping their IDs, sizes and event nanoseconds, with `ts_init = ts_event`.
The `TradeTick` representation drops Alpaca's conditions (`c`), exchange (`x`) and tape (`z`); it does
not filter by those condition flags. `aggressor_side` is `NO_AGGRESSOR`, with no side inferred. These
are the adapter's `parse_trade` semantics and the pinned `TradeTick` fields
(`crates/model/src/data/trade.rs` at `d53849b216e2`).
This is the pinned v2.0.0rc8 catalog's typed writer and coverage API (`docs/concepts/data/catalog.md`,
"Writing data" and "Reading data", and `python/nautilus_trader/persistence/__init__.pyi`, at
`d53849b216e2`, read 2026-11-11).

Both bounds are inclusive, but the loader requires `start < end`; equal bounds are refused. A start
before 1970-01-01 UTC, the catalog's epoch, is refused before lock creation or any request.
The range must end at least 15 minutes before now. Each missing interval is fetched
completely before writing; a rerun skips covered trade intervals. Empty answers and invalid-only
answers write no coverage and are requested again. The adapter excludes canceled and incorrect prints,
keeps corrected prints, and the loader names those exclusions and any rounded prices. More than
100,000 valid prints in one missing interval are loaded in parts, each cut before the last timestamp
its answer reached, so no timestamp is split; more than 100,000 at one timestamp stops the load by
name. It never marks a truncated response as complete. A covered interval is not asked for again, so
a print Alpaca cancels or corrects after the load stays as loaded.

The `.history-load.lock` is shared by bar and trade history loads in one root. The SIP feed marker,
GET-only transport, ADR 0093's 150 data GETs/minute and zero trading GETs, and first-429/refused-feed
stops are shared too. For trades, each actual GET, including every page and SDK 504 retry, starts at
least `60 / 150` seconds after the previous send. Idle time and retry backoffs earn no later burst.
The transport delegates to the existing GET-only timeout/counter adapter and restores it afterward.
Loads into different roots do not share a rate limiter, and the combined rate is UNCALIBRATED. Legacy
catalog writers that do not take this advisory lock must run separately or use other roots. A legacy root
holding IEX data, including this default root, refuses SIP; give SIP its own root.

First evidence to be run by the Claude lane (UNVERIFIED here): one AAPL night, 2026-11-10 21:00 UTC
through 2026-11-11 09:00 UTC, into a separate root holding its definition, then repeat the same command
and verify zero GETs and zero writes. The tests for this path use offline stand-ins.

Source: https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md (read 2026-11-09 and 2026-11-10);
the pinned v2.0.0rc8 catalog bindings, `movers_rank` and `history_load` (read 2026-11-11).

`mise run paper:history <tests|windows> --data quotes --symbols AAPL --start <ISO time with zone>
--end <ISO time with zone> --catalog <existing IEX root>` loads historical IEX quotes as
`QuoteTick` under `data/quotes/<instrument>/`. The root must hold definitions; SIP roots are refused.
ADR 0066's bounds apply: IEX only, start at or after 2025-11-04 00:00 New York, at most 90 elapsed days (a conservative
quarter ceiling); small probes are allowed. Both bounds are inclusive, `start < end`, and the end
must be 15 minutes before now, as for prints. Request/coverage bounds expand to complete microsecond buckets (start rounded
down, end rounded up to the bucket's last nanosecond), matching the SDK's timestamp precision without
losing quotes on extensions. Each missing interval is split into at most hourly pieces, the endpoint's microsecond joining the
last, each asking to the first nanosecond after its last bucket; over 100,000 quotes
in one piece load in parts, each cut before its answer's last microsecond; more than 100,000 in one
microsecond stops by name. Reruns skip covered
pieces. Empty or dropped-only pieces write no coverage. A piece reaching 1,000 successful pages with
more pages remaining, or returning a repeated page token, stops before any of it is written.
The shared catalog lock, GET-only transport,
150 data GETs/minute, zero trading GETs and first-429/refused-feed stops apply; every page and SDK
504 retry is paced from its actual send. Roots do not share a rate limiter.

The conversion follows the adapter's `parse_quote`: `ts_init = ts_event`, timestamps truncated by
the SDK to microseconds, one-sided and finer-than-instrument prices dropped and counted, malformed
quotes refused by name. Quotes sharing a truncated timestamp stay separate. Conditions,
bid/ask exchanges and tape are lost, without filtering by condition flags. Sizes stay in shares:
starts before 2025-11-04 00:00 New York are refused, excluding the entire cutover day (ADR 0066). The reader
verified the calendar date from Alpaca's REST schemas; the venue's exact intraday cutover and
per-symbol historical lot sizes remain UNVERIFIED. No lot-size multiplier is assumed. Under the
catalog lock, all quote identifiers are checked: roots with earlier quotes or non-bucket-aligned
coverage are refused before fetching, even when those symbols were not requested. Use a separate
clean root. The adapter itself is unchanged. Invalid CLI quote bounds are refused before client setup.
Source: the pinned SDK's `QuoteSet`/`QUOTE_MAPPING`, the adapter's quote request and parser, and the
pinned `write_quote_ticks`/`QuoteTick` contract (ADR 0093 clause 10, read 2026-11-11).
Paper, 2026-11-11, account A's pair: PLCE's IEX quotes for that day's regular session (14:30Z to
21:00Z) loaded 2,156 quotes in seven hourly pieces, in time order, `ts_init = ts_event`, on whole
microseconds, with contiguous coverage; the rerun sent no request and wrote nothing. Paper, 2026-11-12, the same pair, one whole hour,
2026-11-11 14:30Z to 15:30Z: SPY held more than the row bound and loaded in seven parts, six of
100,000 quotes in 11 requests each, each next part starting at the microsecond its predecessor's
answer was cut before, and 42,672 in 5: 642,672 quotes in 71 requests, in time order, `ts_init = ts_event`, on whole
microseconds, none crossed, in seven contiguous coverage intervals. PLCE's 148 quotes came in one
piece that held the endpoint's microsecond. The rerun sent no request and wrote nothing. An hour
with no quote (a night, a weekend) writes no coverage, so a rerun across one asks for it again.
