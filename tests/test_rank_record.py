"""Record the real actor on neutral bars; every write stays in system temp."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog

from canon_trading import movers_rank

NY = ZoneInfo("America/New_York")
START = datetime(2025, 11, 10, 9, 30, tzinfo=NY)
SECOND = 1_000_000_000


@pytest.fixture
def rank_spec(alpaca_bars, monkeypatch):
    # A record names clean code; the tests stand in a commit for the working tree.
    monkeypatch.setenv("CANON_COMMIT", "0" * 40)
    with TemporaryDirectory(prefix="canon-rank-record-test-") as folder:
        root = Path(folder)
        catalog = ParquetDataCatalog(str(root))
        instruments = [alpaca_bars.equity(name) for name in ("AAA", "BBB", "CCC")]
        catalog.write_instruments(instruments)
        sessions = tuple(START.date() - timedelta(days=n) for n in (7, 6, 5, 4, 3))
        baseline_path = root / "baseline.json"
        movers_rank.write_baselines(
            baseline_path,
            START.date(),
            {
                i.id.symbol.value: movers_rank.Baseline(
                    cumulative=tuple(1000.0 * (n + 1) for n in range(movers_rank.SEGMENTS)),
                    sessions=sessions,
                    traded_sessions=(5,) * movers_rank.SEGMENTS,
                )
                for i in instruments
            },
        )
        for instrument in instruments:
            bars = []
            for n in range(5):
                at = int((START + timedelta(minutes=n)).timestamp()) * SECOND
                # CCC overtakes only on its last bar: future data must not leak backward.
                close = (30 if n == 4 else 8) if instrument.id.symbol.value == "CCC" else 10
                price = Price(close, 4)
                bars.append(
                    Bar(
                        BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL"),
                        price,
                        price,
                        price,
                        price,
                        Quantity.from_int(100_000),
                        at,
                        at + 60 * SECOND,
                    )
                )
            catalog.write_bars(bars)
        (root / "SNAPSHOT.json").write_text(json.dumps({"snapshot": str(root)}))
        yield {
            "bars_root": str(root),
            "baseline_path": str(baseline_path),
            "trade_date": START.date().isoformat(),
            "entry_notional": "500",
            "reference_closes": {
                i.id.symbol.value: {"close": 8.0, "session": sessions[-1].isoformat()}
                for i in instruments
            },
        }


def test_record_captures_actor_signal_clocks_without_future_bars(rank_spec):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    record = rank_record.record_rankings(rank_spec, target)
    assert json.loads(target.read_text()) == record
    assert record["snapshot"]["path"] == rank_spec["bars_root"]
    assert record["commit"]
    assert record["configuration"]["part"] == "regular"
    assert record["configuration"]["feed"] == "sip"
    assert record["configuration"]["rank_without_baseline"] is False
    assert record["configuration"]["publish_interval_secs"] == 60
    assert record["configuration"]["baseline_sha256"]
    rows = record["generations"]
    assert rows and record["engine_errors"] == []
    change = int((START + timedelta(minutes=5)).timestamp()) * SECOND
    assert all(row["published_ns"] == row["ts_event"] for row in rows)
    assert all("CCC.ALPACA" not in row["value"] for row in rows if row["ts_event"] < change)
    assert any(row["value"].startswith("CCC.ALPACA=") for row in rows if row["ts_event"] > change)


@pytest.mark.parametrize(
    "key,value",
    [
        ("part", "overnight"),
        ("feed", "boats"),
        ("rank_without_baseline", True),
        ("publish_interval_secs", 30),
        ("entry_notional", "501"),
        ("max_data_age_secs", 60),
    ],
)
def test_record_refuses_a_different_configuration_by_name(rank_spec, key, value):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    rank_record.record_rankings(rank_spec, target)
    with pytest.raises(ValueError, match=key):
        rank_record.read_record(target, {**rank_spec, key: value})


def test_record_refuses_changed_baselines_and_references(rank_spec):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    rank_record.record_rankings(rank_spec, target)
    changed = copy.deepcopy(rank_spec)
    changed["reference_closes"]["AAA"]["close"] = 7
    with pytest.raises(ValueError, match="reference_closes"):
        rank_record.read_record(target, changed)
    baseline = Path(rank_spec["baseline_path"])
    baseline.write_text(baseline.read_text() + "\n")
    with pytest.raises(ValueError, match="baseline_sha256"):
        rank_record.read_record(target, rank_spec)


def test_record_refuses_wrong_snapshot_and_out_of_order_generations(rank_spec):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    record = rank_record.record_rankings(rank_spec, target)
    broken = copy.deepcopy(record)
    broken["generations"].reverse()
    target.write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="published_ns"):
        rank_record.read_record(target, rank_spec)
    target.write_text(json.dumps(record))
    manifest = Path(rank_spec["bars_root"]) / "SNAPSHOT.json"
    manifest.write_text(json.dumps({"snapshot": "another snapshot"}))
    with pytest.raises(ValueError, match="snapshot"):
        rank_record.read_record(target, rank_spec)


def test_record_requires_a_snapshot_and_adjacent_output(rank_spec, tmp_path):
    from canon_trading import rank_record

    with pytest.raises(ValueError, match="snapshot folder or beside"):
        rank_record.record_rankings(rank_spec, tmp_path / "rank.json")
    manifest = Path(rank_spec["bars_root"]) / "SNAPSHOT.json"
    manifest.unlink()
    with pytest.raises(FileNotFoundError, match="SNAPSHOT.json"):
        rank_record.record_rankings(rank_spec, Path(rank_spec["bars_root"]) / "rank.json")


def test_record_allows_ignored_snapshot_artifacts_but_not_repository_files(rank_spec):
    from canon_trading import rank_record

    root = Path(rank_spec["bars_root"])
    # A standalone temporary repository, never the real checkout or its catalog.
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("rank.json\n")
    target = root / "rank.json"
    assert rank_record.record_rankings(rank_spec, target)["generations"]
    with pytest.raises(ValueError, match="repository"):
        rank_record.record_rankings(rank_spec, root / "tracked-rank.json")


def test_existing_record_is_reused_without_running_an_engine(rank_spec, monkeypatch):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    first = rank_record.record_rankings(rank_spec, target)
    monkeypatch.setattr(rank_record, "_WORKER", "raise AssertionError('ranking ran twice')")
    assert rank_record.record_rankings(rank_spec, target) == first


def test_selected_names_are_every_name_published_up_to_the_window_end():
    """RecordedRankings publishes from 04:00, so a name listed only before the window is one the
    Mover watches then, as it does in the full replay; a name published after the end is not."""
    from canon_trading import rank_record

    record = {
        "generations": [
            {"value": "OLD.ALPACA=0.1", "published_ns": 10},
            {"value": "AAA.ALPACA=0.1", "published_ns": 20},
            {"value": "BBB.ALPACA=0.2", "published_ns": 40},
            {"value": "LATE.ALPACA=0.3", "published_ns": 60},
        ]
    }
    assert rank_record.listed_names(record, 50) == ["AAA.ALPACA", "BBB.ALPACA", "OLD.ALPACA"]


def test_record_refuses_an_unwritable_destination_before_ranking(rank_spec, monkeypatch):
    from canon_trading import rank_record

    monkeypatch.setattr(rank_record, "_WORKER", "raise AssertionError('ranking ran')")
    root = Path(rank_spec["bars_root"])
    root.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            rank_record.record_rankings(rank_spec, root / "rank.json")
    finally:
        root.chmod(0o755)
    assert not (root / "rank.json").exists()


def test_record_refuses_a_folder_without_hard_links_before_ranking(rank_spec, monkeypatch):
    from canon_trading import rank_record

    monkeypatch.setattr(rank_record, "_WORKER", "raise AssertionError('ranking ran')")

    def no_link(src, dst, *args, **kwargs):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(rank_record.os, "link", no_link)
    target = Path(rank_spec["bars_root"]) / "rank.json"
    before = sorted(target.parent.iterdir())
    with pytest.raises(OSError, match="not supported"):
        rank_record.record_rankings(rank_spec, target)
    assert sorted(target.parent.iterdir()) == before


def test_a_record_published_meanwhile_is_read_not_overwritten(rank_spec, monkeypatch):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    first = rank_record.record_rankings(rank_spec, target)
    published = target.read_bytes()
    target.unlink()
    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        # Another recorder publishes the same record just before this one links its own.
        if Path(dst).name == target.name:
            target.write_bytes(published)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(rank_record.os, "link", racing_link)
    assert rank_record.record_rankings(rank_spec, target) == first
    assert target.read_bytes() == published


def test_a_failed_recording_leaves_no_file(rank_spec, monkeypatch):
    from canon_trading import rank_record

    monkeypatch.setattr(rank_record, "_WORKER", "raise SystemExit(3)")
    target = Path(rank_spec["bars_root"]) / "rank.json"
    before = sorted(target.parent.iterdir())
    with pytest.raises(RuntimeError, match="ranking process failed"):
        rank_record.record_rankings(rank_spec, target)
    assert sorted(target.parent.iterdir()) == before


@pytest.mark.parametrize("commit", ["0" * 40 + " with uncommitted changes", "unknown: OSError: x"])
def test_record_refuses_code_it_cannot_name(rank_spec, monkeypatch, commit):
    from canon_trading import rank_record

    target = Path(rank_spec["bars_root"]) / "rank.json"
    rank_record.record_rankings(rank_spec, target)
    monkeypatch.setenv("CANON_COMMIT", commit)
    with pytest.raises(ValueError, match="commit"):
        rank_record.read_record(target, rank_spec)
    with pytest.raises((ValueError, RuntimeError), match="commit"):
        rank_record.record_rankings(rank_spec, Path(rank_spec["bars_root"]) / "other.json")


def test_record_refuses_a_changed_reference_root(rank_spec, alpaca_bars, tmp_path):
    from canon_trading import rank_record

    references = tmp_path / "references"
    references.mkdir()
    catalog = ParquetDataCatalog(str(references))
    first = datetime(2025, 11, 3, tzinfo=NY)
    for symbol in ("AAA", "BBB", "CCC"):
        catalog.write_bars(alpaca_bars.daily(alpaca_bars.equity(symbol), (8.0,) * 4, first=first))
    spec = {k: v for k, v in rank_spec.items() if k != "reference_closes"}
    spec["reference_root"] = str(references)
    target = Path(spec["bars_root"]) / "rank.json"
    rank_record.record_rankings(spec, target)
    rank_record.read_record(target, spec)
    repaired = alpaca_bars.daily(
        alpaca_bars.equity("AAA"), (7.0,), first=datetime(2025, 11, 7, tzinfo=NY)
    )
    catalog.write_bars(repaired)
    with pytest.raises(ValueError, match="reference_root"):
        rank_record.read_record(target, spec)
