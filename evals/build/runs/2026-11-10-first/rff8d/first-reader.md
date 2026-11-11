# First reader: rff8d
Mark: fix
Blocker: none
## Findings
1. src/canon_trading/ledger.py:215-221 — `_half_made` reads `study.direction`, which Optuna raises `RuntimeError` on for a multi-objective study; `samples()` (line 274) and the creation recovery (line 207) call it. A study in the ledger file that this code did not make, with two directions and no sample, now makes `open_study` raise `RuntimeError` instead of its documented `LedgerNotFound`, and `create_ledger_study` raise `RuntimeError` instead of `DuplicatedStudyError`. Still a refusal, so not a blocker; `study.directions == [MAXIMIZE]` (as r1e4c writes it) is the fix. Verified by probe (severity: fix)
## Review findings not taken
1. ledger.py:463: finding 1.
2. ledger.py:427 (bar cache capacity): pinned by the supplied test's design; no run sets it; the change and report state the limit.
3. returns_matrix.py:99 (first session day possibly absent from NautilusTrader's series): required by the statement and the supplied test.
4. backtest.py:366 (consolidation advice could paper over a real gap): the statement asks for exactly that remedy for a stretch a load showed holds no bar; the message says any other gap has to be loaded.
5. ranking.py:614 (two stops for a lone foreign-sample record): both are true; the ranking stops either way.
6. ADR 0035:61 (no Evidence line for the correction): ADR 0041 carries the dated correction line covering the same change; records may be corrected under the brief.
7. ADR 0041:41 (decision rewritten, 157 lines): the brief lets the worker correct records its change makes untrue; the file was already past 130 lines, as the report says.
8. ledger.py:433 (tie order): speculative.
9. backtest.py:377 (bar types derived three times): cleanup.
## Per-defect check
1. Digest and session days from the cached bars (`bars_read(run, sample)`), checked against iterations and the sample; told failed otherwise. Removed.
2. Missing intervals refused before a trial, each named, with the remedy. Removed.
3. Orphan failures read on the current sample in both `rank` and `_to_record`; the runner re-records the key (G1); a run with a foreign sample number stops, named. Removed.
4. Interrupted creation completed for an empty maximizing study; finding 1 for a multi-objective foreign study. Removed.
5. A session day without a return refused, named. Removed.
## What I ran
- Probe G1 (scratch test, removed): orphan failure, `record_grid`, `rank`: key re-recorded, N = 2.
- Probe (scratch script outside the tree): foreign multi-objective study: open -> RuntimeError, create -> RuntimeError (r1e4c and rd513: LedgerNotFound and DuplicatedStudyError).
