---
status: proposed
date: 2026-11-11
decision-makers: the owner for the direction (2026-11-11); the lead for the loop's shape, the lanes and the floor of sessions (2026-11-11); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# The nightly study loop: record, study, paper, compare, each part an adopted upstream; no paper rule changes from paper profit and loss

## Context and Problem Statement

The owner decided on 2026-11-11 that paper trading here learns and compounds day over day, built on
current upstream sources. Roadmap step 7 ranked one pre-registered plan once; step 8 reads a pick on
paper against its backtest and is not started. What joins them each night, which upstream carries each
part, what may change a paper rule, and what does a night with a few days of data say?

## Decision Drivers

* A false pass is the one failure the loop cannot have: a method that flatters is worse than none (ADR 0010).
* Every trial is counted, and the counter is never turned back (ADR 0010 step 1, ADR 0041).
* Upstream parts as shipped; a missing part is a stated gap, not code written here (rule 3).
* A standing duty runs where no session's exit drops it (ADR 0078 item 1).
* Paper profit and loss decides nothing (the owner's requirement of 2026-11-11; ADR 0083: paper is never
  gated by profit and loss).

## Considered Options

1. Tune paper rules from each day's paper profit and loss.
2. A new study each night, on that night's data.
3. One study a plan: each night its sample moves to all the recorded data, every registered row runs
   again and is ranked through the gate; paper fills are read against the replay; a pass is a proposal.
4. An optimiser's search each night (an Optuna sampler over the rows' ranges).

## Decision Outcome

Chosen: **option 3**. Option 1 is tuning on hindsight from one day, which the requirement refuses.
Option 2 turns the counter back every night: each pick would be deflated by that night's trials alone.
Option 4 makes every configuration the sampler tries a trial (ADR 0010 step 1), so N grows with the
search and not with what was registered.

**The loop.** `nt:` is `nautilus-trader@d53849b216e2` (v2.0.0rc8), `ap:` `alpaca-py@c42add7c0a70`
(v0.45.0), `op:` `optuna@01ddd17ab9f3` (v5.0.0), `sk:` `skfolio@772109053763` (v1.12.9).

| Step | Upstream part, as shipped | Here | State |
|---|---|---|---|
| Record | The stock stream's bars (`ap:alpaca/data/live/stock.py:99`); `ParquetDataCatalog.write_bars` and `consolidate_data` (`nt:python/nautilus_trader/persistence/__init__.pyi:182`, `:270`) | The recorder, with a record of its own, not yet on `main`: 1-minute `sip` bars, `<symbol>.ALPACA-1-MINUTE-LAST-EXTERNAL`, in `catalog/sip/` of the main checkout | Begun 2026-11-11; UNVERIFIED here until the first night's run reads it |
| Study | `get_intervals`, `query`, `get_missing_intervals_for_request` (`nt:…/persistence/__init__.pyi:387-418`); `Study.ask`, `tell` (`op:optuna/study/study.py:514`, `:600`); `deflated_sharpe_ratio` (`sk:src/skfolio/measures/_measures.py:1411`) | `canon_trading.nightly_study`, which calls `variant_study.run_study` and `ledger.move_sample` and holds no statistic | Built; the dry-run plan below; it waits on the gate's fix (term 6) |
| Paper | The session windows (ADR 0044, ADR 0050) | The incumbent rows in the next day's windows; a pick that passes beside the incumbent on the other account (ADR 0010 step 4, roadmap step 8) | Not built: no row is registered on `main` and none has passed |
| Compare | `ReportProvider.generate_fills_report` (`nt:python/nautilus_trader/analysis/reporter.py:111`); pandas v3.1.1 `merge`, `merge_asof` | `canon_trading.fill_gap`: each paper fill beside the replay's of the same instrument, side and UTC minute; the quoted half-spread at each entry | Built and tested offline; not wired, since a window's record holds event names and no fill price (`session_window.py:788`, `:1806`) |
| Cost of the next study | `MakerTakerFeeModel(maker_rate, taker_rate)` (`nt:python/nautilus_trader/execution/__init__.pyi:197-204`) | `taker_rate` = the median half-spread over the mid at entry, at one and two times (ADR 0083's doubled costs) | Shape only, UNCALIBRATED and unbuilt: every trial key holds zero rates (`backtest.py:172`, `ledger.py:636`; ADR 0067) |
| Schedule | launchd `StartInterval` (launchd.plist(5)) through `scripts/automation`; `mise run study:nightly` | A user agent every 30 minutes in the main checkout; a row of `scripts/automation status`; the session brief's morning line | Built; installed by the maintenance agent from `main` (ADR 0065); every firing waits until the gate's fix lands (term 6) |
| Handbook | none | The night's dated entry, written beside the ledger and landed in `docs/handbook/nights.md` by a session through `scripts/land`: every change lands reviewed (ADR 0017), and the scheduled job commits nothing | Built; the page is not yet on `main` |

**Terms.**

1. **The rule.** Nothing changes a paper rule from paper profit and loss. A row changes only when a
   study of a plan that may adopt passes, and then as a separate reviewed change: the nightly study
   writes a verdict and a proposal, never a setting.
2. **One study a plan.** Each night the sample moves to all the recorded data (`move_sample`, ADR 0041
   term 5) and every row runs again, so N is every key the study ever tried and T grows. A row may be
   added to a study and is never dropped from it: the gate stops on a key with no record on the current
   sample. What a plan does to drop a row is not decided here.
3. **A firing** is safe to repeat. Its cutoff is the latest 20:00 New York at least five minutes old
   (Alpaca's after-hours ends at 8:00pm ET; `zoneinfo`, never the host's zone). The sample starts where
   the study's first sample did and ends at the last bar every bar type holds at or before the cutoff. A
   range the catalog does not cover whole waits until the recorder closes it (UNVERIFIED: the recorder's
   record); `consolidate_data` covers only a stretch a load showed to be empty (ADR 0041 term 1). A
   settled night is not run again; a failed one is tried three times in all, since each attempt at a
   failing run adds a record, and the attempt is written before the pass so that a firing killed in it
   is counted. A range that ends before the study's sample is refused: the catalog lost bars. A firing
   that fails before it knows its night leaves the last night's record as the latest. Another cutoff
   than a session's close can be given only to a rehearsal on a temporary ledger and a copy of the
   catalog, which counts no trial that lasts; on the default ledger it is refused by name.
4. **The verdict only narrows the gate's.** `passed` needs the gate's pass (the pick's deflated Sharpe
   ratio at 0.95 or above, N every trial; ADR 0010), at least five sessions, and a pick whose Sharpe
   ratio is above zero. A session here is a New York day on which every bar type of the plan has a bar,
   and the gate's T must reach five as well: T counts UTC days (ADR 0035 term 3), which a winter day's
   last after-hours hour splits, so T alone reaches five after three trading days. Fewer sessions read
   `not yet: too few sessions`, with the gate's figure beside it, and the trials are recorded all the
   same; a rejected pick or a ranking that stopped is `not yet`.
   A gate pass for a pick whose ratio is not above zero is `failed`: eq. 2 gives such a pick under 0.5
   against a benchmark never below zero, so it is a defect of the gate and no result. Holding flat is
   that zero, not a trial: a trial that never trades stops the ranking (ADR 0010, Limits).
5. **What a night shows** (the lead, 2026-11-11): N, T, the pick, its Sharpe ratio with the count and
   spread of its session returns, its DSR and the verdict, in the status row, the morning line and the
   entry, so a ratio of implausible size is seen; and the paper windows that started in the 24 hours to
   the night's close, 20:00 New York on its day, counted from their records, as `paper:session` keeps
   them outside the temporary folder where it has (ADR 0050).
6. **Nothing is ranked before the gate's fix.** A firing waits in a checkout whose
   `tests/test_ranking.py` lacks the test of the defect that passed a negative ratio next to the
   threshold at 1.0 (`nightly_study.GATE_FIX_TEST`). That fix and its test are in no commit at this
   record's date (the defect has no record yet: UNVERIFIED here), so until they land every firing reads
   `waiting`; the name checked follows the test that lands.

**The plan there is, a dry run, registered before any trial.** It proves the path and adopts nothing.

| Item | Value | Where it comes from |
|---|---|---|
| Study | `nightly-dry-run-ma-cross-1-minute-spy-qqq`, N = 8 | this record |
| Strategy | `MaCross`, the one class the ledger builds (`ledger.py:188`, `:616`, `:955`) | ADR 0048; the window strategy `SessionMaCross` and the mover strategy are refused by `trial_config` |
| Periods, size, balance | (10, 20), (5, 20), (10, 50), (20, 100); 100 shares; `100_000 USD` | ADR 0048, upstream's EMA cross pairs |
| Bars | SPY and QQQ, `<symbol>.ALPACA-1-MINUTE-LAST-EXTERNAL`, the recorder's `sip` bars | the recorder |
| Sample | from the first instant on or after 2026-11-11 00:00 New York that the catalog covers for both, to the last common bar at or before each night's cutoff | term 3 |
| Costs | zero rates, as every trial key holds | ADR 0067 |

**UNCALIBRATED rows** (in `docs/handbook/rows/`): five sessions; the five minutes
after the close; three attempts; a quote no older than one minute at an entry; a firing every 30 minutes;
the cost shape above. Five sessions is where the measured false-pass rate falls under 5% (Confirmation),
and the lead confirmed it as the starting row on 2026-11-11. Two records disagree with it and are not
smoothed over. ADR 0083's floor is for merit on paper and pairs five sessions with 30 round trips; here
it is a floor on a replay's T, without the round trips, which the gate does not count. ADR 0041 refused a
floor on T for want of a source; this one is a row of the driver that only narrows the gate, not a gate
term, and its source is the measurement.

**Not built, each named.** A ledger that builds the mover strategy and keys a cost model: a later
change to the gate's modules, by the lead's ruling of 2026-11-11, and the first movers study waits for it. The
registered mover rows and the hold-to-close baseline: the strategy's own record, numbered 0088 and not
yet on `main`. Fill prices in a window's record, and the replay of a window. `BacktestRunConfig`'s
`chunk_size` (`nt:python/nautilus_trader/backtest/__init__.pyi:267`), which a replay of the whole market
needs: `canon_trading.backtest` does not set it, and a firing reads every bar of its plan from the start.

### Consequences

* Good: N and T are the ledger's, a night costs one firing, and a few days of data say `not yet`.
* Good: every part but the wiring is upstream's, and each gap is named where the work stops.
* Bad: until the ledger builds the mover strategy and a cost model, the loop studies a stand-in.
* Bad: a session day is a UTC day (ADR 0035 term 3), so a New York winter day's last after-hours hour
  books on the next UTC day and a Friday's makes a one-hour Saturday session. Every trial shares those
  days and T counts them, which is why the floor counts New York days (term 4).
* Bad: the firing runs in the checkout the maintenance agent fast-forwards, so a round may move the
  tree while a study runs; the modules a run uses are loaded at its start.
* Bad: a known coupling. `nightly_study` takes its atomic write and its commit stamp from
  `session_window` (`write_atomically`, made public there on `main`, and `running_commit`), so an
  offline job imports the paper window's module.
* Bad: a night ends at 20:00. Alpaca's overnight session, 8:00pm to 4:00am ET, in which paper windows
  trade (ADR 0044), is outside the recorder's hours and so outside every sample.

### Confirmation

* `tests/test_nightly_study.py`, offline, on 1-minute bars shaped as the adapter parses them: the first
  night creates the study on the recorded range; a second firing does nothing; the next night moves the
  sample and runs every row again with N unchanged; a night the catalog does not cover waits and moves
  nothing, then runs once consolidated; a failed night is tried three times, and a firing killed in its
  pass is counted; a catalog that lost bars is refused, back to a night already studied too; a failure
  before the night is known leaves the last night's record; a chosen cutoff runs on a temporary ledger
  and is refused on the default one; the verdict's cases. Each fix made after
  the code review was taken out again in turn, and the test that holds it failed.
* The floor, in that file: on the fixture's two New York days, three UTC session days, the gate itself
  passes the pick at a DSR of 0.9536, and the night reads `not yet: too few sessions`. On its first
  night, two UTC days, the ranking stops: skfolio returns a value that is not a number for two returns.
* Measured 2026-11-11, the pick of 8 series of T independent standard normal returns with no edge,
  through `skfolio.measures.deflated_sharpe_ratio(pick, sharpes)` as `rank` calls it, each Sharpe ratio
  the mean over the sample standard deviation: `false_passes((2, 3, 4, 5, 10, 20, 30, 60, 250), 40_000)`
  in `tests/test_nightly_study.py`, seed 20261111, 40,000 rounds a length. A test there holds the same
  measurement at 2,000 rounds: none readable at T = 2, over 5% at T = 3, under it at T = 5.

  | T | 2 | 3 | 4 | 5 | 10 | 20 | 30 | 60 | 250 |
  |---|---|---|---|---|---|---|---|---|---|
  | DSR ≥ 0.95 | none: not a number | 8.695% | 4.525% | 2.673% | 0.643% | 0.175% | 0.120% | 0.093% | 0.048% |

* `tests/test_fill_gap.py`, offline: a replay's fills are upstream's report; the gap's sign on each side;
  partial fills of a minute; what is found on one side only; the spread at entry and its rate.
* `scripts/test-maintenance` (the agent's plist, `differs`, each state of the row), `scripts/test-lanes`
  (the task's timeout, uninstall), `scripts/test-hooks` (the morning line).
* The first night's run, its command, exit and record, is added here when it has run; this record stays
  `proposed` until the lead has read it after that run.

## Evidence

Read 2026-11-11. Fetched means read in this session as raw text; resolved means read in a local copy at
the commit `canon/upstreams.lock.json` records; run means executed in this checkout.

* The owner's requirement of 2026-11-11, restated: paper trading learns and compounds day
  over day on upstream parts; a row changes between sessions only on a replay that beats the incumbent
  and holding flat; the nightly replay writes proposals only. An instruction, not evidence.
* NautilusTrader at the pin (https://github.com/nautechsystems/nautilus_trader/tree/d53849b216e20f2c122ca01b4dd0da377638b186)
  — every `nt:` line above; `docs/concepts/backtesting/apis-and-runs.md:155` ("Set `chunk_size` to a value
  in `[1, 1_000_000]` to enable streaming"). Resolved.
* alpaca-py, Optuna and skfolio at their pins — the `ap:`, `op:` and `sk:` lines above. Resolved.
* pandas v3.1.1 in `.venv` — `merge_asof`'s docstring ("Both DataFrames must be first sorted by the merge
  key"; `direction`, `by`, `tolerance`) and `DataFrame.merge`'s `indicator`. `bundled`.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "After-hours: 4:00pm - 8:00pm ET, Monday to
  Friday". Fetched, as the fetch tool quoted it.
* launchd.plist(5) on macOS 27.2 — `StartInterval`: an interval missed while the system sleeps or the job
  runs. `bundled` (the installed manual page).
* The measurement and the two fixture results under Confirmation. Run.
* `src/canon_trading/` (`ledger.py`, `backtest.py`, `variant_study.py`, `session_window.py`) at the lines
  cited. Read.
* ADR 0010, 0035, 0041, 0044, 0048, 0050, 0065, 0067, 0074, 0078, 0083 — as cited. Read. ADR 0080 and
  ADR 0081 hold the gate's numeric check and the ledger's writing terms since 2026-11-11; nothing here
  changes either.
* Where the requirement and the code disagreed: the dry run was to be `SessionMaCross`, whose config
  the ledger refuses by name, so it is `MaCross`; baselines of holding flat and hold-to-close and costs
  from recorded quotes were asked for, and none can be a trial today (term 4, the table, "Not built").
* Not relied on: NautilusTrader v2.0.0rc9 is released and not pinned (`scripts/canon outdated`); every
  `nt:` line is rc8's.
* `/code-review` at high effort, 2026-11-11, read only: the floor counted the gate's UTC days, so five
  were reached after three trading days; a failure before the night was known hid the last night's
  record; lost bars back to a studied night's end read as nothing new; a firing killed in its pass was
  never counted; windows after the settling firing were never counted; the heartbeat carried the
  firing's start; the brief's reminder read the session's own tree and the latest night only; a line
  could carry a home path; every firing read the study's whole history. Each is fixed, with a test.
* Second reader (`evidence-verifier`), 2026-11-11: the 37 line citations, the plan, the firing's and the
  verdict's rules, the other records and the measurement hold (re-run at 40,000 rounds). Corrected on its
  reading: that the gate's fix is in no commit, so a firing waits; the source of the handbook step; what
  ADR 0041 term 1 says of consolidation; the two disagreements over the floor; the overnight session; the
  T = 3 figure's rounding; wording. Not applied: the split the length rule asks for.
* `/code-review` at medium effort, 2026-11-11, on the branch rebased onto `main`: the windows were
  read only from the temporary folder a restart empties; their stretch ended at the last common bar,
  not the night's close; the rehearsal guard compared an unresolved default ledger with a resolved
  one; a fill upstream's report stamps at the epoch (a missing `ts_event`, `_ns_to_dt(ts or 0)`) was
  counted. Each is fixed, with a test. Left as limits: a hole the recorder never closes waits for
  good; a pass re-runs the whole range, so its cost grows against a fixed timeout.
