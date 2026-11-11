# Report: five gate defects in `src/canon_trading/`

## What was wrong

1. **Digest of a later catalog.** After the replay, `record_trial` read the bars again from the catalog
   (`bars_read`, `ParquetDataCatalog.query`) to get `session_days` and `data_digest`. If the catalog was
   replaced in between, the record got the new bars' digest, so runs on different bars agreed. Nothing
   checked that the bars it hashed were the ones the run had replayed.
2. **Any overlap accepted.** `backtest_run_config` refused a sample only when no stored interval
   overlapped it at all. A catalog covering only part of the sample, or with a hole in it, was accepted.
   (A sample with no overlap at all was refused, but with a message that did not say what was missing.)
3. **Keyed failure with no sample.** The ranking kept only records whose `sample` equalled the current
   number. A record told `FAIL` after its key was written but before its `sample` was therefore never
   seen, so it could not stop the ranking. A record that *ran* with a `sample` that is not one of the
   study's numbers (7, `"0"`, `True`) was quietly ignored as well.
4. **Half-made study.** `create_ledger_study` writes the study (`create_study`) and its `samples`
   attribute in two separate writes. An interrupt between them left a name that `open_study` refused
   ("no sample") and `create_study` refused too (`DuplicatedStudyError`).
5. **Session day with no return.** `daily_series` kept only the returns that fell on session days.
   A session day with no return just disappeared from the calendar, so the trial ranked on fewer days
   than it had sessions.

## What I changed, and why that way

- **`backtest.py`**
  - `BacktestRun` has a new field, `bars`: every bar of the run's bar types that the engine's cache
    holds, read with `Cache.bars` before the node is disposed. The cache returns the latest bar
    first, so the list is reversed to oldest first. These are the bars the run actually replayed,
    held in memory, so a later change to the catalog cannot touch them.
  - `backtest_run_config`, when given both `start` and `end`, now asks upstream
    `ParquetDataCatalog.get_missing_intervals_for_request` for the missing parts of the sample. It
    refuses the sample if any part is missing, naming each gap to the nanosecond: "does not cover the
    sample: missing … to …". The message also says that `ParquetDataCatalog.consolidate_data` closes a
    stretch a load showed to hold no bar. With only one bound, the old overlap check stays:
    `tests/test_backtest.py` records that upstream returns nothing for an inverted range, so that case
    cannot go through the upstream check.
  - The module header names the new upstream calls. A new known limit: the cache keeps at most
    `CacheConfig.bar_capacity` bars of a bar type (10,000 by default).
- **`ledger.py`**
  - `bars_read`, which read the catalog again, is replaced by `bars_replayed(run, bar_types, sample)`.
    It raises `RuntimeError` if any cached bar is:
    - of a bar type the run does not load;
    - stamped with a `ts_init` outside the sample;
    - or if the cached bar count differs from `BacktestResult.iterations`.
  - This check runs inside the `try` that tells a failure, before anything about the run is written.
    So a run whose bars cannot all be accounted for is told `FAIL`/`failed`, with no `data_digest`.
    Session days and the digest now come from these bars.
  - `create_ledger_study`: on `DuplicatedStudyError` it loads the study. If the study has no `samples`
    attribute and no trials, it completes it by writing the sample. Otherwise it raises the error again.
  - `samples()` now says, for a study with no `samples` attribute, that its creation was interrupted
    and that `create_ledger_study` completes it.
  - Optuna has no call that creates a study and sets its user attributes in one write, so I made the
    second creation finish the job rather than trying to make creation atomic.
- **`ranking.py`**
  - A keyed record is placed on a sample only if `sample` is an `int`, not a `bool`, and one of the
    study's sample numbers.
  - Otherwise, if it shows no run (`ran_something` is false), it is read as on the current sample. It
    then stops the ranking, through the existing "no later record ran to the end after …" stop, until a
    later record of its key runs to the end.
  - If it shows a run, it stops the ranking, named: "#n (traded) ran with sample …, not a sample
    number of the study", or "… ran with no sample number".
- **`returns_matrix.py`**: `daily_series` refuses a session day with no return, naming the trial,
  how many days lack a return, and the first of them.
- **Records.** I corrected text that the change made untrue, without changing any decision:
  - ADR 0041 term 1: "do not reach" becomes "do not cover in full".
  - ADR 0041 term 3: the bars are those the run replayed, from the cache, not a catalog query.
  - ADR 0035 term 3: session days come from those bars too.
  - Each ADR gets one Evidence line dated with this review, as `.claude/rules/adr.md` asks.
  - Module headers and docstrings are updated in all four modules.
- **New test**, `tests/test_ranking_sample_attribute.py`: a keyed record that ran with no `sample`
  attribute at all stops the ranking, named. The given tests do not reach that branch. No existing test
  file was edited.

## Upstream API or contract followed

- NautilusTrader v2.0.0rc8, read from the installed stubs (`bundled`):
  - `Cache.bars`, `Cache.bar_count` and `CacheConfig.bar_capacity` (`common/__init__.pyi`);
  - `BacktestResult.iterations` and `BacktestNode.get_engine_cache` (`backtest/__init__.pyi`);
  - `ParquetDataCatalog.get_missing_intervals_for_request`, `get_intervals` and `consolidate_data`
    (`persistence/__init__.pyi`).
- Behaviour I ran in this checkout (scratch scripts outside the tree):
  - The cache held all 25 bars of a 25-iteration bounded run, latest first. The bars print the same
    as the ones written.
  - `CacheConfig().bar_capacity` is 10,000.
  - `get_missing_intervals_for_request` named, as the tests expect:
    - the weekend hole between two loads;
    - the missing year before the catalog's bars;
    - the year after them;
    - a 2015 range the catalog does not reach at all.
  - `consolidate_data` merged two intervals across a hole into one.
- Optuna 5.0.0: `create_study` raises `DuplicatedStudyError` for a taken name, and `load_study` opens
  it. That is already the module's documented contract.
- ADR 0010, 0035 and 0041 are the contracts the module headers name.

## What I ran, with results

- Baseline, before the change: `mise exec -- uv run --locked pytest -q` gave 14 failed, 499 passed and
  24 skipped. The 14 are the 10 named tests with their parameter cases.
- After the change:
  - `mise exec -- uv run --locked pytest -q`: **514 passed, 24 skipped**, none failed. The 10 named
    tests (14 cases) pass when run alone.
  - `ruff check .`, `ruff format --check .` and `ty check` (each through `mise exec -- uv run --locked`):
    all clean.
  - `scripts/canon lint`: clean.
- Not run:
  - `scripts/ci`, because the brief says this copy lacks files it needs.
  - The paper tests.
  - `/code-review` and the `evidence-verifier`, which AGENTS.md requires before landing. Nothing lands
    here and the brief allows no other agents, so this change has had **no independent review**.
- Nothing is committed; the change is in the working tree.

## What I am unsure of

- **Cache capacity.** A run that replays more than 10,000 bars of one bar type (minute bars over a
  few weeks, for example) will now always be told failed: the cache holds fewer bars than were
  replayed. That is the safe outcome, but it makes long minute-bar studies impossible until
  someone decides to raise `CacheConfig.bar_capacity` in the engine config. I did not raise it. Its
  memory cost, and whether it should be part of the config key, are a decision for the owner.
- **`iterations` as a bar count.** Treating `iterations` as the number of bars replayed holds for runs
  that load only bars; I saw it on a one-bar-type run. I did not test a run with several bar types or
  instruments, so whether it counts every data point there is `UNVERIFIED`.
- **Completing a half-made study.** This trusts that a study with no `samples` attribute and no
  trials was left by this module. Another writer could create such a study, and it would then be
  completed with the caller's sample.
- **No-run record on an older sample.** A keyed record with no valid sample that shows no run is
  counted on the current sample even if it was recorded while an older sample was current. That
  errs toward stopping the ranking: it needs one more record of its key on the current sample, which
  a moved sample needs anyway.
- **ADR edits.** ADR 0041 terms 1 and 3 were reworded. I judged that a correction to what term 2
  already requires, not a new decision, but the `evidence-verifier` should confirm that before
  anything lands.
