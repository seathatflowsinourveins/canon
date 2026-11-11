# Report: five ways a study could pass the gate, or be left unusable, on inputs ADRs 0010, 0035 and 0041 refuse

## What was wrong

1. **Digest and session days read after the run.** `record_trial` read the bars again from the catalog
   (`bars_read`, a `ParquetDataCatalog.query`) after the replay. If the catalog was replaced in between,
   the old run's record got the new bars' digest, so two runs on different bars agreed and the gate ranked them.
2. **Partial coverage accepted.** The only check on the sample was `backtest_run_config`'s "some stored
   interval overlaps [start, end]". A sample the catalog covered only in part was run on the fragment.
3. **Keyed records with no valid sample number.** The ranking kept only records whose `sample` equalled
   the current sample's number. If `record_trial` failed after writing the key and before writing
   `sample`, its record had a key and no sample. The ranking silently dropped that record instead of
   counting it as unresolved. A record that ran to the end with a sample number the study doesn't have
   (`7`, `"0"`, `True`; and `True == 1` would have matched a sample 1) was dropped too, or wrongly matched.
4. **Half-made study.** `optuna.create_study` commits the name before `create_ledger_study` writes the
   `samples` attribute. If creation was interrupted in between, `open_study` refused the study (it has
   no sample) and a second creation raised `DuplicatedStudyError`. Nothing short of editing the file
   could recover it.
5. **Session day with no return.** `daily_series` kept only the returns that fall on session days. A
   session day missing from the returns was silently dropped, so a trial ranked on fewer days than it had sessions.

## What I changed, and why that way

- `src/canon_trading/backtest.py`: `BacktestRun` has a new field, `bars`. `run_backtest_config` fills
  it before the node is disposed: for each bar type the run loads it reads `Cache.bars(bar_type)`
  (newest first upstream) and turns the list oldest first. The bars come from the engine's own cache,
  so they are the bars the run read, not a second read of a catalog that may have changed.
- `src/canon_trading/ledger.py`:
  - `require_coverage` replaces nothing upstream and runs before the trial is asked for. It merges the
    catalog's stored intervals (`get_intervals`, both ends inclusive, ranges that touch merged) and
    names every part of the sample outside them in one `LookupError` ("does not cover the sample:
    missing … to … of <bar type>"). For a hole between two stored ranges it adds that
    `ParquetDataCatalog.consolidate_data` closes a hole where no bar can fall: per
    `bars_to_catalog`'s header, a load whose window holds no bar records nothing. If the catalog
    stores no range for a bar type at all, the check leaves it to `backtest_run_config`'s existing
    "holds no … bars" refusal, so those messages are unchanged.
  - `bars_replayed` replaces `bars_read`, which is removed. It takes `run.bars`, refuses any bar with
    a bar type the run doesn't load or a `ts_init` outside the sample ("the run replayed N bars
    outside sample K …"), and refuses a count different from `result.iterations` ("the engine's cache
    holds X bars and the run replayed Y …"). It raises `RuntimeError` inside `record_trial`'s `try`,
    so the record is told `FAIL` / `failed` with no `data_digest`. `data_digest` and `session_days`
    are now taken from these bars.
  - `create_ledger_study`: on `DuplicatedStudyError` it loads the study, still under the existing
    exclusive creation `flock`. If the study has no `samples` and no trial, it writes the sample
    (completing an interrupted creation). Otherwise it re-raises. `samples()` now says a study with no
    `samples` attribute "was interrupted …; create_ledger_study completes it", so `open_study` points
    to the fix.
- `src/canon_trading/ranking.py`: a keyed record counts on a sample only if `sample` is a plain `int`
  (not `bool`) that is one of the study's sample numbers. A record with no such number is handled two ways:
  - If it ran to the end (`_ran`), the ranking stops, naming it: "#n (traded) ran with sample 7, not a sample number of study …".
  - Otherwise it is read as on the current sample. It stays unresolved until a later record of its
    key runs to the end; record numbers only grow, so a later record always has a higher number.
- `src/canon_trading/returns_matrix.py`: `daily_series` refuses session days that have no return,
  giving how many and the first ("a: no return on 1 of its 31 session days, the first 2025-04-10").
  `rank` already passes this `ReturnsRejected` through with the trial number.
- Docs made untrue by the change, now corrected:
  - the module headers and docstrings of `ledger.py`, `ranking.py`, `returns_matrix.py` and `BacktestRun`;
  - ADR 0041 terms 1, 2, 3 and 5, plus an Evidence line dated 2026-11-10 saying what was corrected;
  - ADR 0035 term 3, which said session days were "read from the catalog".
- New test file `tests/test_ledger_sample_records.py`:
  - a record that ran with no `sample` attribute at all stops the ranking;
  - a study with no sample but with a trial is not "completed": it stays a duplicate.

## Upstream API or contract followed

- NautilusTrader 2.0.0rc8, the installed `.pyi` files (checked this session):
  - `Cache.bars(bar_type) -> list[Bar] | None` and `CacheConfig.bar_capacity` (default 10,000, printed
    from `CacheConfig()`), in `common/__init__.pyi`;
  - `BacktestResult.iterations`, in `backtest/__init__.pyi`;
  - `ParquetDataCatalog.get_intervals` and `consolidate_data`, in `persistence/__init__.pyi`.
- Upstream behaviour I ran this session:
  - a 70-bar run held 70 bars in its cache, `iterations` was 70, and `Cache.bars` returned them newest first;
  - two stored ranges with a weekend between them came back from `get_intervals` as two;
    `consolidate_data` merged them into one.
- Optuna 5.0.0: `create_study` raises `DuplicatedStudyError` and `load_study` loads an existing study,
  as `ledger.py`'s header already cites.

## What I ran, and the result

- `mise exec -- uv run --locked pytest`: **515 passed, 24 skipped**. All 10 named tests pass, and so do
  the 2 I added. The 24 skips are all paper tests skipped for "Alpaca paper credentials not in the
  environment", as expected.
- `ruff check .`: clean. One import-order finding in `ranking.py` was fixed with `--fix`.
- `ruff format --check .`: clean. `ty check`: clean.
- `scripts/canon lint`: clean, after the ADR edits.
- Not run:
  - `scripts/ci`: the brief says this copy lacks files it expects.
  - `/code-review` and the `evidence-verifier`: the brief allows no subagents. The repo's ADR 0017
    requires both before anything lands, so this change still needs that review. It touches the gate,
    so it is probably large enough to justify `/code-review ultra`, which only the owner can start.
- Nothing was committed or landed.

## What I am unsure of

- **Cache capacity.** The cache keeps at most `bar_capacity` bars of a type (10,000 by default). A run
  that replays more is now told `failed` ("cache holds 10000 … replayed N"); it is not ranked on bars
  it cannot account for. That is safe, but it means large intraday studies cannot be recorded until
  the engine's `CacheConfig(bar_capacity=…)` is raised. I did not change the cache config: it is not
  in the config key, and choosing a value is a decision for an ADR.
- **`iterations` counts only bars.** That holds here because the run loads bars and nothing else. I
  checked it on one run; I did not read it in the Rust source.
- **The consolidation hint.** The message says `consolidate_data` closes a hole only where no bar can
  fall. Consolidating also merges ranges across a hole that should have had bars, so this relies on
  the user reading the message. I did not verify whether `ensure_contiguous_files` would refuse a
  real gap.
- **Failed record from an earlier sample.** A keyed record that failed before its sample was written
  is read as on the current sample, whichever sample it came from. If its key has no other record on
  the current sample, the stop says "no record ran to the end" rather than "not run on the current
  sample". Either way the ranking stops.
