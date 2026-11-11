# Report: five ways a study could pass the gate on inputs ADR 0010, 0035 and 0041 refuse

The change is in the working tree and not committed. Nothing was landed or pushed.

## What was wrong

1. **The digest was not of the bars the run read.** `record_trial` ran the backtest, then queried the
   catalog again (`bars_read(catalog_path, …)`, `ParquetDataCatalog.query`) for the data digest and
   the session days. If the catalog was replaced in between, the old run was given the new bars'
   digest, so runs on different data agreed. Nothing checked that the bars hashed were the bars the
   run had replayed.
2. **Any overlap counted as coverage.** `backtest_run_config` accepted a catalog whose stored intervals
   touched the sample anywhere. Two configurations could then pass on a fragment of the sample.
3. **Records with no sample number were dropped, not counted.** The ranking selected records with
   `user_attrs.get("sample") == sample.number`. A keyed record that failed before `sample` was written
   matched nothing, so it never stopped the ranking. A record that ran with a bad sample number
   (`7`, `"0"`, `True`) was dropped silently too.
4. **An interrupted creation could not be recovered.** `create_ledger_study` writes the study name and
   then its `samples` attribute. If it stopped in between, `open_study` refused the study for having no
   sample and `create_study` refused the name as a duplicate.
5. **Session days with no return were dropped.** `daily_series` kept only the keys that were session
   days. A session day with no key was left out without any error, so the trial ranked on fewer days
   than it had sessions.

## What I changed, and why

- **`backtest.py`**
  - `BacktestRun` has a new field, `bars`: the bars the engine's cache holds for each bar type the run
    loaded (`Cache.bars`). They are read before the node is disposed, like the orders and positions
    beside them.
  - When `start` or `end` is given, every gap that `ParquetDataCatalog.get_missing_intervals_for_request`
    reports is refused before anything is built. The `LookupError` names each gap
    (`unix_nanos_to_iso8601`), the bar type, and the remedy: a gap between two loads that held no bar
    is closed by `consolidate_data`; any other gap has to be loaded.
  - An inverted range (one bound past the stored intervals, the other not given) is still refused as
    "holds no … bars". Upstream returns no gaps for an inverted range (I observed this), so it has to be
    checked first. The existing test `test_one_bound_past_the_catalog_is_refused` covers it.
  - I first wrote my own gap computation, then replaced it with the upstream call once I found it
    (`bars_to_catalog.py` already uses it).
- **`ledger.py`**
  - `bars_read(run, sample)` now takes the run's own cached bars. It raises `RuntimeError` if any bar
    has a `ts_init` outside the sample, or if the count differs from `BacktestResult.iterations`.
    Because it raises inside `record_trial`'s `try`, the run is told `FAIL` / `failed` and no digest is
    written. The session days and `data_digest` come from those bars.
  - `create_ledger_study` handles a half-made study under the same creation lock. On
    `DuplicatedStudyError` it loads the study, and if the study has no `samples`, no trials and the
    maximize direction, it writes the sample; anything else re-raises.
  - `samples()` names a half-made study with "create_ledger_study completes it".
  - New `sample_number(trial, samples)`: only an `int` (not a `bool`) that is one of the study's sample
    numbers counts.
- **`ranking.py`**
  - A keyed record with no valid sample number that shows no run is read as on the current sample. It
    therefore stays unresolved until a later record of its key runs.
  - One that shows a run (`ran_something`) is a named stop: "… ran with sample 7, not a sample number
    of the study".
- **`variant_study.py`**: `_to_record` uses the same reading, so a pass records such a key again rather
  than skipping it. The gate would otherwise stay stopped.
- **`returns_matrix.py`**: `daily_series` refuses a series that is missing a return on any of its
  session days. The message gives how many are missing and the first one.
- **Records and docstrings that the change made untrue:**
  - the module headers of `ledger.py`, `backtest.py`, `ranking.py` and `returns_matrix.py`;
  - ADR 0035 term 3 (session days were said to be "read from the catalog");
  - ADR 0041 terms 1, 2, 3 and 6, plus one Evidence line dated 2026-11-10 saying a later review
    corrected them. This follows `.claude/rules/adr.md`.
- **New test** `tests/test_variant_study_sample_numbers.py`: `_to_record` re-records a key whose
  latest record is a keyed failure with no sample number. It would fail on the old code. No existing
  test file was edited.

## Upstream API or contract followed

All of these are from nautilus-trader 2.0.0rc8 as installed in `.venv` (bundled `.pyi`), checked this
session:

- `Cache.bars(bar_type)` and `CacheConfig.bar_capacity`, which defaults to 10,000.
- `BacktestResult.iterations`.
- `ParquetDataCatalog.get_intervals`, `get_missing_intervals_for_request` and `consolidate_data`.
- `nautilus_trader.core.datetime.unix_nanos_to_iso8601`.

From Optuna 5.0.0: `create_study` / `DuplicatedStudyError`, `load_study`, `Study.user_attrs` and
`Study.direction`.

The digest, the sample and the config key are still the local glue that ADR 0041 defines.

What I ran to observe the upstream behaviour (scripts in the scratchpad, not in the repo):

- `get_missing_intervals_for_request` returns inclusive gaps (for example
  `(2025-05-04T00:00:00.000000001, 2025-05-04T23:59:59.999999999)` for the weekend hole), and returns
  `[]` for an inverted range.
- `consolidate_data` leaves a single interval spanning both loads.
- The cache holds 40 bars for 40 iterations, newest first. With `bar_capacity=10` it keeps the 10
  newest, so the count check catches a run longer than the capacity.

## What I ran

| Command | Result |
|---|---|
| `mise exec -- uv run --locked pytest` before the change | 14 failed (the 10 named tests, some parametrized), 499 passed, 24 skipped |
| `mise exec -- uv run --locked pytest` after the change | **514 passed, 24 skipped** (the same 24 as before; one more test is mine) |
| The 10 named tests alone | 14 passed |
| `ruff check .`, `ruff format --check .`, `ty check` | all pass |
| `scripts/canon lint` | clean |

I did not run `scripts/ci`: the brief says it needs files this copy leaves out. I did not run the
paper tests.

## What I'm unsure of, and what is left

- **No independent review.** AGENTS.md requires `/code-review` (and the `evidence-verifier` for the ADR
  edits) before landing, but this brief forbids starting other agents. I read the diff myself instead.
  That review is still owed before this change lands.
- **`iterations` is assumed to count bars only.** It is the number of data points the engine
  processed. If a run ever loads other data types as well, the count check would tell the run failed;
  it would never pass a bad digest.
- **Runs over 10,000 bars of one bar type now fail.** That is the cache's default capacity. They are
  told failed (loudly) rather than given a digest of only part of their bars. The step-7 study is about
  1,370 bars per symbol, so it is not affected. Raising the capacity would be a separate decision.
- **A keyed failure with no sample number from an earlier sample** also stops the current ranking until
  its key runs on the current sample. ADR 0041 term 5 requires that run anyway, so I treated this as
  intended.
- **ADR 0041 is now 157 lines.** It was already past the ~130 lines at which `.claude/rules/adr.md`
  says to split; I added to it and did not split it.
