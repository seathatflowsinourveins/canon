---
status: accepted
date: 2026-11-12
decision-makers: lead orchestrator (Claude) for the rule, the five plan-sourced codes and the window record; the adapter's building session (Claude) for the form
review-by: 2026-12-12
---

# A trade print becomes a tick only if Alpaca's minute bars take its price, behind the adapter's `trade_condition_filter`, off by default

## Context and Problem Statement

The adapter turned every Alpaca trade print into a tick and dropped its condition codes, so an odd lot,
an average-price print or an out-of-sequence print could elect a stop or lift a trail's peak. A strategy
that elects on prints needs to know which prints carry a price. Account C and live run no print-elected
protector until this filter is in their tree and on.

## Decision Drivers

* The rule should be one a venue documents, not one invented here (AGENTS.md rule 3).
* Movers trade 24/5, so a rule must leave prints outside the regular session.
* The ranking reads Alpaca's bars, so ticks and bars should see the same prints.
* A code nobody has judged must not pass or be dropped silently.

## Considered Options

1. **Alpaca's minute-bar rule**: a print becomes a tick if and only if Alpaca's own minute bars take its
   price.
2. **The consolidated tapes' last-sale rule**: a print becomes a tick if the plan's "update last" column
   says so (CTS for tapes A and B, UTP for tape C).
3. Option 2 in the regular session and option 1 outside it.
4. No filter (today).

## Decision Outcome

Chosen: **option 1** (the lead, 2026-11-12), as `AlpacaDataClientConfig(trade_condition_filter=True)`,
off by default, turned on for a strategy that elects on prints.

* **Where the sources disagree.** Both plans say `T` (extended-hours trade on A and B, Form T on C) does
  not update the last sale. Alpaca's own row for `T` (FAQ line 302) is green for the minute bar, the
  daily bar and updated bars alike. UTP's Note 1 (p.51) applies Form T to trades from 12:00 a.m. to
  9:30 a.m. and from 4:00:01 p.m. to 8:00 p.m. So under option 2 a print in those hours does not update
  the last sale, and a 24/5 strategy's print-elected stop has nothing to fire on. No measured tape here
  holds a `T` print, so how many prints outside the regular session carry it is UNVERIFIED. Option 3
  adds session logic for no gain over option 1. `T` is kept in every part of the session.
* **Where the plans and Alpaca agree, and where they do not.** On odd lots and average-price prints,
  the plans and Alpaca agree. They differ on these codes, and option 1 follows Alpaca on each:
  * `Z` (sold out of sequence): conditional in both plans; Alpaca's minute bar is red (FAQ line 309).
  * `9` (corrected consolidated close): the plans update the last sale; Alpaca's minute bar is red
    (line 316).
  * `L` (sold last): conditional in the plans; Alpaca's minute bar is green (line 294).
  * `P`, `Z`, `4` and `G`: UTP's footnote 1 (p.49) lets the day's "first or only" last sale update the
    last price, while CTS note #2 says only "if it is the only qualifying last". Alpaca's minute bars
    never take their price.
* **The table.** `packages/canon-alpaca/src/canon_alpaca/trade_conditions.py` holds one rule per code
  per tape, each citing its FAQ line. The codes are exactly those Alpaca's per-tape map returned on
  2026-11-12: 26 on tapes A and B, 33 on tape C. A print takes the price when every code it carries does,
  the FAQ's "strictest rule".
* **Five codes are plan-sourced.** The FAQ has no row for them:
  * `8` on tapes A and B, from CTS p.82: no tick.
  * `1` and `S` on tape C, from UTP p.48: they take the price. Whether Alpaca's bars take them is
    unknown.
  * `8` and `E` on tape C, from UTP p.48 (TBD there): no tick.

  If Alpaca's FAQ later gains a row for one of them, the FAQ row wins.
* **Unknown codes.** Before the first subscription, the connect reads the per-tape map (three GETs) and
  refuses to go on if the map shows a code the table does not know, naming it. The node then never
  starts its strategies. A print with an unknown
  code, an unknown tape or no conditions becomes no tick and is counted as `unknown:<what>`.
* **SIP only.** The table and its measurement are on SIP prints, so with the filter on a trade
  subscription or request on any other feed is refused. So is a trade request with `update_catalog`,
  whose filtered ticks would go into the catalog under the unfiltered trades key.
* **Counts.** Every judged print is counted, as `kept` once its tick is made or by each excluding code,
  apart for the stream and for requests. A left-out print uses none of a request's limit. With a filter
  on, the window record carries the counts at its end as `mover.trade_conditions`, so each class's real
  frequency is on file from the first window. With every filter off, the field is absent.
* **Measured cost.** These are the 2026-11-12 tapes, fetched with conditions on account A. The requests
  made were three one-page tape reads and three map reads at 18:52Z, and one more map read at 19:07Z.
  All prints were on tape C, every code was known, and none lacked conditions. The table is the lead's
  accepted measurement:

| Span (UTC) | Prints/s before → after | Kept | Excluded | Gap between ticks, median and max |
|---|---|---|---|---|
| TSLA 15:50:50–15:51:25 | 58.0 → 10.0 | 17.2% | I 1678, 4 35, W 2 | 0 → 5 ms; 0.31 → 1.97 s |
| TSLA 16:12:05–16:12:40 | 49.0 → 4.3 | 8.9% | I 1561, 4 12, W 1 | 0 → 50 ms; 0.25 → 1.76 s |
| ADXN 17:48:00–18:16:00 | 1.1 → 0.43 | 40.5% | I 1067, 4 1 | 7 → 9 ms; 17.7 → 47.6 s |

  Odd lots were 83 to 91% of TSLA's prints and 60% of ADXN's in these three spans. A print stop on a thin
  name can wait tens of seconds longer for its next tick.

  The cost at a level, on TSLA's first span: with a stop level of 388.72, the first print at or under it
  was an odd lot (`@ I`, 388.7169) at 15:50:50.387Z, and the first print the filter keeps at or under it
  (`@`, 388.691) came at 15:50:54.961Z, 4.6 s later. Whether odd lots stay excluded is to be decided on a
  wider table of such delays, not on this one. Every span fell between 10:50 and 13:16 New
  York, so the cost of keeping `T` on a 24/5 name, outside the regular session, is not measured.

  Tapes A and B were checked against real prints too: one page each of KO (tape A, 55 prints) and SPY
  (tape B, 97 prints), 18:30Z, three map reads and two pages. Every code was known. The filter kept 27
  of KO's prints and 43 of SPY's. The rest carried `I`, `4`, `B` or `F` with `I`; a lone `F`
  (intermarket sweep) was kept. The pages are outside the repository (sha256 `f91f4308…`, `eba78cbb…`).

### Consequences

* Good: a print-elected stop fires on the same prints Alpaca's minute bars price, by a documented rule,
  and every left-out print is counted by its code.
* Good: the setting is off by default, so no current path changes.
* Bad: on a thin name the gap between ticks grows. ADXN's longest gap went from 18 s to 48 s.
* Bad: the connect makes three more GETs when the filter is on.
* Limit: corrections (`T` "c") and cancels (`T` "x") on the stream are logged as before, not applied to
  ticks already sent.
* Limit: `paper:history --data trades` writes trade ticks to the catalog without the filter, and the
  catalog keeps no conditions.
* Limit: the odd-lot rule is moving under the SEC's 2024 amendments, and Alpaca's table can change. Hence
  the 30-day review.

### Confirmation

* `packages/canon-alpaca/tests/test_trade_conditions.py`: the table knows exactly the map's codes, each
  rule cites its FAQ line or plan row, and one test covers each class (regular, odd lot, average price,
  out of sequence, `T`, the plan-sourced five, unknown).
* `packages/canon-alpaca/tests/test_data_client.py`:
  * the setting is named and off by default;
  * with it on, only kept prints reach a strategy, `T` among them;
  * the counts by code and source, `kept` only once a tick is made;
  * the connect refuses a code the map shows that the table does not know, and in a node with an
    execution client the strategy never starts and nothing is subscribed;
  * a trade request keeps only the prints the bars take, and a left-out print uses none of its limit;
  * a trade subscription or request off `sip`, or a request with `update_catalog`, is refused;
  * with it off, no map is read.
* `tests/test_mover_window.py`: the counts are in the window record with a filter on, and absent with
  none. `tests/test_trade_conditions.py`
  covers `paper:history <account> conditions`, the GET of the map and of raw prints with their
  conditions.

## Evidence

Read 2026-11-12. Each was fetched to disk with curl, the PDFs read as text through macOS PDFKit, not
through a summariser.

* https://docs.alpaca.markets/us/docs/market-data-faq.md (updatedAt 2026-09-21), sha256
  `b6d9225409d2211b60cfeed72e8716fc1e05f2d7593ffed6dc120a0b5ebaef13`, lines 255-339:
  * the per-code, per-tape, per-bar-type table (lines 278-317);
  * "the strictest rule is applied";
  * "The rules are based on the guidelines of the SIPs"

  Fetched.
* https://www.ctaplan.com/publicdocs/ctaplan/CTS_Pillar_Output_Specification.pdf (v2.11b, January 29, 2026),
  sha256 `44609348e4c67ca58d8dd469389e74bc5d399f096dd56cc194816f9108624a72`, pp. 82-83: the consolidated
  LAST column, `T Extended Hours Trade NO`, `8 Reserved NO`, notes #2-#4 (#2: "YES, if it is the only
  qualifying last"), and "the 'NO' takes precedence". Fetched.
* https://utpplan.com/DOC/UtpBinaryOutputSpec.pdf (UTP Data Feed Services Specification 4.1, September 2026),
  sha256 `68659d2f3ab4057adfad52cc20fb1c51c5b69481865d365014cec237a5399401`, pp. 47-49: Update Last `T Form T
  No`, `1` and `S` Yes/Yes, `8` and `E` TBD, footnote 1 ("the first or only last sale"); p.51, Note 1:
  Form T for trades "between 12:00 a.m. and 9:30 a.m." and "between 4:00:01 p.m. and 8:00 p.m.". Fetched.
* https://docs.alpaca.markets/us/reference/stocklatesttrades-1.md, sha256
  `b3020aeafaa20e5b74b63500ca598a49164b17578e4d2e46f7224c8c71399bcc`, line 12: "Trades with any conditions
  that causes them to not update the bar price are excluded", the same rule on Alpaca's latest-trades
  endpoint. Fetched.
* https://docs.alpaca.markets/us/reference/stockmetaconditions-1.md — `GET /v2/stocks/meta/conditions/{ticktype}`
  with `tape` — fetched. The map it returned on account A at 2026-11-12T18:52:56Z
  (3 GETs) is the table's code list: kept outside the repository, sha256
  `b88d6228d72893fc37321caf9b33abdeb01c7c95bbef5465095caf574d672d0e`.
* https://docs.alpaca.markets/us/reference/stocktrades-1.md — `GET /v2/stocks/trades`, each print's `c` and
  `z` — read through alpaca-py's own use of the path (`alpaca/data/historical/stock.py:132`) and
  `TRADE_MAPPING` (`alpaca/data/mappings.py:26-34`) at `c42add7c0a70`. The measured tapes are kept outside
  the repository (sha256 `54e9d54b…`, `0380a0c7…`, `5ca8c47f…`).
* alpaca-py at `c42add7c0a70`: no call for stock conditions; `RESTClient.get` (`alpaca/common/rest.py:215-228`)
  as `get_option_exchange_codes` uses it (`alpaca/data/historical/option.py:99-113`); `Trade.conditions`
  and `Trade.tape` (`alpaca/data/models/trades.py`) — resolved.
* SEC amendments to odd-lot information (Release 34-101070, 2024-09-18) — UNVERIFIED, from a summarised
  search; cited only as the reason for the short review.
