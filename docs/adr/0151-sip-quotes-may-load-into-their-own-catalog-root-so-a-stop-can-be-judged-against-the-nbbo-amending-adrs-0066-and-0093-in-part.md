---
status: proposed
date: 2026-11-12
decision-makers: the lead (2026-11-12), under the owner's delegation (ADR 0074); master orchestrator (Claude) for the record
review-by: 2027-02-10
---

# SIP quotes may load into their own catalog root, so a stop can be judged against the NBBO; amends ADRs 0066 and 0093 in part (proposed: open until one SIP quote load has run)

## Context and Problem Statement

ADR 0066 loads historical quotes on IEX only and keeps SIP "observed, not built on". Alpaca elects a
stop only on a consolidated print "not outside of the NBBO" (orders page). ADR 0119 holds a stop on that
rule, and leaves its second half, the test against the quote, unbuilt as its first gap. IEX's best bid
and ask are not the NBBO (ADR 0066, ADR 0067), so a check or replay on IEX quotes cannot judge it.
ADR 0093's clause 10 repeats it for the loader: "The root is IEX: a SIP root is refused by name". Both
are `accepted` and only those lines change, so this record carries the change with a dated pointer in
each, as ADR 0106 does for ADR 0044 (ADR 0106 is itself still `proposed`).

## Decision Drivers

* The stop's rule is Alpaca's, and its quote half needs the consolidated quote.
* ADR 0066's write path, window, coverage and size limits stand; only the feed it may name changes.
* One feed per catalog root: catalog keys do not distinguish feeds (`bars_to_catalog.py`'s feed marker;
  ADR 0077).

## Considered Options

1. Keep IEX only, and leave ADR 0119's quote half unjudged.
2. Judge the quote half on IEX quotes.
3. Let `paper:history --data quotes` name `sip`, into a root that holds SIP data only.

## Decision Outcome

Chosen, proposed: **option 3**. Option 2 tests a print against a quote Alpaca does not test against.
Option 1 leaves ADR 0119's first gap open with no data that could close it. The gap is the print
trigger's: under the Mover's default trigger (`bid_ask`) a quote's bid elects the trailing sell
(`session_window.py:761-769`), so the print-and-NBBO rule bears on the `last_price` trigger and on
replays of it. ADR 0119 is itself `proposed`.

What changes in ADR 0066 and in ADR 0093's clause 10:

* "Chosen: option 1, on IEX only": a quote load may name `sip` as well. IEX stays the default. Nothing
  falls back from one feed to the other.
* A SIP load writes to a root whose feed marker says `sip`. The loader already refuses a root of another
  feed (`history_load._check_feed`: "give each feed its own root"; its module header: "SIP and IEX need
  separate roots").
* What had to change for that, built on 2026-11-13 by the change that loads SIP quotes within the
  bound below and cites this record: `load_quotes` refused any feed but IEX
  (`history_load.py:946-947`); the command line had no `--feed` for quotes; `mise.toml`'s `paper:history`
  refused `--feed` in load mode (`:766`); `catalog/README.md` ("SIP roots are refused", `:115`) and
  `AGENTS.md`'s `paper:history` row said IEX quotes. Each now takes `sip` within the bound.
* The bound a SIP quote load keeps, the lead's size for a case replay and UNCALIBRATED: at most 8
  symbols, named with `--symbols` (never a symbols file) and counted after strip and upper case; at most
  60 minutes from start to end; and a root of its own that holds no bars or trades, by default
  `catalog/sip-quotes`. SIP quotes, bars and trades share one feed marker, so a bars or trades load is
  refused in a root that holds quotes as well, judged by the pinned catalog's `list_instruments`. A
  feed of `iex` outside a quote load, a symbols file and more than 8 symbols are refused before any
  credential is read; the span and the root are checked after the pair is read and before any request
  or lock.
* ADR 0066's reason for holding SIP back was that Alpaca's pages disagree on whether this kind of account
  may use SIP. That reason no longer decides here. Since 2026-11-11 the windows stream `sip` in
  pre-market and after-hours (ADR 0044's feed table, the lead's decision; `sip` is not yet seen there in
  after-hours), and `paper:history` loads `sip` bars (ADR 0093, the coordinator's choice; one paper run
  recorded; its SIP trades load is still UNVERIFIED). Alpaca serves SIP history without a subscription
  when `end` is at least 15 minutes old (the market data FAQ; the quotes reference's default `end`), and
  the loader's `PUBLICATION_DELAY` is 15 minutes. Account A's pair was served SIP quotes in ADR 0066's
  probe; account B's pair is not observed (UNVERIFIED). A refused SIP request stops the load by name, as
  it does for bars.
* The window, the start-after limit, the hourly pieces and the 90-day ceiling apply to SIP as written.
  SIP carries more quotes than IEX: the probe counted 275,800 SPY quotes on SIP against 212,537 on IEX,
  10:00 to 10:15 on 2026-11-09 (ADR 0066). An answer over the per-call row cap is loaded in parts; only
  more than the cap in one microsecond stops the load by name.

When it takes effect: once this record has had its `evidence-verifier` pass and one SIP quote load has
run through `paper:history`, refused nothing, and written a root marked `sip`. The test against the
quote itself (ADR 0119's first gap) is its own unit and its own failing test first, and is not decided
here.

### Consequences

* Good: a stop, a replay or a check can test a print against the consolidated quote rather than IEX's.
  Alpaca's quotes reference calls `sip` "all US exchanges" and does not say NBBO; Alpaca staff on its
  forum call historical quotes older than 15 minutes "full market NBBO quotes" (2024), and say the
  SIPs' quotes after 16:00 ET "are not technically NBBO quotes" (2026). So a replay inside the regular
  session reads the NBBO by a staff post, not a documented page (UNVERIFIED there), and one outside it
  reads the SIPs' best quote, which is not the NBBO by definition: a named limit for an extended-hours
  case. A
  `QuoteTick` keeps no exchange or condition fields (`bx`, `ax`, `c` are dropped, ADR 0093 clause 10).
* Bad: a SIP root is larger and slower to load than an IEX root over the same span. Its size per hour on
  the busiest names is UNVERIFIED.
* Bad: two quote roots for one instrument, one per feed; a reader names the root it reads.

### Confirmation

* Offline: a failing test first that `paper:history --data quotes` on `sip` writes a root marked `sip`,
  and that it refuses an IEX-marked root.
* On paper: one bounded SIP quote load, GET only, with one account's pair, under its catalog lock.
* The `evidence-verifier` reads this record before it changes status.

## Evidence

Read 2026-11-12.

* https://docs.alpaca.markets/us/docs/orders-at-alpaca, line 1594 of a copy saved 2026-11-12 (sha256
  `18c2cc647066e1b8…`): "Stop orders and trailing stops are elected on the consolidated print. Your sell
  stop order will only elect if there is a trade on the consolidated tape at or lower than your stop price
  and provided the electing trade is not outside of the NBBO." — fetched earlier and read in the copy.
* ADR 0066: IEX only, SIP "observed, not built on", the probe's SIP counts; ADR 0093 clause 10: "The
  root is IEX: a SIP root is refused by name" — read on `main` at `c2592006`.
* https://docs.alpaca.markets/us/docs/market-data-faq: "For historical queries, the `end` parameter must
  be at least 15 minutes old to query SIP data without a subscription" — fetched 2026-11-12 by the second
  reader (sha256 `b6d9225409d2211b…`), line read here.
* https://docs.alpaca.markets/us/reference/stockquotes-1: `end` defaults to "15 minutes before the
  current time" without real-time access for the feed — fetched 2026-11-12 by the second reader
  (sha256 `e9e6e5441d363529…`), line read here.
* https://docs.alpaca.markets/us/docs/paper-trading and
  https://docs.alpaca.markets/us/docs/historical-stock-data-1: the pages still disagree, the paper page
  saying "As an Alpaca Paper Only Account holder, you are only entitled to receive and make use of IEX
  market data" and the historical page calling IEX "the only feed
  that can be used without a subscription" against the FAQ above — fetched 2026-11-12 by the second
  reader, read here.
* ADR 0067: "IEX's best bid and ask are not the NBBO" — read in the tree.
* ADR 0119: the quote half of the breach "is not built: the first gap below" — read in the tree.
* `src/canon_trading/history_load.py` on `main` at `c2592006`: `_check_feed`, the header's "SIP and IEX need
  separate roots", `load_quotes`' IEX-only refusal (`:946-947`); `bars_to_catalog.py:62-67`, one feed per
  root; `mise.toml:766`; `catalog/README.md:115`; `PUBLICATION_DELAY` (15 minutes, `bars_to_catalog.py:117`)
  — read in the tree.
* Second reader (evidence-verifier, 2026-11-12): 6 held, 5 half right, 1 unlabelled, none failed, and
  ADR 0093 amended unnamed; its ten corrections are made above.
* https://docs.alpaca.markets/us/reference/stockquotes-1: `feed` `sip` is "all US exchanges", no NBBO
  named — fetched 2026-11-13.
* https://forum.alpaca.markets/t/14055, an Alpaca staff reply of 2024-04-17: "Historical quotes older than
  15 minutes are full market NBBO quotes on either plan." — fetched 2026-11-13.
* https://forum.alpaca.markets/t/19161, an Alpaca staff reply of 2026-06-25: "The SIP's continue to
  publish quotes after 4:00 PM ET, but they are not technically NBBO quotes", and "NBBO quotes are only
  defined during regular market hours 9:30-16:00 ET" — fetched 2026-11-13.
* The bound above is the builder's, relayed 2026-11-13 from its change in review, which cites this
  record; its reader's NBBO finding is the three lines above — `UNVERIFIED` until that change lands.
* Second reader (evidence-verifier, 2026-11-13, on the record renumbered from 0131 and rebased onto
  `main`): every code and record citation held but one moved line (`mise.toml:766`); the Alpaca pages
  were fetched again and hold, the paper page now quoted; the print trigger's scope added.
