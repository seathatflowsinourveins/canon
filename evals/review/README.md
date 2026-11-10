# Reviewer benchmark

A fixed set of past commits whose defects were later confirmed, one brief for every reviewer, and a
scoring rule. It answers one question coarsely: does a reviewer find the defects we know about? It
cannot rank close reviewers. The design and its sources are in
[ADR 0046](../../docs/adr/0046-reviewer-benchmark-from-confirmed-defects.md).

| Path | Holds |
|---|---|
| `cases/<id>.toml` | One case each |
| `brief.md` | The brief every reviewer gets, word for word |
| `runs/<date>-<label>/` | A run's reports and score sheet, committed by the scoring task (added with the first run) |

`tests/test_review_cases.py` checks every case file; it runs in `scripts/ci`.

## A case

```toml
id = "exec-client-first-slice"      # the file name without .toml
status = "ready"                    # ready: scored; pending: not yet, see `confirm`
commit = "<40-hex sha>"             # the commit to review; the diff is commit^..commit
title = "<its subject line>"
paths = ["packages/canon-alpaca/"]  # where the change is; goes into the brief
clean = true                        # optional: ready with no confirmed defect; then no [[defects]]
confirm = "..."                     # pending only: what would confirm its defects

[[defects]]                         # ready and not clean: one or more
id = "startup-reconciliation-race"
location = "<file>: <function>"
summary = "what is wrong and how it fails"
match = "the least a finding must say to count as this defect"
reported_by = "who first reported it: a reviewer, or how it showed"
fix = "<40-hex sha>"                # a later commit on main that fixes it
test = "<file>::<test name>"        # the regression test the fix adds or changes
```

A defect is confirmed when its fix is on `main` with a regression test whose commit message reports it
failing (or hanging) without the fix. A claim a review made that nothing has confirmed yet is not a
defect here. A known defect the commit itself states as a limit is left out of its case, so a reviewer
who respects a stated limit is not marked down. The reviewer is never shown the fix, the `summary`, the
`match` line or this folder.

The check fails unless: every field above has the right type; `commit` and each `fix` exist; each `fix`
descends from `commit` and is an ancestor of `HEAD` (`main` once the change lands, so a fix only on an
unlanded branch is never cited); each `test` names a file the `fix` commit changes and that defines that
test there; a ready case has defects or `clean = true`, not both; a pending case has `confirm`. That
the test failed before the fix is not checked; it rests on the fix's message.

## Running a benchmark (the coordinator)

Only the coordinator starts reviewers. A *reviewer* is a runtime, model and effort, for example a Claude
worker on `opus` at `high`, or a Codex worker on its defaults. `runs` is the number of runs per
reviewer per case, by default 1.

1. Pick a run label (`<date>-<label>`) and a report folder outside every checkout, for example under the
   scratchpad. Report path: `<folder>/<case id>/<reviewer label>-<n>.md`.
2. For each `ready` case, each reviewer and each run: start one review task in a new workspace, as ADR
   0043 rule 3 sets up, and detach it at the case's commit (`git checkout --detach <commit>`; check that
   `git rev-parse HEAD` prints it and `git status` is clean). The task is `brief.md`'s text with
   `<commit>`, `<paths>` (joined by ", ") and `<report path>` filled in, and nothing else. The task never
   names the case, its defects or another reviewer's report.
3. When it reports, check the workspace has no change and no new commit and that the report file exists,
   then remove the workspace. A report that is missing is recorded as missing, not re-run silently.
4. Hand the report folder to a scoring task.

## Scoring (a later task)

Two scorers work apart: the scoring task and a second reader it starts, each with the case files, this
section and the reports, and neither with the other's sheet.

For each report and each confirmed defect of its case, the scorer marks **found** if one finding names
the defect's location (the file and the function or test, or a line inside it) and says what its `match`
line asks; otherwise **missed**. One finding counts for at most one defect. Every finding that matched no
defect is **unconfirmed**: counted, listed, never called false. On a clean case every finding is unconfirmed.

The score sheet (`runs/<date>-<label>/scores.md`) gives, per reviewer: found out of confirmed defects,
per case and in total, with the defects it first reported itself (`reported_by`) counted apart;
unconfirmed findings, per case and in total; missing reports. It lists every
mark on which the two scorers disagreed and the raw agreement (marks equal over marks made). A
disagreement is settled by both rereading the finding against the `match` line; one still open is
reported as disputed and counted as missed. With `runs` = 1 the sheet says that a single run per cell
measures no variance between runs.

Later, someone may check unconfirmed findings; one that is confirmed (a fix on `main` with a failing
test) becomes a `[[defects]]` entry and counts from the next scoring on.

## Reading a score

With n confirmed defects, a found-rate p has a standard error of about √(p(1−p)/n). At the 10
defects of the first run (`runs/2026-11-10-first/`), 8 of them in one case, a difference between two
reviewers below about 40 points of found-rate is noise. With the 21 the cases hold now (10 in
`order-modify`, 9 in `trial-ledger-gate`), it is about 30 for the whole set, and defects bunched in a few
cases widen both (ADR 0046).
