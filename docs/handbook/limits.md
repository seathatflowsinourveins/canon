# Named limits

The cases the trading path knowingly does not handle, one line each, kept in one place and judged
together. The rule behind the page is the lead's of 2026-11-13, set as the trailing stop closes:

- A reader's finding opens a new unit on the Mover's stop path only when two things hold. The owning
  lane has reproduced it, and it gives a wrong order or a missed sale under `main`'s default settings.
  Such a finding goes to the lead.
- Every other finding is a line here, not a unit of its own.
- The list is judged once a week, as a whole. A line is kept, removed because a later change answered
  it, or taken up as a unit; it is not landed one line at a time.

A line names the case, the reader that found it, the test that reproduces it as
`tests/<file>.py::<function>` or `none`, and the date it was received. `tests/test_handbook.py` checks that
form, and that a named test exists.

| Case | Reader | Reproducing test | Received |
|---|---|---|---|
