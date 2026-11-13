# The lessons journal

What the work has taught, one entry per lesson, append-only: an entry is never edited or removed, and a
correction is a new entry that names the one it supersedes. The requirement is the owner's of
2026-11-11 ([philosophy.md](philosophy.md), "How we learn"); `tests/test_handbook.py` checks each entry's
form and that the entries already on `main` are unchanged.

## The learn step

A change that taught something lands with its lesson: a new entry here, or a line on a handbook page,
in the same pull request. A practice that is stopped is deleted where it is stated, not left as a
fallback, and the entry that stopped it says so. Landing itself is ADR 0014's and its review ADR 0017's.

## The session-end reflection

Every session ends with a reflection, written as one entry here with the source `reflection`. Its
`result` gives, for each rule that acted, what it did against holding flat over the session, with the
count of exits behind it; and the movers missed, from the recorder's ranking against the day's final
gainers. Each exit's counterfactuals (held to now, matched holding flat, the managed policy, the managed
policy with re-entry, and each shadow arm, with counts and costs at one times and stressed) are its
`evidence`. The loop that writes it is not yet built (roadmap step 6).

## An entry's form

A heading `### L<four digits>: <subject>`, the ids in order from `L0001` with none skipped, then these
ten fields as list lines, in this order, each with a value:

```text
### L0001: <the subject, one line>

* area: <ranking | entries | exits | sizing | execution | data | risk | strategies>
* session: <the session's date, YYYY-MM-DD>
* result: <what happened, with its sample size>
* evidence: <a path inside the repository, or a record named that is kept outside it; never a private path>
* lesson: <what it taught>
* change: <the change it led to and its reference (a record, a pull request), or none>
* next: <what comes next>
* status: <open | confirmed | refuted | adopted | superseded>
* supersedes: <the id of an earlier entry, or none>
* source: <reflection | nightly | review | lead | owner>
```

## Entries

<!-- append-only: add new entries below this line; never edit or remove one above it -->

### L0001: Five sessions is the least that keeps a best-of-eight false pass under three per cent

* area: strategies
* session: 2026-11-11
* result: The share of false passes at 0.95 when the best of 8 variants with no edge is picked on a replay of T sessions, over 40,000 rounds a length: 8.695, 4.525, 2.673 and 0.643 per cent at T = 3, 4, 5 and 10 sessions; at T = 2 the computation stops
* evidence: docs/adr/0089-the-nightly-study-loop.md (Confirmation: measured through `false_passes` in tests/test_nightly_study.py at 40,000 rounds a length; a test there holds the same measurement at 2,000 rounds); the row is `MIN_SESSIONS` in docs/handbook/rows.md
* lesson: A floor of 5 sessions on a replay is where a variant picked from eight with no edge passes the gate by chance less often than the nominal 5 per cent; fewer sessions let it pass far more often. It is the nightly study's floor on a replay's sessions, not ADR 0083's floor for merit on paper, which pairs 5 sessions with 30 round trips
* change: `MIN_SESSIONS` = 5 seeded in docs/handbook/rows.md; ADR 0089 holds the measurement and keeps the row UNCALIBRATED
* next: A new entry confirms or refutes this one once a study with real variants has run at five sessions and more
* status: open
* supersedes: none
* source: lead

### L0002: On a wide overnight quote the spread floor, not the ATR, set the default arm's distance

* area: exits
* session: 2026-11-13
* result: One paper window in the overnight part, arm OWN1 (ATR, k 1, cap own), from 97011e71: one entry and one sale in CBRS, flat at the end, no error and nothing left. The distance was the spread floor, two quoted spreads of 0.27 taken onto the step as 0.55 a share, over a one-minute ATR of 0.216; the ATR warmed from 14 requested bars and 1 cached bar; the entry met neither the cross-dollar case nor a cold opening. One round trip
* evidence: window record window-20261113T070811Z, kept outside the repository; the distance rule at src/canon_trading/mover.py:4321-4327 at efd37edb
* lesson: Where the quote is wide the floor sets the distance and the ATR does not reach it; one round trip shows the path ran and says nothing of merit
* change: none
* next: Count, over the next windows, how often the floor binds over the ATR, by part of the session
* status: open
* supersedes: none
* source: lead

### L0003: Two openings can sit under the spread floor on main

* area: exits
* session: 2026-11-13
* result: A review of pull request 338 found two P1 findings that the lead read true: where the stop crosses one dollar, the distance rounded down onto the entry's step can fall under two quoted spreads with no refusal; and an opening entry with a cold ATR takes the fixed 0.02 and only records the floor. Whether any of tonight's entries met either case is not counted here: UNVERIFIED
* evidence: the Antigravity reader's report on pull request 338, kept outside the repository; src/canon_trading/mover.py:3967-3973 and :4245-4258 at efd37edb
* lesson: A bound stated once is skipped by a rounding made after it and by a fallback branch taken before it; check it at the value that is sent
* change: none on main; the fix is in the A3+O unit, the cross-dollar case with a failing test first; the cold-opening case's test passed on that unit's tree before any fix
* next: A new entry closes this one when A3+O is on main
* status: open
* supersedes: none
* source: review

### L0004: A window that enters nothing leaves its path unproven on paper

* area: execution
* session: 2026-11-13
* result: Four proof windows tonight ended flat and clean with nothing entered: F-1's first proof, A-2/A-3 and the held stop with 0 orders, and F-2's proof with 21 of 23 slots warm and 0 entries. Of the windows that entered, one did 8 times (distance from the ATR 3 times, the floor once, the cold fallback 4 times), another twice, F-1's second run 3 times on the cold fallback (that source marked UNVERIFIED at the time), and the window of L0002 once
* evidence: the deputy's log of 2026-11-13, kept outside the repository, which names each window's record
* lesson: A flat, clean end is not evidence that a path ran; the path's own events have to be counted, and a unit proven by an empty window lands with that gap stated
* change: those units land with the gap stated in their pull requests (the lead's ruling of 2026-11-13, 01:52)
* next: A window that reaches each such path on paper, and a new entry for each
* status: open
* supersedes: none
* source: lead

### L0005: Readers after the required review kept finding P1s on the trading path

* area: execution
* session: 2026-11-13
* result: Additional readers found P1 findings in many units tonight, among them a custody check whose final read was discarded, reading a false flat, and a raise before our own orders were cancelled; a held stop lost when a native trail resumed; a soft line's recent count and a re-entry's trade-date rollover; and the two of L0003; others were in batch A, the successor, the 09:30 branch and two more readers' units. Each went to its lane as a claim to reproduce; one was reproduced as a state defect the window cannot reach, and was put after 09:30
* evidence: the readers' reports of 2026-11-13, kept outside the repository, and the deputy's log of that night
* lesson: A finding is a claim until a failing test shows it; the count of these findings says what was found tonight, not what a second reader is worth
* change: each finding went to its lane, in its one fix round or a later unit
* next: A new entry with the count of findings reproduced, fixed and declined once those rounds land
* status: open
* supersedes: none
* source: review

### L0006: An entry's cap refused a stop for a long that was already held

* area: exits
* session: 2026-11-13
* result: Under an own-cap row the protect task twice refused to plan a stop for a long it found in the account, once when the ATR distance and once when the spread floor came out over the 0.02 entry cap; two firings earlier the same holding's distance was under the cap and it got its stop, so a volatile name had a stop or none depending on the minute
* evidence: ADR 0142's amendment of 2026-11-13; tests/test_protect.py, the cases a found long keeps its own distance over the entry cap and an own distance over the ceiling is named and sends no stop
* lesson: A bound written for choosing an entry's size does not apply to a holding that has no size left to choose; a protection path takes the bound of what it protects, here the sizing ceiling, and refuses past it by name
* change: a found holding under an own cap keeps its distance up to the 0.10 sizing ceiling and is refused over it, with the Mover's entry path unchanged (ADR 0142)
* next: The paper run of this path on account A at the next regular session, and a standing set of protection cases every protection unit passes
* status: open
* supersedes: none
* source: lead

### L0007: A name that ranked in after the start was passed over for want of its entry context

* area: entries
* session: 2026-11-13
* result: Under the registered arm the Mover read entry contexts only for the startup roster, so every name that ranked into a slot after the node started was dropped as having no entry context supplied; in the simulation a late name with a rising tape made no entry before this change and one entry, protected by its trailing sell, after it
* evidence: tests/test_mover.py, the cases a name loaded on demand gets an entry context and enters, is protected after its fill like a roster name, and an unreadable on-demand history names the failure and sends no order; tests/test_mover_on_demand_context.py
* lesson: A late-ranked name passed over for lack of context is a lost entry; its context is read on demand within a bounded request budget, and a name whose context cannot be read is named and passed over
* change: the Mover reads a late name's context on demand on one worker, at most 20 reads and 150 data requests a minute of the context client's own (not a limiter shared with the window's other data reads), joins it with the minutes the node already holds, retries a failed read after 60 s and holds a missing day until the next, in this pull request
* next: The first registered-arm window after 04:00 New York on the next weekday reports the late name's entry or its named refusal
* status: open
* supersedes: none
* source: review

### L0008: A submit Alpaca never answered was rejected and its later fill booked as another's

* area: execution
* session: 2026-11-13
* result: Under the engine's default an unanswered submit was rejected on its in-flight timeout, so a fill Alpaca made on it later was booked to an external order and the strategy could send again; the four required cases were red with the port's source and green after it, and two paper windows on account B ran with the change ending flat
* evidence: ADR 0044 and ADR 0075; packages/canon-alpaca/tests/test_execution_client.py, the cases an order that cannot be read is withheld and read again, and a retained filled submit keeps reading past the fill retry budget
* lesson: An order the venue never answered is not known to have failed; it is retained, its fill is named by the client order ID the venue's read of the order shows, and it is asked about again until it is named or the node stops
* change: the paper nodes retain an unresolved submit and name its fill by the client order ID Alpaca echoes (pull request 404)
* next: A Mover window on account B as the forward check; the lost-submit path itself is shown offline only
* status: open
* supersedes: none
* source: review
