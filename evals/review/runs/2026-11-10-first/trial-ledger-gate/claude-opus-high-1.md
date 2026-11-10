# Review of e21935c (trial ledger, returns matrix, deflated Sharpe gate): `src/canon_trading/`, `tests/`

Reviewer: Claude Opus 5.5, high effort. Commit read with `git diff e21935c^ e21935c`. Offline suite run with
`mise exec -- uv run --locked pytest tests -q`: 83 passed. Each probe below was a throwaway script in the
session scratchpad, run with `mise exec -- uv run --locked python <script>`. The checkout was not edited
and `git status` is clean.

## Defects, most severe first

### 1. The config key does not identify the data, and the ranking then reports a false disagreement

- **Where:** `src/canon_trading/ledger.py:71-73` (`_ENGINE_LEFT_OUT` and its comment "catalogs and
  streaming name places, not data") and `trial_config`, line 161 (`left_out=("catalog",)`). The effect
  shows up in `src/canon_trading/ranking.py`, `rank`, lines 148-157.
- **What goes wrong:** the key leaves out the data config's catalog and the engine's catalogs, and
  nothing else in the key stands for the bars. With `start` and `end` unset, which is how
  `backtest_run_config` always builds them, the key says nothing about which bars were replayed or over
  what range. So the same strategy over different data gets the same `config_key`:
  - two catalogs holding different bars;
  - a catalog refreshed with more days;
  - a catalog whose bars were corrected.

  `rank` then counts those runs as one configuration (N = 1, not 2), and stops with "records #0, #1
  ran the same configuration and disagree on their range or returns". That message is false: they did
  not run the same thing. ADR 0035 term 5 says records are never deleted, so a routine data refresh
  blocks every configuration in the study for good. The comment's claim that a catalog names "places,
  not data" is false, because the catalog decides which bars a run sees. ADR 0010 step 1 says to ledger
  each trial's "config, data range and returns". The returns are stored, but the key, which is what N
  counts, holds no data identity.
- **How shown (run):** I wrote two catalogs with `tests/test_ledger.py`'s `write_catalog`, the second
  with every other close raised by 2.0. I recorded `strategy()` once on each into one study, then ran
  `rank`. Output: `same key: True`, `same returns: False`, then
  `The ranking of study 's' (N = 1) stopped: * 7086718f…: records #0 (traded), #1 (traded) ran the same
  configuration and disagree on their range or returns`. `tests/test_ledger.py::test_the_catalog_path_does_not_change_the_key`
  covers only the case of identical contents, so it cannot catch this.
- **Caveat:** ADR 0035 term 4 itself decides to leave out the catalog, so this may be what was intended.
  Even so, the comment is false, the stop message is wrong, and N is undercounted.

### 2. `rank` crashes with `ValueError`, not `RankingStopped`, when the trials are almost perfectly correlated

- **Where:** `src/canon_trading/ranking.py`, `rank`, lines 218-228 (the N-hat block).
- **What goes wrong:** `n_hat = rho + (1 - rho) * n` comes out just below 1 when the mean correlation
  `rho` rounds to slightly above 1.0, which happens with floating point. skfolio's
  `expected_max_sharpe_ratio` raises `ValueError("n_trials must be at least 1")` for anything below 1.
  The gate has already computed and passed at that point, but nothing catches the error, so `rank`
  raises a bare `ValueError`. Its docstring promises a report or a `RankingStopped`, and the report is
  lost. N-hat is never meant to affect the gate (ADR 0010 step 3), yet here it stops the gate.
- **How shown (run):** I built a study with two trials, `PICK` and `PICK` shifted by a constant `s`.
  The Sharpe ratios differ, so V > 0, and the correlation is 1 up to rounding. With `s` = 0.002, 0.0007
  or 0.003, skfolio's correlation was `1.0000000000000002`, and `rank` raised
  `ValueError n_trials must be at least 1, got 0.9999999999999998`. With `s` = 0.001, 0.0005 or 0.0013
  the correlation was `0.9999999999999997` or `0.9999999999999998`, and `rank` returned a report. Any
  set of trials whose returns are affine copies of each other has this exposure; whether it happens
  depends on the rounding.

### 3. The test of the N-hat DSR passes for the wrong reasons

- **Where:** `tests/test_ranking.py::test_a_pick_above_the_threshold_passes`, line 156:
  `assert report.dsr_at_n_hat is not None and report.dsr_at_n_hat > report.dsr`.
- **What goes wrong:** this line is the only check on `dsr_at_n_hat`. Any benchmark below SR₀ passes
  it, so a wrong standard deviation, a wrong trial count or a dropped benchmark goes unnoticed. The
  module docstring and the commit's Test Plan say the picks are "worked with the standard library". N-hat
  is worked that way (2.7755), but the DSR at N-hat, which the report prints, is not.
- **How shown (run):** I patched skfolio in a probe and ran `rank` on the test's passing study
  (`PICK`, `SECOND`, `THIRD_CLOSE`). Each of the wrong computations below gives a DSR at N-hat above the
  DSR of 0.988485, so the assertion still holds for all three:

  | Computation | DSR at N-hat |
  |---|---|
  | As shipped | 0.990631 |
  | Population std passed to `expected_max_sharpe_ratio` | 0.994539 |
  | Half of N-hat passed | 0.999436 |
  | Benchmark ignored (benchmark 0) | 0.999676 |

### 4. Two ledger writes sit outside the failure handling, against the `record_trial` docstring (unsure; needs a storage error)

- **Where:** `src/canon_trading/ledger.py`, `record_trial`: lines 238-239 (`set_user_attr("config")` and
  `set_user_attr("config_key")`, after `study.ask()` but before the `try`) and lines 259-263
  (`set_user_attr(OUTCOME, outcome)` and `study.tell`, after the `try`).
- **What goes wrong:** the docstring (line 216) says "a run … whose result cannot be recorded, is told
  `FAIL` with outcome `failed`". A storage error in any of those four writes, for example SQLite
  "database is locked" with a second writer, skips `_tell_failed`. The trial then stays `RUNNING`:
  - If the error is at lines 238-239, the trial may have no `config_key`. `rank` then files it under
    `no key, trial #N`, a configuration of its own that no later run can resolve. That raises N by one
    and stops every later ranking of the study.
  - If the error is at lines 259-263, the trial keeps outcome `traded` but never gets a final state, and
    it carries no `error` attribute to say why.
- **How shown:** by reading the code only. I did not inject a storage fault.

## Checked and found sound

- Every field in the run config, venue, data and engine `Debug` output is either in `trial_config` or
  deliberately left out (catalog, logging, instance ID, streaming, catalogs). I compared the printed
  `Debug` of each against the dumped canonical config.
- The docstring numbers in `test_ranking.py` match a standard-library computation:

  | Quantity | Docstring | Computed |
  |---|---|---|
  | Skewness | −0.14482 | −0.144818 |
  | Kurtosis | 1.96139 | 1.961394 |
  | ρ | 0.11226 | 0.112259 |
  | N-hat | 2.7755 | 2.775481 |

- The calendar dates in the docstrings of `test_returns_matrix.py` and `test_ledger.py` are correct.
- skfolio's `variance` and `standard_deviation` default to the sample estimate (`biased=False`), which
  matches the ranking docstring.
