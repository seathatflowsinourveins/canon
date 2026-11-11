---
status: accepted
date: 2026-11-10
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# Deflated Sharpe computation: eqs. 1–2 through skfolio, with the input it lets pass refused

## Context and Problem Statement

ADR 0010 ranks strategy variants and rejects the pick when its deflated Sharpe ratio (DSR) is below 0.95,
with N = all trials in the ledger. Which formula and conventions compute that DSR, which code runs them,
and what input must the ranking refuse before it does?

## Decision Drivers

* A comparison method that flatters a strategy is worse than none.
* ADR 0001: where upstream is silent, say so. Every statistic comes from a published source.

## Considered Options

1. Write our own selection statistics (ADR 0010, option 1).
2. NautilusTrader's Sharpe ratio, skewness and kurtosis.
3. skfolio's measures, under the paper's conventions.

## Decision Outcome

Chosen: **option 3**. Option 1 invents statistics. Option 2 is ruled out by the conventions below.

ADR 0010, step 3, **DSR of the pick** (eqs. 1–2):
SR₀ = √V·[(1−γ)Z⁻¹(1−1/N) + γZ⁻¹(1−1/(Ne))] and DSR = Z[(SR−SR₀)√(T−1) / √(1 − γ₃SR + (γ₄−1)/4·SR²)].

* SR and V are per session, not annualised. V is the variance of every trial's SR.
* γ₃ is the pick's skewness and γ₄ its raw kurtosis, which is 3 for a Normal distribution.
* The gate, at 0.95 with N = all trials, and the report of N̂ are ADR 0010's, step 3.
* Do not take Sharpe, skewness or kurtosis from NautilusTrader. Its default Sharpe is annualised, its
  skewness is bias-corrected, and its kurtosis is excess.
* Implementation: **skfolio v1.12.9**, `measures.deflated_sharpe_ratio(returns, trial_sharpe_ratios)`.
  It takes the pick's return series and every trial's Sharpe ratio, takes N from that array, and uses
  the sample standard deviation. For N̂, call `expected_max_sharpe_ratio` and then
  `probabilistic_sharpe_ratio`.
* skfolio raises nothing on three inputs that make a pick pass more easily. It leaves a NaN trial
  Sharpe ratio out of N; it ignores a NaN in the pick's returns; and for one trial, or trials with
  equal Sharpe ratios, SR₀ is 0. So the ranking stops unless every trial's Sharpe ratio and every
  return of the pick is finite, N ≥ 2 and V > 0. Until 2026-11-11, when the addition below
  replaced the rule: V counts as 0 when every trial's Sharpe ratio equals
  the first's under `math.isclose` at its documented default (`rel_tol` 1e-9): copies of one
  configuration differ only in rounding. That tolerance is a definition of floating-point noise chosen
  here; no statistical source gives one, and whether it is enough is UNVERIFIED. With `abs_tol` 0 no
  nonzero value is close to 0, so ratios within rounding of 0 are not counted equal; their pick's DSR
  is then near 0.5, below the gate.
* Two more stops. Keys tied at the highest Sharpe ratio under the same tolerance (until
  2026-11-11; term 3 below replaces it) are one pick when
  their returns are equal day by day under it, with `abs_tol` 1e-9 of the largest return so a 0 against
  rounding counts as equal (each still counts in N, and they give one DSR); tied
  keys whose returns differ leave no one pick and stop the ranking. A DSR skfolio gives as not finite
  stops it too. Each stop names its keys and trial numbers. N̂ below 1 (ρ̂ rounding above 1, its
  bound on p. 14) is raised to 1, skfolio's least; the DSR at N̂ is withheld when its benchmark is
  below 0, and the report says why. Neither enters the gate.

**One numeric check, and ratios compared by their own rounding (added 2026-11-11).** The gate's numeric family was found in
its rounds 5 (squares that overflow), 6 (moments that underflow) and 7, so under ADR 0073 rule 4 it is
one model, and it replaces the checks of those rounds and the two tolerances above.

1. **One check, in one place.** `canon_trading.returns_matrix.session_returns` drops the closed days
   and then computes, on exactly the series of session days the gate takes and by the functions the
   gate uses, everything the DSR reads: skfolio's Sharpe ratio, `variance`, `fourth_central_moment`,
   `skew`, `kurtosis`, and `correlation` of the series with itself. It refuses the series by name, in
   this order: with fewer than two session days; with no spread at its own size (term 2); with the
   square of its second central moment below float64's smallest normal, where the kurtosis rounds
   toward 1 or 0 or is NaN (round 6's floor); with any of the six not finite. It returns the series
   with its Sharpe ratio; the ranking and the value `record_trial` tells read both from that
   result, and nothing else checks a moment of a return series. Taken over every UTC day, the zeros
   of closed days hid session-day moments that overflow or underflow.
2. **No spread at its own size.** skfolio's `variance` is NumPy's `nanvar`, which subtracts the
   computed mean once and does not centre again, so n equal returns keep the mean's rounding error
   as every deviation: thirty of −0.01 have a standard deviation of 1.8e-18 and a Sharpe ratio of
   −5.67e15, where thirty of 0.07, whose mean comes out exact, have 0 and none. The measure, written
   once, is n·ulp(m), m the largest return in size and ulp float64's spacing there: n − 1 additions
   and a division, in any order, move the mean by less than it while the sum is finite. A constant
   series' sample standard deviation is, to first order, that error times √(n/(n−1)), at most √2 of
   it, so a series is refused when its standard deviation is at most 2 times the measure, 2 being
   the least whole number above √2. Equal returns, zeros among them, are refused as such before
   any statistic is read: at 1e200 the square of that error overflows. skfolio's own floor for
   skewness and kurtosis, NaN when the centred second moment is at most (eps·mean)², lies inside
   this one. Where a sum or a square overflows instead, the statistic is not finite and term 1
   refuses the series for that. The factor is a choice made here on that derivation; no
   statistical source gives one.
3. **Ratios compared by their own rounding.** A computed Sharpe ratio is known no more finely than
   rounding can move it. For a series that passed, two computations summing in any order differ by
   less than 13·r·max(1, |SR|), r being the measure over its standard deviation, under 1/2 (to
   first order; the derivation is in `canon_trading.returns_matrix`'s header); its bound is
   16·r·max(1, |SR|), 16 the next power of two. Two trials' ratios are told apart only when they
   differ by more than the sum of their two bounds: each carries its own rounding, and their
   difference carries both. Both rules below are judged on the set, against its highest ratio,
   never pair by pair, so no order of recording changes either; where several keys have the highest
   ratio, or the lowest, the widest of their bounds stands for it. The ranking takes the trials in
   the order of their keys, so V, N̂ and which of several keys with the same returns is named the
   pick are the same to the bit whatever the order of recording.
   * V counts as 0 when the highest and the lowest ratio are not told apart, read from the ratios
     because skfolio's variance of equal numbers can round above 0. The ranking also stops when
     that variance is not finite or not above 0.
   * The top group is every key whose ratio is not told apart from the highest. A key within
     rounding of a member of the group, but not of the highest, is not in it. Inside the group
     returns are compared exactly, day by day: the same on every day, the group is one pick with
     one DSR; with any day that differs the ranking stops, naming the group as ranking within
     rounding of each other on returns that differ.

   Rounding decided the result under the rule this replaces, and under the exact rule first built
   in its place. Within 1e-9, two series of mean 2⁻⁷
   in steps of 2⁻⁴¹, kurtosis 1.0 and 4.0, were one pick, passed at 1.0 recorded in one order and
   rejected at 0.9395 in the other. Compared exactly, one configuration and its copy at 3 times the
   scale, ratios one step apart, ranked with V = 1.2e-32 and passed at the undeflated 0.9997, and at
   7 times, the same ratio to the bit, stopped.
4. **A value told** is held to the same bound of the one computed again from the stored returns.
   No other tolerance is kept. The bound is about 2e-13 for thirty daily returns of a per cent, and
   2.1e-11 to 2.6e-11 for the eight step-7 records of 1,366 sessions, whose ratios are no closer
   than 1.6e-4.
5. **Limits, recorded once.** Trials whose ratios differ by more than rounding, but little else,
   give a small V and a pick near its undeflated value: thirty returns with one moved by 1e-9 have
   ratios 9e-10 apart, V = 4.4e-19 and a DSR at the undeflated 0.9997. That is what the statistic
   computes for trials that are nearly one. The report prints N̂ beside it, and the registration,
   read before any trial (ADR 0048), is where near-copies are seen. The 1e-9 rule stopped ratios
   up to 1e-9 of their size apart, so a return moved by 1e-12 to 1e-10, which stopped then, ranks
   now, as one moved by 1e-9 did then. A series next to term 2's threshold has a bound above its
   own ratio, so no trial is told apart from it and the ranking stops, naming it as the cause; one
   clear of it, a standard deviation more than 16 times the measure, ranks with the Sharpe ratio
   its returns give, up to about 2⁴⁹/n, and can pass, or with that ratio negative
   raises V until no pick passes; a value told for it is held only to its wide bound. The check
   separates spread from rounding; it does not judge whether a series is plausible.

Who decided, 2026-11-11. The lead decided the terms: one check with one call site; degeneracy relative
to the series' own size; no free tolerance; and, on reading the exact rules first built, that ratios
are equal within the rounding of their own computation, judged on the set, with returns in the top
group compared exactly. Chosen in building them, under the delegation below: the standard deviation
as what is compared, the factors 2 and 16, the sum of two bounds between two trials and the widest
bound at a shared extreme, the series' correlation with itself as the correlation input, the
order of the refusals, and the order of the keys for the columns. The lead also decided that this
record lands at its length, past the length at which the rule for records asks for a split: the
number 0080 is kept for one.

Who decided. Under the owner's delegation of 2026-11-10 (Evidence), the orchestrator chose skfolio.

### Consequences

* Good: the gate is specified down to the formula, and its implementation reproduces the paper's example.
* Bad: skfolio brings 13 packages for three functions, among them scikit-learn, scipy, cvxpy-base, clarabel
  and plotly. It has 130 releases since 2023-12-14, 27 of them since 2026-10-16.

### Confirmation

* `tests/test_deflated_sharpe.py` runs the DSR example on pp. 9–10 through skfolio: N = 100, V = 1/2,
  T = 1250, γ₃ = −3, γ₄ = 10, SR = 2.5, with V and SR annualised at 250 periods per year. It passed on
  Python 3.14.10 on 2026-11-10 with these results:
  * SR₀ ≈ 0.1132 and DSR ≈ 0.9004;
  * DSR = 0.9505 at N = 46;
  * DSR = 0.9505 at N = 88, with γ₃ = 0 and γ₄ = 3.

  skfolio takes a return series, not moments, so the test builds series that carry them. γ₃ = −3 with
  γ₄ = 10 is only reachable by a two-valued series, and 1250 observations give −2.9994 and 9.9965.
  It also works eqs. 1–2 without skfolio, under the conventions read in skfolio's source, and gets the
  same values. And it pins what skfolio does with input ADR 0010's protocol forbids: the three cases above,
  and annualised trial Sharpe ratios beside daily returns, which send a passing pick below 0.01.
* That test covers skfolio. The code that applies the gate, `canon_trading.ranking`, is tested in
  `tests/test_ranking.py`: each refusal above, and a passing and a failing pick worked without skfolio.
* The addition of 2026-11-11, each a test that fails against the code before the change
  (`tests/test_returns_matrix.py` for the check, `tests/test_ranking.py` for the ranking), the first
  three the cases of the gate's seventh outside round. Those of the check itself fail there for the
  function it adds, `session_returns`; the copies at another scale, the ratio one step ahead, the
  two within rounding, the all-zero series and the trials of one ratio stopped before too and fail
  on the message; the ratio beyond rounding ahead ranked as it does now, and is kept as the limit's
  test; the series just above the threshold was accepted before, and its test fails on the refusal
  at 96 ulp:
  * closed-day zeros no longer hide session-day moments that underflow, where a pick rejected at 0.8610
    at its own scale passed at 1e-81 of it, or that overflow
    (`test_closed_day_zeros_do_not_hide_moments_that_underflow`, `..._that_overflow`,
    `test_closed_day_zeros_do_not_let_a_pick_whose_moments_underflow_pass`,
    `test_closed_day_zeros_do_not_let_moments_that_overflow_into_the_matrix`);
  * the pair of term 3 stops in both orders, naming both
    (`test_a_tie_between_other_returns_stops_whatever_the_recording_order`), and so does a
    configuration with its copy at 0.97, 3, 7 or 1/7 times the scale, where the exact rule first
    built ranked three of the four and the code before the change stopped all four on its tie rule
    (`test_copies_at_another_scale_stop_by_name_in_either_order`), and with its
    returns reversed, a ratio one step ahead
    (`test_a_ratio_one_rounding_step_ahead_is_no_pick_in_either_order`);
  * a constant series is refused whatever its mean rounds to, is told no value and stops the ranking
    (`test_a_constant_series_is_refused_whatever_its_mean_rounds_to`,
    `test_a_constant_series_is_told_no_value_and_stops_by_name`), one too large to square included
    (`test_a_constant_series_too_large_to_square_is_refused_as_one_that_does_not_vary`);
  * a series of zeros, a configuration that never trades, is refused as one that does not vary
    (`test_a_series_of_zeros_is_refused_as_one_that_does_not_vary`,
    `test_a_traded_record_whose_returns_are_all_zero_stops_by_name`), while a record whose outcome
    is idle stops as never traded and for that alone (`test_an_idle_trial_stops`);
  * term 5's limits: thirty returns of 1 and 1 + 128 ulp alternating, a standard deviation of 65 ulp
    against a threshold of 60, are accepted with a Sharpe ratio of 6.9e13 and a bound 7 times that,
    and stop the ranking; at 4,096 ulp they rank with a ratio of 2.2e12
    (`test_a_series_just_above_the_threshold_is_accepted_with_the_sharpe_ratio_it_has`,
    `test_a_series_next_to_the_threshold_stops_and_one_clear_of_it_ranks_as_it_is`); and a ratio
    beyond rounding ahead is the pick in either order, with the small V the two have
    (`test_a_ratio_beyond_rounding_ahead_is_the_pick_in_either_order`);
  * five trials of one Sharpe ratio, whose variance rounds to 9.6e-34, stop as V = 0
    (`test_trials_with_one_sharpe_ratio_have_no_v_whatever_its_variance_rounds_to`), as do two
    within rounding of each other (`test_two_trials_within_rounding_of_each_other_have_no_v`); a
    value told 1e-10 off stops (`test_a_told_value_is_held_to_the_rounding_of_its_own_series`);
  * five trials, two of them one series under two keys, give the same pick and the same V, DSR and
    N̂ to the bit in three orders of recording, where with the columns in the order of recording
    the pick's name followed it
    (`test_the_report_is_the_same_to_the_bit_whatever_the_recording_order`).

  `tests/test_deflated_sharpe.py` pins the two things skfolio does that the terms rest on: a constant
  series keeps the spread its mean rounds to, and the variance of equal ratios can be above 0.

## Evidence

* Split from ADR 0010 on 2026-11-10 with no change of decision: the text above is ADR 0010's own, moved,
  except the title, the Context, the Considered Options, the `Chosen` line, the line introducing step 3,
  the line pointing to ADR 0010's gate, the `Who decided` line, which keeps only the choice of skfolio,
  and the reference to the three cases in Confirmation. The Decision Drivers are ADR 0010's, copied. The
  lines below keep their original dates and how each was checked. The paper and the delegation support
  both records and are kept whole in each; the second reader's line keeps only what concerns this record.

Read 2026-11-10. Fetched means read in this session as raw text or source; run means executed in this
checkout.

* https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — the DSR paper, version of 2014-07-31, sha256
  ca4a1e83. Read: eqs. 1 and 2, the example on pp. 9–10, pp. 3, 6 and 14–15, where the paper writes M for
  all trials. Fetched; the equations read from page images.
* The owner decided on 2026-11-10 that the orchestrator proceeds automatically on state-of-the-art
  convergence, leaving the open choices to it. The choices under "Who decided" were made under that delegation by the
  orchestrator, not by the owner item by item.
* https://pypi.org/pypi/skfolio/json — 1.12.8, released 2026-11-08, is the latest of 130 releases; BSD
  3-Clause; `requires-python >=3.10`, with a Python 3.14 classifier. Fetched.
* https://raw.githubusercontent.com/skfolio/skfolio/433991778607285ecd274942e89029190c45e44c/src/skfolio/measures/_measures.py
  — the three functions: raw kurtosis, √(T−1), N as the count of finite trial Sharpe ratios, sample
  standard deviation. Fetched; identical to the file in the PyPI source archive (sha256 3f479188). Tag
  `v1.12.8` resolved to that commit.
* At the same commit: `tests/test_measures/test_measures.py` asserts 0.9004 through
  `probabilistic_sharpe_ratio`, on the two-valued series this repo's test also uses; and
  `.github/workflows/ci.yml` tests Python 3.10 to 3.14 on Linux, and 3.14 on macOS and Windows. Fetched.
* `uv add skfolio==1.12.8` on Python 3.14.10, macOS arm64 — installed skfolio and 13 dependencies from
  wheels. `uv run --locked pytest tests/test_deflated_sharpe.py`: 12 passed. Run.
* Moved to v1.12.9, 2026-11-10. https://github.com/skfolio/skfolio/releases/tag/v1.12.9 lists two
  prior-model fixes (EntropyPooling, BlackLitterman) and type-check rules updated for ty 0.0.100. The compare
  v1.12.8...v1.12.9 changes 20 files. `measures/` and `population/` are not among them, and
  `portfolio/_base.py`, `portfolio/_portfolio.py` and `utils/stats.py` change only in `ty: ignore`
  comments. Read with `gh api`. `measures/_measures.py` has the same sha256 (761cbbe3) at tag `v1.12.8`
  (`433991778607`), at tag `v1.12.9` (`772109053763`) and in the installed wheel. Fetched. At
  https://pypi.org/pypi/skfolio/1.12.9/json, `requires-python` is `>=3.10` and there is one
  `py3-none-any` wheel. Fetched. `uv add skfolio==1.12.9`, then
  `uv run --locked pytest tests/test_deflated_sharpe.py tests/test_ledger.py tests/test_returns_matrix.py
  tests/test_ranking.py tests/test_variant_study.py`: 230 passed. Run.
* Second reader of the move, 2026-11-10 (`evidence-verifier`): every claim of the move holds. It
  corrected the release-note wording above: the notes update type-check rules, and the rule rename shows
  only in the compare's `pyproject.toml`.
* Read on 2026-11-09 and checked again by the second reader on 2026-11-10:
  * nautilus-trader 2.0.0rc8 in `.venv`, on a known series: `SharpeRatio()` is mean/std(ddof 1)·√252,
    skewness is bias-corrected and kurtosis is excess. `bundled`, run.
* Second reader (`evidence-verifier`), 2026-11-10, the part that concerns this record: the equations, the
  example, the pins and the test hold; corrected how skfolio counts N, "maintainers" and the release
  cadence; could not check the delegation.
* Full review, 2026-11-10 (`/code-review`): V > 0 was an exact test, and one configuration at two
  scales gave V = 1.1e-27 and passed with an undeflated DSR of 0.9843. The tolerance above corrects it.
* https://docs.python.org/3/library/math.html — `math.isclose(a, b, *, rel_tol=1e-09, abs_tol=0.0)`;
  the default "assures that the two values are the same within about 9 decimal digits". Fetched.
* Full review, 2026-11-10 (`/code-review`): an exact tie between bit-identical columns stopped for good;
  N̂ of 0.9999999999999998 reached skfolio and raised a bare `ValueError`; N̂ = 1.003 gave a "DSR at
  N̂" above the undeflated value. The tie, DSR and N̂ terms above answer them.
* Second reader of the split, 2026-11-10 (`evidence-verifier`): nothing of ADR 0010 is missing from the two
  records and every citation names the right one. It corrected a reference to a step 3 this record does
  not have, and the second reader's line above, which had named ADR 0010's option 2 and its PBO half.
* Added 2026-11-11, for the addition of that date. Run means executed in this checkout on Python
  3.14.10 with skfolio 1.12.9 and NumPy 2.5.4.
  * https://raw.githubusercontent.com/skfolio/skfolio/7721090537634a549008a546c8968cdec0eaf69a/src/skfolio/measures/_measures.py
    — `variance` returns `np.nanvar(returns, ddof=0 if biased else 1, axis=0)` (line 196) and
    `standard_deviation` its square root (line 300); `_standardized_moment` (lines 1620–1629)
    subtracts the mean of the deviations again, "the residual offset left by rounding the mean",
    and gives NaN when the second moment is at most `(np.finfo(float).eps * mean_return) ** 2`;
    `correlation` (lines 1240–1242) is `np.cov` over the outer product of standard deviations.
    Fetched: read in the local copy at the pin, sha256 761cbbe3 as above.
  * https://raw.githubusercontent.com/skfolio/skfolio/7721090537634a549008a546c8968cdec0eaf69a/src/skfolio/portfolio/_base.py
    — a ratio measure is `(self.mean - self.risk_free_rate) / risk` (lines 861–865), so
    `Portfolio.sharpe_ratio` is the mean over the standard deviation. Fetched, the same way.
  * NumPy 2.5.4 in `.venv`: `nanvar` calls `np.var` when no value is NaN
    (`numpy/lib/_nanfunctions_impl.py`), and `_var` (`numpy/_core/_methods.py`) divides one sum by
    the count, subtracts it once, and divides the sum of squares by n − ddof. `bundled`, read.
  * Python 3.14.10: `math.ulp(x)` is "the value of the least significant bit of the float x", its
    docstring. `bundled`.
  * The bound in term 2 is derived here from IEEE 754 rounding to nearest, each operation within
    half a spacing of its result. https://arxiv.org/html/2107.01604v1 §2.1: Hallman and Ipsen cite
    Higham, "Accuracy and Stability of Numerical Algorithms", 2nd ed. (SIAM, 2002), eq. (4.3), for
    summation in any order, |eₙ| ≤ u·Σₖ₌₂ⁿ|ŝₖ|, the form used here. Fetched by the second reader.
    The book itself, and its γₙ₋₁·Σ|xᵢ| form, were not read: UNVERIFIED. Run: over 200,000 constant
    series of 2 to 400 values the standard deviation
    was at most 0.41 of the measure, against the 2 allowed; and of 300,000 nearly constant series,
    each of the 85,344 skfolio's floor left undefined was at or under 1.12 of it.
  * Run: thirty returns of −0.01 give a standard deviation of 1.76e-18 and a Sharpe ratio of
    −5.667716462397839e15; `PICK` of `tests/test_ranking.py` at 3 times its scale gives a ratio one
    step from its own, V = 1.2e-32 and a probabilistic Sharpe ratio of 0.9997, and at 7 times the
    same ratio to the bit; the bound of term 3 is 1.9e-13 for that series, and 2.1e-11 to 2.6e-11
    for the eight records of the step-7 study (ADR 0052), whose ratios lie 1.6e-4 apart at the
    closest. Over reordered sums the ratio moved by at most 0.56·r·max(1, |SR|) (0.53 against
    exact rational arithmetic, second reader), against the 16 allowed. With its first return 1e-9 higher, `PICK` has a ratio 9.4e-10 lower, V
    is 4.4e-19 and the DSR 0.9996755631592 against the undeflated 0.9996755631612. Thirty returns
    of 1 and 1 + k ulp alternating have a bound of 7.4, 0.92 and 0.23 of their ratio at k = 128,
    1,024 and 4,096.
  * Code review of the change, 2026-11-11 (`/code-review`, high): returns that differ and whose
    squares underflow were refused under the wrong cause, as not varying at 1e-170 and as a NaN
    kurtosis at 1e-90, and are now refused as underflow
    (`test_returns_that_vary_and_underflow_are_refused_as_underflow`); an idle record's zeros gave
    a second stop beside "never traded"; and term 5 did not say that near-copies the 1e-9 rule
    stopped now rank, that a large negative ratio raises V, or how loosely a value told is held
    next to the threshold. Each is corrected above. A second review, of the rules as they stand:
    thirty returns of 1e200 were refused for a variance of inf, not as constant; a record RUNNING
    with a value and outcome idle, which `record_trial` never leaves, was told to rank again
    (`test_a_running_idle_record_with_a_value_is_another_writer_s`). Both are corrected. Its
    other points stand as decided: the near-copies of term 5; this record's length; an idle record
    with one session day, which stops for that too; and the tests that pin NumPy's rounding to the
    bit, which fail when a move of NumPy changes what the terms rest on.
  * Second reader of the addition, 2026-11-11 (`evidence-verifier`), twice. The first pass, on the
    exact rules, reproduced the runs and corrected qualifiers, numbers and the sentence on
    which tests failed before. The second, on the rules as they stand, reproduced every number
    above and did not break the bound: against exact rational arithmetic a ratio was off by at
    most 0.033 of its bound, and scaled copies by 0.025 of the sum of theirs. It corrected what the
    code before the change did with scaled copies (it stopped them on its tie rule), the figure for
    reordered sums and the source for the summation bound, and found that the pick's name among
    keys with the same returns, and the last bit of V and N̂, followed the order of recording; the
    columns are now taken in the order of the keys.
  * The gate's seventh outside round, 2026-11-11, read only, by Antigravity: its first three
    findings are the first three cases under Confirmation, each reproduced as a failing test before
    the change. Rounds 5 and 6 are in ADR 0054's term 6.
