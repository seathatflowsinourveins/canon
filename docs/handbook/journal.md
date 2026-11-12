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
