# Defect Review of Commit `e21935c1ada09318cdb92f929333b6295eca24c8` (`src/canon_trading/`, `tests/`)

Defects are numbered and ordered from most severe to least severe. All reproductions were run offline against `.venv` (`mise exec -- uv run --locked python`) using temporary directories outside the repository checkout.

---

### 1. Bars whose `ts_init` lands on exact UTC midnight mismatch `returns_series` day keys, corrupting `_session_sharpe` and causing `daily_series` / `rank` to reject valid backtests

1. **File and location**:
   - `src/canon_trading/ledger.py`, `session_days` (lines 180–190) and `_session_sharpe` (lines 279–284)
   - `src/canon_trading/returns_matrix.py`, `daily_series` (lines 82–89)
2. **What goes wrong**:
   When a bar's availability timestamp `ts_init` falls on an exact UTC midnight (`bar.ts_init % DAY_NS == 0`), NautilusTrader's daily portfolio analyzer (`result.returns_series`) attributes the `00:00:00Z` mark-to-market valuation to the **preceding** UTC day (`ts_init - DAY_NS`), whereas `session_days` buckets each bar into `bar.ts_init // DAY_NS * DAY_NS` (the new UTC day starting at that midnight). This causes two failures on real runs:
   - In `record_trial`, `_session_sharpe` (`values = [returns[day] for day in days if day in returns]`, line 281) silently omits the final session day (absent from `returns_series`), omits non-zero returns on days such as Mondays whose `00:00:00Z` close was recorded by `session_days` under Tuesday, and includes Saturday `0.0` carry-forward entries instead, telling Optuna `TrialState.COMPLETE` with a wrong per-session Sharpe ratio.
   - `daily_series` (and therefore `rank`) then raises `ReturnsRejected` (`"return ... on <date>, a day with no bar of the run"`), blocking `rank(study)` on valid backtests whose bar intervals close at `00:00:00Z` (for example, US extended-hours bars ending at `20:00 America/New_York` during EDT, or UTC daily/intraday bars aligned to `00:00 UTC`).
3. **Input, sequence of events, and how shown**:
   - Built a temporary `ParquetDataCatalog` using `canon_alpaca.providers.parse_equity` and `canon_alpaca.data.parse_bar` with 40 `AAPL.ALPACA-1-HOUR-LAST-EXTERNAL` weekday bars starting `2025-03-10 19:00 America/New_York` (`23:00 UTC` during EDT, so `parse_bar` sets `ts_event = 23:00 UTC` and `ts_init = 00:00 UTC` the next day, `bar.ts_init % DAY_NS == 0`) with the same 40 closes used in `tests/test_ledger.py`.
   - Ran `record_trial` for `MaCross(fast=3, slow=8)` and `MaCross(fast=4, slow=9)`, then called `rank(study)`:
     - `session_days` spanned `2025-03-11` to `2025-05-03`, while `result.returns_series` spanned `2025-03-11` to `2025-05-02` (leaving session day `2025-05-03` missing from `returns_series` and placing non-zero Monday mark-to-market returns on `2025-03-24`, `2025-03-31`, and `2025-04-28`, none of which were in `session_days`).
     - `record_trial` marked Trial #0 `TrialState.COMPLETE` with wrong `trial.value = 0.35970328563850207`.
     - `rank(study)` raised `RankingStopped` with `return 9.998000399913387e-05 on 2025-03-24, a day with no bar of the run (#0)`.

---

### 2. A traded record told `FAIL` for a non-finite full-run Sharpe ratio can pass `rank()` and win as `pick` after `returns_matrix` trims to a shorter common range

1. **File and location**:
   - `src/canon_trading/ranking.py`, `_ran` (lines 107–115, including docstring lines 110–112) and `rank` (lines 130–197)
2. **What goes wrong**:
   `_ran` returns `True` for a trial with `TrialState.FAIL` and `outcome == "traded"`, and its docstring (lines 110–112) claims: *"A traded record told `FAIL` because its own Sharpe ratio is not finite ran to the end too; the gate stops on it below, under ADR 0031, and no later record of its key resolves it."* However, `rank` never checks `trial.state` or the trial's full-series Sharpe ratio; lines 185–190 only check `sharpes` computed on `matrix` **after** `returns_matrix` trims every trial to the common `[first:last]` window. If a trial's early finite returns (outside the common range) overflow its full-run mean/standard deviation so `_session_sharpe` is `nan` and `record_trial` tells Optuna `TrialState.FAIL`, trimming away those early rows leaves finite returns with a finite Sharpe ratio on the common range. `rank` neither stops on that trial nor rejects it, and can return the `TrialState.FAIL` trial as the passing `pick`.
3. **Input, sequence of events, and how shown**:
   - In an Optuna SQLite study, recorded Trial #0 (`key="overflow_early"`, `outcome="traded"`, `TrialState.FAIL`) starting on day `0` with 32 finite returns `[1e308, 1e308, *PICK]` (verified `_session_sharpe` over days `0..31` is `nan`, while `math.isfinite(1e308)` is `True`), together with `"second"` (`SECOND` starting on day `2`) and `"third"` (`THIRD_CLOSE` starting on day `2`).
   - Ran `rank(study)`: `returns_matrix` trimmed all three trials to days `2..31`, where `"overflow_early"` had returns `PICK` (`sharpe = 0.69911`), and `rank(study)` succeeded with `RankingReport(pick='overflow_early', passed=True, dsr=0.988485067749776)` despite Trial #0 having `TrialState.FAIL`.

---

### 3. `rank` omits `"session_days"` and `"outcome"` when checking whether multiple finished records of one `config_key` agree

1. **File and location**:
   - `src/canon_trading/ranking.py`, `rank` (lines 147–164)
2. **What goes wrong**:
   When multiple records of the same `config_key` ran to the end (`ran`), `rank` checks agreement only on `keep = ("backtest_start", "backtest_end", "returns_series")` (line 148) and then inspects only `first = ran[0]`, ignoring both `"session_days"` and `"outcome"` (`OUTCOME`):
   - **`"session_days"` disagreement**: Because `catalog` path is omitted from `config_key`, two runs of the exact same configuration against two catalogs that differ by an interior bar on a day when the strategy is flat and no signal changes produce the exact same `config_key`, `backtest_start`, `backtest_end`, and `returns_series` (`0.0` on that day), but different `"session_days"`. `rank` fails to stop and silently uses `ran[0]`, making `T`, per-session Sharpe ratios, `V`, `DSR`, and calendar-mismatch errors depend on record order.
   - **`"outcome"` disagreement**: If one record of a key has `outcome == "idle"` (`0.0` returns) and another has `outcome == "traded"` with `0.0` net daily returns over the same `[backtest_start, backtest_end]`, `differ` is empty. If the `idle` record ran first, line 158 stops with `<key>: never traded (#0 (idle), #1 (traded))` (falsely stating `#1 (traded)` never traded); if the `traded` record ran first, line 158 does not stop on the `idle` record at all.
3. **Input, sequence of events, and how shown**:
   - **Catalog / `session_days` reproduction**: Wrote two real `ParquetDataCatalog` directories: `cat1` with all 40 bars from `tests/test_ledger.py`, and `cat2` omitting bar index 22 (when `MaCross(fast=3, slow=8)` is flat between bars 18 and 33). Ran `record_trial` on both in one study alongside a second configuration: both records had identical `config_key`, `backtest_start`, `backtest_end`, and 54-day `returns_series`, while `len(session_days)` was `40` vs `39` (`trial.value` `0.363142` vs `0.368286`), and `rank(study)` did not stop on their disagreement.
   - **`outcome` reproduction**: Wrote two records for the same key over days `0..29` with returns `[0.0] * 30`, one with `outcome="idle"` and one with `outcome="traded"`: `differ` was empty; with `idle` first `rank` reported `never traded (#0 (idle), #1 (traded))`, and with `traded` first `rank` bypassed the `idle` check completely.

---

### 4. Floating-point overshoot `rho > 1.0` in `skfolio.measures.correlation` yields `n_hat < 1.0` and crashes `rank()` with `ValueError`

1. **File and location**:
   - `src/canon_trading/ranking.py`, `rank` (lines 218–225)
2. **What goes wrong**:
   When `t >= n * (n - 1) / 2`, `rank` computes `correlations = measures.correlation(matrix.to_numpy())`, averages the off-diagonal entries into `rho`, and passes `n_hat = float(rho + (1 - rho) * n)` to `measures.expected_max_sharpe_ratio(n_hat, ...)`. `skfolio.measures.correlation` divides covariance by `np.outer(std, std)` without clamping to `[-1.0, 1.0]`. When trials have collinear returns with positive slope and distinct non-zero offsets (`V > 0`, so the DSR gate itself succeeds), IEEE-754 rounding can produce `rho = 1.0000000000000002 > 1.0` and therefore `n_hat < 1.0`. `measures.expected_max_sharpe_ratio` raises `ValueError: n_trials must be at least 1`, crashing `rank()` on a diagnostic quantity explicitly marked `"not used in the gate"`.
3. **Input, sequence of events, and how shown**:
   - In an Optuna SQLite study with `N = 3`, `T = 30`, wrote three traded trials: `"a"` with `PICK`, `"b"` with `[1.026 * x - 0.0033 for x in PICK]`, and `"c"` with `[1.026 * 0.71 * x - 0.0066 for x in PICK]`.
   - Ran `rank(study)`: `dsr` succeeded, `rho` evaluated to `1.0000000000000002`, `n_hat` evaluated to `0.9999999999999996`, and line 223 raised an unhandled `ValueError: n_trials must be at least 1, got 0.9999999999999996`.

---

### 5. `daily_series` and `_session_sharpe` silently drop session days absent from `returns_series`

1. **File and location**:
   - `src/canon_trading/returns_matrix.py`, `daily_series` (lines 69–97)
   - `src/canon_trading/ledger.py`, `_session_sharpe` (lines 279–284)
2. **What goes wrong**:
   - `daily_series` checks `itertools.pairwise(keys)` for interior gaps among the keys present in `returns` and checks keys outside `open_days` for non-zero returns, but never checks that every day in `session_days` (`open_days`) is present in `returns` (`open_days <= set(keys)`), raising only when zero session days match (`if not kept:`). If `returns_series` starts after the first session day or ends before the last session day in `session_days` (while remaining consecutive in between), `daily_series` silently drops the missing leading or trailing session days instead of raising `ReturnsRejected`, causing `returns_matrix` to silently trim those days from all trials (`T` reduced).
   - `_session_sharpe` similarly takes `[returns[day] for day in days if day in returns]` without checking either that all `days` exist in `returns` or that `returns` satisfies `daily_series`'s shape invariants (so an entry with missing session days, interior gaps, or non-zero closed-day returns can still be told `TrialState.COMPLETE` with a finite Sharpe ratio).
3. **Input, sequence of events, and how shown**:
   - Ran `daily_series("a", {str(DAY_0 + n * DAY_NS): 0.01 * (n + 1) for n in range(2, 6)}, [str(DAY_0 + n * DAY_NS) for n in range(6)])` where `session_days` has 6 session days (`0..5`) and `returns` only covers days `2..5`: `daily_series` succeeded and returned a 4-row `Series` starting `2025-03-13`, silently dropping session days `0` and `1`. Joined with a second valid 6-day trial in `returns_matrix`, the matrix silently shrank to `T = 4` (`days 2..5`).

---

### 6. `_canonical` on `dict` silently collides stringified keys, depends on insertion order, and lets `NaN`/`Inf` keys bypass `allow_nan=False`

1. **File and location**:
   - `src/canon_trading/ledger.py`, `_canonical` (line 108)
2. **What goes wrong**:
   Line 108 converts dicts via `{str(_canonical(key)): _canonical(item) for key, item in value.items()}`:
   - Distinct keys whose canonical representations stringify identically (such as `1`, `"1"`, and `Decimal("1")`; `True` and `"True"`; `None` and `"None"`; or `InstrumentId.from_str("AAPL.ALPACA")` and `"AAPL.ALPACA"`) silently overwrite one another in iteration order. Consequently, two equal Python dicts `{1: "int_val", "1": "str_val"}` and `{"1": "str_val", 1: "int_val"}` produce **different** `config_key` digests, while two unequal dicts `{1: "int_val", "1": "str_val"}` and `{"1": "str_val"}` produce the **same** `config_key`.
   - Because `float` keys pass through `_canonical` and are then passed to `str(...)`, `{float("nan"): 1}` and `{float("inf"): 1}` become `{"nan": 1}` and `{"inf": 1}`, bypassing `canonical_json`'s `allow_nan=False` guard instead of raising.
3. **Input, sequence of events, and how shown**:
   - Ran in Python: for `d1 = {1: "int_val", "1": "str_val"}` and `d2 = {"1": "str_val", 1: "int_val"}`, `d1 == d2` is `True`, yet `canonical_json(_canonical(d1))` returned `'{"1":"str_val"}'` while `canonical_json(_canonical(d2))` returned `'{"1":"int_val"}'` (`config_key` differed, and `config_key(_canonical(d1)) == config_key(_canonical({"1": "str_val"}))` was `True`).
   - Calling `canonical_json(_canonical({float("nan"): 1}))` returned `'{"nan":1}'` without raising `ValueError`.

---

### 7. `_canonical` mishandles Python `Enum`, `IntEnum`, and `StrEnum` against ADR 0035 term 4 ("enums by name")

1. **File and location**:
   - `src/canon_trading/ledger.py`, `_canonical` (lines 99–120)
2. **What goes wrong**:
   `_canonical`'s docstring states that it returns `value` *"in the canonical form of ADR 0035 term 4, or `TypeError` if it has no stable one"*, where ADR 0035 term 4 specifies *"enums by name"*. Only PyO3 enums under `nautilus_trader.*` reach line 109 and print by name; standard library `enum.Enum` raises `TypeError` (because `type(value).__module__` is not `"nautilus_trader..."`), while `enum.IntEnum` and `enum.StrEnum` match `isinstance(value, bool | int | float | str)` on line 101 and serialize by **value** (e.g. `1` or `"live"`) rather than by **name** (`"FAST"` or `"LIVE"`), colliding with raw ints and strings.
3. **Input, sequence of events, and how shown**:
   - Defined `class Color(enum.Enum): RED = 1`, `class Speed(enum.IntEnum): FAST = 1`, and `class Mode(enum.StrEnum): LIVE = "live"`.
   - Ran `_canonical` and `canonical_json`: `_canonical(Color.RED)` raised `TypeError: Color <Color.RED: 1> has no stable text form for a config key`, while `canonical_json({"s": _canonical(Speed.FAST), "m": _canonical(Mode.LIVE)})` produced `'{"m":"live","s":1}'` instead of `'{"m":"LIVE","s":"FAST"}'`.

---

### 8. `_is_method` / `_properties` silently skip class-level callable config attributes instead of refusing them

1. **File and location**:
   - `src/canon_trading/ledger.py`, `_is_method` (lines 123–128) and `_properties` (lines 131–143)
2. **What goes wrong**:
   `_properties`'s docstring (lines 134–135) states: *"Methods are skipped. A field or property whose value is callable has no stable text form and is refused by `_canonical`, not skipped."* However, `_is_method(type(obj), name)` treats any non-type callable attribute on `type(obj)` as a method (`callable(attribute) and not isinstance(attribute, type)`). When a `StrategyConfig` subclass defines a class-level callable field or default (for example `sizer: Any = len`, or a callable object instance not stored in `obj.__dict__`), `"sizer"` is absent from `own` (`obj.__dict__`) and excluded from `names` by `_is_method`, so `_properties` silently omits `"sizer"` instead of passing it to `_canonical` to raise `TypeError`. `tests/test_ledger.py` (`test_a_callable_config_field_is_refused`, line 379) missed this because it used `setattr(config, "sizer", len)` on the instance `__dict__`.
3. **Input, sequence of events, and how shown**:
   - Defined `class SubConfig(MaCrossConfig): sizer: Any = len` and instantiated `sub = SubConfig(bar_type=BAR_TYPE, trade_size=Quantity.from_int(10))`.
   - Verified `sub.sizer is len`, yet `_properties(sub)` succeeded without raising `TypeError` and returned a dictionary with `"sizer" not in _properties(sub)`.

---

### 9. `returns_matrix` uses positional index endpoints instead of min/max and checks duplicate `Series.name` values before `str()` coercion

1. **File and location**:
   - `src/canon_trading/returns_matrix.py`, `returns_matrix` (lines 102–116)
2. **What goes wrong**:
   - **Unsorted `DatetimeIndex` handling** (lines 109–116): `Population.returns_df` sorts `joined`'s index chronologically, but lines 109–110 take `first = max(s.index[0] for s in series)` and `last = min(s.index[-1] for s in series)` by positional index rather than `s.index.min()` and `s.index.max()`. If a caller passes a `pd.Series` whose `DatetimeIndex` is not sorted ascending, `returns_matrix` either falsely raises `ReturnsRejected("the trials have no day in common...")` on identical date sets or silently drops valid dates outside `[s.index[0], s.index[-1]]`.
   - **Name collision after `str(s.name)`** (lines 102–107): Line 103 checks `len(set(names)) != len(names)` on raw `[s.name for s in series]`, then line 106 passes `name=str(s.name)` to `Portfolio`. Two series with distinct Python names that stringify identically (e.g. `1` and `"1"`, or `None` and `"None"`) pass line 103's check, and `Population.returns_df` silently renames the duplicate column to `'1_1'` instead of raising `ValueError`.
3. **Input, sequence of events, and how shown**:
   - Passed `u1 = pd.Series([0.03, 0.01, 0.02], index=pd.to_datetime([day2, day0, day1], unit="ns", utc=True), name="u1")` and `u2` (sorted `[day0, day1, day2]`) to `returns_matrix([u1, u2])`: raised `ReturnsRejected: the trials have no day in common: the latest first day is 2025-03-13 and the earliest last day 2025-03-12`. With `u3` indexed `[day1, day0, day2]`, `returns_matrix([u3, u2])` silently dropped `day0` (`2025-03-11`).
   - Passed two `pd.Series` with `name=1` and `name="1"` to `returns_matrix`: returned a `(2, 2)` `DataFrame` with columns `['1', '1_1']` instead of raising `ValueError`.

---

### 10. `record_trial` writes `OUTCOME` and calls `study.tell` outside `try...except` and writes `OUTCOME` before `tell`, falsifying docstrings and producing `#0 (traded)` on a `RUNNING` trial

1. **File and location**:
   - `src/canon_trading/ledger.py`, `record_trial` (docstring lines 216–219 and code lines 237–263)
   - `src/canon_trading/ranking.py`, `_describe` (lines 103–104) and `_ran` (docstring lines 110–111)
2. **What goes wrong**:
   - `record_trial`'s docstring states (lines 216–219): *"A run that raises, or whose result cannot be recorded, is told `FAIL` with outcome `failed` and its error, and the exception passes through. ... The outcome is written last, so a record that has one ran to the end."* `ranking.py` (lines 110–111) repeats: *"The ledger writes the outcome last, so a record that has one of those finished its run."*
   - Both statements are false:
     1. Failed runs also write `OUTCOME = "failed"` (`_tell_failed`, line 271) despite not running to the end.
     2. `trial.set_user_attr("config", ...)` / `trial.set_user_attr("config_key", ...)` (lines 238–239) and `trial.set_user_attr(OUTCOME, outcome)` / `study.tell(...)` (lines 259–263) sit **outside** `try ... except BaseException` (lines 240–258), and `OUTCOME` (line 259) is written **before** `study.tell(...)` (lines 261/263), not last.
   - If an exception or `KeyboardInterrupt` occurs on lines 259–263, `_tell_failed` is never called; if `study.tell(...)` fails or is interrupted after line 259 commits `OUTCOME = "traded"` (or `"idle"`), the trial remains `TrialState.RUNNING` with `user_attrs["outcome"] == "traded"`. In `rank`, `_ran(trial)` returns `False` (`trial.state.is_finished()` is `False`) while `_describe(trial)` formats `trial.user_attrs.get(OUTCOME, ...)` first, raising the self-contradictory error `<key>: no record ran to the end (#0 (traded))`.
3. **Input, sequence of events, and how shown**:
   - Simulated an interruption between `ledger.py` line 259 and line 261 on an Optuna SQLite study (`trial = study.ask()`, `trial.set_user_attr("config_key", "interrupted_key")`, `trial.set_user_attr(OUTCOME, TRADED)` with no `study.tell`): calling `rank(study)` raised `RankingStopped` with `* interrupted_key: no record ran to the end (#0 (traded))`.

---

### 11. Assertions in `tests/test_ledger.py` use `sessions & set(returns)` and `if day in returns`, allowing tests to pass when session days are missing from `returns_series`

1. **File and location**:
   - `tests/test_ledger.py`, `test_the_recorded_series_is_the_shape_the_returns_matrix_accepts` (line 201) and `test_a_trial_round_trips_through_the_sqlite_file` (line 184)
2. **What goes wrong**:
   - In `test_the_recorded_series_is_the_shape_the_returns_matrix_accepts` (whose docstring claims *"NautilusTrader's own daily series, on a real run, passes ADR 0010 step 2 and loses only zeros"*), line 201 asserts `assert len(series) == len(sessions & set(returns))`. Because `daily_series` constructs `series` from `[key for key in keys if key in open_days]` (the exact set intersection `sessions & set(returns)`), `len(series) == len(sessions & set(returns))` is a tautology whenever `daily_series` returns without raising: if `returns_series` omitted 39 of 40 `session_days`, the assertion would still pass (`1 == 1`) instead of failing `len(series) == len(sessions)` (`sessions <= set(returns)`).
   - Similarly, line 184 in `test_a_trial_round_trips_through_the_sqlite_file` computes `on_session_days = [returns[day] for day in trial.user_attrs["session_days"] if day in returns]`, copying `_session_sharpe`'s `if day in returns` filter so that missing session days in `returns_series` are ignored by the test as well.
3. **Input, sequence of events, and how shown**:
   - Verified with `sessions = {str(DAY_0 + n * DAY_NS) for n in range(6)}` and `returns = {str(DAY_0 + n * DAY_NS): 0.01 * (n + 1) for n in range(2, 6)}` (missing 2 of 6 session days): `series = daily_series("t", returns, sorted(sessions))` has `len(series) == 4 != len(sessions) == 6`, yet `len(series) == len(sessions & set(returns))` evaluates to `4 == 4` (`True`).

---

### 12. False docstring statement on `run_backtest` in `src/canon_trading/backtest.py`

1. **File and location**:
   - `src/canon_trading/backtest.py`, `run_backtest` (line 206)
2. **What goes wrong**:
   Line 206 states: *"A run meant as a trial goes through `canon_trading.ledger.record_trial`, which calls this."* That statement is false: `canon_trading.ledger` never imports or calls `run_backtest`; it imports `backtest_run_config` and `run_backtest_config` (lines 49–50 of `src/canon_trading/ledger.py`) and calls those directly (lines 223, 227, and 241).
3. **Input, sequence of events, and how shown**:
   - Inspected `src/canon_trading/ledger.py` lines 49–50 and 222–241 (`run_backtest` does not appear anywhere in `src/canon_trading/ledger.py` other than docstrings).
