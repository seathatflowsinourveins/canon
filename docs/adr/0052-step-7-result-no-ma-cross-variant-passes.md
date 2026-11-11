---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# Step 7 result: no `MaCross` variant passes the gate; the pick, SPY 5/20, has a DSR of 0.9338

## Context and Problem Statement

ADR 0048 pre-registered the step 7 study: eight `MaCross` variants on SPY and QQQ daily bars, N = 8, one
sample, through `canon_trading.ranking.rank` under ADR 0010's protocol. The study has run. What did the
gate decide, on what condition is that result accepted, and what can it show? Split from ADR 0048 on
2026-11-10, with no change of decision.

## Decision Drivers

* The gate decides, not the reader of the numbers; a rejection is a valid outcome (ADR 0010).
* The gate's open defects are stated, not smoothed over; a result they could have touched is not
  accepted as clean.

## Considered Options

None of its own: the result is what ADR 0048's plan and the gate produced. What this record decides is
the condition it is accepted on.

## Decision Outcome

**Result: no variant passes.** The pick, SPY 5/20, has a deflated Sharpe ratio of 0.9338 and is rejected
at 0.95 (Confirmation holds `rank`'s output). This is a valid outcome of the protocol. ADR 0048's plan
made acceptance wait for the gate's open fixes; four defects reported after them are still open, so this
record is accepted on the condition that none of them arose in the run, which Confirmation argues. A fix
of them that changes what this study reads calls for running it again on a moved sample.

**What a result can and cannot show.** It is a test of the protocol on real bars, not of a trading
idea, and recommends nothing. An order sent from `on_bar` fills at that bar's close, a price already
past (`canon_trading.backtest`, Known limits); prices are raw, so a dividend shows as a price drop and
is never paid; a bar is built from IEX's trades alone, so its close is not the consolidated close; no
cost or slippage is modelled; each run's warm-up (20 to 100 sessions) holds no position, so its
returns there are zero, inside the sample; and the DSR checks none of these (ADR 0010, Limits). A
rejection says the best of these eight is not distinguishable, at 0.95, from the best of eight
trials with no skill; it says nothing about `MaCross` elsewhere, and a pass would not have been a
recommendation either.

### Consequences

* Good: the protocol has run end to end on real bars, and its output is kept here in full.
* Bad: the result is accepted on a condition; a fix of the open defects that changes what the study
  reads means running it again on a moved sample.

### Confirmation

* The run, 2026-11-10, after the gate's fixes of the same day were on `main`: `python -m
  canon_trading.variant_study --create`, on the ledger's shared file, recorded trials 0 to 7, all
  `traded`, and printed `rank`'s report in full:

  ```text
  Study 'step7-ma-cross-daily-spy-qqq-2020-07-to-2025', sample 0 (2020-07-27T16:00:00+00:00 to 2026-01-01T17:00:00+00:00): 8 records on it, trials 0 to 7, recorded 2026-11-10T23:08:04.799290+00:00 to 2026-11-10T23:08:04.998709+00:00; 8 records on every sample.
  N = 8 configurations over every sample, T = 1366 sessions (2020-07-28 to 2026-01-01), V = 0.000112974.
  Pick a437779d65601e16bb9ccfb46e9cfc2a9c26c49477063a31cdec742742c28297 (trials 1): Sharpe ratio 0.0565838 per session, DSR 0.9338; it is rejected at 0.95.
  N-hat = 2.996, DSR at N-hat 0.9592 (not used in the gate).
  ```

  √V = 0.0106, so SR₀ = 0.0155 and, by ADR 0048's table, a pick under Normal returns needed about
  0.060 per session. N̂ is reported since T ≥ N(N−1)/2 = 28; the DSR at N̂ is above 0.95, and ADR 0010 never lets N̂
  replace N in the gate.
  The session days are UTC days of each bar's `ts_init`, so they run one day after the trading days.

  | Trial | Configuration | Sharpe ratio per session | Sessions with a non-zero return | First |
  |---|---|---|---|---|
  | 0 | SPY 10/20 | 0.0281 | 914 | 2020-08-25 |
  | 1 | SPY 5/20 (pick) | 0.0566 | 901 | 2020-08-25 |
  | 2 | SPY 10/50 | 0.0510 | 948 | 2020-10-13 |
  | 3 | SPY 20/100 | 0.0473 | 969 | 2020-12-17 |
  | 4 | QQQ 10/20 | 0.0290 | 888 | 2020-08-25 |
  | 5 | QQQ 5/20 | 0.0446 | 867 | 2020-08-25 |
  | 6 | QQQ 10/50 | 0.0334 | 904 | 2020-10-10 |
  | 7 | QQQ 20/100 | 0.0472 | 951 | 2020-12-17 |

  The ratios are the values the ledger told each trial, skfolio's per-session Sharpe ratio over its
  session days. Every trial covers the same 1,366 session days; each symbol's four trials share one
  data digest. The ledger file is local and not committed.
* Four gate defects that a further read-only review of the gate reported on 2026-11-10 are open and
  not reproduced here (UNVERIFIED; two agree with the code as read: `mark_not_run` takes a keyless
  record that shows no run, and `move_sample` rewrites the sample list with no lock): the coverage
  check reads the catalog apart from the replay; the ranking accepts some malformed completed records;
  a keyless `COMPLETE` trial can be marked not run; two sample moves at once can overwrite each other.
  None arises in this run if its conditions held. Those that can be read after the run did: the
  study has one sample, never moved, and no `not_run` mark, and every record is in the form
  `record_trial` writes. Observed once on this machine and not reproducible from the repository: the
  catalog's bar files were last written at 17:31 New York, before the first trial at 18:08; and that
  only one process wrote the study is consistent with the pass lock's file and with trials that ran
  back to back, not proved by them. Since the run, all four were reproduced as failing tests and
  fixed on `main` (`dc49fb6`, ADR 0041's third review); `rank` from that code up to `05955f9`, on a
  copy of the ledger, gives the report above word for word (Evidence, run at `05955f9`); after it, the
  study stops, below. Whether the study is to be recorded again on a
  moved sample under the fixed recording code is not decided here; it was recorded again on
  2026-11-11 (below).
* What ran: `main` at `ba44c25` with ADR 0048's wiring (`canon_trading.variant_study`) before its last
  change, which places a completed record with no sample number as `rank` does (Evidence, code review).
  On a fresh study every version records all eight keys, so that change touches no trial. The bars each
  trial read are identified by their data digests: SPY
  `a0b82593e1401adbd875b142f24c414ad77e3ffa92c6c690bcaff5a02a09ad09`, QQQ
  `e2e493377d3614bbc3373867b0e789bc353b6322bb032757c3730b1d4457db40` (ADR 0053 term 1).
* The fourth review of the gate (ADR 0048's Evidence) changes nothing read here: one pass recorded all
  eight planned keys, all `traded`, so N = 8 is the plan's, and the study has no `not_run` mark, no
  queued trial and no record out of `record_trial`'s shape.
* The instrument definitions the eight trials read were not recorded: the data identity then held the
  bars only (ADR 0053 term 1, corrected by its fifth review). One catalog served all eight, so each
  symbol's four trials read one definition unless the catalog's instrument files changed during the
  pass, which nothing recorded can rule out (UNVERIFIED). Under the corrected rule the study as stored
  stops, every record's digest lacking its instrument, until its eight configurations run again on a
  moved sample; their keys are unchanged, so N stays 8. That re-run is the next entry.
* The re-run, 2026-11-11 00:04 New York, inside the same study as the corrected rule asks: the bars
  loaded again from Alpaca into a fresh catalog (`mise run paper:test -- -s -v -k variant_study_bars`,
  exit 0, 1,366 bars a symbol over the same window); `ledger.move_sample` gave the study sample 1, the
  same range as sample 0, under the ledger's lock; `python -m canon_trading.variant_study` recorded
  trials 8 to 15, all `traded`, and printed `rank`'s report:

  ```text
  Study 'step7-ma-cross-daily-spy-qqq-2020-07-to-2025', sample 1 (2020-07-27T16:00:00+00:00 to 2026-01-01T17:00:00+00:00): 8 records on it, trials 8 to 15, recorded 2026-11-11T05:04:28.301977+00:00 to 2026-11-11T05:04:28.755036+00:00; 16 records on every sample.
  N = 8 configurations over every sample, T = 1366 sessions (2020-07-28 to 2026-01-01), V = 0.000112974.
  Pick a437779d65601e16bb9ccfb46e9cfc2a9c26c49477063a31cdec742742c28297 (trials 9): Sharpe ratio 0.0565838 per session, DSR 0.9338; it is rejected at 0.95.
  N-hat = 2.996, DSR at N-hat 0.9592 (not used in the gate).
  ```

  N is still 8: the eight keys are those of the plan, and sixteen records over two samples count eight
  configurations. Every statistic equals the first run's to the digit printed: N, T, V, the session
  range, the pick and its Sharpe ratio, the DSR, N-hat and the DSR at N-hat; only the sample, trial
  numbers, times and record count differ, as they must. That is what the inputs predict: the bars'
  digests are those of the first run (SPY `a0b82593...`, QQQ
  `e2e49337...`), so Alpaca served the same bars; each trial's configuration and key, session days and
  returns series equal those of the trial of the same key on sample 0 (compared from the ledger). The
  code has every change since `ba44c25` in the modules the study runs through, among them the gate's
  checks, the data identity, the run reading the bars its coverage check read (`dc49fb6`), the key
  holding base strategy fields set away from their defaults (`3c89ce6`), the config's keyword checks,
  and skfolio at v1.12.9, whose deflated Sharpe functions are byte-identical to v1.12.8 (#125). The
  equal keys, returns and report show that none of them moved a figure here. The new digests also name
  each instrument's definition: SPY
  `208607431e57368eba0d3e659523742b69b3934ecef825ba5c98bba764baf981`, QQQ
  `eabf7a651d7c50fada58491e37b225757779a95e9fbfe93704cd1c48d3eae091`. No record holds a system
  attribute, on either sample. What ran: the checkout was at `main`'s `71e542e` (from the reflog),
  with an uncommitted comment-only change in `ma_cross.py`; the study run's own log names no package
  versions. The result stands: no variant passes, and the pick's DSR is 0.9338.

## Evidence

Read 2026-11-10, and 2026-11-11 for the re-run. Fetched means read in this session as raw text or
source; run means executed here.

* The run above and a read of the eight records through `open_study`. Run.
* Code review of the result, 2026-11-10 (`/code-review`): the pass still counted a completed record
  with no sample number as a run of its key, which `rank` does not; the record named no code or data
  for the run, and let unrepeatable observations stand as run. Fixed, with a test, and stated above.
  It also asked for one public helper shared with the gate, which is the gate's code to change; since
  done: `ranking.on_current_sample` and `ran_to_the_end`, which `variant_study` imports (read).
* https://docs.alpaca.markets/us/docs/market-data-faq.md — IEX against SIP: on one day 12,630 eligible
  IEX trades against more than 535,136 in all; a historical SIP query without a subscription needs an
  `end` at least 15 minutes old. Fetched.
* ADR 0031 — eqs. 1–2 and their conventions, used for ADR 0048's table and SR₀ under Confirmation; computed with Python's
  `statistics.NormalDist`. Run.
* Second reader, 2026-11-10 (`evidence-verifier`), on the result: it re-read the ledger and `rank`'s
  report, which match, and corrected the unsourced open defects, the single-process claim, and the code
  that ran. Its findings on the plan are in ADR 0048's Evidence.
* Second reader (`evidence-verifier`) of this record, 2026-11-10: the report, the trial table, √V, SR₀
  and the digests hold, re-read from a copy of the ledger with `rank` at `dc49fb6` and later (run); the
  four defects listed as open were fixed at `dc49fb6`, and the shared helper is public; both stated
  above. The catalog's write time could not be re-read: the bars are no longer in the catalog.
* ADR 0048 as it stood on `main` before 2026-11-10's split: the result, what it can show, the run's
  Confirmation and its evidence, moved here with no change of decision (run: `git show` of the record
  on `main`).
* `rank` at `05955f9`, on a backup copy of the shared ledger, 2026-11-10: the report above word for
  word; at the next change, which adds the instrument definitions to the data identity (ADR 0053's
  fifth review), the same copy stops on all eight records, each digest lacking its instrument. Run.
* The re-run on sample 1 above, 2026-11-11, and the ledger read that compares its trials with sample
  0's; the shared ledger was copied before the sample moved. Run.
* Second reader of the re-run, 2026-11-11 (`evidence-verifier`): re-ranked copies of the ledger before
  and after the move, recomputed the instrument digests from the catalog, and compared every pair of
  trials; corrected above: "every statistic", not every figure; the code changes no longer presented
  as the full list; skfolio's `_measures.py` the same at v1.12.8 and v1.12.9 (sha256, fetched from the
  tags); what is known of the code that ran; the two lines the re-run settles.
