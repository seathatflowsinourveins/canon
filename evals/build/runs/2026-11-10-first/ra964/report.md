# Report: five ways a study could pass the gate, or be left unusable

## What was wrong

All five defects were reproduced first: the ten named tests (14 cases with
parametrisation) failed on the untouched tree.

1. **Digest and session days re-read from the catalog.**
   `canon_trading.ledger.record_trial` ran the backtest, then called
   `bars_read(catalog_path, …)` with `ParquetDataCatalog.query`. A catalog
   replaced between replay and that read gave the old run the new bars'
   `session_days` and `data_digest`, so two runs on different bars could agree
   and rank. Nothing checked that the bars accounted for the run.
2. **Any catalog overlap accepted.** `canon_trading.backtest.backtest_run_config`
   only refused a catalog with no interval overlapping the range, and the
   ledger added no coverage check, so a sample the catalog covered only in
   part (or, with a mistyped year, not at all beyond that overlap test's
   message) recorded trials on a fragment of the sample.
3. **Keyed records without a sample number dropped from the ranking.**
   `canon_trading.ranking.rank` filtered each key's records by
   `user_attrs.get("sample") == sample.number`. A record that failed after
   its `config_key` was written but before its `sample` was written was
   therefore invisible, instead of stopping the ranking until a later record
   of its key resolved it. A record that ran to the end with a sample value
   that is not a sample number of the study (unknown int, text, `True`) was
   only reported as "not run on the current sample".
4. **Interrupted creation unrecoverable.** `create_ledger_study` committed
   the study name with `optuna.create_study` and only then wrote `samples`.
   An interruption in between left a study `open_study` refused as having
   no sample and `create_ledger_study` refused as a duplicate.
5. **Session day with no return dropped.** `canon_trading.returns_matrix.daily_series`
   kept only return keys that fell on session days and dropped the rest of
   the calendar, so a trial recording 31 session days with 30 returns ranked
   at T = 30.

## What I changed, and why that way

- `src/canon_trading/backtest.py` — `BacktestRun` now carries `bars`: every
  bar the engine cached for the run's bar types, read from
  `node.get_engine_cache(config.id)` before disposal, sorted by bar type,
  `ts_init`, `ts_event`. This is the only new field; the digest question —
  "which bars did this run read?" — is answered by the run itself, not by
  whoever holds the catalog path afterwards.
- `src/canon_trading/ledger.py`
  - `record_trial` takes `session_days` and `data_digest` from `run.bars`
    (sorted again at the point of use, since a caller may hand back any
    order), never from the catalog after the replay. Before recording them
    it refuses, as a told-`FAIL`/`failed` run on the existing terms, a run
    whose cached bars do not count to `BacktestResult.iterations`
    ("the engine's cache holds N bars and the run replayed M") or that
    replayed a bar outside the sample ("the run replayed N bars outside
    sample K (…)"). The old `bars_read` helper is removed.
  - New `_require_sample_covered`, called in `record_trial` before
    `backtest_run_config` and before any trial is asked for: for each bar
    type the run loads, the merged `ParquetDataCatalog.get_intervals`
    intervals must cover `[sample.start, sample.end]` with no gap,
    otherwise `LookupError: The catalog at … does not cover the sample:
    missing <first instant> to <last instant> for <bar type>; … consolidating
    the catalog (ParquetDataCatalog.consolidate_data)`. A bar type with no
    intervals keeps the existing "holds no … bars" refusal. This is placed
    in the ledger, not in `backtest_run_config`, because the range in
    question is the study's sample (ADR 0041) and the missing instant is
    named in UTC nanoseconds there; `backtest_run_config`'s docstring now
    says exactly what it itself refuses.
  - `create_ledger_study` catches `DuplicatedStudyError`, loads the study,
    and — only when it has no `samples` attribute at all and no trials,
    which is exactly what an interruption between name and sample leaves —
    writes the sample it was given. A study that already has a sample
    re-raises the duplicate error. `samples()` now adds
    "; create_ledger_study completes it" to its `LedgerNotFound` only when
    the attribute is missing, so opening a half-made study names the remedy.
- `src/canon_trading/ranking.py` — new `_sample_number` helper: a trial's
  `sample` counts only if it is an `int` (not `bool`) among the study's
  sample numbers. Per key, records on the current sample are the chosen
  set as before; a keyed record with no such number is added to the
  considered set only if it did not run to the end, so the existing
  "no later record ran to the end after #N (failed)" resolution rule
  applies to a failure before its sample was written, and a later record
  of the key clears it. A keyed record that did run to the end with no
  such number stops the ranking as "#N (traded) ran with sample X, not a
  sample number of the study (ADR 0041)". Report counts still count only
  records actually on the current sample.
- `src/canon_trading/returns_matrix.py` — `daily_series` now refuses a
  session day with no return key, after the existing gap check so the
  "skips from" refusal still wins for a missing interior day:
  "a: no return on 1 of its 31 session days, the first 2025-04-10".
  `ranking.rank` already gathers `ReturnsRejected` per trial, so every
  such trial is named at once.
- `src/canon_trading/variant_study.py` — `_to_record` uses the same rule
  as the gate: a keyed record with no sample number of the study that did
  not run is its key's latest record for the rerun decision, so a pass
  re-runs a configuration a killed pass left in that state instead of
  skipping it because its last on-sample record ran.
- Records corrected where the change made them untrue:
  `docs/adr/0035-trial-ledger-in-an-optuna-study.md` terms 1, 3 and 5, and
  `docs/adr/0041-trial-ledger-sample-and-data-identity.md` terms 1–3, each
  with a short "Later review, 2026-11-10" evidence line; the module
  headers of `backtest.py`, `ledger.py`, `ranking.py` and
  `returns_matrix.py` say the same where the code does it. `scripts/canon
  lint` still passes.

No test file was edited. The named tests are already the maintained
regression tests for each defect, so no new test file was added.

## Upstream API or contract followed

- NautilusTrader v2.0.0rc8, installed stubs read this session:
  `nautilus_trader/common/__init__.pyi` — `Cache.bars(bar_type)` returns
  the bars the engine cached for that bar type, and
  `BacktestNode.get_engine_cache(run_config_id)` (in
  `nautilus_trader/backtest/__init__.pyi`) returns that cache; both are
  read before `node.dispose()`, as the existing cache reads in
  `run_backtest_config` already were. `BacktestResult.iterations` is the
  count the cached bars are checked against. I verified by probe that the
  cache holds 40 bars for a 40-bar run with `iterations == 40`, in
  descending order, hence the sort.
- `nautilus_trader/persistence/__init__.pyi` —
  `ParquetDataCatalog.get_intervals` for stored coverage and
  `ParquetDataCatalog.consolidate_data` for closing a no-bar stretch; I
  verified by probe that two loads leaving a Sunday gap give two
  intervals, and one interval after consolidation.
- Optuna 5.0.0: `optuna.create_study` raising `DuplicatedStudyError` and
  `optuna.load_study`, as the ledger header already names; completion only
  re-uses those two calls under the existing creation `flock`.
- The decisions implemented are ADR 0010 step 2/3, ADR 0035 terms 1, 3, 5
  and ADR 0041 terms 1–3, as amended above. No dependency was added.

## What I ran, with its result

- The ten named tests on the untouched tree: 14 cases, all failed (each
  defect reproduced).
- The same ten after the change:
  `mise exec -- uv run --locked pytest <the ten tests> -q` — **14 passed**.
- Focused files: `tests/test_ledger.py tests/test_ranking.py
  tests/test_returns_matrix.py tests/test_backtest.py
  tests/test_variant_study.py -q` — **157 passed**.
- Full offline suite: `mise exec -- uv run --locked pytest -q` —
  **513 passed, 24 skipped** (the skips are the paper tests, which skip
  without a key pair; I did not run `mise run paper:test` or read any
  credential).
- `mise exec -- uv run --locked ruff check .` — all checks passed.
- `mise exec -- uv run --locked ruff format --check .` — 111 files already
  formatted.
- `mise exec -- uv run --locked ty check` — all checks passed.
- `scripts/canon lint` — clean. (`scripts/ci` was not run: this copy
  leaves out files it expects, as the task notes.)

## Anything I am unsure of

- `BacktestResult.iterations` counting cached bars is verified for the
  one- and two-instrument runs in this checkout's tests, but upstream's
  stub does not document it as a bar count; if a future pin counted
  something else, the accounting check would tell such runs `failed`
  rather than rank them, which is the safe direction.
- A keyed record that ran to the end with a valid sample number of an old
  sample is still handled by the pre-existing "not run on the current
  sample" stop; I did not change that, and no named test asks for more.
- Coverage is judged on the catalog's stored intervals, as upstream
  records them (a load window counts as covered even where it holds no
  bar). A sample inside one load window therefore passes even if a
  weekday inside it has no bar; that trial then stops at the ranking's
  session-day checks, which is the existing division of labour.
