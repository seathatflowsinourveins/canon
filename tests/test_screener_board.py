"""Local board reader: fixture files only, sockets refused, no API or credentials."""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)
LABELS = [
    "Alpaca's movers: gainers",
    "Alpaca's movers: losers",
    "Alpaca's most actives by volume",
    "Alpaca's most actives by trades",
]
TRADING_MODULES = (
    "canon_trading.arming",
    "canon_trading.sizing",
    "canon_trading.position_cap",
    "canon_trading.session_ma_cross",
    "canon_trading.paper_node",
    "canon_trading.trail",
    "canon_trading.trail_profile",
    "canon_trading.rebuilt_level",
    "canon_trading.movers_stream",
    "canon_trading.ranking",
    "canon_trading.rank_shared",
    "canon_trading.movers_rank",
    "canon_trading.movers_rank_signal",
    "canon_trading.movers_rank_actor",
    "canon_trading.mover",
    "canon_trading.mover_window",
    "canon_trading.session_window",
    "canon_trading.reentry_replay",
    "canon_trading.protect",
)
"""The ranking, the Mover, the on-demand entry (a mover window, `session_window`), the re-entry and the
protector: none of them may load the board, directly or through another module."""


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def offline_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    def refused(*args, **kwargs):
        raise AssertionError("the board reader tried to open a network socket")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)


def make_board(folder, *, age=60.0):
    def movers(prefix):
        return [
            {"symbol": f"{prefix}{n:02}", "price": 8.0, "change": n / 10, "percent_change": n}
            for n in range(23)
        ]

    def actives(prefix):
        return [
            {"symbol": f"{prefix}{n:02}", "volume": 1000 - n, "trade_count": 100 - n}
            for n in range(23)
        ]

    board = {
        "source": "alpaca screener",
        "limits": [
            "Share volume and trade count, not dollars.",
            "Alpaca documents no refresh interval.",
        ],
        "at": (NOW - timedelta(seconds=age)).isoformat(),
        "movers": {
            "last_updated": "2026-11-13T20:56:00+00:00",
            "gainers": movers("GAIN"),
            "losers": movers("LOSS"),
        },
        "most_actives_volume": {
            "last_updated": "2026-11-13T15:58:00-05:00",
            "most_actives": actives("VOLUME"),
        },
        "most_actives_trades": {
            "last_updated": "2026-11-13T20:57:00+00:00",
            "most_actives": actives("TRADES"),
        },
    }
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "screener-board.json"
    path.write_text(json.dumps(board))
    return path


@pytest.mark.parametrize("top", [None, 2, 30])
def test_fresh_reader_shows_each_top_list_and_update_time_newest_list_first(
    tmp_path, monkeypatch, capsys, top
) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    path = make_board(tmp_path / "watch")
    before = path.read_bytes()
    args = ["--board-dir", str(path.parent)]
    if top is not None:
        args += ["--top", str(top)]
    assert module.main(args) == 0
    output = capsys.readouterr().out
    assert "age 60.0 seconds" in output.splitlines()[0]
    assert "alpaca screener" in output
    assert "not dollars" in output and "no refresh interval" in output
    for label in LABELS:
        assert label in output
    assert (
        output.index(LABELS[2])
        < output.index(LABELS[3])
        < output.index(LABELS[0])
        < output.index(LABELS[1])
    )
    assert output.count("last_updated: 2026-11-13T20:56:00+00:00") == 2
    assert "last_updated: 2026-11-13T15:58:00-05:00" in output
    assert "last_updated: 2026-11-13T20:57:00+00:00" in output
    count = min(20 if top is None else top, 23)
    for prefix in ("GAIN", "LOSS", "VOLUME", "TRADES"):
        for n in range(count):
            assert f'"{prefix}{n:02}"' in output
        assert f'"{prefix}{count:02}"' not in output
        assert output.index(f'"{prefix}00"') < output.index(f'"{prefix}01"')
    assert path.read_bytes() == before


@pytest.mark.parametrize("age, expected", [(119.9, 0), (120.0, 0), (120.001, 1), (86400.0, 1)])
def test_stale_is_strictly_older_than_120_seconds_and_named_on_first_line(
    tmp_path, monkeypatch, capsys, age, expected
) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    path = make_board(tmp_path / "watch", age=age)
    assert module.main(["--board-dir", str(path.parent), "--top", "1"]) == expected
    output = capsys.readouterr().out
    first = output.splitlines()[0].lower()
    assert ("stale" if expected else "fresh") in first
    assert f"age {age:.1f} seconds" in first
    assert all(label in output for label in LABELS)


@pytest.mark.parametrize("custom", [False, True])
def test_missing_is_first_line_and_exit_two_without_creating_any_folder(
    tmp_path, capsys, custom
) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    folder = tmp_path / "chosen" if custom else tmp_path / "home" / ".claude" / "canon" / "watch"
    args = ["--board-dir", str(folder)] if custom else []
    assert module.main(args) == 2
    output = capsys.readouterr().out
    assert "missing" in output.splitlines()[0].lower()
    assert str(folder / "screener-board.json") in output
    assert not folder.exists()


def test_reader_uses_the_default_watch_folder(tmp_path, monkeypatch, capsys) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    make_board(tmp_path / "home" / ".claude" / "canon" / "watch")
    assert module.main(["--top", "1"]) == 0
    assert '"GAIN00"' in capsys.readouterr().out


@pytest.mark.parametrize("top", ["0", "-1"])
def test_top_must_be_positive(tmp_path, capsys, top) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    with pytest.raises(SystemExit) as raised:
        module.main(["--board-dir", str(tmp_path), "--top", top])
    assert raised.value.code == 2
    assert "--top must be positive" in capsys.readouterr().err


def test_python_module_entrypoint_reports_missing_board(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "canon_trading.screener_board",
            "--board-dir",
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "missing" in result.stdout.splitlines()[0].lower()


def test_the_board_says_it_is_display_only_and_whose_lists_these_are(tmp_path, monkeypatch, capsys):
    """The lead's term of 2026-11-13: Alpaca's most-actives and movers lists are for a person to look at;
    the board says so before any row, and names each list as Alpaca's."""
    module = importlib.import_module("canon_trading.screener_board")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    make_board(tmp_path)
    assert module.main(["--board-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("Display only:")
    assert "never feed" in lines[1]
    titles = [line for line in lines if " | last_updated: " in line]
    assert len(titles) == 4 and all(t.startswith("Alpaca's ") for t in titles)
    assert "DISPLAY ONLY" in (module.__doc__ or "")


def test_no_trading_module_loads_the_board() -> None:
    """None of the ranking, the Mover, the on-demand entry, the re-entry or the protector has the board in
    its imports, directly or through another module; only the poller that writes it names it."""
    probe = (
        "import importlib, sys\n"
        f"for name in {TRADING_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "print('canon_trading.screener_board' in sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"
    root = Path(__file__).resolve().parents[1]
    sources = [*(root / "src").rglob("*.py"), *(root / "packages").glob("*/src/**/*.py")]
    naming = sorted(
        p.stem
        for p in sources
        if "screener_board" in (text := p.read_text()) or "screener-board.json" in text
    )
    assert naming == ["movers_screener", "screener_board"]


@pytest.mark.parametrize(
    ("content", "why"),
    [
        ("", "JSONDecodeError"),
        ('{"at": "2026-11-13T20:59:00+00:00"}', "KeyError"),
        ("[1]", "TypeError"),
        ("NAIVE", "TypeError"),
    ],
    ids=["empty", "missing-key", "not-an-object", "naive-time"],
)
def test_a_malformed_board_is_named_on_the_first_line_with_its_own_exit(
    tmp_path, monkeypatch, capsys, content, why
) -> None:
    """A board that cannot be read as one (empty after a crash, a key missing, not an object, a time with
    no offset) is named on the first line with exit 3, never a traceback or Stale's exit 1."""
    module = importlib.import_module("canon_trading.screener_board")
    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    path = make_board(tmp_path)
    if content == "NAIVE":
        board = json.loads(path.read_text())
        board["at"] = "2026-11-13T20:59:00"
        content = json.dumps(board)
    path.write_text(content)
    assert module.main(["--board-dir", str(tmp_path)]) == 3
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith(f"Malformed screener board: {path}: ") and why in first
