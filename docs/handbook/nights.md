# The nightly study

One dated entry per night the study runs, append-only: an entry is never edited or removed. The nightly
replay runs when markets are closed and writes evidence and proposals only; adopting a proposal is a
separate reviewed change that passes ADR 0083's adoption test ([rows.md](rows.md)). A lesson it
finds goes into [the journal](journal.md) with the source `nightly`, and its entry here names it. The
study is `canon_trading.nightly_study` (ADR 0089): it writes the night's entry beside the ledger, and a
session lands it here. No night has been landed yet. `tests/test_handbook.py` checks each entry's form and
that the entries already on `main` are unchanged.

## An entry's form

A heading with the night's date, the dates strictly increasing, then these six fields as list lines, in
this order, each with a value:

```text
### YYYY-MM-DD

* sessions: <the sessions replayed, by date>
* data: <what was replayed, its feed and its identity in the catalog>
* ran: <the command, its exit code and where its log is kept>
* result: <what each rule did against holding flat and the incumbent, with sample sizes>
* proposals: <each proposed change to a row or rule, or none>
* journal: <the journal entries it wrote, by id, or none>
```

## Entries

<!-- append-only: add new entries below this line; never edit or remove one above it -->
