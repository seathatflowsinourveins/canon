# Score sheet: run 2026-11-10-second

The second run of the reviewer benchmark (ADR 0046), scored by the rule in
[`evals/review/README.md`](../../README.md) ("Scoring"). The reports are in this folder:
`order-modify/<reviewer>-1.md`.

## The run

* **Case:** `order-modify` (`3a00f61`), with its 10 confirmed defects, `ready` since the first run and not
  in it. One case, so every number below comes from one commit.
* **Reviewers**, each given `brief.md` word for word in a new workspace detached at `3a00f61`:

  | Label | Runtime, model, effort |
  |---|---|
  | `claude-opus-high` | Claude Code, `claude-opus-5-5`, effort high, started with auto memory off (below) |
  | `codex` | Codex CLI on its own defaults |
  | `muse` | Muse Code on its defaults |
  | `antigravity` | Antigravity CLI on its defaults |

* **Runs per cell:** 1. A single run per cell measures no variance between runs: the same reviewer on the
  same case may find a different set next time, and nothing here says by how much.
* **Reports:** 4 of 4 present, none missing. Each workspace was checked at the case commit with no change
  and no new commit before it was removed.
* **Case files used:** as they stand on `main` at `d167055`.

### How the reviewers were started, and what that does to the numbers

1. **The Claude reviewer** was started with `--settings '{"autoMemoryEnabled": false}'` on its launch
   command, in a terminal opened for it, because the cockpit's usual launch takes no such option. That
   the flag was on the command line is checked. That the session then loaded no memory is not
   (UNVERIFIED).
2. **Codex, Muse and Antigravity:** whether they keep memory across sessions was not checked
   (UNVERIFIED).
3. **The case is biased toward three of the four.** Its case file says so: all ten defects were first
   reported by Codex, Muse or Antigravity, in their own earlier reviews of this same commit, after it had
   passed a Claude `/code-review` at high effort. So three of these reviewers re-read a commit they had
   reviewed before, with whatever their runtime keeps between sessions, and the Claude reviewer did not.
   **The found-rates below do not compare the four on equal terms.** The defects each reviewer first
   reported are counted apart for that reason.
4. **DeepSeek Harness did not take part.** ADR 0045 admits it only once its review settings are
   installed, which had not happened. Its earlier trial on this case, which was not independent, is in
   ADR 0045 and is not a score.

## Totals per reviewer, this run

Found counts a confirmed defect that a finding names by location and by what its `match` line asks.
"First reported itself" counts the found defects whose `reported_by` names that reviewer's runtime. Where
`reported_by` names Muse and Antigravity together ("which was first is not known"), the defect counts as
first reported by each. Unconfirmed counts every finding that matched no confirmed defect. It is a count,
not a judgement that the finding is false.

Read every row of this table with point 3 above: three of the four reviewers had reviewed this commit
before, and the case holds what they found then.

| Reviewer | Found / 10 | of them first reported itself | Found, not first reported itself | Unconfirmed | Missing reports |
|---|---|---|---|---|---|
| `claude-opus-high` | 4 | 0 | 4 | 3 | 0 |
| `codex` | 5 | 2 | 3 | 9 | 0 |
| `muse` | 3 | 2 | 1 | 5 | 0 |
| `antigravity` | 7 | 5 | 2 | 5 | 0 |

Findings per report, by each report's own top-level numbering (sub-points inside a numbered finding
belong to it; closing sections such as "checked and found sound" or "not verified" are not findings):
`claude-opus-high` 7, `codex` 14, `muse` 8, `antigravity` 12.

### Every mark

| Defect | First reported by | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|---|
| `filled-replacement-lost-when-replaced-missed` | Codex | found (#1) | found (#1) | missed | found (#1) |
| `refused-replace-rejected-twice` | Codex | found (#3) | missed | found (#1) | found (#8) |
| `replaced-applied-twice` | Codex | missed | found (#6) | missed | missed |
| `replacement-fill-before-replaced` | Muse, Antigravity | missed | missed | missed | found (#2) |
| `late-cancel-of-replaced-order` | Antigravity | missed | missed | missed | missed |
| `pending-replace-fails-startup` | Antigravity | missed | found (#4) | missed | found (#3) |
| `replacement-moved-on-before-replaced` | Muse, Antigravity | missed | missed | missed | found (#5) |
| `second-modify-refusal-taken-for-retry` | Antigravity | missed | missed | missed | found (#6) |
| `answers-kept-for-the-session` | Muse, Antigravity | found (#4) | found (#7) | found (#3) | missed |
| `rejected-replacement-after-fill-never-reached` | Muse, Antigravity | found (#2) | found (#5) | found (#2) | found (#7) |
| **Found** | | 4 | 5 | 3 | 7 |

No reviewer found `late-cancel-of-replaced-order`. Every reviewer found
`rejected-replacement-after-fill-never-reached`.

Why each found mark holds:

* `filled-replacement-lost-when-replaced-missed`: `claude-opus-high` #1, `codex` #1 and `antigravity` #1
  each name `_order_report`'s guard on a closed replacement whose venue ID differs from the node's, and
  say that when `replaced` was missed and the replacement then fills, the old order is skipped as
  `replaced` and the replacement by the guard, so no report or fill reaches the node's order.
* `refused-replace-rejected-twice`: `claude-opus-high` #3 names `_apply`'s `order_replace_rejected` branch
  and says one modify refused both on the 422 and on the stream gives two `OrderModifyRejected`. `muse` #1
  names `_replace_refused` and that branch and says the same. `antigravity` #8 lists the stand-in and the
  test as its location, but its body names `_replace_refused` and the branch in `execution.py` (lines
  1232 to 1240) and says the two paths give two `OrderModifyRejected` for one modify.
* `replaced-applied-twice`: `codex` #6 gives `execution.py` lines 1228 to 1230 and 1280 to 1321, inside
  `_apply` and `_replaced` at `3a00f61` (it names no function; the rule accepts a line inside the
  location), and says a duplicate or delayed `replaced` generates a fresh `OrderUpdated`, since nothing
  checks the transition was already applied.
* `replacement-fill-before-replaced`: `antigravity` #2 names `_apply` and says a fill on the replacement
  that arrives before `replaced` is applied under the old quantity and dropped as an overfill.
* `pending-replace-fails-startup`: `antigravity` #3 names `OPEN_STATUSES` and `order_status_report`.
  `codex` #4 names `order_status_report` and gives lines 272 to 279, inside `OPEN_STATUSES` at
  `3a00f61`. Both say an order in `pending_replace` raises, failing start-up reconciliation.
* `replacement-moved-on-before-replaced`: `antigravity` #5 names `_replaced` and says a replacement read
  from Alpaca that is itself `replaced` raises and gives no `OrderUpdated`.
* `second-modify-refusal-taken-for-retry`: `antigravity` #6 names `_replace_refused` and
  `replaces_as_asked` and says the check compares only the terms the second request set, so its refusal
  is taken for a retry of the first and no `OrderModifyRejected` comes.
* `answers-kept-for-the-session`: `claude-opus-high` #4 and `muse` #3 name `_modify_order`, where the
  answer is stored, and `_replaced`; `codex` #7 gives lines 962 to 978 and 1292 to 1305, inside those two
  at `3a00f61`. Each says that when `replaced` is applied before the PATCH
  call resumes, the answer is stored afterwards and never removed. `muse` #3 says it is unsure the
  interleaving is reachable; the brief asks for such a defect to be reported, and the rule does not weigh
  confidence.
* `rejected-replacement-after-fill-never-reached`: all four name `_apply`'s `_finished` check (`codex`
  #5 by the name `_finished` and lines 1223 to 1224) and the test
  `test_rejected_replacement_is_modify_rejected_not_order_rejected`, and say the old order's fill
  makes `_apply` return before the rejected-replacement branch, so it never runs and the test passes
  without it.

Close calls, settled as missed:

* `codex` #12 says the stand-in's dual answer "creates two attempts to emit a rejection", but names only
  `conftest.py` and the test, not `_replace_refused` or the branch in `_apply`
  (`refused-replace-rejected-twice`: the location half fails).
* `codex` #6 also says a replacement read at `replaced` that is already `replaced` is refused
  (`replacement-moved-on-before-replaced`), but one finding counts for at most one defect, and #6's claim
  is the duplicate `replaced`.
* `codex` #9 says the retry check compares only the requested terms, but about another actor's
  replacement, and its claim is a 422 during the pending phase that rejects a modify which later
  succeeds; it does not say a second modify of the node is taken for a retry and never rejected
  (`second-modify-refusal-taken-for-retry`).
* `muse` #3 also says a replayed `replaced` would pop a stale answer and give a second `OrderUpdated`
  (`replaced-applied-twice`), but #3 counts for `answers-kept-for-the-session`.
* `muse` #5 and `antigravity` #9 name `_order_status_report`'s walk of `replaced_by` when the replacement
  cannot be read, not `_replaced` reading a replacement that has moved on
  (`replacement-moved-on-before-replaced`).
* `antigravity` #2 says the answer is left in `_answers`, and #7 that it is left when the rejected
  branch runs, but both count for other defects, and neither scenario is an answer stored after its
  `replaced` (`answers-kept-for-the-session`).
* `antigravity` #3 also says `_replaced` raises on a replacement in `pending_replace`
  (`replacement-moved-on-before-replaced`), but #3 counts for `pending-replace-fails-startup`, and #5
  is credited for that defect anyway.

### Unconfirmed findings

Listed, not triaged here. Each is a claim nothing has confirmed yet, not a finding judged false.

| Reviewer | Count | Findings |
|---|---|---|
| `claude-opus-high` | 3 | #5 a false "not in this slice" warning for every `replaced` order; #6 the new paper modify tests do not skip outside the regular session, and the node test modifies before the order is routed; #7 stale or contradictory test-coverage statements, and the chain cache not keeping a too-long miss |
| `codex` | 9 | #2 a transient error tracing a replacement hides an open order from reconciliation; #3 a replacement lookup with no HTTP timeout holds up every later trade update; #8 chains longer than 16 links lose the node's order after a cold restart; #9 a 422 while a replace is pending rejects a modify that later succeeds; #10 the direct paper test can leave orders or a position; #11 the node paper test can modify before the order is replaceable; #12 the stream-rejection test passes with the stream handler disabled; #13 the direct paper test's fixed grace can miss the confirmation; #14 the README and header contradict the code on events, refusals and the stop trigger type |
| `muse` | 5 | #4 a chain past `MAX_REPLACE_CHAIN` is not cached as a miss and is walked again; #5 a single-order report fails if the replacement it follows cannot be read; #6 a false "not in this slice" warning for every `replaced` order; #7 the README still states the old stop trigger type and `replaced` as unmapped; #8 the cancel redirect for a replaced order has no test that can fail |
| `antigravity` | 5 | #4 a modify to quantity 0 raises a `ValidationError` and leaves the order pending update; #9 a 404 while walking `replaced_by` is not caught, and the false warning for `replaced` orders; #10 a replacement traced before start-up reconciliation fills the cache stays foreign for the session; #11 defects in the two new paper tests; #12 "walked again after a restart" is false for a node restart |
| **Total** | 22 | |

## Both runs, as the cases stand now

The cases now hold 23 confirmed defects, and every reviewer here has reviewed all six ready cases: the
five of the first run and `order-modify`. Three of the 23 were entered after the first run was scored,
and count for its reports from this scoring on (README, "Scoring"): `n-hat-dsr-test-cannot-fail` in
`trial-ledger-gate` (entered with the first run's sheet), and `unholdable-client-id-aborts-startup` in
`stop-orders` and `session-day-without-return-dropped` in `trial-ledger-gate` (entered by #92). The first
run's sheet is not edited. Its reports are marked against those three entries here, by the same
two-scorer rule.

### The first run's reports against the three later entries

| Case | Defect | First reported by | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|---|---|
| `stop-orders` | `unholdable-client-id-aborts-startup` | `claude-opus-high`, `antigravity` | found (#1) | missed | missed | found (#3) |
| `trial-ledger-gate` | `n-hat-dsr-test-cannot-fail` | `claude-opus-high` | found (#3) | missed | missed | missed |
| `trial-ledger-gate` | `session-day-without-return-dropped` | `muse`, `antigravity` | missed | missed | found (#3) | found (#5) |

None of these findings was credited to another defect in the first run's sheet. `antigravity` #5 of
`trial-ledger-gate` was a close call there for `common-range-trim`, settled as missed.

* `claude-opus-high` #1 and `antigravity` #3 (`stop-orders`) name `_order_report`'s
  `ClientOrderId(order.client_order_id)` lookup and say an empty or non-ASCII ID now raises outside
  `order_status_report`'s `except ValueError`, so a closed such order fails the mass status and start-up.
* `claude-opus-high` #3 (`trial-ledger-gate`) names `test_a_pick_above_the_threshold_passes`' N-hat line
  and says it only checks that the DSR at N-hat exceeds the DSR, so three wrong computations still pass.
* `muse` #3 and `antigravity` #5 (`trial-ledger-gate`) name `daily_series` and say a session day with no
  return is dropped instead of refused, so the trial's series is shorter than its sessions.

Close calls, settled as missed: `muse` #3 and #4 of `stop-orders` name `order_status_report` and
`_order_report`, but about unmapped open statuses and the trigger type, not a client order ID
NautilusTrader refuses. `codex` #2 of `trial-ledger-gate` says a zero-return day can be kept or dropped,
but names `rank`, not `daily_series`, and is about two records' calendars disagreeing. `antigravity` #4
of `trial-ledger-gate` is about N-hat below 1 in `rank`, not the test.

These six marks were all counted unconfirmed in the first run's sheet; its unconfirmed totals stand as
scored there.

### Found over both runs

| Reviewer | First run, 13 defects | This run, 10 defects | Both runs, found / 23 | of them first reported itself | Found, not first reported itself |
|---|---|---|---|---|---|
| `claude-opus-high` | 3 | 4 | 7 | 2 | 5 |
| `codex` | 1 | 5 | 6 | 2 | 4 |
| `muse` | 3 | 3 | 6 | 3 | 3 |
| `antigravity` | 3 | 7 | 10 | 7 | 3 |

The first run's column is its sheet's 1, 1, 2 and 1 of 10, plus the marks above. "First reported itself"
is circular for the three later entries: each was entered from the very report scored here. And the
bias of point 3 above holds for this run's column and so for the total: on `order-modify` three of the
four had reviewed the commit before and the case holds what they found then.

Per case, over both runs:

| Case | Defects | `claude-opus-high` | `codex` | `muse` | `antigravity` |
|---|---|---|---|---|---|
| `cancel-all` | clean | – | – | – | – |
| `data-client-hardening` | 1 | 0 | 0 | 1 | 1 |
| `exec-client-first-slice` | 1 | 0 | 0 | 0 | 0 |
| `stop-orders` | 1 | 1 | 0 | 0 | 1 |
| `trial-ledger-gate` | 10 | 2 | 1 | 2 | 1 |
| `order-modify` | 10 | 4 | 5 | 3 | 7 |
| **Total** | 23 | 7 | 6 | 6 | 10 |

## Agreement between the two scorers

Two scorers marked the reports apart. Each had the case files as they stand on `main`, the README's
scoring section and the reports. For the later entries, each also had the first run's "Every mark" table,
to apply "one finding counts for at most one defect". Neither saw the other's sheet. The second
scorer's sheet is not kept in the repository, as the first run's was not; the figures below come from
comparing the two sheets cell by cell for this one.

* **Raw agreement: 52 of 52 marks** (100%). That is 40 marks on this run (4 reviewers × 10 defects) and
  12 on the first run's reports against the three later entries (4 × 3). Taken apart, 40 of 40 and 12 of
  12.
* The finding counts per report (7, 14, 8, 12) and the unconfirmed counts (3, 9, 5, 5) also agree, 4 of
  4 each, and so do the unconfirmed findings by number.
* **Disagreements: none**, so nothing was settled by rereading and **no mark is disputed**.
* Both sheets named the same close calls, listed above. The second scorer also recorded two found marks
  as held with some doubt: `codex` #7 says the answer is "stranded" rather than never removed, and
  `codex` #6 frames its claim around a chain of three orders. Both scorers marked both found.

Both scorers are models. They have not been calibrated against a person, so this agreement shows
consistency, not correctness (ADR 0046).

## Reading these numbers

With n confirmed defects, a found-rate p has a standard error of about √(p(1−p)/n). This run has n = 10,
all in one case, and found-rates from 0.3 to 0.7, so each has a standard error of about 14 to 16 points;
ADR 0046 puts the 95% interval on a difference at about ±44 points for 10 defects, and defects bunched
in one case widen it. The largest gap here is 40 points (`antigravity` 7, `muse` 3), within that, and the
case is biased toward three of the four reviewers (point 3), so this run ranks no reviewer above
another. Over both runs, n = 23 and the found-rates are 0.26 to 0.43; the largest gap is 4 defects (17
points), below the about 30 points the README gives for the whole set. That figure is itself too narrow
here: 20 of the 23 defects sit in two cases, which widens it (README, ADR 0046), and one of those two
is the biased case. The unconfirmed counts measure how
much a reviewer reports, not how much of it is right.

## Defects not entered: four fixes in `d5134ec`

`d5134ec` (#67) fixes four behaviours of `trial-ledger-gate`'s commit with tests: N-hat below 1, the
writes outside `record_trial`'s `try`, a record not in the writer's shape, and a bar stamped at 00:00
UTC. They were left out of the case because its message does not report any one of these tests failing
first ([`triage.md`](../2026-11-10-first/triage.md)). Its message says thirteen reproductions were
written against the code before the change, eleven failed and two passed, and names neither of the two.

Those tests have since been reported failing, one by one, on `d5134ec`'s parent's source, in a run made
outside this repository and not recorded in it; except for the one reproduced below, these results are
UNVERIFIED here:
`test_affine_copies_report_n_hat_at_one_instead_of_raising` (`ValueError: n_trials must be at least 1`),
`test_an_error_after_the_trial_is_asked_for_is_told_failed[config_key]` and `[outcome]`
(`KeyError: 'outcome'`), `test_a_record_not_in_the_writers_shape_stops_naming_it[leading zero]` and its
other ten cases (`KeyError`), and `test_daily_bars_stamped_at_utc_midnight_rank` (`RankingStopped`). That
run used today's locked environment with two names the parent lacks shimmed. One of the four was
reproduced for this sheet: `d5134ec`'s `tests/test_ranking.py`, with `mark_not_run` (absent before it)
replaced by a stub, run under today's locked environment with `PYTHONPATH` set to `d5134ec^`'s
`src/canon_trading`. `test_affine_copies_report_n_hat_at_one_instead_of_raising` failed with
`ValueError: n_trials must be at least 1, got 0.9999999999999998` from skfolio's
`expected_max_sharpe_ratio`; with `d5134ec`'s own source it passed. The other three are UNVERIFIED here.

**They are still not entered.** The README's rule is that a defect is confirmed when its fix is on `main`
"with a regression test whose commit message reports it failing (or hanging) without the fix". The fix
is `d5134ec`, and its message reports none of these four failing. A run made later, however clear, is
not that message. What would admit them is a change to the rule, in the README and ADR 0046 together:
for example, accepting a dated reproduction recorded in the case file (the fix's tests run on its
parent's source, the command, the environment and each failure) where the fix's message is silent. That
change is not made here.

## Redactions

The four reports are copied as written. None contained an absolute home path (the report path included),
a user, account or machine name, or an email address. `muse-1.md` names `/tmp/probe1`, a scratch path
that names no person or machine; it was left as written, as the first run left such paths.
