---
status: accepted
date: 2026-11-13
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-11
---

# The handbook keeps each practice and each row in a file of its own, after towncrier and changesets read as reference

## Context and Problem Statement

Two handbook pages were shared lists:

- `docs/handbook/practices.md` held 56 practice lines under eight moves;
- `docs/handbook/rows.md` held 77 rows in two tables.

Every unit that added or changed a practice or a row edited one of those files. On 2026-11-13, units that
did this landed side by side and conflicted, and had to land one after another. The lead's order of that
day was that neither page stay a shared append point, and that the form follow an upstream practice for
change fragments. Both are from the day's lane messages and are not recorded in this repository
(`UNVERIFIED` here).

## Decision Drivers

* Two units that each add a practice, or a row, change disjoint files, so their merge is clean.
* No practice or row is lost or reworded by the change.
* No new tool: the pages are plain Markdown checked by `tests/test_handbook.py`.

## Considered Options

1. Keep each page as one list, and land the units that touch it one at a time.
2. One file per practice and per row, in a folder per move and per table, and no build step that collects
   them.
3. Option 2 with a build step, as towncrier does, that collects the files into the page.

## Decision Outcome

Chosen: **option 2**.

**The layout.**

* A practice is one file holding its one line, at `docs/handbook/practices/<move>/<place>-<slug>.md`.
* A row is one file holding its one table line, at `docs/handbook/rows/<table>/<place>-<name>.md`.
* `practices.md` and `rows.md` keep their introductions as indexes of the folders.

**What was taken from towncrier and changesets.**

* From both: one file per change, kept in a directory of its own.
* From changesets: a file name chosen so that two units never pick the same one.

**What differs, because a practice or a row is a standing rule and not a release note.**

* There is no build step. towncrier's build removes the fragments and appends them to one news file, and
  a committed collected file would be the shared append point again. So option 3 is rejected.
* There is no issue id, change type or semver field.
* The name is a three-digit place spaced by ten, followed by a slug of the line's first words or the
  row's name. The place sets the reading order, so changesets' random names are not used.
* A new file takes any free place between two others, so no number has to be picked in sequence and
  nothing is renumbered. Two rows at one place still write two different files, because row names are
  unique. Two practices at one place write the same file only when their first words match; no check
  guards that case, and the second unit's merge then conflicts on that one file.

**The registry.** Both upstreams are registered as `reference`, read for this pattern only:

* towncrier, tracked at its release tag;
* changesets, at its CLI's release tag.

Neither is installed or run (`canon/upstreams.json`).

**The journal (2026-11-13, the lead's ruling).** The lessons journal takes the same form. Each entry is
one file, `docs/handbook/journal/<YYYY-MM-DD>-<slug>.md`, whose name is its id and whose date is its
session, so two units writing lessons the same day never pick one counter. `L0001` to `L0009` stay on
`journal.md`, whose append point is closed. Each file adds two fields, `owner` and `closes`: an open
entry names who closes it and its verifiable end state, as Google SRE's postmortem action items ask; a
closed one says `none` in both. A correction supersedes an entry of its own date or earlier, never in a
cycle. Google SRE's workbook is registered as `reference`, read for this practice only.

### Consequences

* Good: a unit adding a practice or a row merges clean beside another that does the same.
* Bad: neither index page shows its practices or values. A reader opens the folder, or the files.
* Bad: a unit that changes the meaning of a whole move still touches several files.

### Confirmation

`tests/test_handbook.py` checks the following:

* each practice file holds one line naming a record or a roadmap step, and each row file holds one table
  line;
* no row name repeats across files;
* neither index page holds a practice or a row;
* no tracked file repeats a practice;
* two practices added under one move, and two rows added to one table at one place, merge clean in a
  scratch repository. The practices case does not test one place.

## Evidence

Read 2026-11-13.

* https://towncrier.readthedocs.io/en/stable/tutorial.html: news fragments live in a directory, one file
  per change, named by an issue id (or another unique id) and a type, with orphans prefixed `+`. A
  non-draft build removes the fragments with `git rm` and appends the notes to the news file. `fetched`.
* https://github.com/changesets/changesets/blob/main/docs/guide/getting-started.md: "A changeset is a
  Markdown file with YAML frontmatter", and `init` sets up the `.changeset` folder. `fetched`.
* https://github.com/changesets/changesets/blob/main/docs/faq.md: file names are "random human readable
  names by default to avoid collisions", and changesets "are designed to stack". `fetched`.
* https://github.com/twisted/towncrier and https://github.com/changesets/changesets: MIT licence, and the
  latest releases are `26.9.0` and `@changesets/cli@3.0.3`, read with `gh api`. `fetched`; the pins are
  `resolved` by `scripts/canon resolve`.
* The 56 practice lines and 77 rows on `main` were compared with the files by a one-off script, and are
  equal and in order. Only two relative links were rewritten, for the deeper folder.
* https://sre.google/workbook/postmortem-culture/, "Concrete Action Items": "All action items have both
  an owner and a tracking number" and "The action items have a verifiable end state"; the page is
  licensed CC BY-NC-ND 4.0. `fetched` 2026-11-13. Its owner and end state are taken; the tracking number
  is the entry's own id.
* The `evidence-verifier` pass of 2026-11-13 found every upstream claim held. It found two claims only
  partly held: two units at one place, and the merge check at one place. Both are reworded above, and the
  day's conflicts are marked `UNVERIFIED`.
