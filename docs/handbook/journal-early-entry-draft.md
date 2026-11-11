# Early entry on the tape of 2026-11-11: CENN first (draft journal entries)

A draft: these entries move into the lessons journal, with their ids, once the handbook is on `main`.
Every figure here is a DIAGNOSTIC under ADR 0092: it is counted and labelled, and it changes no row.

**How it was made.** `python -m canon_trading.early_entry_study --catalog <a sip root> --day 2026-11-11
--symbols CENN,YMT,NXXT,SCKT,NCT`, at commit `7cfde3b`, on Alpaca's `sip` one-minute bars loaded
through the adapter: today from 04:00 to 13:22 New York and the 20 sessions before it. The rows are the
ones registered before the tape was read (ADR 0083's starting rows and the strategy's defaults): listed
at a move of 3% from the last regular close with dollar volume since 04:00 at 3 times its mean over the
five earlier sessions; entered on three closes in a row each above the one before; sold by a trigger 2%
of the entry under the highest bid. No number was chosen after seeing the tape.

**What it cannot see.** The adapter serves no historical trades, so all timing is from minute bars,
which exist only for minutes with a trade. No spread was given, so every outcome is gross. The reference
close is the last regular minute bar of 2026-11-10, because a session's daily bar loads a day later.
The bars stop at 13:22, so "the high" is the high so far. Only a name's first entry is replayed. Prices
are raw: a split between the reference close and the day is not checked, so a move is as the bars give it.

## CENN

| | |
|---|---|
| Reference close | 0.1200 (the 15:59 bar of 2026-11-10) |
| High so far | 0.4300 at 13:02, +258.3% |
| Last close, 13:22 | 0.2667, +122.3% |
| Listing condition first met | **13:01**, one minute before the high |
| First-rising-run entry | **none**: from 13:01 to 13:22 no three closes rose in a row while it was listed |
| Protection | none armed: there was no position |
| Against holding flat | 0: no trade |
| Against holding to the end | no entry to hold |
| A window watching from 13:18 | began 16 minutes after the high, with the price 46% of the way from the reference to it |

Dollar volume by 15-minute segment, against the mean of the same segment in its five earlier sessions:

| segment | dollars | mean of 5 sessions | ratio | cumulative ratio since 04:00 |
|---|---|---|---|---|
| 04:00 | 5,477 | 184,048 | 0.0 | 0.0 |
| 04:15 | 1,274 | 38,250 | 0.0 | 0.0 |
| 04:30 | 842 | 22,836 | 0.0 | 0.0 |
| 04:45 | 2,012 | 9,280 | 0.2 | 0.0 |
| 05:00 | 2,376 | 36,301 | 0.1 | 0.0 |
| 05:15 | 26 | 453,632 | 0.0 | 0.0 |
| 05:30 | 690 | 78,854 | 0.0 | 0.0 |
| 05:45 | 323 | 208,002 | 0.0 | 0.0 |
| 06:00 | 367 | 130,988 | 0.0 | 0.0 |
| 06:15 | 701 | 41,893 | 0.0 | 0.0 |
| 06:30 | 607 | 42,732 | 0.0 | 0.0 |
| 06:45 | 694 | 27,650 | 0.0 | 0.0 |
| 07:00 | 1,111 | 41,579 | 0.0 | 0.0 |
| 07:15 | 8,626 | 46,348 | 0.2 | 0.0 |
| 07:30 | 3,545 | 39,859 | 0.1 | 0.0 |
| 07:45 | 8,437 | 30,627 | 0.3 | 0.0 |
| 08:00 | 3,876 | 65,380 | 0.1 | 0.0 |
| 08:15 | 894 | 54,584 | 0.0 | 0.0 |
| 08:30 | 2,072 | 33,949 | 0.1 | 0.0 |
| 08:45 | 928 | 52,593 | 0.0 | 0.0 |
| 09:00 | 1,001 | 40,977 | 0.0 | 0.0 |
| 09:15 | 1,472 | 29,203 | 0.1 | 0.0 |
| 09:30 | 5,921 | 284,572 | 0.0 | 0.0 |
| 09:45 | 19,889 | 132,293 | 0.2 | 0.0 |
| 10:00 | 67,635 | 82,768 | 0.8 | 0.1 |
| 10:15 | 7,456 | 139,076 | 0.1 | 0.1 |
| 10:30 | 7,836 | 68,393 | 0.1 | 0.1 |
| 10:45 | 9,748 | 55,405 | 0.2 | 0.1 |
| 11:00 | 7,515 | 53,777 | 0.1 | 0.1 |
| 11:15 | 2,960 | 27,882 | 0.1 | 0.1 |
| 11:30 | 48,565 | 36,069 | 1.3 | 0.1 |
| 11:45 | 239,956 | 30,901 | **7.8** | 0.2 |
| 12:00 | 52,409 | 20,625 | 2.5 | 0.2 |
| 12:15 | 140,978 | 28,890 | 4.9 | 0.2 |
| 12:30 | 1,048,681 | 20,387 | **51.4** | 0.6 |
| 12:45 | 6,011,829 | 23,622 | **254.5** | 2.8 |
| 13:00 | 15,944,873 | 11,411 | 1,397.3 | 8.7 |

The 13:15 segment is left out: the bars cover seven of its minutes, and the run that printed this
table set them against a full segment's mean (since corrected in the code).

**Reading.** The segment's own ratio passed 3 in the 11:45 segment and 50 in the 12:30 segment. The
listing's measure, volume since 04:00 over its normal, passed 3 only at 13:01. CENN's five earlier
sessions were heavy before and at the open (the 04:00, 05:15 and 09:30 segments average $184,000,
$454,000 and $285,000), and today's morning was quiet, so the day began far behind its own normal and had
to make up a whole morning before the ratio could say anything. By then the move was at its high. The
entry rule then needs three rising closes after the listing, and the tape after 13:02 was falling.

### L (id assigned when it joins the journal): CENN was listed one minute before its high, because volume since 04:00 hid a move that began at midday

* area: ranking
* session: 2026-11-11
* result: One name, bars to 13:22. Listing condition first met 13:01; high 0.4300 (+258.3%) at 13:02; the 15-minute segment ratio was 7.8 at 11:45, 51.4 at 12:30 and 254.5 at 12:45 while the ratio since 04:00 stood at 0.2, 0.6 and 2.8; no entry; gross result 0 against flat
* evidence: `canon_trading.early_entry_study` at commit `7cfde3b` on `sip` minute bars in the local catalog; the command is at the top of docs/handbook/journal-early-entry-draft.md and its full output is kept outside the repository
* lesson: A volume ratio counted from 04:00 answers "is the whole day unusual", not "is it unusual now". A name with heavy earlier mornings, as a recent mover has, starts each day in deficit and is listed late or after its move; a ratio over a recent stretch of minutes would have spoken about 75 minutes earlier here
* change: none. A diagnostic changes no row (ADR 0092). The ranking's volume measure is the ranking record's to decide
* next: Replay the same day under ADR 0092's arms, and put a ratio over recent minutes to the ranking record as a row to register; measure on whole-market days how many names that did not move it would also list
* status: open
* supersedes: none
* source: lead

## The other four names

Bars to 13:22; gross; registered rows. "Done" is the share of the move from the reference to the high
so far that was behind the entry.

| Name | Reference | High so far | Listed | Entry | Done at entry | Sold | Managed | Held to 13:22 |
|---|---|---|---|---|---|---|---|---|
| YMT | 0.2611 | 1.4500 at 12:26, +455.3% | 11:13 | 0.5200 at 11:40, 27 min after listing | 21.8% | 0.7265 at 11:46 | +39.7% | −32.5% |
| NXXT | 0.1867 | 3.0500 at 12:20, +1,533.6% | 09:28 | 0.5335 at 09:37, 9 min | 12.1% | 0.5893 at 09:38 | +10.5% | +471.7% |
| SCKT | 0.2511 | 0.6684 at 10:09, +166.2% | 04:23 | 0.4207 at 09:54, 331 min | 40.6% | 0.4308 at 09:55 | +2.4% | +2.3% |
| NCT | 0.4200 | 1.1101 at 11:13, +164.3% | 08:59 | 0.4889 at 09:02, 3 min | 10.0% | 0.4791 at 09:03 | −2.0% | +22.4% |

Four of five entered, none of them after half its move; CENN never entered. One lesson a name:

* **YMT**: listed at 11:13 and entered 27 minutes later, with 78% of the move still ahead. The wait was
  for three rising closes on a name with 120 bars all day: a thin tape makes the run rule slow.
* **NXXT**: entered with 88% of a move of fifteen times still ahead and sold one minute later. The first
  entry was early; the 2% trigger gave the rest away, and what a re-entry would have done is not replayed.
* **SCKT**: listed at 04:23 on $273 of trading (a ratio of 3 over a normal of a few dollars), five and
  a half hours before its move began at 09:45. A ratio of two small numbers lists a name that is not
  moving; the listing needs a floor in dollars.
* **NCT**: entered at 09:02 with 90% of the move ahead, and was sold at the first trigger a minute
  later, 2% down. The high came two hours after.
* **All four exits** fired within a minute of the entry (six for YMT). A 2% trigger is inside one
  minute's range on these names, and a minute bar does not say whether its high or its low came first:
  the bar model takes the high first, as NautilusTrader does by default, so YMT's and NXXT's gains are
  that convention's and not a measurement. The exit needs trades or quotes, which the adapter's history
  does not serve yet.

**Could the 13:18 window have been early?** No. On the bars to 13:22, each of the five had made its high
so far before 13:18 (SCKT 10:09, NCT 11:13, NXXT 12:20, YMT 12:26, CENN 13:02), and each had met the
listing condition between 17 minutes and nine hours before the window began. Replayed from 13:18 the
rule enters none of them in the four minutes of bars there are.

### L (id assigned when it joins the journal): On minute bars a 2% trigger sells in the minute after the entry, and the bar cannot say at what price

* area: exits
* session: 2026-11-11
* result: Four entries on four names (YMT, NXXT, SCKT, NCT), bars to 13:22, gross: three sold in the next minute and one in six; managed +39.7%, +10.5%, +2.4% and −2.0% against holding to 13:22 at −32.5%, +471.7%, +2.3% and +22.4%
* evidence: `canon_trading.early_entry_study` at commit `7cfde3b`; the table above; the bar walk is NautilusTrader's docs/concepts/backtesting/bar-execution.md at the pin
* lesson: On names whose one-minute range is wider than the trigger's distance, the exit's price depends on whether the minute's high or low came first, which a bar does not record. The managed outcome on bars is not evidence for or against the trigger; the entry's timing is
* change: none. A diagnostic changes no row (ADR 0092)
* next: Measure the exit on the paper window's own `sip` quotes and trades; add historical trades to the adapter's data client; report each managed outcome under both orders of high and low until then
* status: open
* supersedes: none
* source: lead
