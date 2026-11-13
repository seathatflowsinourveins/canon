"""The existing screener poll publishes its complete answer without another request.

All clients are offline stand-ins; every file, including the default board, is under tmp_path.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from alpaca.data.models.screener import MostActives
from alpaca.data.models.screener import Movers

from canon_trading import movers_screener
from canon_trading import screener_board

AT = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)
UPDATED = "2026-11-13T20:59:00+00:00"
READS = ["movers", "most_actives_volume", "most_actives_trades"]


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return AT if tz is not None else AT.replace(tzinfo=None)


class Client:
    """Real alpaca-py response models, with distinct answers and a counted call per read."""

    def __init__(self, failures=()) -> None:
        self.failures = failures
        self.calls: list[tuple[str, int]] = []
        self.generation = 0

    def get_market_movers(self, request_params):
        self.calls.append(("movers", request_params.top))
        self.generation += 1
        if "movers" in self.failures:
            raise OSError("offline movers failure")
        return Movers.model_validate(
            {
                "market_type": "stocks",
                "last_updated": UPDATED,
                "gainers": [
                    {
                        "symbol": f"GAIN{self.generation}",
                        "percent_change": 31.5,
                        "change": 1.2,
                        "price": 5.0,
                    }
                ],
                "losers": [
                    {"symbol": "LOSS", "percent_change": -20.0, "change": -2.0, "price": 8.0}
                ],
            }
        )

    def get_most_actives(self, request_params):
        name = f"most_actives_{request_params.by.value}"
        self.calls.append((name, request_params.top))
        if name in self.failures:
            raise OSError(f"offline {name} failure")
        return MostActives.model_validate(
            {
                "last_updated": UPDATED,
                "most_actives": [
                    {"symbol": request_params.by.value.upper(), "volume": 1e6, "trade_count": 5e3}
                ],
            }
        )


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(movers_screener, "datetime", FrozenDatetime)
    monkeypatch.setattr(movers_screener, "request_counts", list)


def test_successful_poll_publishes_its_own_time_and_exact_response_rows(tmp_path) -> None:
    log = tmp_path / "poll.jsonl"
    folder = tmp_path / "new" / "watch"
    assert movers_screener.run(Client(), 1, log, board_dir=folder) == 0
    record = json.loads(log.read_text().splitlines()[0])
    board = json.loads((folder / "screener-board.json").read_text())
    assert board["source"] == "alpaca screener"
    assert board["at"] == record["at"] == AT.isoformat()
    assert {name: board[name] for name in READS} == {name: record[name] for name in READS}
    assert board["movers"]["last_updated"] == UPDATED
    assert board["movers"]["losers"][0]["symbol"] == "LOSS"
    limits = " ".join(board["limits"]).lower()
    assert "share volume" in limits and "trade count" in limits and "not dollars" in limits
    assert "alpaca documents no refresh interval" in limits


def test_each_successful_poll_replaces_the_board_with_its_new_rows(tmp_path, monkeypatch) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    folder = tmp_path / "watch"
    destination = folder / "screener-board.json"
    replaced = []
    replace = module.os.replace

    def observe(source, target):
        replace(source, target)
        replaced.append(json.loads(destination.read_text()))

    monkeypatch.setattr(module.os, "replace", observe)
    log = tmp_path / "poll.jsonl"
    assert (
        movers_screener.run(
            Client(), 2, log, board_dir=folder, clock=lambda: 0.0, sleep=lambda _: None
        )
        == 0
    )
    assert [board["movers"]["gainers"][0]["symbol"] for board in replaced] == ["GAIN1", "GAIN2"]
    assert json.loads(destination.read_text()) == replaced[-1]


def test_replace_receives_a_whole_closed_temporary_file_in_the_same_folder(
    tmp_path, monkeypatch
) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    folder = tmp_path / "watch"
    folder.mkdir()
    destination = folder / "screener-board.json"
    destination.write_text("old board\n")
    replace = module.os.replace
    seen = []

    def observe(source, target):
        temporary = Path(source)
        assert temporary.parent == folder and temporary != destination
        assert Path(target) == destination
        assert destination.read_text() == "old board\n"
        board = json.loads(temporary.read_text())
        assert board["at"] == AT.isoformat()
        assert all(name in board for name in READS)
        seen.append(temporary)
        replace(source, target)

    monkeypatch.setattr(module.os, "replace", observe)
    movers_screener.run(Client(), 1, tmp_path / "poll.jsonl", board_dir=folder)
    assert len(seen) == 1
    assert list(folder.iterdir()) == [destination]


@pytest.mark.parametrize(
    "failures", [("movers",), ("most_actives_volume",), ("most_actives_trades",), tuple(READS)]
)
def test_partial_or_complete_read_failure_preserves_last_good_board_and_names_errors(
    tmp_path, failures
) -> None:
    folder = tmp_path / "watch"
    log = tmp_path / "poll.jsonl"
    movers_screener.run(Client(), 1, log, board_dir=folder)
    destination = folder / "screener-board.json"
    good = destination.read_bytes()
    client = Client(failures)
    assert movers_screener.run(client, 1, log, board_dir=folder) == len(failures)
    assert destination.read_bytes() == good
    record = json.loads(log.read_text().splitlines()[-2])
    assert record["failed"] == len(failures)
    for name in failures:
        assert record[name]["error"].startswith("OSError: offline ")
    assert [name for name, _ in client.calls] == READS


def test_failed_first_poll_does_not_create_a_board(tmp_path) -> None:
    folder = tmp_path / "watch"
    assert movers_screener.run(Client(READS), 1, tmp_path / "poll.jsonl", board_dir=folder) == 3
    assert not (folder / "screener-board.json").exists()


def test_replace_failure_preserves_previous_board_cleans_temp_and_is_not_silent(
    tmp_path, monkeypatch, capsys
) -> None:
    module = importlib.import_module("canon_trading.screener_board")
    folder = tmp_path / "watch"
    log = tmp_path / "poll.jsonl"
    client = Client()
    movers_screener.run(client, 1, log, board_dir=folder)
    destination = folder / "screener-board.json"
    good = destination.read_bytes()

    def refused(source, target):
        raise OSError("offline replace failure")

    monkeypatch.setattr(module.os, "replace", refused)
    # the board is for display only: a failed write is named and the poll it came from is still kept,
    # and the polling goes on
    assert movers_screener.run(client, 2, log, board_dir=folder, sleep=lambda _: None) == 0
    assert destination.read_bytes() == good
    assert list(folder.iterdir()) == [destination]
    polls = [json.loads(x) for x in log.read_text().splitlines()[2:]]  # after the first run
    assert [p.get("poll") for p in polls] == [1, 2, None] and polls[-1]["summary"]
    said = capsys.readouterr().err
    assert said.count("screener board not written: offline replace failure") == 2


def test_board_write_changes_neither_log_bytes_nor_the_three_client_calls(
    tmp_path, monkeypatch
) -> None:
    enabled = Client()
    with_board = tmp_path / "with.jsonl"
    movers_screener.run(enabled, 1, with_board, board_dir=tmp_path / "watch")
    monkeypatch.setattr(screener_board, "write_board", lambda *args: None)
    disabled = Client()
    without_board = tmp_path / "without.jsonl"
    movers_screener.run(disabled, 1, without_board, board_dir=tmp_path / "unused")
    assert with_board.read_bytes() == without_board.read_bytes()
    assert (
        enabled.calls
        == disabled.calls
        == [("movers", 50), ("most_actives_volume", 100), ("most_actives_trades", 100)]
    )
    baseline, bad = movers_screener.poll(Client())
    expected = {"poll": 1, "at": AT.isoformat(), "failed": bad, **baseline}
    summary = {"summary": True, "polls": 1, "failed_reads": 0, "requests": []}
    assert with_board.read_text() == json.dumps(expected) + "\n" + json.dumps(summary) + "\n"


@pytest.mark.parametrize("custom", [False, True])
def test_main_publishes_to_default_watch_folder_or_cli_board_dir(
    tmp_path, monkeypatch, custom
) -> None:
    client = Client()
    monkeypatch.setattr(movers_screener, "screener_client", lambda: client)
    folder = tmp_path / "chosen" if custom else tmp_path / "home" / ".claude" / "canon" / "watch"
    args = ["--minutes", "1", "--log-dir", str(tmp_path)]
    if custom:
        args += ["--board-dir", str(folder)]
    assert movers_screener.main(args) == 0
    assert (folder / "screener-board.json").is_file()
    assert len(client.calls) == 3


def test_commands_row_and_screener_header_document_the_local_board() -> None:
    root = Path(__file__).resolve().parents[1]
    commands = (
        (root / "AGENTS.md").read_text().split("## Commands\n", 1)[1].split("## Layout\n", 1)[0]
    )
    assert "python -m canon_trading.screener_board" in commands
    assert "screener-board.json" in (movers_screener.__doc__ or "")
    assert "adds no request" in (movers_screener.__doc__ or "")


def test_any_failure_of_the_board_write_leaves_the_polling_going(
    tmp_path, monkeypatch, capsys
) -> None:
    """Not only a file error: whatever the display's write raises is named and the record goes on."""

    def broken(*args):
        raise TypeError("not serializable")

    monkeypatch.setattr(screener_board, "write_board", broken)
    log = tmp_path / "poll.jsonl"
    assert movers_screener.run(Client(), 2, log, board_dir=tmp_path, sleep=lambda _: None) == 0
    assert json.loads(log.read_text().splitlines()[-1])["summary"]
    assert capsys.readouterr().err.count("screener board not written: not serializable") == 2
