---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
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

Chosen: **option 1, on IEX only, over a window of weeks to a quarter of 2025 or later**. It is ADR
0040's path for bars and keeps its conventions (window, coverage, re-runs). The catalog guide's rules
apply as written: a request with a record limit or a source `schema` is refused for write-back, and a
request split across several source envelopes skips it. So the hook answers each request with one
response, and a load is a run of requests an hour long at most, not one request: alpaca-py's
`get_stock_quotes` holds every page of a request in memory before it returns, and SPY on IEX averaged
0.74 M quotes an hour over the regular session of 2026-11-06 (the busiest hour unmeasured; memory
UNVERIFIED). The key pair reaches a process only through `mise run paper:test` or `paper:session` (ADR
0029, ADR 0044), and `paper:test` holds the paper lock with a deadline of 7,140 s, less its wait for the
lock, up to 3,900 s. Under that task a load would run in pieces (condition 2 below says it does not,
while windows run): about two weeks of 2025 or one week of 2026 for both symbols, each inside the
deadline at the probe's page rate (41 and 31 minutes against
54; parsing, writing and start-up unmeasured). Each piece asks only for what the catalog
does not cover. An empty hour (night, weekend) may record no coverage (ADR 0040 saw an empty bars
response write nothing), so it is asked for again, at one call each. Whether a piece stopped at the
deadline by SIGKILL leaves a partial Parquet file is UNVERIFIED (settled 2026-11-11: it does not,
Confirmation). Condition 2 below says where the bulk
load runs. That write-back works for quotes at the pin is UNVERIFIED until that code runs (it does,
Confirmation, 2026-11-11). ADR 0040
observed that an empty bars response
wrote nothing, though the guide says it can record known-empty coverage. Option 2 would need ADR 0040's
reasons against it answered again. Option 3 stays open if the owner finds the load too large.

**Conditions of acceptance** (accepted 2026-11-10, below):

1. First, the hook and one small write-back on paper: a few minutes of SPY quotes on IEX, one request.
   It settles the three UNVERIFIED points the rest rests on: whether write-back works for quotes at the
   pin, what an empty response records, and what a request stopped in mid-write leaves in the catalog.
2. The bulk load does not run under `paper:test` once session windows run around the clock, since a
   window that fires during a piece fails. It waits for whichever comes first: a task for REST data
   reads only, decided in its own record first since it would be a third path for the key pair, at a
   rate that leaves room for the windows' own data calls, with whether the 200 a minute is shared
   measured first; or a weekend window checked first to be idle, since whether Alpaca's clock is
   closed all weekend is UNVERIFIED (ADR 0050), the overnight session opens on Sunday evening, and
   another lane's paper run may take the lock at any hour.
3. The first study on quotes takes the small end: about a month of 2025 for SPY and QQQ, about 110 M
   quotes and under a gigabyte in the catalog. It is pre-registered in its own record before any
   trial, with its own N; a study that repeats an earlier one's configurations says so and counts them,
   and a re-run on a moved sample stays in its study.
4. A result on IEX quotes is read as ADR 0067 reads it: closer to paper, not paper; IEX is about 2.5% of
   volume and its best bid and ask are not the NBBO; SPY's IEX quotes are thin before 2022.

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
SPY days of one 2026 week ranged 2.31 M to 4.86 M). A day's quotes for SPY and QQQ: 2022 0.79 M and
0.82 M; 2023 1.76 M and 1.94 M; 2024 0.68 M and 0.95 M; 2025 2.22 M and 3.03 M; the 2026 days counted,
6.7 M to 8.9 M for the two, so a
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
calls 200 / min". On IEX the bar study's sample is not usable for SPY before 2022, being served but
sparse, and would take more than a day. So the pre-registration of a quote study picks a window of
weeks to a quarter from 2025 on, and loads it before any trial runs (condition 3).

### Consequences

* Good: the shipped `EmaCross` can run on the quotes it subscribes to, over the path the bars take.
* Bad: a quote study's sample is months, not years, and IEX is about 2.5% of volume (historical data
  page). A load holds the key's data budget for hours. A session window (ADR 0050) waits at most 900 s
  for the paper lock, so a load under `paper:test` would make a window that fires during a piece fail,
  placing nothing, which is why condition 2 keeps the bulk load out of it.

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

**Condition 1** (2026-11-11). The data client answers `_request_quotes` (v1's
`_request_quote_ticks`, renamed in v2 by the adapter guide's migration table), IEX only, one
response a request, each quote stamped `ts_init = ts_event` as upstream's Databento client stamps a
historical range (`canon_alpaca.data`). On paper, 01:09 New York, `mise run paper:test -- -s -v -k
quotes_write_back`, exit 0, 3 passed in 5 s after the paper lock came free (log kept outside the
repository), each catalog in a temporary folder:

* Write-back works for quotes at the pin: SPY on IEX, 10:00 to 10:03 New York on 2026-11-09, one
  request, 48,924 quotes in one file, `data/quotes/SPY.ALPACA/2026-11-09T15-00-00-000000000Z_2026-11-09T15-03-00-000000000Z.parquet`,
  the window recorded as covered; a second request over it wrote nothing.
* An empty response records nothing: SPY on IEX, 12:00 to 12:01 New York on Sunday 2026-11-08, no quote;
  the engine logged "Received empty QuoteTick response", wrote no file and recorded no coverage, as ADR
  0040 found for bars, so an empty hour is asked for again.
* A request killed in mid-write leaves no partial file: the same window, requested in a child process
  sent SIGKILL as its catalog logged "Writing", left no data file, no staging file and no coverage; a
  request after it filled the window with the same quotes. Offline, with a million stand-in quotes, the
  catalog encoded the file whole, wrote it as `<name>#1` and renamed it into place, about 80 ms after its
  "Writing" line; killed on seeing the staging file, the rename had already landed, and the catalog
  held the complete file. A staging file left behind, even cut short, is not read: no coverage, no
  quote, the window still reported missing, and the next write-back writes the file beside it.

Each is an offline test in `packages/canon-alpaca/tests/test_data_client.py` (`test_a_quote_request_writes_back_and_a_covered_one_asks_alpaca_nothing`,
`test_an_empty_quote_response_records_nothing_and_is_asked_for_again`,
`test_a_staging_file_a_killed_write_leaves_is_not_read_and_the_window_is_written_again`) and a paper test in
`test_paper_quotes_write_back.py`. Also observed at the pin, offline: a request with a `limit` is answered
and not written back, with no failure. Upstream's TC-D21 (`DataTester`, `request_quotes=True`) is added to
`test_paper_matrix.py` and runs in the regular session; it has not run yet.

Quote sizes: the quotes reference gives bid and ask size "in shares (round lots prior to November 3,
2025)"; the real-time page still gives round lots for the stream. Of the 97,848 sizes above, 3,755 were a
multiple of 100 and the largest 640, which does not settle the unit. Nothing converts a size, so a
catalog spanning 2025-11-03 may change unit there (UNVERIFIED which).

**Quote sizes measured** (2026-11-11). On paper, 09:38 to 09:45 New York, a throwaway `alpaca_stream`
test under `mise run paper:test`, exit 0, 1 passed (log kept outside the repository): SPY quotes on IEX
from the stream for 180 s from 14:42:07 UTC, raw, and the historical quotes of the same span. Live
36,769, history 37,162, every live quote matched in history by timestamp and both prices; bid sizes
equal on 36,502 and ask sizes on 36,666, a factor of 100 either way on none. Sizes ran 40 to 560, and
about 91% were not multiples of 100. So since 2025-11-03 sizes are shares on the stream and in history
alike, and a load from that date converts nothing. Alpaca's two quotes references, historical and
latest, give sizes "in shares (round lots prior to November 3, 2025)" and agree with the measurement.
Alpaca's real-time page gives the stream's sizes "in round lots": fetched 2026-11-11; contradicted by
measurement the same day. History before 2025-11-03 is in round lots by those references (UNVERIFIED
here: only today's quotes were measured), so a load spanning that date multiplies its earlier sizes by
100 or starts after it.

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
* Read 2026-11-11: https://docs.alpaca.markets/us/reference/stockquotes-1.md (inclusive `start` and
  `end`; sizes "in shares (round lots prior to November 3, 2025)") and
  https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data.md (sizes "in round lots"), fetched;
  https://docs.alpaca.markets/us/reference/stocklatestquotes-1.md (the same size wording as the
  historical reference), fetched with the size measurement;
  https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md (`_request_quotes`; v1
  `quote_ticks` hooks became `quotes` hooks) and `concepts/data/catalog.md` (write-back rules), fetched;
  `developer_guide/spec_data_testing.md` (TC-D21), fetched. At the pinned commit,
  `crates/adapters/databento/src/historical.rs` and `decode/market_data.rs` (historical quotes decoded with
  no `ts_init`, so each takes its record's own timestamp) and `crates/testkit/src/testers/data/actor.rs`
  (TC-D21 asks for the hour before now), fetched. The write-back runs under Confirmation, run.
* The probes under Confirmation. Run.
* Accepted 2026-11-10 by the master orchestrator under the owner's delegation of 2026-11-10, on the
  four conditions under Decision Outcome.
* Second reader, 2026-11-10 (`evidence-verifier`): the counts, first quotes, refusals and table hold
  against the logs; corrected above: the hook's class, write-back for quotes unverified and its catalog
  rules, the JSON byte measure, SPY (not QQQ) thin before 2022, `boats` documented for the free plan,
  the `overnight` disagreement, requests split into short windows, 2026's larger days. Its re-read
  after the code review corrected the hourly figure, the size of a piece against the deadline and its
  lock wait, that session windows fail rather than wait, the unsourced per-key budget, and the empty
  hours asked for again. Code review (`/code-review`):
  the paper test now has the HTTP timeout and reports a refusal's status; requests an hour long for
  memory; a load in pieces under `paper:test`'s deadline; the page rate stated unambiguously.
