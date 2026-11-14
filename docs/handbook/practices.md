# Practices

One file per practice, under `practices/`, in the folder of its move. Each file holds one line that names
the record here that decides it, or says "not yet built" with the roadmap step that will build it. The
requirement behind them is [philosophy.md](philosophy.md); every number is a row of [the rows
table](rows.md). `tests/test_handbook.py` checks that each fragment holds one line, that the line names a
record that exists or says "not yet built" with its step, and that this page lists none of them. ADR 0087,
0088, 0089, 0091, 0092, 0096, 0100, 0101, 0103, 0106, 0107 and 0110 are `proposed` as this page is
written: what a line cites from them is built and on `main`, and open until the record is accepted.

The form follows two upstream change-fragment practices, read on 2026-11-13: towncrier's news fragments
(<https://towncrier.readthedocs.io/en/stable/tutorial.html>), one file per change in a fragments directory
named by an identifier and a type, and changesets
(<https://github.com/changesets/changesets/blob/main/docs/guide/getting-started.md>), one Markdown file per
change in `.changeset/`. A practice is a standing rule, not a release note, so no build step collects the
files into this page: a collected file would be the shared append point again.

Adding a practice is adding one new file in the move's folder, and nothing here. Its name is a place and
a slug: three digits, a hyphen, then its first words in lower case, as `045-a-new-practice.md`. The
places are spaced by ten, so a new practice takes any free number between the two it reads between, and
nothing is renumbered; two units that pick the same number still write two different files, which read
in the order of their slugs. A file's move is its folder; within a move, files read in the order their
names sort. No unit's addition touches another unit's file.

## Moves

- [Two gates, kept apart](practices/two-gates-kept-apart/)
- [Rank](practices/rank/)
- [Enter](practices/enter/)
- [Protect](practices/protect/)
- [Re-enter](practices/re-enter/)
- [Custody and budgets](practices/custody-and-budgets/)
- [Sessions](practices/sessions/)
- [Evidence and learning](practices/evidence-and-learning/)

## Pages

- [The simulation practice](simulation.md): how a run on NautilusTrader's engine and simulated venue is
  made, the venue's profile, and what a simulation shows and does not show.
