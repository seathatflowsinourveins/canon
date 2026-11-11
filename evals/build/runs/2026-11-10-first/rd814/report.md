# Study integrity repair

All five defects are fixed in the working tree. The full offline suite passes, including every
supplied regression, and all three required code checks pass. No existing test file was edited and
no dependency or lockfile changed.

The ledger previously queried the catalog after replay to identify the run. Replacing that catalog
could assign new bars' digests and session days to old returns. `BacktestRun` now carries the bars
read from the engine's cache before disposal, and `record_trial` sorts those bars in ADR 0041's
canonical order for both the digest and session days. Before writing result attributes, it requires
the cached count to equal `BacktestResult.iterations`, every timestamp to lie inside the inclusive
sample, and every bar type to belong to the run configuration. Violations raise inside the existing
ask/tell failure handler, leaving a failed trial without a misleading data digest. The unused
post-replay catalog reader was removed.

Catalog validation previously accepted any overlap with the sample. `backtest_run_config` now asks
`ParquetDataCatalog.get_missing_intervals_for_request` for gaps across the entire requested range
of each bar type, and refuses missing coverage before `study.ask()`. Diagnostics identify the bar
type and missing UTC intervals, preserving nanosecond precision. They also name
`ParquetDataCatalog.consolidate_data` as the explicit remedy for an intervening gap a load found
empty. The existing refusal of an inverted range with an omitted bound remains intact.

Ranking previously filtered records by sample before considering unresolved failures, dropping a
keyed failure whose sample write failed. Ranking now validates sample numbers against the study's
sample history, accepting only actual integers, and keeps records without a valid sample and without
run evidence in the unresolved records for their key. A later completed run of that key can resolve
them. A record showing a run with an absent, malformed, or unknown sample number stops ranking and
is named, even if another record of its key is valid. N still counts distinct keys across all records
and samples.

Study creation previously committed the name before the initial sample, leaving an interrupted
creation impossible to open or recreate. Under the existing exclusive creation lock, a duplicate
name is now loaded and completed only when it is a single-objective maximizing study with no
attributes and no trials. Initialized, occupied, malformed, minimizing, and multiobjective studies
remain duplicates. Opening an unfinished creation stays read-only and gives the actionable message
that `create_ledger_study` completes it.

Daily-series validation previously intersected session days with return keys, silently dropping
sessions outside the series. It now refuses any session day without a return before dropping closed
days, naming the series, number missing, and earliest missing date. The ranking includes each trial
number when gathering these refusals.

The changes follow ADR 0010's whole-sample evaluation, ADR 0035's Optuna ask/tell ledger and failure
resolution, and ADR 0041's sample and data identity contracts. The upstream APIs were checked locally
in the packages pinned by this checkout:

- NautilusTrader 2.0.0rc8: `backtest/__init__.pyi` for `BacktestNode.get_engine_cache` and
  `BacktestResult.iterations`; `common/__init__.pyi` and `Cache.bars`' docstring for cached bar access
  and ordering; `persistence/__init__.pyi` and the installed catalog docstrings for covered intervals,
  missing intervals, and consolidation. Real replay tests verify the cache/count relationship.
- Optuna 5.0.0: `optuna/study/study.py` for duplicate creation, loading by name, study directions,
  JSON user attributes, reading all trials in trial-number order, and ask/tell. Recovery uses these
  public APIs under the repository's existing `fcntl.flock` contract.
- The existing pandas/skfolio daily-return contract remains in place; the new validation enforces
  complete session coverage before constructing its series or computing statistics.

The module contract comments and ADRs 0035 and 0041 were corrected to describe the actual bar source,
coverage refusal, sample validation, and creation recovery. Seven additional cases in the new
`tests/test_study_integrity.py` protect occupied studies from recovery, exercise real cache eviction,
and check missing session returns at both ends of a series.

Validation ran with the checkout's locked environment:

| Command | Result |
| --- | --- |
| `mise exec -- uv run --locked pytest -q tests/test_ledger.py tests/test_ranking.py tests/test_returns_matrix.py` before changes | 14 failed, 110 passed; reproduced all supplied failures including parametrizations |
| `mise exec -- uv run --locked pytest -q --tb=short --show-capture=no tests/test_ledger.py tests/test_backtest.py tests/test_ranking.py tests/test_returns_matrix.py` after repairs | 148 passed |
| `mise exec -- uv run --locked pytest -q --tb=short --show-capture=no tests/test_study_integrity.py` | 7 passed |
| `mise exec -- uv run --locked pytest -q --tb=short --show-capture=no --ignore-glob='**/test_paper_*.py'` | 520 passed in 127.79 seconds |
| `mise exec -- uv run --locked ruff check .` | Passed |
| `mise exec -- uv run --locked ruff format --check .` | Passed; 112 files already formatted |
| `mise exec -- uv run --locked ty check` | Passed |
| `git diff --check` | Passed |

The final full suite includes the final versions of all added tests. Intermediate formatting and
test-annotation diagnostics were corrected before the final passing checks.

The cache remains bounded: its observed upstream default is 10,000 bars per bar type. A trial whose
cache evicts earlier bars now fails explicitly; this change does not add a separate replay recorder
or silently reconstruct missing bars from the catalog. An unrelated empty maximizing Optuna study
with no attributes or trials is indistinguishable from an interrupted creation; the recovery guard
therefore deliberately requires exactly that empty shape.

UNVERIFIED: paper-account behavior and upstream source beyond the installed bindings/docstrings and
the repository's dated evidence were not checked, as this task prohibits credentials and external
network research. Paper tests were excluded from collection. `scripts/ci`, independent agent review,
and `scripts/land` were not run under the task's restrictions. No requested work remains; the
uncommitted change is ready for the coordinator's review.
