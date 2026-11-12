"""`canon_trading.trade_conditions`, the GET of Alpaca's trade-condition codes per tape that
`mise run paper:history <account> conditions` runs, offline: every request is answered by a stand-in at
requests' transport."""

import json
import os
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import requests
from canon_alpaca import providers
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import ENV_API_KEY
from canon_alpaca.providers import ENV_SECRET_KEY
from requests.adapters import HTTPAdapter

from canon_trading import trade_conditions
from canon_trading.history_load import HistoryStopped
from canon_trading.history_load import history_client
from canon_trading.session_window import SOURCE_ROOT

PLACEHOLDER = "placeholder-not-a-key"
CODES = {
    "A": {" ": "Regular Sale", "B": "Average Price Trade", "I": "Odd Lot Trade"},
    "B": {" ": "Regular Sale", "T": "Extended Hours Trade"},
    "C": {"@": "Regular Sale", "W": "Average Price Trade"},
}


class Venue:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, list[str]]]] = []
        self.status = 200
        self.body: object = None

    def answer(self, request: requests.PreparedRequest) -> tuple[int, object]:
        url = urlsplit(request.url or "")
        query = parse_qs(url.query)
        self.sent.append((request.method or "", url.path, query))
        if self.body is not None:
            return self.status, self.body
        if url.path == "/v2/stocks/trades":
            token = query.get("page_token", [""])[0]
            page = int(token or 0)
            symbol = query["symbols"][0]
            rows = [
                {"t": f"2026-11-12T15:50:5{page}Z", "p": 1.0, "s": 1, "c": ["@", "I"], "z": "C"}
            ]
            return self.status, {
                "trades": {symbol: rows},
                "next_page_token": str(page + 1) if page < 2 else None,
            }
        return self.status, CODES[query["tape"][0]]


@pytest.fixture
def venue(monkeypatch) -> Venue:
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    monkeypatch.setattr(providers, "REQUESTS", providers.RequestCounter())
    stand_in = Venue()

    def send(self, request, **kwargs):
        status, body = stand_in.answer(request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return stand_in


def test_one_get_per_tape_on_alpacas_documented_path_paced(venue: Venue) -> None:
    slept: list[float] = []
    pace = trade_conditions.pacer(slept.append)
    codes = trade_conditions.read_trade_conditions(history_client(), pace=pace)
    assert codes == CODES
    assert [(m, p, q["tape"]) for m, p, q in venue.sent] == [
        ("GET", "/v2/stocks/meta/conditions/trade", ["A"]),
        ("GET", "/v2/stocks/meta/conditions/trade", ["B"]),
        ("GET", "/v2/stocks/meta/conditions/trade", ["C"]),
    ]
    gap = 60 / trade_conditions.PACE.data_per_minute
    assert slept == [gap, gap], "paced at ADR 0093's row between GETs, none before the first"


@pytest.mark.parametrize(
    "body", [[], {}, {"@": 1}, {"": "Regular Sale"}, {"AB": "Two characters"}, {"@": ""}, "text"]
)
def test_an_answer_that_is_not_a_map_of_one_character_codes_stops_by_name(
    venue: Venue, body: object
) -> None:
    venue.body = body
    with pytest.raises(HistoryStopped, match="tape A"):
        trade_conditions.read_trade_conditions(history_client(), pace=lambda: None)
    assert len(venue.sent) == 1, "nothing more is asked after a malformed answer"


def test_a_429_stops_the_read_and_asks_for_nothing_more(venue: Venue) -> None:
    venue.status, venue.body = 429, {"message": "too many requests"}
    with pytest.raises(HistoryStopped, match="429"):
        trade_conditions.read_trade_conditions(history_client(), pace=lambda: None)
    assert len(venue.sent) == 1


def test_main_writes_the_codes_with_when_and_where_they_were_read(
    venue: Venue, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "conditions" / "trade.json"
    assert trade_conditions.main(["--out", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["tapes"] == CODES
    assert written["path"] == "/v2/stocks/meta/conditions/trade"
    assert written["read_at"].endswith("Z")
    printed = capsys.readouterr().out
    assert "tape A: 3 codes" in printed and "tape C: 2 codes" in printed
    assert PLACEHOLDER not in printed and PLACEHOLDER not in out.read_text()


def test_main_refuses_an_out_file_under_the_ledger(venue: Venue, tmp_path: Path) -> None:
    out = trade_conditions.LEDGER / "trade.json"
    with pytest.raises(SystemExit):
        trade_conditions.main(["--out", str(out)])
    assert venue.sent == []


def _route(tmp_path: Path, **usage: str) -> subprocess.CompletedProcess[str]:
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:history"]
    security = tmp_path / "security"
    security.write_text(f"#!/bin/sh\necho {PLACEHOLDER}\n")
    security.chmod(0o700)
    timeout = tmp_path / "timeout"
    timeout.write_text('#!/bin/sh\nif [ "$1" = -s ]; then shift 2; fi\nshift\nexec "$@"\n')
    timeout.chmod(0o700)
    uv = tmp_path / "uv"
    uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$SENT"\nprintf "%s\\n" "$CANON_ALPACA_DATA_ONLY" >> "$SENT"\n'
    )
    uv.chmod(0o700)
    script = (
        task["run"]
        .replace("/usr/bin/security", str(security))
        .replace("/usr/bin/timeout", str(timeout))
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": f"{tmp_path}:{os.defpath}", "SENT": str(tmp_path / "sent"), **usage},
        capture_output=True,
        text=True,
        check=False,
    )


def test_mise_conditions_mode_passes_symbols_and_window(tmp_path: Path) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_mode="conditions",
        usage_symbols="TSLA,ADXN",
        usage_start="2026-11-12T15:50:50Z",
        usage_end="2026-11-12T15:51:25Z",
    )
    assert done.returncode == 0, done.stderr
    sent = (tmp_path / "sent").read_text().splitlines()
    assert sent[5:13] == [
        "--out",
        "catalog/conditions/trade.json",
        "--symbols",
        "TSLA,ADXN",
        "--start",
        "2026-11-12T15:50:50Z",
        "--end",
        "2026-11-12T15:51:25Z",
    ]


def test_mise_conditions_mode_runs_the_read_in_data_only_mode(tmp_path: Path) -> None:
    done = _route(tmp_path, usage_account="tests", usage_mode="conditions")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "sent").read_text().splitlines() == [
        "run",
        "--locked",
        "python",
        "-m",
        "canon_trading.trade_conditions",
        "--out",
        "catalog/conditions/trade.json",
        "1",
    ]
    done = _route(
        tmp_path, usage_account="tests", usage_mode="conditions", usage_catalog="/elsewhere"
    )
    assert done.returncode == 0, done.stderr
    assert "/elsewhere/conditions/trade.json" in (tmp_path / "sent").read_text()


def test_mise_conditions_mode_refuses_a_load_or_baseline_flag_before_any_pair(
    tmp_path: Path,
) -> None:
    for flag, value in (("usage_bars", "1-DAY"), ("usage_data", "trades"), ("usage_feed", "sip")):
        done = _route(tmp_path, usage_account="tests", usage_mode="conditions", **{flag: value})
        assert done.returncode == 3, (flag, done.stderr)
        assert (
            "conditions mode takes no flag but --catalog" in done.stderr
            and "nothing was read" in done.stderr
        )
        assert not (tmp_path / "sent").exists()


def test_mise_load_mode_still_runs_history_load(tmp_path: Path) -> None:
    done = _route(
        tmp_path,
        usage_account="tests",
        usage_symbols="AAPL",
        usage_start="2026-11-11T14:30:00Z",
        usage_end="2026-11-11T15:00:00Z",
    )
    assert done.returncode == 0, done.stderr
    sent = (tmp_path / "sent").read_text().splitlines()
    assert sent[:5] == ["run", "--locked", "python", "-m", "canon_trading.history_load"]
    assert sent[sent.index("--symbols") + 1] == "AAPL"


def test_raw_trades_keep_each_prints_conditions_page_by_page(venue: Venue, tmp_path: Path) -> None:
    slept: list[float] = []
    pages = trade_conditions.read_raw_trades(
        history_client(),
        "TSLA",
        "2026-11-12T15:50:50Z",
        "2026-11-12T15:51:25Z",
        pace=trade_conditions.pacer(slept.append),
    )
    assert [p["trades"]["TSLA"][0]["c"] for p in pages] == [["@", "I"]] * 3
    sent = [(path, q) for _, path, q in venue.sent]
    assert all(path == "/v2/stocks/trades" for path, _ in sent)
    assert [q.get("page_token") for _, q in sent] == [None, ["1"], ["2"]]
    assert all(q["feed"] == ["sip"] and q["symbols"] == ["TSLA"] for _, q in sent)
    assert len(slept) == 2


def test_raw_trades_stop_at_the_page_cap(venue: Venue, monkeypatch) -> None:
    monkeypatch.setattr(trade_conditions, "RAW_MAX_PAGES", 2)
    with pytest.raises(HistoryStopped, match="2 pages"):
        trade_conditions.read_raw_trades(
            history_client(), "TSLA", "2026-11-12T15:50Z", "2026-11-12T15:51Z", pace=lambda: None
        )


def test_main_with_symbols_writes_each_symbols_raw_pages(venue: Venue, tmp_path: Path) -> None:
    out = tmp_path / "conditions" / "trade.json"
    argv = ["--out", str(out), "--symbols", "TSLA", "--start", "2026-11-12T15:50:50Z"]
    assert trade_conditions.main([*argv, "--end", "2026-11-12T15:51:25Z"]) == 0
    raw = json.loads((out.parent / "trades-TSLA-2026-11-12T15:50:50Z.json").read_text())
    assert raw["symbol"] == "TSLA" and len(raw["pages"]) == 3
    assert raw["start"] == "2026-11-12T15:50:50Z" and raw["end"] == "2026-11-12T15:51:25Z"


def test_one_pace_runs_between_every_get_of_a_run_maps_pages_and_symbols(
    venue: Venue, tmp_path: Path, monkeypatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    out = tmp_path / "conditions" / "trade.json"
    argv = ["--out", str(out), "--symbols", "TSLA,ADXN", "--start", "2026-11-12T15:50:50Z"]
    assert trade_conditions.main([*argv, "--end", "2026-11-12T15:51:25Z"]) == 0
    assert len(venue.sent) == 3 + 3 + 3, "three maps, then three pages for each symbol"
    gap = 60 / trade_conditions.PACE.data_per_minute
    assert slept == [gap] * (len(venue.sent) - 1), "a gap before every GET but the first"


@pytest.mark.parametrize(
    "argv",
    [
        ["--start", "2026-11-12T15:50:50Z", "--end", "2026-11-12T15:51:25Z"],
        ["--symbols", "TSLA", "--start", "2026-11-12T15:50:50Z"],
        ["--end", "2026-11-12T15:51:25Z"],
    ],
)
def test_main_refuses_a_window_without_symbols_or_symbols_without_a_window(
    venue: Venue, tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(SystemExit):
        trade_conditions.main(["--out", str(tmp_path / "trade.json"), *argv])
    assert venue.sent == []


def test_mise_conditions_mode_hands_a_partial_window_to_the_module_to_refuse(
    tmp_path: Path,
) -> None:
    done = _route(
        tmp_path, usage_account="tests", usage_mode="conditions", usage_start="2026-11-12T15:50:50Z"
    )
    assert done.returncode == 0, done.stderr
    sent = (tmp_path / "sent").read_text().splitlines()
    assert sent[5:9] == [
        "--out",
        "catalog/conditions/trade.json",
        "--start",
        "2026-11-12T15:50:50Z",
    ]


def test_mise_load_mode_ignores_a_module_named_in_the_environment(tmp_path: Path) -> None:
    done = _route(tmp_path, usage_account="tests", usage_symbols="AAPL", module="os")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "sent").read_text().splitlines()[4] == "canon_trading.history_load"
