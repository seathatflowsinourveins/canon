---
status: proposed
date: 2026-11-10
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-10
---

# Alpaca's historical quotes into the catalog: write-back through the data client, IEX only, a window of weeks

## Context and Problem Statement

Roadmap step 7 still has to rank a strategy upstream ships. Upstream's built-in `EmaCross` keeps its
averages of the quote mid, and on a catalog of bars it placed no order (ADR 0048). Running it needs
SPY's and QQQ's quotes in the catalog, and nothing loads quotes. How far back does Alpaca serve them on
the paper pair, how large is a load, which feed may a load use, and by what path does it reach the catalog?

## Decision Drivers

* ADR 0001: the path upstream documents. ADR 0040 took the node's catalog write-back for bars.
* ADR 0055: a feed served but not documented for the account is observed, not built on.
* The paper pair's market data calls are shared with every other paper run (200 a minute on Basic).

## Considered Options

1. **Write-back**: the adapter's data client answers `_request_quotes`, the hook the adapter guide names
   for quote history (new code: the client answers only `_request_bars` now), and a request with
   `params={"update_catalog": True}` writes the quotes, as ADR 0040 does for bars.
2. **Fetch, then `write_quote_ticks`**, with the coverage bookkeeping written here.
3. **No load**: step 7 stays met in part.

## Decision Outcome

Proposed: **option 1, on IEX only, over a window of weeks to a quarter of 2025 or later**. It is ADR
0040's path for bars and keeps its conventions (window, coverage, re-runs). The catalog guide's rules
apply as written: a request with a record limit or a source `schema` is refused for write-back, and a
request split across several source envelopes skips it. So the hook answers each request with one
response, and a load is a run of requests an hour long at most, not one request: alpaca-py's
`get_stock_quotes` holds every page of a request in memory before it returns, and SPY on IEX averaged
0.74 M quotes an hour over the regular session of 2026-11-06 (the busiest hour unmeasured; memory
UNVERIFIED). The key pair reaches a process only through `mise run paper:test` or `paper:session` (ADR
0029, ADR 0044), and `paper:test` holds the paper lock with a deadline of 7,140 s, less its wait for the
lock, up to 3,900 s. So a load runs in pieces under that task: about two weeks of 2025 or one week of
2026 for both symbols, each inside the deadline at the probe's page rate (41 and 31 minutes against
54; parsing, writing and start-up unmeasured). Each piece asks only for what the catalog
does not cover. An empty hour (night, weekend) may record no coverage (ADR 0040 saw an empty bars
response write nothing), so it is asked for again, at one call each. Whether a piece stopped at the
deadline by SIGKILL leaves a partial Parquet file is UNVERIFIED. A task of its own is a later change. That write-back works
for quotes at the pin is UNVERIFIED until that code runs. ADR 0040 observed that an empty bars response
wrote nothing, though the guide says it can record known-empty coverage. Option 2 would need ADR 0040's
reasons against it answered again. Option 3 stays open if the owner finds the load too large.

**What the paper pair is served** (probe, Confirmation). The first quote is 2020-07-27 for SPY and
2020-07-28 for QQQ on `iex`, 2016-01-04 for both on `sip`, and 2024-09-16 on `boats`. `otc` was refused
("subscription does not permit querying OTC data"). `delayed_sip` and `overnight`, which alpaca-py's
`DataFeed` has but the quotes reference does not list, were refused as invalid feeds. The historical data
page says `feed` switches between all its sources "on all the stock endpoints", `overnight` among them,
while the 24/5 page says `feed=overnight` "will give error" for historical overnight data. The refusal
matches the second page. SPY is thin on IEX before 2022: 150 quotes on 2020-11-09, 4,606 on 2021-11-09
and 786,239 on 2022-11-09. QQQ is not: 810,227 and 599,091 on the 2020 and 2021 days.

**Where Alpaca's pages disagree with this, and with each other.** The plan table gives Basic
"Historical data timeframe: Since 2016". That matches `sip` and not `iex`, whose quotes begin in July
2020, as the bars do (ADR 0048). The plan table ("latest 15 minutes") and the market data FAQ ("the `end`
parameter must be at least 15 minutes old to query SIP data without a subscription") give SIP history to
an account with no subscription. The historical data page says IEX "is the only feed that can be used
without a subscription". The paper page says a Paper Only account is "only entitled to receive and make
use of IEX market data". So SIP history is served, and two pages allow it, but the page about this kind
of account does not. Under ADR 0055 it is observed, not built on. `boats` history is documented for
the free plan ("all 15-minute delayed BOATS data", the 24/5 page), but it covers the overnight session
and starts in September 2024, so it does not serve a regular-session study. It, too, is observed. The
docs' gate on SIP is unsettled, not shown to be wrong.

**Size, on IEX** (probe; one counted November day a year, so each figure is an order of magnitude: three
SPY days of one 2026 week ranged 2.31 M to 4.86 M). A day's quotes for SPY and QQQ: 2022 0.79 M and 0.82 M; 2023 1.76 M and 1.94 M; 2024
0.68 M and 0.95 M; 2025 2.22 M and 3.03 M; the 2026 days counted, 6.7 M to 8.9 M for the two, so a
2026 window is larger than the rows below. A page holds at most 10,000 quotes. A quote is about 133
bytes of JSON (the parsed page serialised again, not the bytes on the wire), and 8 bytes in the
catalog's Parquet (one page, written by `write_quote_ticks`). For both symbols, at 250 sessions a year:

| Window | Quotes | Requests | At 200 a minute | JSON | Catalog |
|---|---|---|---|---|---|
| 2020-07-27 to 2025-12-31, the bar study's sample | 3.3 billion | 330,000 | 27.5 h | 440 GB | 26 GB |
| 2025 | 1.3 billion | 131,000 | 11 h | 174 GB | 10.5 GB |
| One quarter of 2025 | 330 million | 33,000 | 2.75 h | 44 GB | 2.6 GB |
| One month of 2025 | 110 million | 11,000 | 55 min | 15 GB | 0.9 GB |

The probe took 0.47 s a page, its own 0.31 s spacing included. A loader under 200 a minute sends a page
every 0.3 s at best, so the hours above are the floor; at the probe's pace, 2025 would take 17 h and the
full sample 43 h. Whether other paper runs' data calls share that budget while a load runs, and whether
the Trading API's 200 a minute is separate, is UNVERIFIED: the plan page gives only "Historical API
calls 200 / min". On IEX the
bar study's sample is not usable for SPY before 2022, being served but sparse, and would take more than
a day. So the
pre-registration of a quote study picks a window of weeks to a quarter from 2025 on, and loads it before
any trial runs.

### Consequences

* Good: the shipped `EmaCross` can run on the quotes it subscribes to, over the path the bars take.
* Bad: a quote study's sample is months, not years, and IEX is about 2.5% of volume (historical data
  page). A load holds the paper lock for hours, in pieces. A session window (ADR 0050) waits at most
  900 s for the lock, so a window that fires during a piece fails, placing nothing, and shows FAILED.

### Confirmation

Paper, 2026-11-10, read-only probes of `/stocks/quotes` through alpaca-py's `RESTClient.get`, one page a
call, logs kept outside the repository:

* 22:32 New York, `mise run paper:test -- -s -k quote_history`, exit 0, 836 s: the first quote on each
  of six feeds for both symbols; IEX full days 2026-11-06 and 2026-11-09 (SPY 4.86 M and 2.93 M, QQQ
  4.00 M and 3.73 M); 10:00 to 10:15 New York on 2026-11-09 (SPY: 212,537 on IEX, 275,800 on SIP; QQQ:
  166,493 and 498,973) and on 2021-11-09 (SPY: 81 and 467,420; QQQ: 54,643 and 512,993). An earlier
  run was stopped by hand after its first full day, SPY 2026-11-03, 2.31 M on IEX, to keep the paper
  lock short.
* 22:48, `mise run paper:test -- -s -k quote_history_bytes`, exit 0: one page of 10,000 SPY quotes on
  `iex` and on `sip`, 133 and 134 bytes a quote as JSON, 8.1 and 7.8 in the catalog, none one-sided
  under `canon_alpaca.data.parse_quote`; SPY IEX days in 2021 to 2023. The run before it failed (exit
  1) on the test's own missing catalog directory, before any count.
* 22:51, `mise run paper:test -- -s -k quote_history_iex_day_by_year`, exit 0, 545 s: the remaining
  IEX days a year above.
* `tests/test_paper_quote_history.py` keeps the first-quote dates and the three refusals as a paper
  test. A change in what Alpaca serves fails there before a load is sized on stale dates. 23:20, on the
  reviewed code,
  `mise run paper:test -- -v -k quote_history_begins`, exit 0, 1 passed in 2 s. The counting
  probes were throwaway tests, kept outside the repository.

`UNVERIFIED`: a full SIP day (not counted, to keep the lock short); whether HTTP compression shrinks the
133 bytes on the wire; whether a full day compresses like one page; how long a backtest takes over a
quarter of quotes.

## Evidence

Read 2026-11-10. Fetched means read as raw text; run means executed here.

* https://docs.alpaca.markets/us/reference/stockquotes-1.md: `feed` (`sip`, `iex`, `boats`, `otc`,
  default `sip`), `limit` (default 1,000, maximum 10,000, "applies to the total number of data points,
  not per symbol"), `sort`, `page_token`. Fetched. It settles the US page size, which had been sourced
  from the GIFT City reference.
* https://docs.alpaca.markets/us/docs/about-market-data-api.md: Basic, the default for paper accounts,
  "Since 2016", "latest 15 minutes", 200 historical calls a minute. Fetched.
* https://docs.alpaca.markets/us/docs/market-data-faq.md: SIP history without a subscription when `end`
  is at least 15 minutes old. Fetched.
* https://docs.alpaca.markets/us/docs/historical-stock-data-1.md: IEX "the only feed that can be used
  without a subscription", about 2.5% of volume; `feed` "on all the stock endpoints". Fetched.
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md: free plan `boats` history "all
  15-minute delayed BOATS data"; historical `feed=overnight` "will give error". Fetched.
* https://docs.alpaca.markets/us/docs/paper-trading.md: a Paper Only account is entitled to IEX only.
  Fetched.
* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md: "Quote history calls
  `_request_quotes(request)`"; `QuotesResponse`; an empty response with `update_catalog=True` can record
  known-empty coverage. Fetched. At the pin the hook is `DataClient._request_quotes`
  (`nautilus_trader/live/clients.py`), which raises `NotImplementedError` until a client overrides it.
  Bundled.
* https://nautilustrader.io/docs/md/latest/concepts/data/catalog.md: `update_catalog`; write-back needs a
  client that reports completion, "such as ... a Python adapter"; requests with a record limit or a
  source schema are rejected; split responses skip write-back. Fetched.
* alpaca-py 0.45.0 as installed: `get_stock_quotes` pages `/stocks/quotes` at `page_size=10_000` through
  `RESTClient.get`; `DataFeed`'s six members. Bundled.
* ParquetDataCatalog at the pin: `write_quote_ticks` (installed docstring). Bundled.
* The probes under Confirmation. Run.
* Second reader, 2026-11-10 (`evidence-verifier`): the counts, first quotes, refusals and table hold
  against the logs; corrected above: the hook's class, write-back for quotes unverified and its catalog
  rules, the JSON byte measure, SPY (not QQQ) thin before 2022, `boats` documented for the free plan,
  the `overnight` disagreement, requests split into short windows, 2026's larger days. Its re-read
  after the code review corrected the hourly figure, the size of a piece against the deadline and its
  lock wait, that session windows fail rather than wait, the unsourced per-key budget, and the empty
  hours asked for again. Code review (`/code-review`):
  the paper test now has the HTTP timeout and reports a refusal's status; requests an hour long for
  memory; a load in pieces under `paper:test`'s deadline; the page rate stated unambiguously.
