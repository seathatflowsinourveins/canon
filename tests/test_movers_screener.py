"""The movers probe's screener poll (ADR 0070, its addition of 2026-11-11): data-only, every method but GET
refused with nothing sent, no trading client, no stream lock, bounded polls. Offline: requests' transport is
replaced by a recorder that answers here, so nothing reaches Alpaca."""

from __future__ import annotations

import ast
import gc
import json
import re
import tomllib
from datetime import UTC
from datetime import datetime
from pathlib import Path

import alpaca.trading.client
import pytest
import requests
from alpaca.data.historical.screener import ScreenerClient
from alpaca.trading.client import TradingClient
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import timeout_adapter
from requests.adapters import HTTPAdapter

from canon_trading import movers_screener
from canon_trading.session_window import SOURCE_ROOT
from canon_trading.session_window import START_UP_SECS

PLACEHOLDER = "offline-placeholder"
NOW = "2026-11-11T15:00:00Z"
ANSWERS = {
    "/v1beta1/screener/stocks/movers": {
        "gainers": [{"symbol": "AAA", "percent_change": 31.5, "change": 1.2, "price": 5.0}],
        "losers": [{"symbol": "BBB", "percent_change": -20.0, "change": -2.0, "price": 8.0}],
        "market_type": "stocks",
        "last_updated": NOW,
    },
    "/v1beta1/screener/stocks/most-actives": {
        "most_actives": [{"symbol": "CCC", "volume": 1e6, "trade_count": 5e3}],
        "last_updated": NOW,
    },
}


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Data-only mode with placeholder keys; every request that reaches requests' transport is recorded
    and answered offline from `ANSWERS` by path (`{}` otherwise)."""
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    recorded: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        recorded.append((request.method, request.url))
        path = requests.utils.urlparse(request.url).path
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(ANSWERS.get(path, {})).encode()
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return recorded


@pytest.fixture
def no_trading_client(monkeypatch) -> None:
    """Building a `TradingClient` fails the test."""

    def refused(self, *args, **kwargs):
        raise AssertionError("a TradingClient was built")

    monkeypatch.setattr(alpaca.trading.client.TradingClient, "__init__", refused)


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_a_method_other_than_get_is_refused_with_nothing_sent(sent, method) -> None:
    """The module's client, as `main` builds it: every method but GET refused before requests' transport
    sees it; a GET goes out."""
    client = movers_screener.screener_client()
    assert isinstance(client, ScreenerClient)
    with pytest.raises(MethodRefused, match="nothing was sent"):
        getattr(client, method)("/screener/stocks/most-actives", {})
    assert sent == []
    adapter = timeout_adapter(client)
    assert adapter is not None and adapter.get_only
    client.get("/screener/stocks/most-actives")
    assert [m for m, _ in sent] == ["GET"]


def test_no_client_is_built_outside_data_only_mode(monkeypatch) -> None:
    monkeypatch.setenv(DATA_ONLY_ENV, "0")  # recorded, so teardown restores what was there
    monkeypatch.delenv(DATA_ONLY_ENV)
    with pytest.raises(movers_screener.DataOnlyRefused, match="nothing was built"):
        movers_screener.screener_client()


def test_a_run_builds_no_trading_client_and_sends_only_gets(
    sent, no_trading_client, monkeypatch, tmp_path
) -> None:
    """`main` end to end, offline: data-only set by the module itself, three GETs a poll to the screener's
    two endpoints, one line a poll and a summary, and no `TradingClient` built or alive."""
    monkeypatch.setenv(DATA_ONLY_ENV, "0")
    assert movers_screener.main(["--minutes", "1", "--log-dir", str(tmp_path)]) == 0
    assert {m for m, _ in sent} == {"GET"} and len(sent) == 3
    assert {requests.utils.urlparse(u).path for _, u in sent} == set(ANSWERS)
    (log,) = tmp_path.glob("movers-screener-*.jsonl")
    poll, summary = [json.loads(line) for line in log.read_text().splitlines()]
    assert poll["failed"] == 0 and poll["movers"]["gainers"][0]["symbol"] == "AAA"
    assert poll["most_actives_trades"]["most_actives"][0]["symbol"] == "CCC"
    assert summary["summary"] is True and summary["failed_reads"] == 0
    gc.collect()
    assert not [o for o in gc.get_objects() if isinstance(o, TradingClient)]


def test_the_modules_import_nothing_that_trades() -> None:
    """Neither probe module imports alpaca-py's trading package, its streams or a NautilusTrader execution
    client: they hold no way to place an order."""
    for name in ("movers_screener", "movers_stream"):
        tree = ast.parse((SOURCE_ROOT / "src" / "canon_trading" / f"{name}.py").read_text())
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.startswith("alpaca.trading") or "execution" in m], name


class Client:
    """A stand-in `ScreenerClient` whose reads are scripted."""

    def __init__(self, fail: dict[str, BaseException] | None = None) -> None:
        self.fail = fail or {}
        self.calls: list[str] = []

    def get_market_movers(self, request_params):
        self.calls.append("movers")
        if "movers" in self.fail:
            raise self.fail["movers"]
        from alpaca.data.models.screener import Movers

        return Movers.model_validate(ANSWERS["/v1beta1/screener/stocks/movers"])

    def get_most_actives(self, request_params):
        self.calls.append(f"actives-{request_params.by}")
        from alpaca.data.models.screener import MostActives

        return MostActives.model_validate(ANSWERS["/v1beta1/screener/stocks/most-actives"])


def test_polls_are_a_minute_apart_and_a_failed_read_is_logged_counted_and_passed(tmp_path) -> None:
    now = [1000.0]
    slept: list[float] = []

    def sleep(secs: float) -> None:
        slept.append(secs)
        now[0] += secs

    client = Client(fail={"movers": OSError("network down")})
    log = tmp_path / "x.jsonl"
    failed = movers_screener.run(client, 3, log, clock=lambda: now[0], sleep=sleep)
    assert failed == 3 and slept == [60.0, 60.0]
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [ln.get("poll") for ln in lines[:3]] == [1, 2, 3]
    assert lines[0]["movers"] == {"error": "OSError: network down"}
    assert lines[0]["most_actives_volume"]["most_actives"][0]["symbol"] == "CCC"
    assert lines[3]["failed_reads"] == 3


def test_a_refused_method_ends_the_run(tmp_path) -> None:
    client = Client(fail={"movers": MethodRefused("refused")})
    with pytest.raises(MethodRefused):
        movers_screener.run(
            client, 2, tmp_path / "x.jsonl", clock=lambda: 0.0, sleep=lambda s: None
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["--minutes", "0", "--log-dir", "/tmp/x"],
        ["--minutes", "86", "--log-dir", "/tmp/x"],
        ["--minutes", "1", "--log-dir", "relative"],
        ["--minutes", "1"],
    ],
)
def test_a_run_is_1_to_85_polls_into_an_absolute_folder(argv) -> None:
    with pytest.raises(SystemExit):
        movers_screener.main(argv)


def test_the_log_is_named_for_the_module_and_its_utc_start(tmp_path) -> None:
    path = movers_screener.log_path(tmp_path, datetime(2026, 11, 11, 9, 5, 7, tzinfo=UTC))
    assert path == Path(tmp_path) / "movers-screener-20261111T090507Z.jsonl"


def test_paper_data_runs_the_screener_without_the_stream_lock_inside_its_deadline() -> None:
    """The task (mise.toml): the screener is on the list, sets no stream lock (`streams=0`, and `stream`
    returns before the lock unless `streams` is 1), logs to the per-user folder, and its start-up and longest
    run fit the deadline."""
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:data"]
    run = task["run"]
    branch = re.search(r"^  movers-screener\) (.*)$", run, re.MULTILINE)
    assert branch is not None
    assert "module=canon_trading.movers_screener" in branch.group(
        1
    ) and "streams=0" in branch.group(1)
    assert '[ "$streams" = 1 ] || return 0' in run
    assert '--log-dir "${tmp}canon-paper-data"' in run
    assert '"movers-screener"' in task["usage"]
    bound = {k: int(v) for k, v in re.findall(r"^(deadline|sync)=(\d+)$", run, re.MULTILINE)}
    assert bound["deadline"] >= bound["sync"] + START_UP_SECS + movers_screener.MAX_MINUTES * 60
