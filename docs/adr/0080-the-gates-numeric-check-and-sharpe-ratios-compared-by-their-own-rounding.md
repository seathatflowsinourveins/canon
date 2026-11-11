---
status: accepted
date: 2026-11-11
decision-makers: repo owner by delegation; master orchestrator (Claude)
review-by: 2026-12-10
---

# The gate's numeric check: each series checked once on its session days and refused at its own size, and Sharpe ratios compared by the rounding of their own computation

## Context and Problem Statement

ADR 0031 computes the pick's deflated Sharpe ratio through skfolio and refuses the input skfolio lets
pass. Three of the gate's outside rounds found returns whose moments overflow, underflow or round into a
result, and under ADR 0031's two tolerances the order of recording decided one. Which series does the
gate refuse before any statistic is read, and when are two Sharpe ratios equal? Split from ADR 0031 on
2026-11-11, with no change of decision.

## Decision Drivers

* ADR 0031's drivers: a comparison method that flatters a strategy is worse than none; where upstream
  is silent, say so; every statistic comes from a published source. The factors of terms 2 and 3 are
  no statistic and come from no source: they are derived here, and the terms say so.
* ADR 0073 rule 4: a family found in two rounds is redesigned as one model before a third round.

## Considered Options

1. The checks of the two earlier rounds where they stood, and ADR 0031's two `math.isclose`
   tolerances (the state before 2026-11-11).
2. One check, with ratios and returns compared exactly (the rule first built).
3. One check, with ratios compared by the rounding of their own computation, on the set, and returns
   in the top group compared exactly.

## Decision Outcome

Chosen: **option 3**, split from ADR 0031 on 2026-11-11 with no change of decision. The terms keep the
numbers they had there. Under options 1 and 2 rounding decided the result (term 3).

**One numeric check, and ratios compared by their own rounding (added to ADR 0031 on 2026-11-11).** The gate's numeric family was found in
its rounds 5 (squares that overflow), 6 (moments that underflow) and 7, so under ADR 0073 rule 4 it is
one model, and it replaces the checks of those rounds and the two tolerances ADR 0031 held until then.

1. **One check, in one place.** `canon_trading.returns_matrix.session_returns` drops the closed days
   and then computes, on exactly the series of session days the gate takes and by the functions the
   gate uses, everything the DSR reads: skfolio's Sharpe ratio, `variance`, `fourth_central_moment`,
   `skew`, `kurtosis`, and `correlation` of the series with itself. It refuses the series by name, in
   this order: with fewer than two session days; with no spread at its own size (term 2); with the
   square of its second central moment below float64's smallest normal, where the kurtosis rounds
   toward 1 or 0 or is NaN (round 6's floor); with any of the six not finite. It returns the series
   with its Sharpe ratio; the ranking and the value `record_trial` tells read both from that
   result, and nothing else checks a moment of a return series. Taken over every UTC day, the zeros
   of closed days hid session-day moments that overflow or underflow. Round 8 replaced the
   count and the list of six by skfolio's own answers (term 6).
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
   statistical source gives one. Round 8 moved it from 2 to 16, the bound's own (term 7).
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
   separates spread from rounding; it does not judge whether a series is plausible. Round 8
   showed the sentence on a series next to the threshold false for a negative ratio, and the
   one on a negative ratio false beside a pick as large: terms 7 and 8 replace both.

**A series is valid exactly when skfolio accepts it, and one threshold for spread (added 2026-11-11,
after the gate's eighth outside round).** The numeric family turned up again in round 8, in two
findings, so under ADR 0073 rule 4 the model is reopened, not patched. The lead decided the model on
2026-11-11: a series is valid for the gate exactly when the pinned skfolio functions the gate calls
accept it and return finite numbers, for the series and its sign as it stands; the gate asks skfolio,
by the same calls when a trial is recorded and when a study is ranked, and keeps no list of skfolio's
own conditions. Terms 6 and 7 replace term 1's count and its list of six statistics, term 2's factor,
and term 5's sentence on a series next to the threshold. Terms 3 and 4 stand.

6. **What is asked.** `session_returns` asks three things of the session-day series:
   `Portfolio.sharpe_ratio`; `measures.deflated_sharpe_ratio` with that ratio as the one trial, the
   function the pick's DSR comes from, whose benchmark is then 0 and whose answer is
   `probabilistic_sharpe_ratio`'s; and `measures.correlation` of the series with itself. A call that
   raises `ValueError`, or returns a number that is not finite, refuses the series by name with what
   skfolio said. skfolio's own conditions apply without being copied here: no ratio for one return;
   no DSR for fewer than three returns, for a variance adjustment 1 − γ₃SR + (γ₄−1)/4·SR² that is
   not above 0, which depends on the ratio's sign, or for a skewness or kurtosis it leaves undefined.
   Neither condition depends on the benchmark, so a series with a DSR of its own has one against any
   trials; the stop on a DSR of the pick that is not finite stays as the last one. `record_trial`
   tells a value through the same function, so a record is `COMPLETE` exactly when the ranking takes
   its series, and one refused is told no value and `FAIL`, still `traded`.
7. **Two conditions are float64's, not skfolio's, and stay**, each because skfolio answers with
   finite numbers that are wrong. They are read before skfolio's answers, so the cause named is the
   rounding.
   * *A spread inside the rounding of the mean.* skfolio's standard deviation subtracts the computed
     mean once, where its skewness and kurtosis centre again: 100 returns of 1.6304211694124828 and
     two or four steps of float64's spacing above it have a Sharpe ratio of 4.46e15 in exact
     arithmetic and 2.15e15 from skfolio, with a finite skewness, kurtosis and DSR. The refusal is
     at the comparison bound's own factor: a series is refused when its standard deviation is at
     most 16 times the measure of term 2, which is when term 3's bound is not below max(1, |SR|).
     The factor 2 goes. Between 2 and 16 a series was accepted with a bound at or above its own
     ratio, and term 5 said the ranking then stops. That held only when its ratio was the highest:
     thirty returns of −1 and −1 − 880 steps, 14.9 times the measure, ranked beside thirty of 1.99
     and 1.99 + 1,020 steps, 17.3 times, and that pick passed at DSR 1.0. Exact constancy stays the
     special case, read from the returns before any statistic.
   * *Moments that underflow.* Round 6's floor, as term 1 had it. skfolio's own floor for skewness
     and kurtosis is relative to the mean and underflows with it: thirty returns about 1e-81 apart
     have a kurtosis of 1.008, skfolio reads 0, and a probabilistic Sharpe ratio of 0.8460 against a
     benchmark of 0.5 reads 0.8613, every number finite.
8. **The limit that remains.** A series clear of the threshold ranks with the Sharpe ratio its returns
   give, up to about 2⁴⁹/n, and can pass. With that ratio negative it raises V, which rejects no pick
   whose own ratio is as large: with the negative series above at 1,010 steps, 17.1 times the
   measure, the same pick passes at DSR 1.0, and eqs. 1–2 in exact rational arithmetic give 1.0 for
   those returns too (ratios −8.77e12 and 1.73e13, SR₀ 9.57e12). That is the statistic's answer for
   the input; the check separates spread from rounding and does not judge whether a series is
   plausible, and the registration, read before any trial (ADR 0048), is where such a series is seen.

Who decided, round 8, 2026-11-11. The lead decided the model, that the gate keeps no list of skfolio's
conditions, and that the factor 2 goes unless something skfolio does not catch needs it. Chosen in
building it, under the owner's delegation of 2026-11-10: the three calls; the deflated Sharpe ratio
asked with the series' own ratio as the one trial; the two float64 conditions kept, each with its
case; and the one threshold at the bound's factor, 16.


Who decided, 2026-11-11. The lead decided the terms: one check with one call site; degeneracy relative
to the series' own size; no free tolerance; and, on reading the exact rules first built, that ratios
are equal within the rounding of their own computation, judged on the set, with returns in the top
group compared exactly. Chosen in building them, under the owner's delegation of 2026-11-10 (ADR
0010's Evidence): the standard deviation as what is compared, the factors 2 and 16, the sum of two bounds between two trials and the widest
bound at a shared extreme, the series' correlation with itself as the correlation input, the
order of the refusals, and the order of the keys for the columns. The lead also decided that ADR
0031 landed with these terms at its length, past the length at which the rule for records asks for a
split, with the number 0080 kept for one: this record.

### Consequences

* Good: one function checks every moment the DSR reads, and no result hangs on the order of recording
  or on a free tolerance (terms 1, 3 and 4).
* Bad: the factor 16, and until round 8 the factor 2, are chosen here, on the derivations of terms 2
  and 3; no statistical
  source gives either.
* Bad: near-copies the 1e-9 rule stopped now rank, with a small V and a pick near its undeflated value
  (term 5).

### Confirmation

* The terms as added on 2026-11-11, each a test that fails against the code before the change
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
* Round 8's terms, added 2026-11-11, each a test that fails against the code before the change. The
  two tests of term 5's limits above changed with the threshold: at 128 ulp the series is now
  refused and stops the ranking by name, at 1,024 ulp, a standard deviation of 521 ulp against a
  threshold of 480, it is accepted with a ratio of 8.6e12 and a bound below it, and at 4,096 ulp it
  ranks as before.
  * two session days, and a variance adjustment not above 0 in either sign, give no series
    (`test_two_session_days_give_no_series_since_skfolio_gives_no_dsr_of_them`,
    `test_a_variance_adjustment_not_above_zero_gives_no_series_in_either_sign`), are told no value and
    stop the ranking by name (`test_a_series_skfolio_gives_no_dsr_of_is_told_no_value_and_stops_by_name`),
    and by the ledger's own writer over a sample of two days
    (`tests/test_variant_study.py::test_a_study_of_two_session_days_is_told_no_value_and_stops_by_name`);
  * a series whose ratio rounding can move by its own size is refused in either sign, at 2.2, 14.9 and
    exactly 16 times the measure (`test_a_series_whose_ratio_rounding_can_move_by_its_own_size_is_refused`),
    and finding 2's pair never ranks, in either order of recording
    (`test_a_negative_ratio_next_to_the_threshold_never_ranks_beside_a_pick_that_passes`);
  * what skfolio raises on refuses the series with its error
    (`test_what_skfolio_raises_on_is_refused_naming_its_error`);
  * kept as controls, passing before and after: the case skfolio does not catch
    (`test_a_spread_inside_the_mean_s_rounding_is_what_skfolio_does_not_catch`) and round 6's and round
    7's underflow cases.

## Evidence

* Split from ADR 0031 on 2026-11-11 with no change of decision: the terms, the `Who decided` paragraph,
  the tests and the lines below are ADR 0031's own, moved, with their dates and how each was checked.
  New here are the title, the Context, the Decision Drivers, the Considered Options, the `Chosen` line
  and the Consequences, which restate the terms. Reworded to read from this record: the heading of
  the terms and its "two tolerances"; in `Who decided`, the delegation, which now names ADR 0010's
  Evidence, and the sentence on ADR 0031's length; the first words of the Confirmation; the preamble
  below; and two pointers in the lines below that now name ADR 0031. `review-by` is ADR 0031's, kept:
  skfolio, which the terms rest on, released 27 times in the month before ADR 0031 was written.
* The terms number the rounds that found the numeric family 5, 6 and 7, as the message of #199 does.
  ADR 0054's Evidence and the message of #179 call the round that found the squares that overflow the
  fourth (read at #177), and the fifth (read at #179) found nothing numeric (`git log`, read). The two
  counts disagree; the terms are left as moved.
* Second reader of the split, 2026-11-11 (`evidence-verifier`): every sentence of ADR 0031 is in one
  of the two records, and each reworded line only names the record that now holds its target. It
  corrected the new text: a Consequence that said no result hangs on a tolerance no source gives,
  against term 2's own factor; a driver of ADR 0031's left out; this list of what was reworded; and
  it found the two counts of rounds above. The skfolio line numbers hold at the pin, and every test
  named exists in its file (run).

Read 2026-11-11. Fetched means read as raw text or source; run means executed in a checkout on Python
3.14.10 with skfolio 1.12.9 and NumPy 2.5.4.

* https://raw.githubusercontent.com/skfolio/skfolio/7721090537634a549008a546c8968cdec0eaf69a/src/skfolio/measures/_measures.py
  — `variance` returns `np.nanvar(returns, ddof=0 if biased else 1, axis=0)` (line 196) and
  `standard_deviation` its square root (line 300); `_standardized_moment` (lines 1620–1629)
  subtracts the mean of the deviations again, "the residual offset left by rounding the mean",
  and gives NaN when the second moment is at most `(np.finfo(float).eps * mean_return) ** 2`;
  `correlation` (lines 1240–1242) is `np.cov` over the outer product of standard deviations.
  Fetched: read in the local copy at the pin, sha256 761cbbe3 as in ADR 0031's Evidence.
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
  other points stand as decided: the near-copies of term 5; ADR 0031's length; an idle record
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
* Added 2026-11-11, for round 8's terms. Run means executed in this checkout on Python 3.14.10 with
  skfolio 1.12.9 and NumPy 2.5.4.
  * `src/skfolio/measures/_measures.py` at the pinned commit `772109053763`, sha256 761cbbe3, read in
    the local copy at the pin: `probabilistic_sharpe_ratio` counts the returns (line 1389), computes the
    variance adjustment (line 1396) and returns NaN where the count is below 3 or the adjustment is not
    above 0 (lines 1403-1407); `deflated_sharpe_ratio` raises `ValueError` for an infinite trial ratio
    and for none that is finite (lines 1484-1489) and takes a standard deviation of 0 for one trial
    (line 1493), for which `expected_max_sharpe_ratio` returns the mean, 0 (line 1322);
    `_standardized_moment` centres again (line 1624) and gives NaN at its floor or for a quotient that
    is not finite (lines 1627-1629); `variance` is `np.nanvar` (line 196).
  * Run, 2026-11-11: every number in terms 6 to 8; the 13 tests of the gate that changed with the model
    and the new ones; step 7's figures unchanged to the bit on a read-only snapshot of the ledger, N = 8,
    T = 1,366, DSR 0.9338299302788473, on the code before and after.
  * The gate's eighth outside round, 2026-11-11, read only, by Antigravity at b586935: findings 2 and 3,
    each reproduced before the change.
  * Code review of the change, 2026-11-11 (`/code-review`, high): no finding that changes a gate
    result. The explanation of skfolio's two conditions was printed beside an error skfolio raised,
    which it does not explain, and the bound was described as below the ratio's own size, where it
    is below the larger of 1 and that size (thirty returns of 0.01 and -0.01 have a ratio of 0.0
    and a bound of 8.2e-14). Both are corrected.
