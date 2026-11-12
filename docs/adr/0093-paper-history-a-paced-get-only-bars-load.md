---
status: accepted
date: 2026-11-11
decision-makers: the lead for the pairs the task may read (2026-11-11); master orchestrator (Claude) for the request path, the pace and its terms, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# paper:history: a paced, GET-only load of SIP bars and trades or IEX quotes into the catalog

## Context and Problem Statement

The movers work needs minute and day bars for a few thousand symbols. The baseline is five sessions over
the eligible universe. `canon_trading.bars_to_catalog.load_bars` loads one bar type of one symbol per
call. Each call builds a node, and the node's instrument provider sends one trading-API GET
(`/v2/assets/{symbol}`) beside the data GETs. Over a whole market that would spend the account's
trading budget, 200 a minute per account (ADR 0069, which marks that figure UNVERIFIED for paper), on asset
reads. It would also take a node build per
symbol. A load of that size needs a pace that is written down, and a stop at Alpaca's first 429.

## Decision Drivers

* No invention: an upstream request path, the adapter's own parsing and window, and the catalog's own
  writer.
* GET only, with no account's lock and no stream lock: the load places nothing and opens no stream.
* The pace is one registered number, and what has not been measured is marked as such.
* No fallback: `sip` is named on every request, and a refusal stops the run.

## Considered Options

1. `load_bars` per symbol and bar type: one node, one asset GET and one or more data GETs per call.
2. alpaca-py's multi-symbol `get_stock_bars` directly: data GETs only, written through
   `ParquetDataCatalog`, with no node and no asset GET per symbol.

## Decision Outcome

Chosen: **option 2**. The coordinator decided it on 2026-11-11, reversibly. It was weighed against the
movers lane's measurement of `load_bars`: 11 loads in 3.1 s, 0.17 to 0.39 s each, and one trading GET per
load. That measurement was reported to the coordinator on 2026-11-11 and is not recorded in this
repository (UNVERIFIED here).

1. **Request.** `mise run paper:history <account>` runs `canon_trading.history_load`. For each bar type
   (1-MINUTE- and 1-DAY-LAST-EXTERNAL), the symbols the root lacks over `[start, end]` go in requests of
   at most 200 symbols, planned for at most `MAX_ROWS_PER_CALL` (100,000) bars. The count allows one bar
   per minute, or per 23 hours for daily bars: New York daily opens can be only 23 hours apart at
   spring DST, although `parse_bar` adds a fixed 24 hours to each open. It also counts a boundary row
   returned again if a venue or test stand-in truncates a gap's nanoseconds to microseconds. The
   stand-in's `datetime.fromisoformat` does this; alpaca-py preserves the request's nanoseconds via
   `isoformat()` (`alpaca/common/requests.py:54` at `c42add7c0a70`). Alpaca's server-side query parsing
   remains UNVERIFIED. A window longer than the bound for one symbol is cut into segments, each
   written over its own bounds. The returned row
   count, including rows outside the gap, is checked before parsing or writing: an overfull response
   stops by name, and none of that response is written. The request is `StockBarsRequest(symbol_or_symbols=…, timeframe, start, end,
   feed=sip)`, with no limit and no adjustment, so Alpaca's default `raw` applies, as for `load_bars`.
   alpaca-py follows `next_page_token` through every page of 10,000 rows.
2. **Window and parsing.** Both are the adapter's: `alpaca_bars_window`, `parse_bar` and `bar_timeframe`.
   A bar written here is the bar `load_bars` writes. `ts_init` is the bar's close, and `[start, end]`
   bounds it. As the engine is said to (UNVERIFIED: no line of the pinned source is cited), this path filters parsed bars to the inclusive missing interval
   before writing; the catalog file's bounds alone do not filter its contents. `bar_roundings` names
   price rounding with one line per symbol and response, giving the rounded-bar count, price precision,
   half-to-even rule and first example. An end less than one bar interval, or 15 minutes, before now is
   refused, by `load_bars`' rule; `--bars 1-MINUTE` serves a more recent end.
3. **Writes.** `write_bars(bars, start, end)` runs per bar type over the bounds asked for, as the node's
   write-back gives them. What the root covers (`get_missing_intervals_for_request`) is not asked for
   again. Symbols with the same gap share a request. The root keeps one feed through `bars_to_catalog`'s
   `alpaca-feed` file, and a root holding another feed is refused. The root is an argument, defaulting
   to `catalog/`, and anything under `ledger/` is refused by the public loader as well as the CLI.
   The root must already be a directory holding the required definitions. The window, nonempty
   symbols, supported nonempty bar specifications, ledger exclusion, directory, feed and definitions
   are validated before creating a lock file; preflight refusals create no catalog directory or lock.
   A nonblocking exclusive `fcntl.flock` on
   `.history-load.lock` in the root is held from the coverage read through the last write; another
   history loader using that root stops by name before requesting data. The marker is rechecked before
   every write and created with exclusive `open(..., "x")`, so a competing marker is never overwritten.
   If a write fails and the root holds no feed data, a marker newly created by this load is removed,
   as `bars_to_catalog` does. Pre-existing markers, markers whose feed has changed, and markers on roots
   holding any feed data are kept.
   This is a catalog lock, independent of account and stream locks. It coordinates history loaders;
   `load_bars` and quote write-back do not take it and must run separately or use other roots. A marker
   changed during a request is refused before writing, but an uncooperative writer changing it after
   that check remains outside this guarantee.
4. **Instruments.** Each bar is parsed against its `Equity` (price precision), read from the catalog
   root. The definitions are meant to come from an asset list load: one trading GET for the whole list
   (`get_all_assets`, then `parse_equity`, then `write_instruments`). This task sends none. That load is
   another lane's work and is not on `main` at this change. Until it lands, the definitions in a root
   are the ones `load_bars` has written, one symbol at a time. That is the per-symbol trading GET this
   record avoids, so a whole-market load waits for the asset list load.
   A symbol with no definition is refused by name before any request. Definitions alone are not a
   feed's data, so `catalog_feed` no longer reads a root holding only `data/equity/` as IEX. Until this
   change, such a root refused every `sip` load.
5. **The pace.** One constant, `history_load.PACE`, holds this table's row. After each bar request the
   adapter's request counter (`canon_alpaca.providers.request_counts`) gives the GETs it sent to each
   API. The next request starts no sooner than its data GETs allow at the row's data rate. The trading
   column is a guard: a request that sent any trading GET stops the run. Inside one request, alpaca-py
   sends its pages back to back. `MAX_ROWS_PER_CALL` plans for ten full pages; this is not a page or GET
   ceiling, because Alpaca can return short pages and alpaca-py retries 504s. The rate holds across
   requests, not inside one. Each process paces itself; simultaneous loads into different roots using
   one account do not share a rate limiter. Their combined rate is UNCALIBRATED, and a 429 stops each
   affected run by name.

   Trades and quotes (amended 2026-11-11, clauses 9 and 10) pace every actual GET through requests' documented transport
   adapter hook, including pages and SDK 504 retries. Each send starts at least `60 / 150` seconds
   after the previous actual send; idle time and retry backoffs earn no later burst. The wrapper
   delegates to the existing GET-only timeout/counter adapters and restores them when the load exits.

   | Task | Data API GETs a minute | Trading API GETs a minute | State |
   |---|---|---|---|
   | `paper:history` | 150 | 0 | UNCALIBRATED |

   How it was set:
   * The counter splits requests between the data API and the trading API by host.
   * Alpaca's market data page gives historical API calls of 200 a minute on its free plan and 10,000
     on its paid one.
   * The same page: "The Basic plan serves as the default option for both Paper and Live trading
     accounts". Which plan applies to this paper login's historical calls was not measured
     (UNVERIFIED), so the row stays under the lower figure.
   * The data API's limit is counted apart from the trading API's 200. This is read from the market
     data page, which gives its own limit; no page says so in as many words (UNVERIFIED).
   * The row moves once the movers lane's measurement of the data API on this pair is recorded.
6. **429.** alpaca-py retries 429 and 504 three times, 3 s apart. Its data client takes no retry
   setting, and a `retry_attempts` of 0 keeps the default. So a requests response hook on the client's
   session raises at Alpaca's first 429, before alpaca-py sends the request again, and the run stops by
   name (exit 4). A 504 is still retried three times, as alpaca-py does it, and each retry is
   counted. The adapter's execution client is the precedent: it ends an order command at its first
   error (`execution.py`, `end_at_first_error`), as an `APIError` on any error rather than a 429 alone. The counter's 429 count is checked after each request as well.
   For bars these retries remain unpaced; trades and quotes apply the per-send spacing of clause 5 (2026-11-11).
7. **Failures.** A refused request, a 403 on `sip` included, stops the run by name, with the HTTP
   status. Nothing falls back to another feed. Whatever an earlier request wrote stays, and a rerun asks
   only for what is missing. Client setup errors (missing credentials, timeout or GET-only session
   validation), transport, parsing and catalog-write errors are also raised as
   `HistoryStopped`, with the operation, symbol or bar type and the original cause; the CLI reports
   them and exits 1. A 429 retains its distinct exit 4.
8. **Pairs.** The lead decided on 2026-11-11 that `paper:history <account>` takes either account's pair,
   `tests` (A) or `windows` (B). It reads market data and nothing of the account. ADR 0069's rule on who
   reads B's pair governs who trades and streams on B, and a GET-only history load does neither. The
   task reads one pair, never both (ADR 0069, clause 2's refusal). It runs in data-only mode (ADR 0070).
9. **Trades, amended 2026-11-11.** `--data trades` now loads individual historical SIP prints for
   `--symbols` or `--symbols-file` through the adapter's `fetch_trades` and `parse_trade`, using
   Alpaca's GET `/v2/stocks/{symbol}/trades` and alpaca-py's public `RESTClient.get`. Each symbol's
   complete missing interval is fetched in ascending pages, with exact raw event nanoseconds and
   separate IDs retained for prints sharing a timestamp. `ts_init = ts_event`, as on the adapter's
   historical response. The writer is the pinned NautilusTrader v2.0.0rc8
   `ParquetDataCatalog.write_trade_ticks(ticks, start, end)`, with inclusive nanosecond bounds;
   coverage is keyed by `NautilusDataType.TradeTick` and instrument ID. No bars are synthesized.
   The existing root, definitions, feed marker, catalog lock, GET-only transport, zero trading GETs,
   first-429/refused-feed stops and publication delay apply. Bounds require `start < end`, though
   both are inclusive; the CLI preserves nanoseconds and a start before 1970-01-01 UTC is refused
   before lock creation. The 150 data-GET row applies to each actual send, including every page and
   504 retry (clause 5); its live calibration remains UNVERIFIED.

   There is no assumed upper rate of prints per time unit. Fetching at most
   `MAX_ROWS_PER_CALL + 1` valid prints detects an overfull answer: the interval is then loaded in
   parts, each written up to one nanosecond before the last timestamp its answer reached and the next
   started there, so a timestamp is never split; more than 100,000 prints at one timestamp stops by
   name. Prints are written in time order, as the pinned writer requires. Coverage once written is
   kept: a print Alpaca cancels, corrects or reports after the load is not picked up by a rerun (how
   late is UNVERIFIED). The
   adapter's page cap still fails incomplete history, including on invalid-only pages. Empty or
   invalid-only intervals write no coverage. Canceled and incorrect prints are excluded by the raw
   `u` field, corrected prints are kept, and exclusions and rounded prices are named.
   The `TradeTick` representation drops Alpaca's conditions (`c`), exchange (`x`) and tape (`z`),
   without filtering by condition flags; `aggressor_side` is `NO_AGGRESSOR`, with no side inferred.
   A real SIP load of one named symbol over one night, then a zero-request rerun, remains for the
   Claude lane (UNVERIFIED here).

10. **Quotes, amended 2026-11-11.** `--data quotes` now loads historical IEX quotes for `--symbols`
    or `--symbols-file`, using alpaca-py's `get_stock_quotes(StockQuotesRequest)` / `QuoteSet` and the
    adapter's `parse_quote`, then NautilusTrader's `write_quote_ticks(ticks, start, end)`. This extends
    the typed-writer choice here to ADR 0066's quotes. The same existing-root/definition preflight,
    catalog lock, GET-only transport, 150 data GETs/minute and zero trading GETs, first-429 and
    refused-feed stops apply. The root is IEX: a SIP root is refused by name; feeds never mix.

    ADR 0066's bounds are enforced by name: IEX only, start in 2025 or later, at most 90 elapsed days
    as a conservative quarter ceiling. Its start-after size-unit option further restricts starts to
    2025-11-04 00:00 New York or later, excluding the entire cutover day. Tiny probes are allowed. The end
    must be the prints' publication delay before now, since covered is covered for good and how soon the
    historical store holds an IEX quote is UNVERIFIED. Each symbol's missing intervals use `NautilusDataType.QuoteTick`
    and instrument ID; pieces are at most one hour, the endpoint's microsecond joining the last, fetched in ascending pages with each actual send
    paced, including SDK 504 retries. A limit of 100,001 detects more than 100,000 quotes in a piece,
    which is then loaded in parts as prints are, each cut before its answer's last microsecond and
    covered to the end of the bucket before it; more than 100,000 quotes in one microsecond stops by name.
    Empty or dropped-only pieces write no coverage. Reruns ask only for missing intervals. Every
    piece also has a 1,000-successful-page ceiling and repeated-token refusal through requests'
    response hook; incomplete history stops before writing, including on empty/sparse pages.
    504 attempts retain their retry policy and per-send pacing and do not count as successful pages.

    Conversion follows the adapter: `ts_init = ts_event`; one-sided quotes and finer-than-instrument
    prices are dropped and counted, other malformed quotes stop. The SDK truncates nanoseconds to
    microseconds, losing sub-microsecond timestamp distinctions. Request and coverage bounds expand
    to whole microsecond buckets: start rounded down, end rounded up to the last nanosecond of its
    bucket. The requests sent cover whole boundary buckets, which prevents hourly or incremental losses.
    Each piece asks to the first nanosecond after its last bucket and keeps only its own quotes, so it
    does not rely on Alpaca comparing the end at nanosecond precision (UNVERIFIED). Quotes
    sharing a truncated timestamp stay separate. The `QuoteTick` drops conditions (`c`), bid/ask
    exchanges (`bx`, `ax`) and tape (`z`), without filtering by conditions. Sizes stay in shares,
    with all earlier starts and the entire 2025-11-03 cutover day refused (ADR 0066, citing
    https://docs.alpaca.markets/us/reference/stockquotes-1.md). That page, fetched again on
    2026-11-12, gives the calendar date; the exact intraday switch and per-symbol historical lot sizes
    remain UNVERIFIED. No lot-size multiplier is assumed. Under the catalog lock all quote
    identifiers, including unrequested ones, are checked for earlier or non-bucket-aligned coverage;
    refuse such legacy roots and ask for a separate clean root. CLI quote bounds are checked before
    client setup (invalid CLI bounds exit 2). The adapter itself is unchanged. Offline stand-ins exercise this
    path in `tests/test_quotes_load.py`. One paper load ran on 2026-11-11 with account A's pair: PLCE's
    IEX quotes for that day's regular session, 2,156 quotes in seven hourly pieces, and a rerun that sent
    no request. A second, on 2026-11-12, loaded the whole hour 2026-11-11 14:30Z to 15:30Z: SPY over the
    row bound in seven parts (642,672 quotes, 71 requests), PLCE in one piece holding the endpoint's
    microsecond, and its rerun sent no request
    (`catalog/README.md`). The pace's calibration remains UNVERIFIED.

### Consequences

* Good:
  * A whole-market load spends no trading budget.
  * Each request carries up to 200 symbols, but the row bound counts every wall-clock minute of the
    window, so the baseline (five sessions of minute bars for a few thousand symbols, about seven
    calendar days) batches about 9 symbols a request: hundreds of requests plus their pages, still not
    thousands of node builds. Counting only session minutes would batch several times better; not done.
* Good: what the root already holds is not fetched again, and a stop leaves what was written.
* Bad: a second writer of bars beside `load_bars`. Both use the adapter's window and parsing, and the
  tests check that the bars and the coverage match.
* Bad:
  * The pace is not calibrated.
  * alpaca-py gathers every page of a request in memory before it returns. The planned bound is
    `MAX_ROWS_PER_CALL`, 100,000 bars, checked after the response: a venue that breaks the requested
    window or the assumed spacing can exceed this memory estimate before the run stops.
  * Counting every interval of wall-clock time overstates a minute window's bars by the hours with
    no trade, so a request carries fewer symbols than the pages could hold.
  * The most symbols Alpaca accepts in one request is not stated (UNVERIFIED).
* Bad: alpaca-py's pydantic model is built for every bar. The movers lane proposed `raw_data=True`,
  which would need a parser of its own. That is left until a measured load shows the cost.

* Bad, recorded limits (code review, 2026-11-11): the load takes no account lock, so on account B it
  shares the data budget with a scheduled window, and a long load can push the account past it; run it
  on B only while the schedule is off or on A between paper tests. It holds none of the locks the
  maintenance agent waits on, so a round can fast-forward the checkout under a running load. Its
  deadline is `timeout -s KILL`, so a kill during a write is not given a grace period, and whether the
  catalog's write is atomic is UNVERIFIED.

### Confirmation

`tests/test_history_load.py` runs offline. It uses alpaca-py's real client and the adapter's timeout
adapter and counter, with requests' transport answered by a stand-in of the bars endpoint. It shows:

* many symbols in one request on `sip`, with no trading host reached;
* every page followed, and the next request waiting for the GETs the last one sent;
* the symbols batched, and a long window cut into segments, by `MAX_ROWS_PER_CALL`;
* overlapping extensions storing each bar once, within the missing interval;
* daily opens across spring DST, and a truncated gap boundary, staying within the planned row bound;
* an overfull response refused before any of its bars are written;
* a competing history loader refused while a request is in flight, and a changed feed refused before
  writing;
* parse, write and transport failures named as `HistoryStopped`, and the CLI reporting a parse failure;
* invalid direct-loader inputs and ledger paths refused without creating catalog directories or a lock;
* a failed first write removing its new marker only while the root holds no feed data, with existing,
  changed and partial-write markers kept;
* client setup failures named as `HistoryStopped` with their causes, and missing credentials reported
  by the CLI with exit 1 and no request;
* rounded SIP prices named with the count and first example;
* a 429 stopping the run with one request sent, where alpaca-py would have sent four;
* a 403 stopping it by name with no `iex` request;
* a covered range asked for nothing;
* a symbol with no definition, and a root holding `iex`, refused before any request;
* the arguments refusing an open-ended or unplaced range;
* `PACE` equal to this record's row;
* the task reading one named pair, with no account or stream lock, in data-only mode.

Trades amendment, 2026-11-11: `tests/test_trades_load.py` exercises the real SDK and trade-tick
catalog with a stand-in HTTP transport. It covers nanoseconds and same-timestamp IDs across pages
and gap extensions, empty history, the row bound, 429/403 stops, invalid prints, rounding, feed/lock
and write failures, CLI symbols/files, and pre-epoch refusal before lock creation. The pacing
regression covers seven pages with three 504 retries each, then fast one-print pages and a second
symbol: 221 actual GETs, each separated by the row's minimum spacing. Both the SDK's normal 3-second
retry wait and a stand-in zero wait are covered; original transport adapters are restored on success
and exhausted retries. Antigravity's review of `5a86570` identified the pacing, pre-epoch and
documentation findings; the behavioral regressions failed before the fixes. The SIP night remains
UNVERIFIED until its paper firing.

`tests/test_bars_to_catalog.py` shows a root holding only definitions taking a `sip` load. One paper run
on 2026-11-11, on account A's pair in data-only mode, loaded SPY and QQQ 1-minute `sip` bars for
14:30-15:30 UTC on 2026-11-10 into a root holding only their definitions: exit 0, 122 bars, one GET; the
same command again wrote nothing and sent no request.

Muse's independent review of `ea705a6` identified the overlap, row-bound, feed-race, unnamed-error and
silent-rounding defects. Each fix above has a regression that failed before the fix in
`tests/test_history_load.py`; the review's feed-marker interleaving is reproduced with the stand-in.
Concurrency with legacy writers that do not take the catalog lock is a recorded limit, not a verified
shared-lock guarantee.

Antigravity's review of `0bcca05` found the premature filesystem writes, unused marker after a failed
first write, and unnamed client setup errors. Each reproduced in this test file before its fix.
DeepSeek Harness's review of `0bcca05` found that the record named the wrong component as truncating
the gap's nanosecond, since the pinned stack keeps it; the text is corrected.

## Evidence

Quotes amendment, source check 2026-11-11: cached alpaca-py `c42add7c0a70`
`alpaca/data/historical/stock.py` (`get_stock_quotes`, page size 10,000), `alpaca/common/rest.py`
(`_get_marketdata`, total limit and page tokens), `alpaca/data/requests.py` (`StockQuotesRequest`),
`alpaca/data/models/quotes.py` (`QuoteSet` / `Quote`) and `alpaca/data/mappings.py` (`QUOTE_MAPPING`);
NautilusTrader `d53849b216e2` `python/nautilus_trader/persistence/__init__.pyi` (`write_quote_ticks`,
coverage queries) and `crates/model/src/data/quote.rs` (`QuoteTick` fields); the adapter's
`_request_quotes` and `parse_quote`. Read, without changing or running upstream sources.
The size-unit requirement and its measured/UNVERIFIED boundary are ADR 0066's 2026-11-11 confirmation.
Antigravity's reader verified Alpaca's calendar date from the three REST quote schemas, but not an
intraday/time-zone boundary or uniform historical lot size. Its four findings were reproduced with
failing regressions in `tests/test_quotes_load.py`: microsecond boundary losses, historical-unit
assumptions/root mixing, unbounded quote pagination, and CLI validation after client setup. The fixes
use the pinned catalog's `list_instruments`/`get_intervals` and inclusive coverage, alpaca-py's
`Quote.timestamp`/request serialization, and requests `6e83187b8feb` response hooks. No NautilusTrader
docs-site content was used for this amendment.

Fix-forward source check, 2026-11-11, without network access: cached alpaca-py `c42add7c0a70`
`get_stock_bars` and `_get_marketdata` (page following, including short pages); NautilusTrader
`d53849b216e2` catalog bindings and engine filtering; the adapter's `alpaca_bars_window`, `parse_bar`
and `bar_roundings`; and Python `b703b2c5e704` (`Doc/library/fcntl.rst`, `contextlib.rst`,
`Doc/builtins/functions.rst`) for advisory locking and exclusive file creation. The fake venue exercises
New York midnights on both sides of spring DST; the venue's live daily-open convention remains
UNVERIFIED as the adapter's header states.

Antigravity follow-up, 2026-11-11: `bars_to_catalog._holds_data` and its failed-load marker cleanup,
the adapter's `credentials_from_env`, `with_http_timeout` and `require_get_only`, and Python
`b703b2c5e704` `Lib/pathlib/__init__.py` (`is_dir`, `resolve`, `unlink`) were checked for preflight and
marker cleanup. Setup-error tests remove only placeholder environment values; no real credential is
read and no request is sent.

Read 2026-11-11. Fetched means read in this session; source means read at the pin in the upstream
source cache.

* https://docs.alpaca.markets/us/docs/about-market-data-api.md — the equities table: "Historical API
  calls | 200 / min | 10,000 / min". Fetched.
* https://docs.alpaca.markets/us/reference/stockbars.md — the limit "applies to the total number of data
  points, not per symbol!", maximum 10000, and "Always check the `next_page_token`". 429: "Too many
  requests. You hit the rate limit." The `X-RateLimit-*` headers, and the feed parameter's `sip`. Fetched.
* https://alpaca.markets/support/usage-limit-api-calls — "200 requests per minute, per account", as ADR
  0069 cites it. Not fetched again.
* alpaca-py 0.45.0 (c42add7c0a70):
  * `alpaca/data/historical/stock.py`: `get_stock_bars`, `page_size=10_000`, and a constructor with no
    retry setting.
  * `alpaca/common/rest.py`: `_request`'s retry loop; `_one_request` raising `RetryException` for a code
    in `_retry_codes`; `retry_attempts` applied only above 0.
  * `alpaca/common/constants.py`: `DEFAULT_RETRY_EXCEPTION_CODES = [429, 504]`.

  Source.
* NautilusTrader v2.0.0rc8 in `.venv` — the docstrings of `ParquetDataCatalog.write_bars(data, start,
  end)`, `get_missing_intervals_for_request` and `instruments(instrument_ids=…)`. `write_instruments`
  writes an `Equity` under `data/equity/`. Run.
* `canon_alpaca` `data.py` and `providers.py` — `alpaca_bars_window`, `parse_bar`, `bar_timeframe`,
  `request_counts`, `with_http_timeout`, `require_get_only`; `execution.py`, `end_at_first_error`, the
  precedent for the hook. Read.
* https://requests.readthedocs.io/en/latest/user/advanced/#event-hooks — "You can also add hooks to a
  `Session` instance. Any hooks you add will then be called on every request made to the session", and
  "Any unhandled exception won't be passed silently". Fetched.
* Second reader, 2026-11-11: an evidence check of this record found the trading budget's figure unlabelled,
  the engine-filter claim uncited and the execution-client precedent loosely stated; each is corrected
  above, and the DeepSeek Harness review is named.
* Trades amendment, source check 2026-11-11: NautilusTrader `d53849b216e2`
  `python/nautilus_trader/persistence/__init__.pyi` (`write_trade_ticks`, coverage queries),
  `crates/persistence/src/python/backend/parquet/catalog.rs` (`start: u64`),
  `crates/model/src/data/trade.rs` (`TradeTick` fields) and `python/nautilus_trader/core/datetime.py`;
  alpaca-py `c42add7c0a70` `alpaca/data/mappings.py` (`TRADE_MAPPING`) and `alpaca/common/rest.py`
  (`_request` / `_one_request` and the 504 retry loop); requests `6e83187b8feb`
  `docs/user/advanced.rst`, Transport Adapters, and `src/requests/adapters.py` / `sessions.py`
  (`BaseAdapter.send`, `Session.mount`, longest-prefix selection). Read from the pinned source cache.
  The adapter's `fetch_trades` and `parse_trade` were read without changing them.
