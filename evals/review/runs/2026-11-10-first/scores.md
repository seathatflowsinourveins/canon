# Score sheet: run 2026-11-10-first

The first run of the reviewer benchmark (ADR 0046), scored by the rule in
[`evals/review/README.md`](../../README.md) ("Scoring"). The reports are in this folder, one per case and
reviewer: `<case>/<reviewer>-1.md`.

## The run

* **Cases:** the five that were `ready` when the benchmark was defined (`de63794`): `cancel-all` and
  `stop-orders` (clean), `data-client-hardening` (1 confirmed defect), `exec-client-first-slice` (1) and
  `trial-ledger-gate` (8). 10 confirmed defects in all, 8 of them in one case.
* **Reviewers**, each given `brief.md` word for word in a new workspace detached at the case commit:

  | Label | Runtime, model, effort |
  |---|---|
  | `claude-opus-high` | Claude Code, `claude-opus-5-5`, effort high; auto memory not turned off, so likely on (UNVERIFIED; README, "Running a benchmark") |
  | `codex` | Codex CLI on its own defaults |
  | `muse` | Muse Code on its defaults |
  | `antigravity` | Antigravity CLI on its defaults |

* **Runs per cell:** 1. A single run per cell measures no variance between runs: the same reviewer on the
  same case may find a different set next time, and nothing here says by how much.
* **Reports:** 20 of 20 present, none missing. Each workspace was checked at the case commit with no
  change and no new commit before it was removed.
* **Case files used:** as they stood at `de63794`. The `[[defects]]` entry added to `trial-ledger-gate`
  after this run (below) is not scored here.

## Totals per reviewer

Found counts a confirmed defect a finding named by location and by what its `match` line asks. "First
reported itself" counts the found defects whose `reported_by` names that reviewer. None does: the two
single defects showed as flaky tests, and the eight `trial-ledger-gate` defects came from an earlier full
review that is not one of these reviewers. Unconfirmed counts every finding that matched no confirmed
defect; it is a count, not a judgement that the finding is false.

| Reviewer | Found / 10 | of them first reported itself | Unconfirmed | Missing reports |
|---|---|---|---|---|
| `claude-opus-high` | 1 | 0 | 27 | 0 |
| `codex` | 1 | 0 | 10 | 0 |
| `muse` | 2 | 0 | 30 | 0 |
| `antigravity` | 1 | 0 | 48 | 0 |

## Per case

Findings are counted by each report's own top-level numbering. Sub-points inside a numbered finding belong
to it, and closing sections such as "checked and found sound", "not verified" or "notes" are not findings.

### Found, out of the case's confirmed defects

| Case | Defects | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|---|
| `cancel-all` | clean | – | – | – | – |
| `data-client-hardening` | 1 | 0 | 0 | 1 | 1 |
| `exec-client-first-slice` | 1 | 0 | 0 | 0 | 0 |
| `stop-orders` | clean | – | – | – | – |
| `trial-ledger-gate` | 8 | 1 | 1 | 1 | 0 |
| **Total** | 10 | 1 | 1 | 2 | 1 |

### Unconfirmed findings

| Case | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|
| `cancel-all` | 4 | 3 | 7 | 10 |
| `data-client-hardening` | 9 | 5 | 7 | 8 |
| `exec-client-first-slice` | 7 | 1 | 6 | 11 |
| `stop-orders` | 4 | 0 ("No defects found") | 5 | 7 |
| `trial-ledger-gate` | 3 | 1 | 5 | 12 |
| **Total** | 27 | 10 | 30 | 48 |

### Every mark

| Case | Defect | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|---|
| `data-client-hardening` | `log-capture-before-writer-flush` | missed | missed | found (#4) | found (#6) |
| `exec-client-first-slice` | `startup-reconciliation-race` | missed | missed | missed | missed |
| `trial-ledger-gate` | `ledger-path-per-directory` | missed | missed | missed | missed |
| `trial-ledger-gate` | `common-range-trim` | missed | missed | missed | missed |
| `trial-ledger-gate` | `key-ignores-the-data` | found (#1) | missed | found (#1) | missed |
| `trial-ledger-gate` | `variance-exact-zero-test` | missed | missed | missed | missed |
| `trial-ledger-gate` | `key-hashes-raw-text` | missed | missed | missed | missed |
| `trial-ledger-gate` | `path-interpolated-into-url` | missed | missed | missed | missed |
| `trial-ledger-gate` | `key-read-from-mutable-config` | missed | found (#1) | missed | missed |
| `trial-ledger-gate` | `no-day-in-common-names-no-trial` | missed | missed | missed | missed |

Why each found mark holds:

* `muse` #4 (`data-client-hardening`) names `logged(capfd)` and the test asserting on the disconnect
  summary, and says the line was written after the capture was read, by a logger that flushes
  asynchronously.
* `antigravity` #6 (`data-client-hardening`) names `logged` and says NautilusTrader writes logs from its
  own thread, so assertions on the captured text race it.
* `claude-opus-high` #1 (`trial-ledger-gate`) names `ledger.py` (`_ENGINE_LEFT_OUT`, `trial_config`) and
  says the key holds no data identity, so runs over different bars share a key and the ranking counts
  them as one configuration that disagrees.
* `muse` #1 (`trial-ledger-gate`) is mainly about `session_days`, but it names `ledger.py`, `trial_config`,
  `left_out=("catalog",)` and says a re-run after the catalog's bars change gets the same key, so the two
  records are taken as one configuration.
* `codex` #1 (`trial-ledger-gate`) names `record_trial` in `ledger.py` and says the key is hashed from a
  config `MaCross` copied at construction, so a changed config records a run that did not happen.

Marks both scorers named as close, and settled as missed:

* `codex` #2 and `antigravity` #3 say the catalog is left out of the config key, as `muse` #1 does, but
  name only `ranking.py`'s `rank`: the location half fails.
* `antigravity` #5 says one trial's shorter series shrinks T for every trial, but places the fault in
  `daily_series` and does not say the ranking or the gate's result changes (`common-range-trim`).
* `antigravity` #2 has a trial told `FAIL` pass after the matrix is trimmed, but names `ranking.py`'s
  `_ran` and `rank`, not the trim in `returns_matrix.py` (`common-range-trim`).
* `antigravity` #6 names `_canonical`, but the equal configurations it means are dicts whose keys
  collide or come in another order, not the `match` line's balance spelled differently or upstream
  defaults (`key-hashes-raw-text`).
* `claude-opus-high` #2 and `antigravity` #9 name the start-up reconciliation test of
  `exec-client-first-slice`, but say its assertions are weak, not that start-up reconciliation races the
  test body (`startup-reconciliation-race`).

## Agreement between the two scorers

Two scorers marked the reports apart, each with the case files at `de63794`, the README's scoring section
and the reports, and neither with the other's sheet.

* **Raw agreement: 40 of 40 marks** (100%). The marks are 4 reviewers × (1 + 1 + 8) confirmed defects;
  the clean cases make no found or missed mark. The 20 unconfirmed counts also agree, 20 of 20.
* **Disagreements: none**, so nothing was settled by rereading and **no mark is disputed**.
* Both sheets named the same close calls, listed above.

## Reading these numbers

With n confirmed defects, a found-rate p has a standard error of about √(p(1−p)/n). Here n = 10 and the
found-rates are 0.1 or 0.2, so each has a standard error of about 9 to 13 points. A gap below about 40
points of found-rate is noise (README, ADR 0046), and 8 of the 10 defects sit in one case, which widens
it. The largest gap in this run is 10 points (one defect), so the run ranks no reviewer above another.
The unconfirmed counts measure how much a reviewer reports, not how much of it is right.

## After this run

* **A new confirmed defect for the next scoring.** `claude-opus-high`'s `trial-ledger-gate` #3, that the
  only test of the DSR at N-hat passes for any benchmark below SR₀, was real on `main` and is fixed in
  `6e6fed7` (#72), whose message reports the new check failing for the wrong computations the old one
  passed. It is now `n-hat-dsr-test-cannot-fail` in `cases/trial-ledger-gate.toml`, with `reported_by`
  naming that reviewer and this run. This run scores the case without it.
* **`order-modify` is now `ready`**, with 10 defects that `52ac69c` (#69) fixes. The fixing lane's report
  counted 17 claims as real; the case follows the fix's message instead, which reports 14 regression tests
  (parametrised cases counted one each) failing before the fix, except one. From those, 10 defects are
  entered. Left out, each for its own reason, given in the case file's header:
  * a rejected replacement reported after a restart: the message says its test did not fail first;
  * a long replace chain walked again each time, and the two new tests the message's Test Plan does not
    list: the message does not report a test failing first for them;
  * a trace that found no node order kept for the session: `3a00f61`'s own message states it as designed
    ("a chain that reaches no node order is remembered"), and a stated limit is left out.

  `reported_by` follows the fixing lane's report. It names runtimes that reviewed `3a00f61` as second
  readers, outside this benchmark and before its brief: Codex 3, Antigravity alone 3, Muse and Antigravity
  both 4 (their reviews ran in parallel, so which was first is not known).
* **That case is biased toward three of these reviewers.** Every one of its defects was first reported
  by Codex, Muse or Antigravity, reviewing `3a00f61` after it had passed a Claude `/code-review` at high
  effort, so it holds defects a Claude review missed and those three found. A later run should read
  their found-rate on it in that light; the defects each first reported are counted apart.

## Redactions

The reports are copied as written, except:

* `cancel-all/muse-1.md`: 11 absolute paths into the reviewer's workspace, on 9 lines, in Markdown link
  targets, became `<checkout>/` followed by the path inside the repository.

No report contained a user, account or machine name or an email address. The other 19 reports are
unchanged; paths under `/tmp` and mentions of a "session scratchpad" name no person or machine and were
left as written.
