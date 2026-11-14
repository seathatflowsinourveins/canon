"""Record one day's ranking actor once, then replay its bus publications.

Contract: NautilusTrader v2.0.0rc8 at d53849b216e2, common/__init__.pyi:634-669,
688-769 (DataActor signals and Clock.set_time_alert_ns), backtest/__init__.pyi:413-537
(streaming BacktestEngine), persistence/__init__.pyi:232-239 (selected definitions).
The actor is mover_window.window_rank, unchanged: this module does no ranking arithmetic.
The recording engine has only that actor, a bus subscriber, and minute bars; its venue has
bar execution off and no strategy or ticks. Regular recordings retain the 04:00 prefix,
just as window_replay does, through 16:00 New York. Other session parts are refused.

The JSON (schema 2) records exact signal text, ts_event and engine publication time, and
every record the actor kept on each tick (the Mover run's `ranking_records`), with snapshot
identity, input digests (the reference root's daily-bar files included), actor settings and
code commit. The actor's settings are the regular replay's own, so a spec naming another
part, feed, interval, data age or baseline rule is refused by name, in the full replay too.
A recording and its replay need a 40-hex `running_commit`: a tree git reports dirty, or no
commit at all, is refused, never matched as text. Exit arms and fill presets are
deliberately not ranking configuration. Signal files live in or beside the snapshot, outside
tracked repository content (ignored catalog snapshots inside a worktree are allowed). The
destination folder is probed before the ranking process starts, by creating a partial file
and hard-linking it to a second name, so a folder that takes no file or no link costs no
ranking; the finished record is linked into place exclusively, and a run that fails leaves
no file. Existing files are read and validated, never
silently overwritten or recomputed. Each recording owns a process for the native logger,
like window_replay. RecordedRankings follows FixedRank and the local p1_overnight
RecordedRankings pattern, with one native alert per recorded tick, named after the actor's
own timer so it keeps the actor's place among timers due at the same nanosecond (rc8
crates/backtest/src/accumulator.rs:44-52 orders them by ts_event, then by name).

Named limits:
- Commit equality is conservative. Any later commit, including one that changes no ranking
  code (an exit arm in session_window.TRAIL_ARMS), voids every recording until it is
  recorded again.
- The commit is session_window.running_commit's: it returns CANON_COMMIT as-is when that
  is set, and git's dirty test there is `status --porcelain -uno`, so an edit under an
  exported CANON_COMMIT, or an untracked module the ranking imports, is not detected.
- The reference root's digest covers every daily-bar file under it, so appending a session
  to the root voids the recordings of earlier dates too. Published snapshots are never
  appended to; a new day is a new version.
- A recording killed outright (SIGKILL) leaves its `.partial` probe beside its target.
- In recorded mode the roster's reference refusal reads the recording's `unreferenced`
  list, which the digests above tie to the same inputs; and a roster with no defined name
  and an empty recording is refused with the catalog's message, not the roster's.
- In the default path the baseline and reference refusals come after the instrument load
  (make_actor), not before it as at 8a51cacd: the same refusals, later and possibly behind
  another one.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from datetime import datetime
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from alpaca.data.models.corporate_actions import ForwardSplit
from alpaca.data.models.corporate_actions import ReverseSplit
from canon_alpaca.constants import VENUE
from nautilus_trader.common import DataActor
from nautilus_trader.model import Equity
from nautilus_trader.persistence import ParquetDataCatalog

from . import movers_rank
from . import movers_rank_actor
from . import session_window
from .mover_window import window_rank
from .movers_rank_actor import MoversRankActorConfig
from .movers_rank_actor import ReferenceRead
from .movers_rank_signal import SIGNAL_NAME
from .movers_rank_signal import decode_ranking

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

    from nautilus_trader.common import Signal
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import InstrumentId

_WORKER = """
import json
import sys
from canon_trading.rank_record import _record_rankings
print(json.dumps(_record_rankings(json.load(sys.stdin)), allow_nan=False))
"""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


ACTOR_SETTINGS = {
    "feed": "sip",
    "part": "regular",
    "rank_without_baseline": False,
    "publish_interval_secs": movers_rank_actor.PUBLISH_INTERVAL_SECS,
    "max_data_age_secs": movers_rank_actor.MAX_DATA_AGE_SECS,
}
"""The ranking actor's settings in a regular replay, as window_replay has always built it."""

COMMIT = re.compile(r"[0-9a-f]{40}")


def clean_commit() -> str:
    """The running code's commit, refused unless it names the code exactly."""
    commit = session_window.running_commit()
    if not COMMIT.fullmatch(commit):
        raise ValueError(f"rank record commit must be a clean commit, not {commit!r}")
    return commit


def _reference_root_sha256(root: Path) -> str:
    """Every daily-bar file under the root, by relative path and content: a re-fetch or repair
    of a prior close changes it."""
    files = sorted(
        path
        for path in (root / "data").rglob("*")
        if path.is_file() and any("-1-DAY-" in part for part in path.relative_to(root).parts)
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def check_settings(spec: Mapping[str, Any]) -> None:
    """Refuse a spec naming an actor setting the regular replay does not run; reads no file."""
    for key, supported in ACTOR_SETTINGS.items():
        if key in spec and spec[key] != supported:
            raise ValueError(f"{key} must be {supported!r} for a regular replay")


def configuration(spec: Mapping[str, Any]) -> dict[str, Any]:
    check_settings(spec)
    baseline = Path(spec["baseline_path"])
    reference_root = Path(spec["reference_root"]) if "reference_root" in spec else None
    return {
        **ACTOR_SETTINGS,
        "publish_offset_secs": movers_rank_actor.PUBLISH_OFFSET_SECS,
        "entry_notional": float(spec["entry_notional"]),
        "baseline_path": str(baseline.resolve()),
        "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest()
        if baseline.exists()
        else None,
        "reference_root": str(reference_root.resolve()) if reference_root is not None else None,
        "reference_root_sha256": _reference_root_sha256(reference_root)
        if reference_root is not None
        else None,
        "reference_closes_sha256": _digest(spec.get("reference_closes")),
        "splits_sha256": _digest(spec.get("splits", [])),
    }


def snapshot_identity(spec: Mapping[str, Any]) -> dict[str, str]:
    root = Path(spec["bars_root"]).resolve()
    manifest = root / "SNAPSHOT.json"
    content = manifest.read_bytes()
    declared = json.loads(content)["snapshot"]
    if Path(declared).resolve() != root:
        raise ValueError(f"snapshot path differs from SNAPSHOT.json: {root}")
    return {"path": declared, "manifest_sha256": hashlib.sha256(content).hexdigest()}


def make_actor(
    spec: Mapping[str, Any],
    instruments: Sequence[Equity],
    roster: set[InstrumentId],
    records: list[dict[str, object]],
) -> tuple[movers_rank_actor.MoversRankActor, list[str]]:
    """The full replay and bars-only recorder use exactly the same offline actor wiring: the
    configuration window_replay built before rank_file existed, with any baseline problem refused."""
    from .window_replay import NY
    from .window_replay import _local
    from .window_replay import _ns
    from .window_replay import _references

    check_settings(spec)
    day = date.fromisoformat(spec["trade_date"])
    baseline_path = _local(spec["baseline_path"], directory=False)
    baseline = movers_rank.read_baselines(baseline_path, day)
    if baseline.problem:
        raise ValueError(f"baseline not usable: {baseline.problem}")
    references, unreferenced = _references(spec, instruments, day, roster)
    actions = tuple(
        (
            ForwardSplit if row["corporate_action_type"] == "forward_splits" else ReverseSplit
        ).model_validate(row)
        for row in spec.get("splits", ())
    )
    splits = movers_rank.SplitRead(
        start=min((ref.session for ref in references.values()), default=day),
        end=day,
        actions=actions,
    )
    opened = datetime.combine(day, time(4), NY)

    def read_references(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        if cutoff != day or session != day:
            raise ValueError("the reference reader is bounded to the spec's trade date")
        return ReferenceRead(
            cutoff, {s: references[s] for s in symbols if s in references}, splits, _ns(opened)
        )

    actor = window_rank(
        MoversRankActorConfig(
            feed="sip",
            order_cap=float(Decimal(str(spec["entry_notional"]))),
            baseline_path=str(baseline_path),
            reference_reader=read_references,
            inline_reads=True,
        ),
        records,
    )
    return actor, unreferenced


class RankSubscriber(DataActor):
    """Observe the bus, including empty rankings; no inference from actor diagnostics."""

    def __init__(self) -> None:
        super().__init__()
        self.generations: list[dict[str, Any]] = []

    def on_start(self) -> None:
        self.subscribe_signal(SIGNAL_NAME)

    def on_signal(self, signal: Signal) -> None:
        self.generations.append(
            {
                "value": signal.value,
                "ts_event": signal.ts_event,
                "published_ns": self.clock.timestamp_ns(),
            }
        )


class RecordedRankings(DataActor):
    """At each recorded tick's engine time, publish its signal with its ts_event, then append the
    actor's record of that tick to `target`, as mover_window.window_rank does after on_tick."""

    def __init__(self) -> None:
        super().__init__()
        self.generations: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.target: list[dict[str, object]] = []
        self._due: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    def on_start(self) -> None:
        now = self.clock.timestamp_ns()
        due: dict[int, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
        for row in self.generations:
            due.setdefault(row["published_ns"], ([], []))[0].append(row)
        for record in self.records:
            due.setdefault(record["latency_ns"]["handled"], ([], []))[1].append(record)
        self._due = {at: rows for at, rows in due.items() if at >= now}
        for index, at in enumerate(sorted(self._due)):
            self.clock.set_time_alert_ns(
                f"{movers_rank_actor.TIMER}-{index:06d}", at, callback=self._publish
            )

    def _publish(self, event: TimeEvent) -> None:
        generations, records = self._due.pop(event.ts_event)
        for row in generations:
            self.publish_signal(SIGNAL_NAME, row["value"], ts_event=row["ts_event"])
        self.target.extend(copy.deepcopy(records))

    def on_stop(self) -> None:
        for name in self.clock.timer_names():
            self.clock.cancel_timer(name)


def read_record(path: str | Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse a stale or mismatched record before any replay catalog query."""
    from .window_replay import NY
    from .window_replay import _ns

    record = json.loads(Path(path).read_text())
    for key, expected in (
        ("schema", 2),
        ("trade_date", spec["trade_date"]),
        ("snapshot", snapshot_identity(spec)),
        ("commit", clean_commit()),
    ):
        if record.get(key) != expected:
            raise ValueError(f"rank record {key} differs from replay")
    for key, expected in configuration(spec).items():
        if record["configuration"].get(key) != expected:
            raise ValueError(f"rank record configuration {key} differs from replay")
    day = date.fromisoformat(spec["trade_date"])
    opened = _ns(datetime.combine(day, time(4), NY))
    closed = _ns(datetime.combine(day, time(16), NY))
    if record.get("start_ns") != opened or record.get("end_ns") != closed:
        raise ValueError("rank record does not cover the full regular part and its prefix")
    previous = opened - 1
    for row in record["generations"]:
        published, event = row["published_ns"], row["ts_event"]
        if type(published) is not int or not previous < published <= closed:
            raise ValueError("rank record published_ns is outside the part or out of order")
        if type(event) is not int or not opened <= event <= published:
            raise ValueError("rank record ts_event is outside its publication bounds")
        decode_ranking(row["value"])
        previous = published
    previous = opened
    for row in record["records"]:
        latency = row.get("latency_ns") if isinstance(row, dict) else None
        handled = latency.get("handled") if isinstance(latency, dict) else None
        if type(handled) is not int or not previous <= handled <= closed:
            raise ValueError(
                "rank record records' handled time is outside the part or out of order"
            )
        previous = handled
    return record


def listed_names(record: Mapping[str, Any], end: int) -> list[str]:
    """Every name a generation published up to `end` lists, from 04:00.

    RecordedRankings publishes from the replay's 04:00 start, as the actor does in the full
    replay, so the Mover watches pre-window names too: each needs its definition and bars.
    """
    rows = [r for r in record["generations"] if r["published_ns"] <= end]
    return sorted({str(e.instrument_id) for row in rows for e in decode_ranking(row["value"])})


def _record_rankings(spec: Mapping[str, Any]) -> dict[str, Any]:
    from .window_replay import BAR_SPEC
    from .window_replay import MINUTE
    from .window_replay import NY
    from .window_replay import _checked_engine
    from .window_replay import _ns
    from .window_replay import add_replay_venue

    snapshot, settings, commit = snapshot_identity(spec), configuration(spec), clean_commit()
    catalog = ParquetDataCatalog(snapshot["path"])
    instruments = [
        i for i in catalog.instruments() if isinstance(i, Equity) and i.id.venue == VENUE
    ]
    if not instruments:
        raise ValueError("the bars catalog holds no ALPACA equity definitions")
    records: list[dict[str, object]] = []
    actor, unreferenced = make_actor(spec, instruments, set(), records)
    subscriber = RankSubscriber()
    day = date.fromisoformat(spec["trade_date"])
    opened, closed = (_ns(datetime.combine(day, time(hour), NY)) for hour in (4, 16))
    errors: list[dict[str, Any]] = []
    bars_read, first = 0, True
    with _checked_engine(errors=errors) as (engine, check):
        add_replay_venue(engine, "touch", "1 USD")
        for instrument in instruments:
            engine.add_instrument(instrument)
        engine.add_actor(subscriber)
        engine.add_actor(actor)
        bar_types = [f"{i.id}-{BAR_SPEC}" for i in instruments]
        low = opened
        while low <= closed:
            high = min(low + 60 * MINUTE - 1, closed)
            bars = catalog.query_bars(identifiers=bar_types, start=low, end=high)
            bars_read += len(bars)
            if bars:
                engine.add_data(bars)
                engine.run(start=opened if first else None, end=closed, streaming=True)
                check()
                first = False
                engine.clear_data()
            low = high + 1
        if first:
            raise ValueError("the snapshot has no bars in the recording range")
        engine.end()
        check()
    return {
        "schema": 2,
        "trade_date": spec["trade_date"],
        "snapshot": snapshot,
        "configuration": settings,
        "commit": commit,
        "start_ns": opened,
        "end_ns": closed,
        "generations": subscriber.generations,
        "records": records,
        "unreferenced": unreferenced,
        "bars_read": bars_read,
        "engine_errors": errors,
    }


def record_rankings(spec: Mapping[str, Any], path: str | Path) -> dict[str, Any]:
    """Create once, or validate and reuse the existing complete daily recording."""
    identity = snapshot_identity(spec)
    root, target = Path(identity["path"]).resolve(), Path(path).resolve()
    if target.parent not in (root, root.parent):
        raise ValueError("rank file must be in the snapshot folder or beside it")
    if any((parent / ".git").exists() for parent in target.parents):
        # Git's ignore decision, as in scripts/test-lanes:3384. Tracked files are never ignored.
        ignored = subprocess.run(
            ["git", "-C", str(target.parent), "check-ignore", "--quiet", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if ignored.returncode == 1:
            raise ValueError(
                "rank file must be outside repository content; use an ignored snapshot"
            )
        if ignored.returncode:
            raise RuntimeError(f"rank file repository check failed: {ignored.stderr}")
    if target.exists():
        return read_record(target, spec)
    configuration(spec)
    clean_commit()
    # Probe the folder before the long ranking process: a folder that takes no file is refused
    # now, not after the whole day is ranked.
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=f".{target.name}.", suffix=".partial", delete=False
    ) as handle:
        partial = Path(handle.name)
    try:
        # The record is linked into place, so the folder must take a hard link too.
        linked = partial.with_name(partial.name + ".link")
        os.link(partial, linked)
        linked.unlink()
        result = subprocess.run(
            [sys.executable, "-c", _WORKER],
            input=json.dumps(dict(spec), allow_nan=False),
            capture_output=True,
            text=True,
            check=False,
            timeout=12 * 3600,
        )
        if result.returncode:
            raise RuntimeError(f"ranking process failed ({result.returncode}):\n{result.stderr}")
        record = json.loads(result.stdout)
        partial.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
        # Exclusive: a record another recorder published meanwhile is never overwritten; it
        # is read and validated instead, as an existing file is.
        try:
            os.link(partial, target)
        except FileExistsError:
            return read_record(target, spec)
    finally:
        partial.unlink(missing_ok=True)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bars-root", type=Path)
    args = parser.parse_args(argv)
    spec = json.loads(args.spec.read_text())
    if args.bars_root is not None:
        spec["bars_root"] = str(args.bars_root)
    record = record_rankings(spec, args.output)
    print(json.dumps({"generations": len(record["generations"]), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
