---
status: accepted
date: 2026-11-11
decision-makers: the lead for the pairs the task may read (2026-11-11); master orchestrator (Claude) for the request path, the pace and its terms, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# paper:history: a paced, GET-only load of historical `sip` bars for many symbols into the catalog

## Context and Problem Statement

The movers work needs minute and day bars for a few thousand symbols. The baseline is five sessions over
the eligible universe. `canon_trading.bars_to_catalog.load_bars` loads one bar type of one symbol per
call. Each call builds a node, and the node's instrument provider sends one trading-API GET
(`/v2/assets/{symbol}`) beside the data GETs. Over a whole market that would spend the account's
trading budget, 200 a minute per account (ADR 0069), on asset reads. It would also take a node build per
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
   at most 200 symbols, and of at most `MAX_ROWS_PER_CALL` (100,000) bars, counting every bar the
   symbols could have in the window, one an interval of wall-clock time. A window longer than that for
   one symbol is cut into segments, each written over its own bounds. The request is `StockBarsRequest(symbol_or_symbols=…, timeframe, start, end,
   feed=sip)`, with no limit and no adjustment, so Alpaca's default `raw` applies, as for `load_bars`.
   alpaca-py follows `next_page_token` through every page of 10,000 rows.
2. **Window and parsing.** Both are the adapter's: `alpaca_bars_window`, `parse_bar` and `bar_timeframe`.
   A bar written here is the bar `load_bars` writes. `ts_init` is the bar's close, and `[start, end]`
   bounds it. An end less than one bar interval, or 15 minutes, before now is refused, by `load_bars`'
   rule; `--bars 1-MINUTE` serves a more recent end.
3. **Writes.** `write_bars(bars, start, end)` runs per bar type over the bounds asked for, as the node's
   write-back gives them. What the root covers (`get_missing_intervals_for_request`) is not asked for
   again. Symbols with the same gap share a request. The root keeps one feed through `bars_to_catalog`'s
   `alpaca-feed` file, and a root holding another feed is refused. The root is an argument, defaulting
   to `catalog/`, and anything under `ledger/` is refused.
4. **Instruments.** Each bar is parsed against its `Equity` (price precision), read from the catalog
   root. The definitions are meant to come from an asset list load: one trading GET for the whole list
   (`get_all_assets`, then `parse_equity`, then `write_instruments`). This task sends none. That load is
   another lane's work and is not on `main` at this change. Until it lands, the definitions in a root
   are the ones `load_bars` has written, one symbol at a time. That is the per-symbol trading GET this
   record avoids, so a whole-market load waits for the asset list load.
   A symbol with no definition is refused by name before any request. Definitions alone are not a
   feed's data, so `catalog_feed` no longer reads a root holding only `data/equity/` as IEX. Until this
   change, such a root refused every `sip` load.
5. **The pace.** One constant, `history_load.PACE`, holds this table's row. After each request the
   adapter's request counter (`canon_alpaca.providers.request_counts`) gives the GETs it sent to each
   API. The next request starts no sooner than its data GETs allow at the row's data rate. The trading
   column is a guard: a request that sent any trading GET stops the run. Inside one request, alpaca-py
   sends its pages back to back, at most ten by `MAX_ROWS_PER_CALL`, so the rate holds across requests,
   not inside one.

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
   name (exit 4). A 504 is still retried three times, unpaced, as alpaca-py does it, and each retry is
   counted. This is the way the adapter's execution client ends an order command at its first
   error. The counter's 429 count is checked after each request as well.
7. **Failures.** A refused request, a 403 on `sip` included, stops the run by name, with the HTTP
   status. Nothing falls back to another feed. Whatever an earlier request wrote stays, and a rerun asks
   only for what is missing.
8. **Pairs.** The lead decided on 2026-11-11 that `paper:history <account>` takes either account's pair,
   `tests` (A) or `windows` (B). It reads market data and nothing of the account. ADR 0069's rule on who
   reads B's pair governs who trades and streams on B, and a GET-only history load does neither. The
   task reads one pair, never both (ADR 0069, clause 2's refusal). It runs in data-only mode (ADR 0070).
9. **Trades.** `--data trades` is refused by name until the adapter's `_request_trades` exists. The
   argument holds its place.

### Consequences

* Good:
  * A whole-market load spends no trading budget.
  * Each request carries up to 200 symbols, so the baseline (five sessions of minute bars for a few
    thousand symbols) is tens of requests plus their pages, not thousands of node builds.
* Good: what the root already holds is not fetched again, and a stop leaves what was written.
* Bad: a second writer of bars beside `load_bars`. Both use the adapter's window and parsing, and the
  tests check that the bars and the coverage match.
* Bad:
  * The pace is not calibrated.
  * alpaca-py gathers every page of a request in memory before it returns: up to
    `MAX_ROWS_PER_CALL`, 100,000 bars.
  * Counting every interval of wall-clock time overstates a minute window's bars by the hours with
    no trade, so a request carries fewer symbols than the pages could hold.
  * The most symbols Alpaca accepts in one request is not stated (UNVERIFIED).
* Bad: alpaca-py's pydantic model is built for every bar. The movers lane proposed `raw_data=True`,
  which would need a parser of its own. That is left until a measured load shows the cost.

### Confirmation

`tests/test_history_load.py` runs offline. It uses alpaca-py's real client and the adapter's timeout
adapter and counter, with requests' transport answered by a stand-in of the bars endpoint. It shows:

* many symbols in one request on `sip`, with no trading host reached;
* every page followed, and the next request waiting for the GETs the last one sent;
* the symbols batched, and a long window cut into segments, by `MAX_ROWS_PER_CALL`;
* a 429 stopping the run with one request sent, where alpaca-py would have sent four;
* a 403 stopping it by name with no `iex` request;
* a covered range asked for nothing;
* a symbol with no definition, and a root holding `iex`, refused before any request;
* the arguments refusing an open-ended or unplaced range;
* `PACE` equal to this record's row;
* the task reading one named pair, with no lock, in data-only mode.

`tests/test_bars_to_catalog.py` shows a root holding only definitions taking a `sip` load. A paper run
of the task against Alpaca is not part of this change (UNVERIFIED until one runs).

## Evidence

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
