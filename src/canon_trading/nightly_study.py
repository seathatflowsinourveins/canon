"""The nightly study: each night, the registered rows replayed on the recorded data and ranked.

Contract: ADR 0089, the loop record -> study -> paper -> compare, of which this module is the study
step. It is wiring: the plan is data, each configuration is recorded and the study ranked by
`canon_trading.variant_study.run_study` (ADR 0010's protocol on the ledger of ADR 0035, the pass
rule of ADR 0048), and the sample grows by `canon_trading.ledger.move_sample` (ADR 0041 term 5).
It computes no statistic and holds no strategy logic. Upstream parts, read 2026-11-11:

* NautilusTrader v2.0.0rc8 `ParquetDataCatalog.get_intervals`, `query` and
  `get_missing_intervals_for_request` (`python/nautilus_trader/persistence/__init__.pyi:387-418`
  at the pinned commit `d53849b216e2`): what the catalog holds of a bar type, and what a range
  lacks, the check `canon_trading.backtest` makes before a run.
* Optuna 5.0.0 through `canon_trading.ledger`: one study, its samples kept in order, every record
  kept (`move_sample`, `samples`, `open_study`).
* Python's `zoneinfo` (`America/New_York`) for the session's end, so the host's time zone is never
  read; Alpaca's after-hours session ends at 8:00pm ET
  (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, "Extended Hours Trading", as
  `canon_trading.session_window` reads it).

One firing (`run_nightly`), safe to repeat, so a schedule may fire it every half hour:

1. **The gate's fix.** Nothing is ranked from a checkout whose `tests/test_ranking.py` lacks
   `GATE_FIX_TEST`, the test of the defect that let a negative ratio next to the threshold pass:
   the firing waits.
2. **The range.** The cutoff is the latest 20:00 New York at least `SETTLE` old (`last_close`).
   The sample starts where the study's first sample started, or, for a new study, at the first
   instant on or after the plan's `first` that the catalog covers for every bar type of the plan;
   it ends at the last bar every bar type holds at or before the cutoff. A range the catalog does
   not cover whole (`get_missing_intervals_for_request`) waits until the recorder has closed the
   night's holes, since `record_trial` would refuse the sample (ADR 0041 term 1).
3. **The sample.** No study: it is created on the range. A study whose current sample is the
   range: nothing moves. A range that ends later: the sample is moved, so every configuration runs
   again on all the data and N stays every key the study ever tried (ADR 0041). A range that ends
   earlier than the sample is refused before the night is named, whichever earlier night it ends
   on: the catalog lost bars.
4. **The pass.** `run_study` records each configuration whose latest record on the sample did not
   run to the end, and ranks.
5. **The verdict** (`verdict`), which only ever narrows the gate's: `passed` needs the gate's pass
   (`RankingReport.passed`, the pick's deflated Sharpe ratio at 0.95 or above), at least
   `MIN_SESSIONS` sessions, and a pick whose Sharpe ratio is above zero. A ranking that stopped,
   too few sessions or a rejected pick is `not yet`, with the gate's figures beside it; too few
   sessions is said first, whatever the gate's figure. A gate pass for a pick whose Sharpe ratio is
   not above zero is `failed`: eq. 2 gives such a pick less than 0.5 against a benchmark that is
   never below zero, so the pass is a defect, not a result.
6. **The record.** `latest.json` and `<study>/<day>.json` beside the ledger, in `nightly/`, local
   as the ledger is; `<study>/<day>.md`, the night's entry for `docs/handbook/nights.md`, which a
   session lands (a scheduled job neither commits nor pushes); and `last-firing.json` as each
   firing ends, which `scripts/automation status` reads for the schedule.

A night that is settled (`passed` or `not yet`) is not run again; a failed one is tried again by
the next firings, `ATTEMPTS` times in all for one range, since each attempt at a failing run adds a
record to the ledger. The attempt is written in the night's own record, as `running`, before the
pass begins, so a firing killed in its pass (the task's timeout) is counted too. A firing that
waited, or failed before it knew its night, leaves the last night's record as the latest: its exit
code and its heartbeat say what it found, and they clear with the next firing that works.

**Sessions.** `MIN_SESSIONS` is a registered row (`docs/handbook/rows.md`), and it counts New York
days on which every bar type of the plan has a bar (`new_york_days`), not the gate's T. A session
day of the ledger is a UTC day (ADR 0035 term 3), so the last after-hours hour of a New York
winter day, 19:00 to 20:00, books on the next UTC day and a Friday's makes a one-hour Saturday
session: every trial shares those days, so the ranking is unaffected, but T reaches five after
three trading days. The floor is held on both: the New York days, and the gate's T.

The gate has no floor on T (ADR 0041 refused one for want of a source), and eq. 2 is an asymptotic
statement. Measured through skfolio as the gate calls it, on the best of 8 series of independent
normal returns with no edge (40,000 rounds a length; ADR 0089 holds the run): at T = 2 the
statistic is not a number, so the ranking stops; the pick reaches 0.95 in 8.7% of rounds at T = 3,
4.5% at T = 4, 2.7% at T = 5 and 0.6% at T = 10. Five is where that rate is under the nominal 5%,
and it is ADR 0083's floor of sessions for merit on paper. A study below it records its trials all
the same.

The plan there is today, `DRY_RUN`, proves the path and adopts nothing: `MaCross`, the one class
the ledger builds (`canon_trading.ledger.trial_config`), over upstream's four EMA cross period
pairs (ADR 0048), on the recorder's 1-minute sip bars of SPY and QQQ, at the zero fee rates every
trial key holds today (ADR 0067). A plan that could adopt a row waits for a ledger that builds the
mover strategy and keys a cost model (ADR 0089).

The paper windows counted with a night are those that started in the 24 hours to its end, from
20:00 New York the evening before, when Alpaca's overnight session for the trade date begins, so
every one has started when the night is studied (`windows_to`, from the records
`canon_trading.session_window.write_record` leaves). Their fill gap against the replay
(`canon_trading.fill_gap`) is not measured yet: a window's record holds each order's event names
and no fill price.

A line (`Night.line`) is printed into sessions by the session brief, so it holds no account
detail and writes the home folder `~`, as `scripts/automation status` does; the full error of a
failed night stays in its local record.

Two firings at once: launchd runs one instance of a job. A run by hand beside it is refused at
the pass by the study's pass lock (`canon_trading.variant_study`); at worst it moved the sample
first, which stops the other pass by name, and the next firing finds the sample moved and runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from canon_alpaca.constants import VENUE
from canon_alpaca.data import NEW_YORK
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model import BarType
from nautilus_trader.model import NautilusDataType
from nautilus_trader.persistence import ParquetDataCatalog

from . import variant_study
from .ledger import LedgerNotFound
from .ledger import default_ledger_path
from .ledger import move_sample
from .ledger import open_study
from .ledger import samples
from .ma_cross import MaCrossConfig
from .ranking import THRESHOLD
from .ranking import RankingReport
from .ranking import RankingStopped
from .returns_matrix import session_returns
from .session_window import running_commit
from .session_window import write_atomically

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ledger import Sample
    from .variant_study import Recorded

SESSION_END = time(20, 0)
"""Alpaca's after-hours session ends at 8:00pm New York (module header)."""
SETTLE = timedelta(minutes=5)
"""How long after the session's end its bars are read: the recorder's last minute is written."""
MIN_SESSIONS = 5
"""The fewest sessions a `passed` is read on. A registered row (module header)."""
ATTEMPTS = 3
"""How many firings try one range before the night is left failed for a person to read."""
GATE_FIX_FILE = Path("tests") / "test_ranking.py"
GATE_FIX_TEST = "test_a_negative_ratio_next_to_the_threshold_never_ranks_beside_a_pick_that_passes"
"""The test of the gate's defect that passed a negative ratio next to the threshold at 1.0."""
CHECKOUT = Path(__file__).resolve().parents[2]

PASSED = "passed"
NOT_YET = "not yet"
FAILED = "failed"
WAITING = "waiting"
NOTHING_NEW = "nothing new"
RUNNING = "running"
"""A night's own record while its pass runs: left so by a firing that was killed, which the next
firing counts as an attempt."""
SETTLED = (PASSED, NOT_YET)
"""The verdicts of a night that ran to its ranking; such a night is not run again."""
NO_FILL_PRICES = "not measured: a window's record holds no fill prices yet (ADR 0089)"


@dataclass(frozen=True)
class Plan:
    """A study's plan, fixed before any trial: changing any of it is a new study (ADR 0048)."""

    study: str
    symbols: tuple[str, ...]
    periods: tuple[tuple[int, int], ...]
    bar_spec: str
    first: datetime
    """The sample starts at the first instant on or after this that the catalog covers."""
    adopts: bool
    """Whether a `passed` of this plan may change a paper row. A path proof's may not."""
    read_as: str

    def bar_type(self, symbol: str) -> BarType:
        return BarType.from_str(f"{symbol}.{VENUE}-{self.bar_spec}")

    def configs(self) -> list[MaCrossConfig]:
        """One `MaCrossConfig` per configuration, instrument first, in the plan's order."""
        return [
            MaCrossConfig(
                bar_type=self.bar_type(symbol),
                trade_size=variant_study.TRADE_SIZE,
                fast_period=fast,
                slow_period=slow,
            )
            for symbol in self.symbols
            for fast, slow in self.periods
        ]


# The plan pre-registered in ADR 0089. The trade size and the starting balance are ADR 0048's
# (`variant_study.TRADE_SIZE`, `STARTING_BALANCES`), which `run_study` keys every trial on.
DRY_RUN = Plan(
    study="nightly-dry-run-ma-cross-1-minute-spy-qqq",
    symbols=variant_study.SYMBOLS,
    periods=variant_study.PERIODS,
    bar_spec="1-MINUTE-LAST-EXTERNAL",
    first=datetime(2026, 11, 11, tzinfo=NEW_YORK),
    adopts=False,
    read_as="a proof of the path on MaCross at zero cost: it adopts nothing",
)


class Waiting(Exception):
    """The night cannot be studied yet, for a reason that clears by itself; nothing was written to
    the ledger."""


@dataclass
class Night:
    """What one firing found, written as JSON beside the ledger (module header, step 6)."""

    study: str
    status: str
    why: str = ""
    day: str = ""
    """The New York day the sample ends on."""
    sample: int | None = None
    start: str = ""
    end: str = ""
    end_ns: int | None = None
    bars: list[str] = field(default_factory=list)
    """The bar types the plan reads."""
    n: int | None = None
    t: int | None = None
    """The gate's T: the UTC session days every trial shares."""
    sessions: int | None = None
    """The New York days on which every bar type of the plan has a bar: what the floor counts."""
    pick: str = ""
    sharpe: float | None = None
    returns: dict[str, float] = field(default_factory=dict)
    """The pick's session returns as the gate read them: their count, mean, standard deviation,
    least and greatest, shown beside its Sharpe ratio so a ratio of implausible size is seen."""
    dsr: float | None = None
    gate_passed: bool | None = None
    threshold: float = THRESHOLD
    min_sessions: int = MIN_SESSIONS
    planned: int = 0
    recorded: int = 0
    already_run: int = 0
    run_failures: list[str] = field(default_factory=list)
    adopts: bool = False
    read_as: str = ""
    costs: str = "zero fee rates, the ones every trial key holds (ADR 0067): UNCALIBRATED"
    summary: str = ""
    """The gate's own report, or the message it stopped with."""
    paper: dict[str, Any] = field(default_factory=dict)
    rows_changed: list[str] = field(default_factory=list)
    attempt: int = 1
    commit: str = field(default_factory=running_commit)
    started: str = ""
    ended: str = ""
    line: str = ""
    """The night in one line, as the session brief and `scripts/automation status` print it."""


def last_close(now: datetime) -> datetime:
    """The latest 20:00 New York that is at least `SETTLE` before `now`."""
    local = now.astimezone(NEW_YORK)
    close = datetime.combine(local.date(), SESSION_END, tzinfo=NEW_YORK)
    if local < close + SETTLE:
        close = datetime.combine(local.date() - timedelta(days=1), SESSION_END, tzinfo=NEW_YORK)
    return close


def gate_fix_in(checkout: Path = CHECKOUT) -> bool:
    """Whether `checkout` holds the test of the gate's fix (module header, step 1)."""
    try:
        return f"def {GATE_FIX_TEST}(" in (checkout / GATE_FIX_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as error:
        raise OSError(
            f"cannot read gate prerequisite {GATE_FIX_FILE}: {type(error).__name__}: {error}"
        ) from error


def _utc(ns: int) -> str:
    return unix_nanos_to_dt(ns).strftime("%Y-%m-%d %H:%M UTC")


def recorded_range(
    catalog_path: Path,
    bar_types: Sequence[str],
    *,
    first: int,
    cutoff: int,
    start: int | None,
    since: int | None = None,
) -> tuple[int, int]:
    """The range the catalog holds for every one of `bar_types` up to `cutoff` (module header, step 2).

    From `start`, a study's own, or with none from the first instant at or after `first` that every
    bar type's intervals reach. `Waiting` if a bar type has no bar there, or the catalog does not
    cover the range whole. The last bar of a bar type is looked for from `since`, the end of the
    study's current sample, and from the start only when there is none after it, so a firing with
    nothing new reads a night's bars and not the study's whole history. Each call builds its own
    catalog object, since one asked again after a write can answer from before it
    (`canon_trading.backtest`).

    With `since`, the previously studied range must still be covered before any waiting verdict:
    missing historical coverage is lost bars, even if too little remains to form a new range.
    """
    path = Path(catalog_path).resolve()
    if not path.is_dir():
        if since is not None:
            raise ValueError(f"the catalog lost bars: there is no catalog at {path}")
        raise Waiting(f"there is no catalog at {path}")
    catalog = ParquetDataCatalog(str(path))
    low = first if start is None else start
    starts, ends = [], []
    for bar_type in bar_types:
        if since is not None:
            lost = catalog.get_missing_intervals_for_request(
                low, since, NautilusDataType.Bar, bar_type
            )
            if lost:
                parts = ", ".join(f"{_utc(lo)} to {_utc(hi)}" for lo, hi in sorted(lost))
                raise ValueError(
                    f"the catalog lost bars: {bar_type} no longer covers the studied range "
                    f"from {_utc(low)} to {_utc(since)} (missing {parts})",
                )
        reach = [
            lo
            for lo, hi in catalog.get_intervals(NautilusDataType.Bar, bar_type)
            if hi >= low and lo <= cutoff
        ]
        bars: list[Any] = []
        if reach:
            for since_or_low in [low] if since is None or since <= low else [since, low]:
                bars = catalog.query(
                    NautilusDataType.Bar, identifiers=[bar_type], start=since_or_low, end=cutoff
                )
                if bars:
                    break
        if not bars:
            raise Waiting(
                f"the catalog holds no {bar_type} bars from {_utc(low)} to {_utc(cutoff)}"
            )
        starts.append(max(low, min(reach)))
        ends.append(max(bar.ts_init for bar in bars))
    begin, end = (max(starts) if start is None else start), min(ends)
    if not begin < end:
        raise Waiting(f"the bar types share no range from {_utc(begin)} to {_utc(cutoff)}")
    for bar_type in bar_types:
        missing = catalog.get_missing_intervals_for_request(
            begin, end, NautilusDataType.Bar, bar_type
        )
        if missing:
            parts = ", ".join(f"{_utc(lo)} to {_utc(hi)}" for lo, hi in sorted(missing))
            raise Waiting(
                f"the catalog does not cover {bar_type} from {_utc(begin)} to {_utc(end)} yet "
                f"(missing {parts}): the recorder has not closed the night's holes",
            )
    return begin, end


def new_york_days(catalog_path: Path, bar_types: Sequence[str], start: int, end: int) -> int:
    """How many New York days have a bar of every one of `bar_types` from `start` to `end`.

    A bar's day is the New York date of its open (`ts_event`, as the adapter stamps a bar), so the
    last after-hours hour of a winter day, which a UTC midnight cuts off, stays with its day. These
    are the sessions `MIN_SESSIONS` counts (module header). Read only by a firing that runs a pass.
    """
    catalog = ParquetDataCatalog(str(Path(catalog_path).resolve()))
    shared: set[date] | None = None
    for bar_type in bar_types:
        days = {
            unix_nanos_to_dt(bar.ts_event).astimezone(NEW_YORK).date()
            for bar in catalog.query(
                NautilusDataType.Bar, identifiers=[bar_type], start=start, end=end
            )
        }
        shared = days if shared is None else shared & days
    return len(shared or ())


def verdict(plan: Plan, outcome: RankingReport | RankingStopped, sessions: int) -> tuple[str, str]:
    """The night's status and why, from what the gate said and the `sessions` the sample holds
    (`new_york_days`; module header, step 5)."""
    stopped = isinstance(outcome, RankingStopped)
    said = (
        f"the ranking stopped: {_first_line(str(outcome))}"
        if isinstance(outcome, RankingStopped)
        else f"the gate's T is {outcome.t} UTC days and its DSR {outcome.dsr:.4f}, which "
        f"{'would pass' if outcome.passed else 'rejects'} at {THRESHOLD}"
    )
    if isinstance(outcome, RankingReport) and outcome.passed and not outcome.sharpe > 0:
        return FAILED, (
            f"the gate passed a pick whose Sharpe ratio is {outcome.sharpe:.6g}, not above zero, "
            f"at DSR {outcome.dsr:.4f}: a defect in the gate, and nothing is adopted"
        )
    if sessions < MIN_SESSIONS:
        short = f"{_days(sessions)} of bars {'is' if sessions == 1 else 'are'}"
        return NOT_YET, f"too few sessions: {short} below the floor of {MIN_SESSIONS}; {said}"
    if isinstance(outcome, RankingReport) and outcome.t < MIN_SESSIONS:
        return NOT_YET, (
            f"too few sessions: the gate's T is {outcome.t} UTC days, below the floor of "
            f"{MIN_SESSIONS} (its DSR is {outcome.dsr:.4f})"
        )
    if stopped:
        return NOT_YET, said
    if not outcome.passed:
        return NOT_YET, f"the pick's DSR {outcome.dsr:.4f} is below {THRESHOLD}"
    if not plan.adopts:
        return PASSED, f"the gate passes the pick; {plan.read_as}"
    return PASSED, "the gate passes the pick"


def _days(count: int) -> str:
    return "1 New York day" if count == 1 else f"{count} New York days"


def _first_line(text: str, limit: int = 240) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def windows_to(records: Path | None, until: datetime) -> dict[str, Any]:
    """The paper windows that started in the 24 hours up to `until`, the night's end, counted from
    their records.

    Each `window-<stamp>.json` in `records` (`canon_trading.session_window.write_record`), its
    stamp the window's start in UTC. The stretch is the one the night's sample closes: from the
    session's end the evening before, when Alpaca's overnight session for this trade date begins,
    to this one, both at 20:00 New York, so every window in it has started when the night is
    studied and a later firing would count the same ones. A window whose record still says
    `running` is counted as that, and one that cannot be read as unreadable, never skipped.
    """
    found: dict[str, Any] = {"to": until.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M New York")}
    found.update(ran=0, ok=0, failed=0, running=0, fills=0, unreadable=0)
    found.update(fill_gap_bps=None, fill_gap=NO_FILL_PRICES)
    if records is None or not Path(records).is_dir():
        return {**found, "fill_gap": "not read: no folder of window records was given or found"}
    for record in sorted(Path(records).glob("window-*.json")):
        try:
            started = datetime.strptime(record.stem, "window-%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        if not until - timedelta(days=1) < started <= until:
            continue
        try:
            window = json.loads(record.read_text(encoding="utf-8"))
            result, fills = str(window["result"]), int(window["fills"])
        except OSError, ValueError, KeyError, TypeError:
            found["unreadable"] += 1
            continue
        found["ran"] += 1
        found["ok"] += result == "ok"
        found["failed"] += result.startswith("failed")
        # a window a signal stopped whose wind-down never finished (session_window._shown_record) did not end
        found["running"] += result == RUNNING or result.startswith("stopping")
        found["fills"] += fills
    return found


def _public(text: str) -> str:
    """`text` with the home folder written `~`, as `scripts/automation status` writes a row: a
    line is printed into sessions and may be quoted into a public record."""
    return text.replace(str(Path.home()), "~")


def _line(night: Night) -> str:
    """The night in one line (`Night.line`): no account detail, and the home folder as `~`."""
    if night.status == WAITING:
        return _public(f"nightly study ({night.study}): waiting: {night.why}")
    head = (
        f"nightly study {night.day} ({night.study})"
        if night.day
        else f"nightly study ({night.study})"
    )
    figures = [f"N={night.n}" if night.n is not None else f"{night.planned} configurations planned"]
    if night.t is not None:
        figures.append(f"T={night.t} over {_days(night.sessions or 0)}")
    if night.pick:
        figures.append(f"pick {night.pick}")
    if night.sharpe is not None:
        figures.append(f"Sharpe {night.sharpe:.4g} per session{_spread(night)}")
    if night.dsr is not None:
        figures.append(f"DSR {night.dsr:.4f}")
    paper = night.paper
    windows = (
        f" | paper windows to {paper['to']}: {paper['ran']} ran, {paper['ok']} ok, "
        f"{paper['fills']} fills, fill gap {paper['fill_gap']}"
        if paper
        else ""
    )
    return _public(f"{head}: {', '.join(figures)}, {night.status}: {night.why}{windows}")


def _spread(night: Night) -> str:
    """The count and spread of the pick's session returns, in brackets, or nothing if not read."""
    found = night.returns
    if not found:
        return ""
    return (
        f" ({found['count']:.0f} session returns from {found['least']:+.4%} to "
        f"{found['greatest']:+.4%}, mean {found['mean']:+.4%}, sd {found['sd']:.4%})"
    )


def entry(night: Night) -> str:
    """The night's dated entry for `docs/handbook/nights.md`, as markdown (module header, step 6).

    What was recorded, what the study tried with every configuration counted, what the paper
    windows did against the replay, and which registered row changed. It holds counts, dates and
    the gate's figures only: no path, no account and no error text, since a session lands it in a
    public repository.
    """
    tried = f"{night.planned} configurations planned"
    if night.n is not None:
        tried += f", N = {night.n} counted from the ledger"
    if night.sessions is not None:
        tried += f", {_days(night.sessions)} of bars"
    if night.t is not None:
        tried += f", T = {night.t} UTC session days"
    if night.pick:
        tried += f"; pick {night.pick}"
    if night.sharpe is not None and night.dsr is not None:
        tried += (
            f", Sharpe ratio {night.sharpe:.4g} per session{_spread(night)}, DSR {night.dsr:.4f}"
        )
    why = (
        "the run failed; its local record holds the error" if night.status == FAILED else night.why
    )
    paper = night.paper
    windows = (
        f"{paper['ran']} windows started in the 24 hours to {paper['to']}, {paper['ok']} ok, "
        f"{paper['failed']} failed, {paper['running']} not ended, {paper['fills']} fills; fill "
        f"gap against the replay {paper['fill_gap']}"
        if paper
        else "not read"
    )
    changed = "; ".join(night.rows_changed) if night.rows_changed else "none"
    recorded = f"{', '.join(night.bars)} from {night.start} to {night.end}"
    return "\n".join(
        [
            f"## {night.day}, {night.study}",
            "",
            f"* **Recorded.** {recorded} (sample {night.sample}).",
            f"* **Study** `{night.study}` ({night.read_as}): {tried}. Verdict: {night.status}: {why}.",
            f"* **Costs.** {night.costs}.",
            f"* **Paper against the replay.** {windows}.",
            f"* **Rows changed.** {changed}.",
            "",
        ]
    )


def _read(path: Path) -> dict[str, Any] | None:
    """A saved record, or none only when it is absent; unreadable evidence is never absence."""
    try:
        read = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(
            f"cannot read nightly record {path.name}: {type(error).__name__}: {error} ({path})"
        ) from error
    if not isinstance(read, dict):
        raise TypeError(f"nightly record {path.name} must contain a JSON object ({path})")
    return read


def _write(out: Path, night: Night, *, latest: bool = True) -> None:
    """Write the night's records (module header, step 6): its own `<study>/<day>.json` when it has
    a day, with its entry once it is settled, and `latest.json` unless `latest` is off."""
    night.why = _public(night.why)
    night.line = _line(night)
    body = json.dumps(asdict(night), indent=2)
    out.mkdir(parents=True, exist_ok=True)
    if night.day:
        folder = out / night.study
        folder.mkdir(exist_ok=True)
        write_atomically(folder / f"{night.day}.json", body)
        if night.status in SETTLED:
            write_atomically(folder / f"{night.day}.md", entry(night))
    if latest:
        write_atomically(out / "latest.json", body)


def _heartbeat(out: Path, status: str, line: str) -> None:
    """Write `last-firing.json`, stamped by the clock as it is written: the end of the firing, which
    `scripts/automation status` counts an interval and the longest firing from."""
    out.mkdir(parents=True, exist_ok=True)
    at = datetime.now(UTC)
    stamp = {"at": at.strftime("%Y%m%dT%H%M%SZ"), "epoch": at.timestamp()}
    write_atomically(
        out / "last-firing.json", json.dumps({**stamp, "status": status, "line": line}, indent=2)
    )


def _known(plan: Plan, ledger: Path) -> list[Sample] | None:
    """The samples of the plan's study, oldest first, or none if the ledger holds no such study."""
    try:
        with open_study(plan.study, ledger) as study:
            return samples(study)
    except LedgerNotFound:
        return None


def _default_ledger() -> Path | None:
    """The default ledger's path, or none where there is no default (outside a git checkout)."""
    try:
        return default_ledger_path().resolve()
    except LedgerNotFound:
        return None


def run_nightly(
    plan: Plan = DRY_RUN,
    *,
    catalog_path: Path,
    ledger_path: Path | None = None,
    out_dir: Path | None = None,
    windows_dir: Path | None = None,
    now: datetime | None = None,
    cutoff: datetime | None = None,
    checkout: Path = CHECKOUT,
) -> Night:
    """One firing (module header): study the latest recorded night once, and write what it found.

    Returns the night, whose `status` is `passed`, `not yet`, `failed`, `waiting` or `nothing new`.
    A night that reached its pass is written as a record. A firing that waited, or failed before
    it knew its night, becomes the latest record only while no night's record is the latest: once
    one is, it stays, and the firing's exit code and heartbeat say what this firing found, so a
    failure that passes by itself does not hide the last night studied. Every firing that returns
    leaves its heartbeat.

    `cutoff` replaces the latest 20:00 New York as the instant the sample may reach. It is for a
    rehearsal on a temporary ledger and a copy of the catalog, and is refused with `ValueError`,
    before anything is read or written, when the ledger is the default one: a real study's samples
    end at a session's close, and the command line and the scheduled task offer no cutoff.
    """
    now = datetime.now(UTC) if now is None else now
    ledger = (default_ledger_path() if ledger_path is None else Path(ledger_path)).resolve()
    if cutoff is not None and ledger == _default_ledger():
        raise ValueError(
            "a chosen cutoff is for a rehearsal on a temporary ledger: on the default ledger a "
            "study's samples end at a session's close (ADR 0089)",
        )
    out = ledger.parent / "nightly" if out_dir is None else Path(out_dir)
    night = Night(study=plan.study, status=WAITING, adopts=plan.adopts, read_as=plan.read_as)
    night.started = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    night.planned = len(plan.configs())
    latest: dict[str, Any] | None = None
    latest_known = False
    try:
        latest = _read(out / "latest.json")
        latest_known = True
        reach = dt_to_unix_nanos(last_close(now) if cutoff is None else cutoff)
        _study(plan, night, Path(catalog_path), ledger, out, windows_dir, reach, checkout)
    except Waiting as waiting:
        night.status, night.why = WAITING, str(waiting)
        if not (latest or {}).get("day"):
            _write(out, night)
    except Exception as error:  # noqa: BLE001, recorded with its trace and reported by exit code
        night.status = FAILED
        night.why = f"{type(error).__name__}: {_first_line(str(error))}"
        night.summary = "".join(traceback.format_exception(error))
        if latest_known and (night.day or not (latest or {}).get("day")):
            _write(out, night)
    night.why = _public(night.why)
    night.line = night.line or _line(night)
    _heartbeat(out, night.status, night.line)
    return night


def _study(
    plan: Plan,
    night: Night,
    catalog_path: Path,
    ledger: Path,
    out: Path,
    windows_dir: Path | None,
    cutoff: int,
    checkout: Path,
) -> None:
    """`run_nightly`'s steps 1 to 6 on `night`; raises `Waiting`, or whatever a step refuses with."""
    if not gate_fix_in(checkout):
        raise Waiting(
            f"this checkout's {GATE_FIX_FILE} has no {GATE_FIX_TEST}: the gate's fix for a "
            "negative ratio passing next to the threshold is not in it, and nothing is ranked",
        )
    history = _known(plan, ledger)
    bar_types = [str(plan.bar_type(symbol)) for symbol in plan.symbols]
    start, end = recorded_range(
        catalog_path,
        bar_types,
        first=dt_to_unix_nanos(plan.first),
        cutoff=cutoff,
        start=None if history is None else history[0].start,
        since=None if history is None else history[-1].end,
    )
    if history is not None and end < history[-1].end:
        # Before the night is named: the range would be an earlier night's, whose record is kept.
        raise ValueError(
            f"the catalog's last common bar is at {_utc(end)}, before the end of study "
            f"{plan.study!r}'s {history[-1].describe()}: the catalog lost bars",
        )
    day = unix_nanos_to_dt(end).astimezone(NEW_YORK).date().isoformat()
    record = out / plan.study / f"{day}.json"
    previous = _read(record)
    if previous is not None:
        for field in ("end_ns", "attempt"):
            value = previous.get(field)
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"cannot read attempt record {record.name}: {field} must be a positive "
                    f"integer ({record})",
                )
    # Do not name the night until its previous attempt is readable: the failure handler would
    # otherwise overwrite that evidence with a fresh attempt, resetting the retry bound.
    night.day = day
    night.start, night.end, night.end_ns, night.bars = _utc(start), _utc(end), end, bar_types
    previous = previous or {}
    same_range = previous.get("end_ns") == end
    if same_range and previous.get("status") in SETTLED:
        night.status, night.why = NOTHING_NEW, f"the night of {night.day} is studied"
        night.line = f"{previous.get('line', '')} (nothing new since)"
        return
    night.attempt = int(previous.get("attempt", 0)) + 1 if same_range else 1
    if night.attempt > ATTEMPTS:
        night.status = NOTHING_NEW
        night.why = f"the night of {night.day} failed {ATTEMPTS} times and is left for a person"
        night.line = f"{previous.get('line', '')} (failed {ATTEMPTS} times; not tried again)"
        return
    # The attempt is written before the pass: a firing killed in it (the task's timeout) leaves this
    # record, so the next firing counts it and the attempts stay bounded.
    night.status, night.why = RUNNING, "the pass began and has not ended"
    _write(out, night, latest=False)
    if history is not None and (history[-1].start, history[-1].end) != (start, end):
        with open_study(plan.study, ledger) as study:
            move_sample(study, start=start, end=end)
    recorded, outcome = variant_study.run_study(
        catalog_path,
        name=plan.study,
        ledger_path=ledger,
        configs=plan.configs(),
        start=unix_nanos_to_dt(start),
        end=unix_nanos_to_dt(end),
        create=history is None,
    )
    night.sessions = new_york_days(catalog_path, bar_types, start, end)
    _settle(plan, night, recorded, outcome)
    if isinstance(outcome, RankingReport):
        night.returns = _pick_returns(plan, ledger, outcome)
    after = _known(plan, ledger)
    night.sample = None if after is None else after[-1].number
    # The stretch ends at the night's close, 20:00 New York on its day, not at its last common bar,
    # so a night whose last minutes had no trade still counts the same windows (windows_to).
    close = datetime.combine(date.fromisoformat(night.day), SESSION_END, tzinfo=NEW_YORK)
    night.paper = windows_to(windows_dir, close)
    night.ended = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    _write(out, night)


def _pick_returns(plan: Plan, ledger: Path, report: RankingReport) -> dict[str, float]:
    """The count and spread of the pick's session returns, from its record as the gate reads it.

    `canon_trading.returns_matrix.session_returns` on the returns and session days of the pick's
    trial (`RankingReport.pick_trials`), the gate's one numeric check, so the figures are those of
    the series its Sharpe ratio was taken of. Shown, never judged: a display beside the DSR.
    """
    with open_study(plan.study, ledger) as study:
        trial = next(
            t for t in study.get_trials(deepcopy=False) if t.number == report.pick_trials[0]
        )
    attrs = trial.user_attrs
    series = session_returns(report.pick, attrs["returns_series"], attrs["session_days"]).series
    return {
        "count": float(len(series)),
        "mean": float(series.mean()),
        "sd": float(series.std(ddof=1)),
        "least": float(series.min()),
        "greatest": float(series.max()),
    }


def _settle(
    plan: Plan, night: Night, recorded: Recorded | None, outcome: RankingReport | RankingStopped
) -> None:
    """Write the pass and the gate's figures on `night`, and its verdict."""
    if recorded is not None:
        night.recorded, night.already_run = len(recorded.recorded), len(recorded.skipped)
        night.run_failures = list(recorded.failed)
    if isinstance(outcome, RankingReport):
        names = variant_study.legend(plan.configs())
        night.n, night.t = outcome.n, outcome.t
        night.pick = names.get(outcome.pick, outcome.pick)
        night.sharpe, night.dsr, night.gate_passed = outcome.sharpe, outcome.dsr, outcome.passed
        night.summary = outcome.summary()
    else:
        night.summary = str(outcome)
    night.status, night.why = verdict(plan, outcome, night.sessions or 0)
    if night.run_failures and night.status == NOT_YET:
        # A run that raised is tried again by the next pass (ADR 0054 term 5), so the night is not
        # settled: the ranking stopped on a failure, not on what the data says.
        night.status = FAILED
        night.why = f"{len(night.run_failures)} runs raised: {_first_line(night.run_failures[0])}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canon_trading.nightly_study")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="the recorder's catalog (default: catalog/sip in the main checkout)",
    )
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="default: nightly/ beside the ledger"
    )
    parser.add_argument("--windows", type=Path, default=None, help="the paper windows' records")
    args = parser.parse_args(argv)
    ledger = default_ledger_path() if args.ledger is None else args.ledger
    catalog = (
        ledger.resolve().parents[1] / "catalog" / "sip" if args.catalog is None else args.catalog
    )
    night = run_nightly(
        catalog_path=catalog, ledger_path=ledger, out_dir=args.out, windows_dir=args.windows
    )
    print(night.line)
    if night.status == FAILED and night.summary:
        print(night.summary, file=sys.stderr)
    return 1 if night.status == FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
