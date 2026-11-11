# Build eval

A fixed set of past fixes, each turned into a task: the tree before the fix, the fix's own failing
tests, and a statement of what is wrong. It answers one question coarsely: can a worker make a
confirmed defect's tests pass here without breaking anything, with a change this repository would land?
It is the measure that keeps or retires a worker that builds here, not a ranking and not a gate: an
adopted runtime may build before its trial (ADR 0058). The design, its sources and the bar are in
[ADR 0057](../../docs/adr/0057-build-eval-from-confirmed-fixes.md).

| Path | Holds |
|---|---|
| `cases/<id>.toml` | One case each |
| `brief.md` | The brief every worker gets, word for word |
| `harness.py` | Makes a case's tree, shows by running that a case is sound, grades a worker's files |
| `runs/<date>-<label>/` | A run's reports, grades and score sheet, committed by the scoring task (added with the first run) |

`tests/test_build_cases.py` checks every case file and the harness; it runs in `scripts/ci`.

## A worker

A worker is a runtime, a model, an effort and a configuration, written
`<runtime>/<model>/<effort>/<configuration>`: `claude/claude-opus-5-5/high/stock`,
`codex/gpt-6.1-sol/max/stock`. The configuration is `stock` (the runtime as its adoption record
installs it) or a name the run's sheet defines as a list of skills and plugins, each adopted through
`adopt-upstream` before it is installed. One runtime may run in several configurations on the same
cases, and each is a worker of its own: a verdict belongs to the label, not to the runtime.

## A case

```toml
id = "cancel-again-and-unholdable-id"   # the file name without .toml
status = "ready"                        # ready: run and scored; left-out: no case, see `reason`
fix = "<40-hex sha>"                    # the fix on main; the worker starts from its parent
review_case = "stop-orders"             # the case in evals/review/cases/ that records its defects
defects = ["<defect id>"]               # the defects there that this fix confirms
paths = ["packages/canon-alpaca/"]      # where the change belongs; goes into the brief
tests = ["<test file>"]                 # every test file the fix changes; supplied as the fix has them
fail_to_pass = ["<file>::<function>"]   # the test functions that fail on the starting tree
admitted = "2026-11-10"                 # the day `harness.py admit` last showed the case sound
statement = "..."                       # what is wrong, as the worker is told it; never how it was fixed
reason = "..."                          # left-out only, in place of the six lines above it
```

The starting tree is the fix's parent with the fix's test files, as SWE-bench applies a pull request's
tests before its other content. A case is sound when, on three runs, the starting tree fails exactly
the `fail_to_pass` functions and nothing else, and the fix passes every test, `ruff check`,
`ruff format --check` and `ty check`:

```sh
mise exec -- uv run --locked python evals/build/harness.py admit <case id>
```

That runs the whole offline suite six times and is not part of `scripts/ci`; `admitted` records the
last day it passed. The check in `scripts/ci` fails unless: every key has the right type; `fix` exists
and is an ancestor of `HEAD`; `tests` are exactly the test files the fix changes; each `fail_to_pass`
function is defined in one of them at the fix; `defects` are the ones the review case confirms by this
fix; the statement does not name the fix; and every fix that confirms a defect in `evals/review/cases/`
has a case, ready or left out. A fix is left out when a case cannot be made of it soundly: its fix is
in the test file a case would supply, or its tests cannot be collected on the starting tree.

Three fixes are left out today, and four are ready, with 31 `fail_to_pass` functions between them:
`cancel-again-and-unholdable-id` 4, `modify-follows-replace` 11, `gate-reads-what-the-run-read` 10 and
`ledger-remaining-defects` 6.

## What a worker is shown, and what is kept from it

Shown: the tree, the statement, the failing tests, the brief. The tests are shown because a builder
here is handed a finding reproduced by a failing test (ADR 0045), and because these tests call the
fix's own helpers, so a correct fix written without them would fail them.

Kept from it, by construction: the fix and all history (the tree is one commit with no remote),
`evals/`, the case file, other workers' reports.

Kept from it only by the brief, and checked afterwards: this repository is public, other checkouts of
it are on the machine, and several runtimes start with no sandbox. The coordinator reads each run's
transcript for a read outside the tree or a use of the network beyond `uv`. A breach voids the run
and counts as a broken term.

Not kept from it at all:

* **Training data.** A model whose training data ends after a case's fix was pushed may have seen the
  fix. The sheet gives each model's published cutoff, or says none is published, and a case is not
  scored for a model whose cutoff is later than its fix. Every fix here was pushed on 2026-11-10.
* **Memory across sessions.** A Claude worker starts with auto memory off, as
  `evals/review/README.md` describes, and DeepSeek Harness with its memory off (ADR 0045). For any other
  runtime the sheet says whether a switch exists and how it was set. With memory on, a worker's later
  runs of a case are not independent of its first.
* **An earlier read of the fix.** Second readers review landed changes on the paths these fixes are
  on (ADR 0045), so a runtime may have been given a case's fix to review. The sheet lists, per case,
  each runtime that was, and counts its runs of that case apart.

## Running a trial (the coordinator)

Only the coordinator starts workers. `runs` is the number of runs per worker per case, by default 3.

1. Pick a run label (`<date>-<label>`) and a folder outside every checkout. For each ready case, each
   worker and each run, make a tree and give it a run id that does not name the worker:
   `harness.py tree <case id> <folder>/<run id>/tree`. Keep the table of run ids and workers out of
   the readers' sight.
2. Start one worker with that tree as its workspace and `brief.md`'s text as its whole task, with the
   case's `paths`, `statement` and `fail_to_pass` and the report path
   (`<folder>/<run id>/report.md`) filled in, and nothing else. The task never names the case.
   The tree is not a worktree of this repository and must not become one, since a worktree carries
   the fix in its history. In the first trial each worker ran in its tree as a folder of its own, and
   each tree still had one commit and no remote when it finished (`runs/2026-11-10-first/scores.md`).
3. When the worker reports, or after a time limit the run's sheet states, grade what is in the tree:
   `harness.py grade <case id> <folder>/<run id>/tree <folder>/<run id>/grade`. The grader makes the
   starting tree again and runs the suite on it, takes the worker's files as a patch, puts back every
   test file and `pytest.toml` of the starting tree, and runs the suite and the three code checks
   itself. `grade.json` gives `resolved`, the test results, any test that passed before and is `lost`,
   the files put back and the `flags`; `change.patch` is the change. A change the grader cannot run is
   unresolved, with the reason. A missing report is recorded as missing, not run again silently.
4. Read the transcript for a broken term (the list in the brief's "Do not" paragraph), and check that
   nothing outside the tree changed: no new branch, commit or stash in any checkout of this
   repository, nothing pushed, no new scheduled job, no process of the worker's left running. Record
   what was checked and how.
5. Remove the tree. Hand the grades, the patches and the reports, named by run id, to a scoring task.

Before the first trial of a set of cases, grade two trees nobody worked in: one holding the fix's own
files, which must resolve, and one with a change that fixes nothing (a comment added to a source
file), which must come back unresolved with its `fail_to_pass` tests failed.

## Scoring (a later task)

A run that broke a term scores nothing and is listed. A run that is not `resolved` is **failed**. Each
resolved run is then read by two readers working apart, the scoring task and a second reader it
starts, each with the patch applied to the starting tree, the case file, the report and this section,
neither with the other's marks or the table of workers. The first reader's reading is ADR 0017's
review: `/code-review` at the effort the case's paths get. A reader started as a subagent writes its
marks to a file and ends with none in its message, which reaches the scoring task, and neither file is
read by the other reader until both readers' marks are final.

A reader marks a run **no** when it finds a blocker, and names it:

1. the change makes the named tests pass without removing the defect: it recognises the tests or their
   inputs, or changes what a test run means (every `flags` entry is looked at for this);
2. it builds a mechanism of its own where the upstream the surrounding code names provides one, or adds
   a dependency (rule 3 of `AGENTS.md`);
3. it hides a failure: a swallowed error, a silent fallback, a weakened check (rule 5);
4. it breaks behaviour the suite does not cover, shown by an input or a sequence of events;
5. its report states something the change does not do.

Otherwise the mark is **fix** when the reader has findings a review round would have had fixed before
landing, each listed, and **land** when it has none. One reader's `no` against the other's `land` or
`fix` is settled by both rereading the blocker; one still open is reported as disputed and counted as
`no`.

The score sheet (`runs/<date>-<label>/scores.md`) gives, per worker: runs resolved out of runs, per
case and in total; the cases resolved in every run; the marks, per case and in total; broken terms;
missing reports; the runs counted apart (an earlier read of the fix, memory left on). It lists every
mark on which the readers disagreed and the raw agreement. It says which runtime versions ran, and it
does not set a Codex result from before ADR 0045 rule 7 beside one from after it without saying so.

## Reading a score

The unit is the case: runs of one case are not independent, so more runs do not add cases. With n
cases a resolved-rate p has a standard error of about √(p(1−p)/n), and a difference between two workers
one of √(SE₁² + SE₂²). To tell a worker that resolves 90% of cases from one that resolves 50%, at the
usual 5% and 80% power, takes about 17 cases; 90% from 70% takes about 60. Pairing the two workers on
the same cases needs fewer, by how far their results agree. With the four cases here nothing but all
against none can be told apart, and the four come from two modules, which widens that. So the sheet is read as ADR 0057's bar reads it: a broken term, a case the reference worker
resolves every time and this worker does not, and a `no` both readers give are each a fact about one
run that needs no statistics. Anything finer is noise.
