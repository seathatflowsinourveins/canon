# The rows table

One line per threshold or setting: its value, whether it is UNCALIBRATED or calibrated, the source or
study behind it, and the date it was set. "Calibrated" means a study or measurement made here set the
value and the row names it; every other row is UNCALIBRATED, however carefully it was chosen. A row
changes between sessions only, and only on ADR 0083's adoption test: a result above the incumbent and
above holding flat at doubled costs, with a deflated Sharpe ratio of 0.95 or above (ADR 0010); the
requirement's walk-forward wording is not what governs ([philosophy.md](philosophy.md)). This table is not
the registry: "registry" here means `canon/upstreams.json`, the pinned upstreams.

The strategy is to read its settings from these values, and a value missing here is unavailable, never
defaulted; that reading is not yet built (roadmap step 7). A row whose source is "the owner's
requirement" rests on the requirement of 2026-11-11, an instruction and not evidence; where ADR 0083
also states it, the row says so, and where it does not, no record holds it yet. `tests/test_handbook.py`
checks the table's form: a unique name, a value, one of the two states, a source and an ISO date. ADR 0087,
0088, 0089, 0091, 0101 and 0106 are `proposed` as this page is written: a row citing one is what is built and
on `main`, and open until the record is accepted.

| Row | Value | Status | Source or study | Date |
|---|---|---|---|---|
| `DSR_THRESHOLD` | 0.95, with N every trial in the ledger | UNCALIBRATED | ADR 0010, chosen under the owner's delegation; computed as ADR 0031 and ADR 0080 say | 2026-11-10 |
| `MERIT_PSR` | 0.95 | UNCALIBRATED | The owner's requirement: merit on paper, a draft only | 2026-11-11 |
| `MIN_ROUND_TRIPS` | 30 independent round trips | UNCALIBRATED | The owner's requirement; ADR 0083 states 30 round trips | 2026-11-11 |
| `MIN_SESSIONS` | 5 sessions | UNCALIBRATED | ADR 0089: the nightly study's floor on a replay's sessions, measured through `false_passes` in `tests/test_nightly_study.py` at 40,000 rounds a length (best-of-8 false passes at 0.95 of 8.695, 4.525, 2.673 and 0.643 per cent at T = 3, 4, 5 and 10 sessions), which a test there holds at 2,000 rounds. The owner's requirement and ADR 0083 state 5 sessions for merit on paper, with 30 round trips: another floor of the same size | 2026-11-11 |
| `NIGHTLY_CUTOFF_AGE` | 5 minutes after 20:00 New York before a night's cutoff is taken | UNCALIBRATED | ADR 0089 | 2026-11-11 |
| `NIGHTLY_ATTEMPTS` | 3 in all for a night whose run failed | UNCALIBRATED | ADR 0089 | 2026-11-11 |
| `NIGHTLY_FIRING` | Every 30 minutes | UNCALIBRATED | ADR 0089 | 2026-11-11 |
| `ENTRY_QUOTE_AGE` | A quote no older than 1 minute at an entry, for the spread it met | UNCALIBRATED | ADR 0089; `fill_gap.QUOTE_AGE` | 2026-11-11 |
| `MIN_REPLAY_CORRELATION` | 0.9 | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `MAX_SLIPPAGE_RATIO` | 1.5 times the slippage model | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `ADOPTION_COST_MULTIPLE` | 2 (doubled costs) | UNCALIBRATED | The owner's requirement (ADR 0083's adoption test) | 2026-11-11 |
| `PBO_LEVEL` | 0.05, reported as a diagnostic, never a pass or fail | UNCALIBRATED | The owner's requirement, carried as a diagnostic by the lead's ruling (ADR 0083, ADR 0027) | 2026-11-11 |
| `PBO_BLOCKS` | S = 16 | UNCALIBRATED | ADR 0027 (Algorithm 2.3) | 2026-11-11 |
| `PBO_MIN_TRIALS` | N at least 20 | UNCALIBRATED | ADR 0027 | 2026-11-11 |
| `PBO_MIN_ROWS` | T at least 32 | UNCALIBRATED | ADR 0027, skfolio's least for 16 blocks | 2026-11-11 |
| `UNIVERSE` | Every NMS common stock and ADR at any price | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `ENTRY_START` | 04:00 New York | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `MIN_MOVE` | 3 per cent, the listing condition on the move alone | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows); `movers_rank.MOVE_CONDITION` (ADR 0087) | 2026-11-11 |
| `MIN_VOLUME_RATIO` | 3 | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `MIN_TRADE_ACCELERATION` | 1.5 | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `MAX_NAMES` | 5 at once | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows); a paper window holds `PAPER_SLOTS` | 2026-11-11 |
| `MOVER_SLOTS` | 2 of the 5 | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `PAPER_SLOTS` | At most 7 held names in every part of a paper session; a run that asks for more is refused | UNCALIBRATED | ADR 0106: the lead's decision, on a request count measured on one fixture; a raise comes only from measured peaks | 2026-11-12 |
| `BASELINE_SESSIONS` | 5 sessions of each symbol's dollar volume, in 15-minute segments | UNCALIBRATED | `movers_rank.BASELINE_SESSIONS` (ADR 0087) | 2026-11-11 |
| `RANK_MAX_AGE_SECS` | 90 seconds; an older ranking means no entry | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `RUN_BARS` | 3 strictly rising one-minute closes | UNCALIBRATED | `MoverConfig` (ADR 0088; ADR 0083's row) | 2026-11-11 |
| `ENTRY_TIMEOUT_SECS` | 30 seconds, then the entry is cancelled | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `TRAIL` | 0.02 of the entry price, a fixed offset under the highest bid | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `CHASE_CAP` | 0.02 under the first sale price, the floor of a sale's re-pricing | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `CHASE_SECS` | 5 seconds between a sale's re-sends | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `COOLDOWN_SECS` | 300 seconds after a sale before the same name is bought again | UNCALIBRATED | `MoverConfig` (ADR 0088) | 2026-11-11 |
| `CANDIDATE_POOL_MULTIPLE` | 3 times the quota | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `RISK_PER_TRADE` | 1 per cent | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows). Neither a paper window nor the Mover replay sizes from it: both divide buying power by the slots (ADR 0106, ADR 0101); it remains for a run that asks for `FixedRiskSizer` explicitly (ADR 0088) | 2026-11-11 |
| `FLOW_CAP` | 5 per cent of the prior five minutes' dollar volume | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows); applied to a paper entry (`MoverConfig.flow_share` of `flow_bars`, ADR 0106), not in the Mover replay (ADR 0101) | 2026-11-11 |
| `BROKER_STOP_MAX` | Under 3 per cent | UNCALIBRATED | The owner's requirement (ADR 0083's starting rows) | 2026-11-11 |
| `ROTATION_LIFT` | More than 2 times the round-trip cost plus a margin; the margin unset | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `ACTION_BUDGET_SHARE` | About 80 per cent of the 200 calls a minute in use over a session, protection first | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `BUDGET_SOFT_LINE` | 140 calls in any 60 seconds on one account, for a mover node; entries yield above it | UNCALIBRATED | The owner's requirement; `MoverConfig.soft_line` (ADR 0088, ADR 0106); the lead's decision of 2026-11-11 that it governs a mover node, and that ADR 0083's run failing at 100 in any 60 seconds belongs to the SPY session window's stack check | 2026-11-11 |
| `BUDGET_FAIL_LINE` | 180 calls in any 60 seconds on one account, for a mover node; a rolling peak at it, or any 429, fails the window | UNCALIBRATED | The owner's requirement; the lead's decision of 2026-11-11 that it governs a mover node, as for `BUDGET_SOFT_LINE`; ADR 0106 | 2026-11-11 |
| `IDLE_INVESTIGATE` | 15 minutes with no action | UNCALIBRATED | The owner's requirement | 2026-11-11 |
| `ALPACA_CALLS_PER_MINUTE` | 200, a working figure, every call counted | UNCALIBRATED | ADR 0049 (archived documentation and an Alpaca staff post; no current page states it) | 2026-11-10 |
| `MAX_ORDER_SUBMIT_RATE` | 50 a minute; 180 a minute on paper nodes | UNCALIBRATED | ADR 0049, a quarter of `ALPACA_CALLS_PER_MINUTE`; ADR 0106 for paper nodes | 2026-11-11 |
| `MAX_ORDER_MODIFY_RATE` | 25 a minute; 180 a minute on paper nodes | UNCALIBRATED | ADR 0049, an eighth of `ALPACA_CALLS_PER_MINUTE`; ADR 0106 for paper nodes | 2026-11-11 |
| `MAX_NOTIONAL_PER_ORDER` | $1,000 for the symbol | UNCALIBRATED | ADR 0049; a Mover window bounds each order at its entry figure times 10 instead (`mover_window.OUTER_BOUND_MULTIPLE`; ADR 0091, ADR 0106) | 2026-11-10 |
| `WINDOW_MAX_MINUTES` | 60, clipped further to the session's end and to the longest window the account allows | UNCALIBRATED | ADR 0044 | 2026-11-10 |

The selectable trail and re-entry rows below are a window's settings, not tuned by the bars study.
The bars replay accepts only the fixed trail and the existing clock cooldown; another mode is refused
by its field and value. These window rows are excluded from its trial keys and decode as the fixed
rows. The defaults remain the fixed trail and the 300-second clock cooldown (the lead, 2026-11-12).

| Row | Value | Status | Source or study | Date |
|---|---|---|---|---|
| `TRAIL_MODE` | fixed by default; atr, tier or ratchet by explicit window selection | UNCALIBRATED | Source b01f4c4; lead's ruling, 2026-11-12, preserves main's default | 2026-11-12 |
| `ATR_K` | 1; alternates 2, 3 | UNCALIBRATED | Source b01f4c4; lead, no primary source | 2026-11-12 |
| `ATR_N` | 14 complete one-minute bars; alternates 7, 28 | UNCALIBRATED | Source b01f4c4; NautilusTrader AverageTrueRange at d53849b216e2; window selection | 2026-11-12 |
| `SPREAD_FLOOR_M` | 2 quoted spreads; alternates 1, 3 | UNCALIBRATED | Source b01f4c4; lead, no primary source | 2026-11-12 |
| `LIQUIDITY_TIER_CUT` | 1,000,000 baseline dollars a minute; alternates 500,000, 2,000,000 | UNCALIBRATED | Source b01f4c4; owner's tier stand-in, not market capitalization | 2026-11-12 |
| `RATCHET_R` | 2 initial trail distances of gain; alternates 1, 3 | UNCALIBRATED | Source b01f4c4; lead's once-only ratchet | 2026-11-12 |
| `RATCHET_G` | 0.5 of the initial trail; alternates 0.25, 0.75 | UNCALIBRATED | Source b01f4c4; lead's once-only ratchet, subject to spread floor | 2026-11-12 |
| `RATCHET_BASE` | atr; alternate tier | UNCALIBRATED | Source b01f4c4; paired window arm | 2026-11-12 |
