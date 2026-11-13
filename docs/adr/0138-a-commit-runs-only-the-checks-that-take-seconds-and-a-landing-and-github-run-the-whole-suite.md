---
status: proposed
date: 2026-11-13
decision-makers: the lead (2026-11-13), under the owner's delegation (ADR 0074); master orchestrator (Claude) for the record
review-by: 2027-02-11
---

# A commit runs only the checks that take seconds, and a landing and GitHub run the whole suite (proposed: open until a timed run of each form is in the record)

Settled 2026-11-13: [ADR 0144](0144-a-landing-runs-the-quick-checks-here-and-the-full-suite-once-on-github.md) puts the quick set at local landing and the full suite in the hosted gate; the missing timing comparison remains UNVERIFIED.

## Context and Problem Statement

Every `git commit` runs the whole of `scripts/ci` (`.githooks/pre-commit`, ADR 0011's commit gate). The hook
says "About eight seconds, offline"; ADR 0013 records the whole script at seven to nine seconds, and ADR 0014
rule 5 relies on its taking seconds before a push. On GitHub's runner the hosted `checks` job, setup
included, now takes about 19 minutes (main, 2026-11-13); on 2026-11-10 it took 22 seconds (ADR 0014). On this Mac, with several lanes at once, one commit's run held a slot for about 20
minutes after waiting about 8 for it. A landing then runs the same suite again on the same tree
(`scripts/ci --landing`, ADR 0097), and GitHub runs it a third time. On the night of 2026-11-12 a typing error
that ty finds in seconds cost a slot 17 minutes, twice (the lead's report: UNVERIFIED here).

## Decision Drivers

* Main gets only what the whole suite passed, on this Mac and on GitHub (ADR 0013, ADR 0014, ADR 0017).
* A commit says within seconds what is wrong with the lines it changes.
* The landing check is the one that matters, so it is not rationed, and nothing trusts a record of a pass
  that ADR 0097 could not make safe.
* Follow what the core upstreams do at commit, and what the hook framework's documentation advises.

## Considered Options

1. Keep the whole suite at commit (today).
2. At commit, the checks that take seconds; at landing and on GitHub, the whole suite.
2b. Option 2 plus the tests mapped to the staged paths, run at commit.
3. Option 1 plus a cache of passed trees, so a landing reuses the hook's pass (ADR 0097's option 2).
4. No hook at all; the landing and GitHub only.

## Decision Outcome

Chosen, proposed: **option 2**, in two units that land with this record as one change.

* **Unit 1, fast checks first.** `scripts/ci` runs the lock check, the environment,
  ruff, the format check and ty first. When one fails, nothing slower starts. It adds ADR 0013's rule 7 and
  lands first.
* **Unit 2, the commit's own scope.** `scripts/ci --staged` runs the canon lint, the lock check and the
  environment, ruff, the format check, ty, pytest's test collection (`pytest --collect-only -q`), shell syntax
  and shellcheck, and the staged secret scan. Every other check prints one skip line, `not run at commit; the
  landing runs it`. `scripts/ci --landing` and the hosted run are unchanged and run everything. A lane still
  runs the tests relevant to its change by hand before it lands, as NautilusTrader asks of its contributors
  before they open or update a pull request.
* **The passed-tree cache is a later unit, not built here.** Under option 2 the landing's run is the only
  full run on this Mac, so the second pass it would reuse is gone, and ADR 0097's two needs (a key covering
  everything the checks read, and a record no agent can forge) stay open. If a landing's run is still the
  long pole once this record has run for a week, the cache is weighed again against ADR 0097.

Option 1 holds a slot for the whole suite once per commit, and every fix commit adds another full run.
Option 2b maps paths to tests by a table of our own, which no upstream read here does; its gain at commit is
what the lane's own test run already gives. Option 3 needs what ADR 0097 found no upstream provides. Option 4
gives up the seconds-long answer at commit, where an error costs least to fix.

Unit 2 changes these lines, each with a dated pointer to this record:

* ADR 0011, the `Commit gate` row: every `git commit` runs the fast checks and test collection
  (`scripts/ci --staged`).
* ADR 0013, rule 1: the hook runs `scripts/ci --staged`, which is now the commit's scope, not every check;
  and its Consequences line "about seven to nine seconds", which becomes the measured figures.
* ADR 0014, rule 5: a failure shows before the push because the landing runs `scripts/ci --landing` before
  it pushes, not because the script takes seconds.
* `.githooks/pre-commit`'s header: "About eight seconds" becomes the measured figure.
* `AGENTS.md`, Automation: "Every commit runs `scripts/ci`" names the commit's scope.

### Consequences

* Good: a commit holds a slot for the fast checks only, not the whole suite (its time UNVERIFIED until the
  timed run below).
* Good: the landing's run is the one full run on this Mac; no record of a pass is trusted.
* Bad: a commit can pass with a failing test; the landing finds it, after the branch is committed, and a
  fix is one more commit. The lane's own test run before committing is what catches it sooner.
* Bad: the records that rest the hook on seconds change in the same unit, so the old figure does not
  survive in one of them.

### Confirmation

* Offline, red first in `scripts/test-ci`: `--staged` starts no self-test, no `scripts/test-*` and no full
  pytest, runs test collection, and prints one skip line for each check left to the landing; a test file
  that fails to import fails collection at commit; `--landing` and a run with no flag run every check.
* A timed table: per check, its time at commit before and after unit 2, from one timed run of each form
  on this Mac. UNVERIFIED until that run; it goes here before this record is accepted.
* The `evidence-verifier` reads this record before it changes status.

## Evidence

Read 2026-11-13.

* `.githooks/pre-commit` and `scripts/ci` at `edec08cd`: the hook runs `scripts/ci --staged` and says
  "About eight seconds, offline" (`:5`, `:20`). `--staged` narrows the secret scan, and checks a shell file
  deleted from the working tree from its staged copy; it skips no test (read in the tree). ADR 0013's "about
  seven to nine seconds" (`:76`) and ADR 0014 rule 5, "`scripts/ci` takes seconds here, so a failure shows
  before the push" (`:45`), and its 22-second hosted run of 2026-11-10 (read in the tree).
* Unit 1 (`7035f7c0`, its commit hook passed 2026-11-13) and unit 2 land with this record: `scripts/ci`,
  `scripts/test-ci`, `.githooks/pre-commit`, ADR 0011, ADR 0013, ADR 0014 and `AGENTS.md` (read in the tree).
* ADR 0097 (proposed): a landing checks the tree its hook checked; the cache's two needs (read in the tree).
* NautilusTrader at `d53849b216e2`, `.github/workflows/build.yml:295-300` (`build` needs `pre-commit`) and
  `:520-522` (the Rust tests need it too); the `pre-commit` job itself is `:201-205` (read at the pin).
* One commit's hook on this Mac, 2026-11-13: started 00:38:42, held a slot from about 00:47 (its `lockf`
  holder, read), passed at 01:06:53 (its log, read).
* NautilusTrader at `d53849b216e2`: `.pre-commit-config.yaml:3` (`default_stages: [pre-commit]`), `:54-57`
  (shellcheck), `:198-204` (`python-test-collection`, `make pytest-collect-fast`), `:501-504` (ruff and its
  format); `Makefile:1417-1424` (`pytest tests/ --collect-only -q`); `CONTRIBUTING.md:151` and `:155-156`
  ("Run all tests relevant to the change locally", before opening or updating a pull request) and `:170-171`
  (`make pre-flight` for the broad local suite). Read at the pin. Against the record: its hook also runs
  `cargo clippy` when Rust or build files change (`:370-375`), so its commit is not limited to the
  seconds-long checks either (its hook's duration was not measured: UNVERIFIED); its collection
  hook is filtered by path (`:203`) and skips when the extension is not built (`Makefile:1419-1420`). A newer
  release (v2.0.0rc9) exists and was not read.
* alpaca-py at `c42add7c0a70`: its hook runs `poetry run pytest` with `always_run: true`
  (`.pre-commit-config.yaml:7-30`; the lead's scout, read directly). It is a small library; Canon's suite is
  not.
* superpowers at `22f0b463e7a5`: ruff, its format and ty on `evals/` only, no tests (`.pre-commit-config.yaml:1-21`;
  the lead's scout, read directly). Of seven repositories the scout read, one runs its tests at commit, four
  run only linters, formatters, type checks or collection, and two showed no hook config (ruff, pydantic,
  uv and codex from summaries: UNVERIFIED).
* https://pre-commit.com/, "always running a hook on all files": "this is not recommended as it has the
  tendancy to be slow and deviates from the normal expectations of pre-commit"; "Confining hooks to run at
  certain stages" suggests `stages: [pre-commit, pre-merge-commit, pre-push, manual]` for a linter or
  formatter (fetched; the quotes re-read word for word from the raw page by the second reader). The "not
  recommended" note is about wiring one hook to every file, not about tests at commit.
* https://git-scm.com/docs/githooks: `pre-commit` "can be bypassed with the `--no-verify` option" (fetched by
  the second reader, word for word). The enforcement point does not move: `scripts/land` and
  GitHub run the whole suite (ADR 0020).
* GitHub Actions, Canon's `ci.yml` on main, 2026-11-13: the `checks` job of runs 43371991829 and 43372390789
  took 18 min 46 s and 19 min 09 s, setup included (`gh run view --json jobs`, read, and re-read by the second
  reader). Whether other runs shared the runner's host was not checked.
* Second reader (`evidence-verifier`, 2026-11-13): 7 claims held, 7 held in part, 1 not supported (the 17
  minutes, now marked), 1 not checkable; corrections made above, among them the ADR 0011 row, which had
  described option 2b, and the two records' lines it had left out.
