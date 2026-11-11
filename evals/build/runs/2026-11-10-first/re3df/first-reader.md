# First reader: re3df
Mark: no
Blocker: 4 — an orphaned keyed failure now stops the ranking, but the study runner still treats its key as resolved and never re-runs it, so after one interrupted record the study can never rank through `run_study`
## Findings
1. src/canon_trading/variant_study.py:122 (unchanged) with src/canon_trading/ranking.py (new orphan stop) — `rank` now stops on a keyed record that failed before its sample number was written, "until a later record of its key resolves it", but `_to_record` still reads only records whose `sample` equals the current sample, so the key's latest on-sample record (which ran) makes it look resolved and `record_grid` skips it. Its docstring ("a key is resolved for the gate ... exactly when its latest record ran") is now untrue. Sequence: create a study; record configurations A and B; record A again with the `sample` write failing (`OSError`, as the supplied test does); run `variant_study.record_grid` over A and B; `rank`. Result: the pass records nothing (both keys skipped) and `rank` stops: "no later record ran to the end after #2 (failed)"; every later pass does the same. r1e4c, rd513 and rff8d re-record A in that pass and rank N = 2. Before the change the ranking passed (ignoring the orphan, the defect); now the runner cannot clear the stop it raises. Verified by probe G1 (severity: blocker)
2. src/canon_trading/ledger.py:198-201 — on `DuplicatedStudyError` the recovery completes any existing study without a `samples` attribute, whatever its directions or trials. Probe: a foreign multi-objective study "x" in the ledger file: `create_ledger_study("x", ...)` succeeds and stamps a sample on it, and `open_study("x")` before that says its "creation was interrupted". The other three runs keep DuplicatedStudyError and LedgerNotFound for it. Verified by probe (severity: fix)
## Review findings not taken
1. backtest.py:290 (bar cache capacity): pinned by the supplied test's design; no run sets it.
2. backtest.py:199 (empty edge of a sample counts as missing): the same upstream call in every run, required by the supplied coverage tests.
3. returns_matrix.py:97 (NautilusTrader's series may lack early session days): the statement and the supplied test require refusing such a day.
4. ledger.py:312 (adopts a foreign study): finding 2.
5. ledger.py:517 (total count across bar types): offsetting errors in two bar types; speculative.
6-9. Bars copied for every caller, double sort, dead `bars_read`, duplicated formatter: cleanups.
## Per-defect check
1. Digest and session days from the run's cached bars, checked against iterations and the sample. Removed.
2. Missing intervals refused before a trial, named. Removed.
3. Ranking side removed (orphan stops, foreign-sample run stops, named); the runner side not updated, so the orphan's stop cannot be cleared by a pass (finding 1).
4. Interrupted creation completed, but without checking the study is one this code left (finding 2).
5. A session day without a return refused, named. Removed.
## What I ran
- Probe G1 (scratch test in `tests/`, removed; tree diff unchanged), the same in all four runs of this case: fails here only.
- Probe (scratch script outside the tree): foreign multi-objective study, open and create.
