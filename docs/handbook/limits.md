# Named limits

The cases the trading path knowingly does not handle, one line each, kept in one place and judged
together. The rule behind the page is the lead's of 2026-11-13, set as the trailing stop closes:

- A reader's finding opens a new unit on the Mover's stop path only when two things hold. The owning
  lane has reproduced it, and it gives a wrong order or a missed sale under `main`'s default settings.
  Such a finding goes to the lead.
- Every other finding is a line here, not a unit of its own.
- The list is judged once a week, as a whole. A line is kept, removed because a later change answered
  it, or taken up as a unit; it is not landed one line at a time.

A line names the case once, the reader that found it, the test that reproduces it as a pytest node id
in this repository (`<file>.py::<test>`, with a class or one parametrized case where it has one) or
`none`, and the date it was received. `tests/test_handbook.py` checks that form, and that a named test
is defined in its file.

| Case | Reader | Reproducing test | Received |
|---|---|---|---|
| Slots: `main` refuses `--slots` over seven (`mover_window.part_slots`), and the measured request bound that #303 proposed is not on `main`. #303 is parked with its branch kept on `origin`, and more than seven slots would come back as a fresh small unit from `main`. | the lead's order of 2026-11-12 | none | 2026-11-13 |
| With `validate_trade_sequence` on (off by default; no task passes it), the adapter measures how far a print's time runs ahead against receipt on this host (`canon_alpaca/data.py`, `STAMP_AHEAD_MOST_NS`), while the Mover measures it against the name's quote reference (`mover.py`, `PRINT_AHEAD_MOST_NS` on `Slot.quote_lag`). With the host's clock ahead, a print the Mover would not use can become the check's newest and set aside the timely prints behind it, so under the default trigger a held stop or a sale with no bid reads an older price. ADR 0134 and the adapter's README name it. | the wiring's pre-landing reader (P1) and `/code-review` at high effort of the first wiring commit | none | 2026-11-13 |
