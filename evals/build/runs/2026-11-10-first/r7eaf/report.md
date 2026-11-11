# Report

## What was wrong

1. `record_trial` in [ledger.py](src/canon_trading/ledger.py) read the bars back from the
   catalog after the replay (`bars_read` / `ParquetDataCatalog.query`). A catalog replaced between
   the replay and that reading gave the old run the new bars' `data_digest` and `session_days`, so two
   runs over different bars could appear to have read the same data and rank. There was also no
   accounting that the bars used for the digest were the bars the run replayed: a short cache, or a
   bar outside the study's sample (`record_trial` bounds the run by that sample), went unnoticed.
2. `backtest_run_config` in [backtest.py](src/canon_trading/backtest.py) accepted a sample when any
   catalog interval overlapped it. Two configurations could therefore be recorded — and rank — on the
   fragment of the sample the catalog reached, including the case where the catalog reached only part
   of the sample on either side. A gap after one load and before the next (e.g. a weekend two loads do
   not record) could not be told from a missing stretch the catalog had never covered.
3. In [ranking.py](src/canon_trading/ranking.py), `rank` kept only records whose `sample` equalled the
   current sample number. A keyed `failed`/`RUNNING` record made before the `sample` write failed was
   therefore dropped rather than stopping its key until a later record of the same key ran to the end
   (ADR 0035 term 5, which attaches resolution to the key and not to the sample write). A record that
   had run to the end (`traded`/`idle`) with a `sample` that was missing, of the wrong type, a `bool`,
   or not a number of any sample the study has, was likewise reported as merely “not run on the
   current sample” instead of being named as a run with no valid sample number.
   `variant_study._to_record`, which claims the gate's resolution rule to decide what a pass records
   again, had the same sample-only filter, so such a key would not have been recorded again either.
4. `create_ledger_study` in [ledger.py](src/canon_trading/ledger.py) created the Optuna study first
   and wrote its `samples` attribute second. A process killed/interrupted in between left the name
   committed with no sample: `open_study` refused it as having no sample, and a second
   `create_ledger_study` refused it as Optuna's `DuplicatedStudyError`, so nothing short of editing
   the SQLite file could recover the name.
5. `daily_series` in [returns_matrix.py](src/canon_trading/returns_matrix.py) kept only return keys
   that fell on a `session_days` entry. A session day that had *no* return key at all was therefore
   simply absent from the built series, so a trial could rank on fewer rows (T) than the number of
   sessions it had recorded.

## What I changed and why

Production changes only in `src/canon_trading/`; one new test file; and corrections to the two ADR
terms the first change makes untrue. Nothing here lands: no `scripts/land`, commit or push was run, and
the change is left in the working tree.

### Bars from the run, not from a later catalog (defect 1)

- [backtest.py](src/canon_trading/backtest.py) — `BacktestRun` now carries `bars`: every bar the
  engine's cache holds for the run's bar types, sorted by bar type, `ts_init`, `ts_event`. They are
  collected in `run_backtest_config` from `node.get_engine_cache(config.id)` before the node is
  disposed, which is where the module already read orders, positions and the account, whose cache is
  emptied on disposal.
- [ledger.py](src/canon_trading/ledger.py) — `record_trial` now takes `session_days` and
  `data_digest` from sorted `run.bars` instead of the removed `bars_read` catalog re-read. Before any
  of that is written it checks the accounting: `len(run.bars)` must equal
  `result.iterations` (`BacktestResult.iterations`, the number of data elements the node replayed)
  and every `run.bars` entry must have `sample.start <= ts_init <= sample.end`. Otherwise it raises
  `RuntimeError` inside the existing `try` that tells the trial `FAIL` with `outcome=failed` and
  re-raises, so the record shows the failure and carries no `data_digest`. Message wording matches
  the two rejected cases: `the engine's cache holds N bars and the run replayed M` and
  `the run replayed N bars outside sample S`. Sorting defensively in the ledger as well keeps the
  digest input in the order `data_digest` documents even if `run.bars` arrives unsorted. The
  `session_days`/`data_digest` docstrings and the module header were updated to name the cache source.

  Sorting order matters and was verified by execution against a real catalog/run: `Cache.bars`
  returned the 40 bars newest-first, so the sort in both `backtest.py` and `ledger.py` restores the
  `ts_init`/`ts_event` order that `data_digest` documents and keeps the digest equal to the digest
  of the bars as originally written (proven by the existing named digest test passing).

### Full coverage of the sample (defect 2)

- [backtest.py](src/canon_trading/backtest.py) — when both bounds are given (which is exactly how
  `canon_trading.ledger.record_trial` passes the study's sample), `backtest_run_config` asks for
  `catalog.get_missing_intervals_for_request(start, end, NautilusDataType.Bar, bar_type)` and raises
  `LookupError` on the first missing interval before building the config, hence before a trial is
  asked for, naming the first missing instant: `The catalog at ... does not cover the sample:
  missing <first ISO instant> to <last> for <bar_type>`. When the missing stretch lies between
  covered intervals, the single-line message also says that *a stretch a load showed to hold no bar
  can be closed by consolidating the catalog (`ParquetDataCatalog.consolidate_data`)*. A missing head
  or tail gets no such hint, because consolidating cannot reach outside the catalog's own coverage.
  An `_iso_ns` helper formats the nanosecond boundary with the 9-digit fraction the pinned
  Parquet file-name/intervals use (needed for e.g. `...T00:00:00.000000001`, one nanosecond after a
  covered-interval end — the boundary of a real hole between two loads).

  Two pre-existing behaviours are deliberately untouched: a bar type with *no* intervals still yields
  `The catalog ... holds no <bar_type> bars`, and with a single bound given (or none) the old
  overlap check still yields `... holds no <bar_type> bars from <start> to <end>`. Both are still
  exercised by tests outside the named set (`test_a_catalog_without_the_bars_is_refused`,
  `test_one_bound_past_the_catalog_is_refused`, `test_a_call_refused_before_a_config_exists_is_not_a_trial`),
  and all pass. I verified the missing-interval behaviour by execution before writing the check: for
  a crafted holed catalog, `get_intervals` reported two intervals with a Sunday gap,
  `get_missing_intervals_for_request(2025-03-10, 2025-06-15)` returned the single missing interval
  `2025-05-04T00:00:00.000000001` → `2025-05-04T23:59:59.999999999`, and after
  `ParquetDataCatalog.consolidate_data(...)` the intervals merged into one covering interval.

### Keyed records that cannot be assigned to a sample (defect 3)

- [ranking.py](src/canon_trading/ranking.py) — added `_sample_number(trial, known)`: the record's
  `sample` attribute only if it is an `int` that is not a `bool` and is one of the study's sample
  numbers (`samples(study)`), otherwise `None`. `rank` now:
  - for each keyed record that ran to the end (`_ran`: finished, with `outcome` in `traded`/`idle`)
    with `_sample_number(...) is None`, gathers a stop formatted as
    `#<n> (traded) ran with sample <repr>, not a sample number of the study; ...` — i.e. named, and
    not resolvable by a later record, because there is no way to know which sample's bars the run
    read; such a key is not chosen for the matrix;
  - treats a keyed record that did *not* run to the end and has no valid sample number as belonging
    to its key: it joins the existing resolution check, so it stops the key with
    `<key>: no later record ran to the end after #<n> (failed|running)` until a later record of the
    key on the current sample ran to the end;
  - preserves every other existing message and behaviour, because records are still read only on
    the current sample for the matrix and the report.
- [variant_study.py](src/canon_trading/variant_study.py) — `_to_record` now counts such a record
  for its key exactly as the gate does (same `_sample_number`/`_ran` from ranking), keeping true
  the claim in its docstring that a pass re-records exactly the keys the gate would stop on.

### Interrupted study creation (defect 4)

- [ledger.py](src/canon_trading/ledger.py) — `create_ledger_study` catches
  `optuna.exceptions.DuplicatedStudyError`, loads the existing study, and re-raises unless the
  loaded study has no `samples` user attribute at all, in which case it writes the sample this call
  gives it and yields that study. This runs under the same creation `flock`. `samples()` now appends
  `; create_ledger_study completes it if a creation was interrupted before its sample` to the
  `LedgerNotFound` it raises, so `open_study` on such a study states both that it has no usable
  sample *and* the recovery, instead of leaving the name recoverable only by editing the file. A
  study that does have a `samples` attribute (including a malformed one) is not overwritten. The
  function docstring, the module header and ADR 0035 term 1 were updated to say precisely that.

### Session day with no return (defect 5)

- [returns_matrix.py](src/canon_trading/returns_matrix.py) — after the existing shape checks
  (off-midnight keys, gaps in the return series, non-finite returns, non-zero returns on closed
  days), `daily_series` computes `missing = session_days` not present among the return keys and
  raises `ReturnsRejected('<name>: no return on N of its S session days, the first <date>')` if that
  set is non-empty. Placing it after the gap check keeps the existing behaviour for a mid-series
  calendar hole in the returns themselves, which is still reported as a skip in the daily series.
  The module header now states this refusal. Because `ranking.rank` wraps `daily_series` failures
  with `(#<trial number>)`, `rank` and `returns_matrix` stop and name the same day from a single
  check.

### Records / documentation corrected

- [docs/adr/0035-trial-ledger-in-an-optuna-study.md](docs/adr/0035-trial-ledger-in-an-optuna-study.md),
  term 1 (interrupted creation is completed by the next create, opening names that completion) and
  term 3 (`session_days` now “read from the bars the run read, as the engine's cache holds them in
  `canon_trading.backtest`'s `BacktestRun.bars`” instead of “read from the catalog”).
- [docs/adr/0041-trial-ledger-sample-and-data-identity.md](docs/adr/0041-trial-ledger-sample-and-data-identity.md),
  term 1 (a sample the catalog does not *cover in full* is refused before a trial is asked for,
  naming the first missing instant, and an interior no-bar stretch can be closed by consolidation),
  term 2 (a session day with no return is a lack and is refused in the same way), term 3 (digest bars
  are read “from the engine's cache as `canon_trading.backtest` carries them in `BacktestRun.bars`”,
  and a run whose cache does not account for every replayed bar, or that replayed a bar outside the
  sample, is told `failed`), and term 5 (a keyed record with no sample number stops its key until a
  later record of the key ran to the end; a record that ran to the end with a `sample` that is not a
  number of the study stops the ranking, named). The ADR Evidence sections were left untouched, as
  they record what was read at the time.

I did **not** need to re-decide anything new or write a new ADR: each change is the mechanical
enforcement of a behaviour the three governing ADRs already state (sample identity, resolution by
key, same session days), implemented through an upstream-reported reading (cache / missing
intervals) that the ADRs' own evidence already lists.

## Which upstream API / contract the change follows

- NautilusTrader v2.0.0rc8 (pinned), installed stub
  `nautilus_trader/common/__init__.pyi`: `Cache.bars(bar_type) -> list[Bar] | None`, plus
  `BacktestNode.get_engine_cache(run_config_id) -> Cache` in `nautilus_trader/backtest/__init__.pyi`.
  `BacktestRun.bars` is populated from that cache before `node.dispose()`, in the same place the code
  already read `cache.orders()`, `cache.positions()`, `cache.position_snapshots()` and
  `cache.account_for_venue(...)`. The digest itself remains ADR 0041 term 3's local glue (one line
  `bar_type|open|high|low|close|volume|ts_event|ts_init` per bar, SHA-256 hex), unchanged.
- NautilusTrader v2.0.0rc8, `nautilus_trader/persistence/__init__.pyi`:
  `ParquetDataCatalog.get_intervals(data_type, identifier)` (already used) and
  `ParquetDataCatalog.get_missing_intervals_for_request(start, end, data_type, identifier)` —
  the same call [bars_to_catalog.py](src/canon_trading/bars_to_catalog.py) already uses to detect a
  load whose bars did not all reach the catalog — plus the documented `ParquetDataCatalog.consolidate_data(...)`
  cited in the hole error.
- `BacktestResult.iterations` (installed stub, `nautilus_trader/backtest/__init__.pyi`) as the count
  of data elements the node replayed — the same counter existing tests already assert against
  (`test_backtest.py` “the engine replayed every bar”, ledger “the node replayed both bars at the
  bounds”) — used to account for the cache's bars.
- Optuna 5.0.0 (pinned): `optuna.create_study` without `load_if_exists` raising
  `optuna.exceptions.DuplicatedStudyError`, and `optuna.load_study`, per ADR 0035's existing
  contract notes and `optuna/study/study.py` in the installed package. Local adaptation: a
  `DuplicatedStudyError` whose loaded study has no `samples` attribute is completed, which no
  upstream offers and which ADR 0035 term 1 (as corrected) now records.
- pandas/skfolio usage in `returns_matrix.py` and `ranking.py` is unchanged; the new refusal in
  `daily_series` is this repo's own ADR 0010 step 2 / ADR 0041 term 2 rule, and no new dependency
  was added anywhere (`uv.lock` untouched).

## What I ran, and its result

All commands used the checkout's offline lock (`mise exec -- uv run --locked ...`). Paper tests were
not run and no credential or network access was used, per the task.

- The ten named failing tests (14 collected cases, first run before any edit, `-x` then full set):
  before the change, all 14 cases failed (digest from the later catalog, both partial-coverage cases,
  the mistyped-year case, the keyed-failure case, the interrupted-creation case, both cache-accounting
  cases, the hole case, all three invalid-sample ranking cases, session-days-with-no-return in the
  ranking, and the session-day-with-no-return in `returns_matrix`). After the change:
  `14 passed in 6.71s`.
- Full offline test suite:
  `mise exec -- uv run --locked pytest --ignore=tests/test_paper_bars_to_catalog.py --ignore=tests/test_paper_ema_cross.py --ignore=tests/test_paper_variant_study_bars.py -q`
  (the three `tests/test_paper_*.py` files need a key and were excluded on the task's instruction):
  **514 passed, 21 skipped in 94.24s**. I did not run `scripts/ci`, which the task notes expects
  files this copy omits, and I ran no test under `mise run paper:*`.
- Code checks, each run after the final edit:
  `mise exec -- uv run --locked ruff check .` — All checks passed!
  `mise exec -- uv run --locked ruff format --check .` — 111→112 files already formatted, all passed
  `mise exec -- uv run --locked ty check` — All checks passed!
- Two additional ad-hoc real-run probes (written to `/tmp`, since deleted) verified against real
  catalogs and a real backtest node: cache bars for a 40-bar run (40 bars, newest-first, hence the
  sort) and the holed-catalog interval/missing-interval/consolidation behaviour quoted above. That
  evidence is reproduced inside the test suite by the named digest/cache/hole tests; it is not left
  in the working tree (`git status` shows only the files listed below as modified/untracked).

New test added (existing files untouched, as instructed):

- [tests/test_variant_study_recovery.py](tests/test_variant_study_recovery.py) — one test, written in
  the ledger's own record form with no backtest run, showing that a pass re-records a key whose
  latest relevant record failed before its sample was written, and stops re-recording it once a
  later record of that key has run to the end. This is the `variant_study._to_record` half of defect
  3, which the ten named tests exercise only through the gate. It passed standalone (1 passed) and
  is included in the 514 passed above. The other five defects were already covered by the ten named
  tests that live in `tests/test_ledger.py`, `tests/test_ranking.py` and
  `tests/test_returns_matrix.py`, which I did not edit.

Modified files:

- [src/canon_trading/backtest.py](src/canon_trading/backtest.py)
- [src/canon_trading/ledger.py](src/canon_trading/ledger.py)
- [src/canon_trading/ranking.py](src/canon_trading/ranking.py)
- [src/canon_trading/returns_matrix.py](src/canon_trading/returns_matrix.py)
- [src/canon_trading/variant_study.py](src/canon_trading/variant_study.py)
- [docs/adr/0035-trial-ledger-in-an-optuna-study.md](docs/adr/0035-trial-ledger-in-an-optuna-study.md)
- [docs/adr/0041-trial-ledger-sample-and-data-identity.md](docs/adr/0041-trial-ledger-sample-and-data-identity.md)
- Added: [tests/test_variant_study_recovery.py](tests/test_variant_study_recovery.py)

## Anything I am unsure of

- `scripts/ci` was not run — the task states it expects files this checkout copy omits, so a failure
  there may not be attributable to this change; the graded gates (`pytest`, `ruff check`,
  `ruff format --check`, `ty check`, all offline/locked) all ran and passed.
- The three-way paper test files were excluded rather than run, on the task's instruction, so no
  statement is made about a live `LiveNode` catalog load (`load_bars`, ADR 0040) other than that the
  offline mocked/real-catalog tests pass, including `consolidate_data` on real Parquet files.
- The 21 `skipped` entries reported by the full offline run come from skip-conditions in the
  collected workspace suite (the workspace also collects the adapter package's tests); they were
  skipped conditions, not failures, but I did not run them with `-rs`, so I am not listing their
  individual reasons here.
- Judgment call, stated plainly: I amended the *normative terms* of ADR 0035 and ADR 0041 where the
  cache-based reading and the full-coverage refusal make the old wording untrue, and left their
  dated `Evidence` sections as written, because those sections record what was read at the time.
  If the repo's convention for such a pin-level mechanism change is instead a new superseding ADR,
  an `evidence-verifier`/`new-adr` pass should supersede those terms rather than edit them in place;
  the code behaviour itself is unchanged either way.
- Judgment call on scope: `get_missing_intervals_for_request` is only consulted when *both* sample
  bounds are given, which is the ADR 0041 `record_trial` path. A direct caller of
  `backtest_run_config` with only one bound keeps the previous overlap behaviour (“holds no … bars
  from …”), since a single-bound run has no fixed sample to cover in full.
