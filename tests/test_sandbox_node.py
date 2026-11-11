"""The sandbox node (ADR 0070, terms 3, 4 and 6): built only in data-only mode, refused when an Alpaca
execution client exists after the build, and otherwise upstream's example's node on Alpaca's data. Offline:
nothing here connects."""

from __future__ import annotations

import gc
import os
import re
import tomllib

import pytest
from canon_alpaca.providers import DATA_ONLY_ENV

from canon_trading import sandbox_node
from canon_trading.paper_node import alpaca_paper_builder
from canon_trading.sandbox_node import DataOnlyRefused
from canon_trading.sandbox_node import alpaca_execution_clients
from canon_trading.sandbox_node import refuse_alpaca_execution
from canon_trading.session_window import SOURCE_ROOT
from canon_trading.session_window import START_UP_SECS


@pytest.fixture
def data_only(monkeypatch) -> None:
    monkeypatch.setenv(DATA_ONLY_ENV, "1")


def test_the_sandbox_node_is_not_built_outside_data_only_mode(monkeypatch) -> None:
    monkeypatch.setenv(DATA_ONLY_ENV, "0")  # recorded, so teardown restores what was there
    monkeypatch.delenv(DATA_ONLY_ENV)
    with pytest.raises(DataOnlyRefused, match="nothing was built"):
        sandbox_node.sandbox_node()


def test_the_sandbox_node_holds_no_alpaca_execution_client(data_only) -> None:
    """Upstream's example's shape: the Alpaca data client and the sandbox execution client; the check after
    the build finds no Alpaca execution client, so the node is returned."""
    gc.collect()
    assert alpaca_execution_clients() == [], "another test left an Alpaca execution client alive"
    node = sandbox_node.sandbox_node()
    try:
        assert alpaca_execution_clients() == []
        assert node.trader_id == sandbox_node.TRADER_ID
    finally:
        node.dispose()


def test_a_node_carrying_an_alpaca_execution_client_is_refused(data_only) -> None:
    """Term 4: the paper node's builder, which adds the Alpaca execution client, is refused by the check the
    sandbox node runs after its build, and the node is disposed of."""
    node = alpaca_paper_builder("CANON-NOT-SANDBOX", sandbox_node.INSTRUMENT_ID).build()
    try:
        assert len(alpaca_execution_clients()) == 1
        with pytest.raises(DataOnlyRefused, match="1 AlpacaExecutionClient"):
            refuse_alpaca_execution(node)
    finally:
        node.dispose()
        del node
        gc.collect()


def test_the_module_sets_data_only_mode_itself_and_bounds_its_minutes(monkeypatch) -> None:
    """`main` sets the mode before it builds, whatever the task set, and refuses a run outside 1 to 60
    minutes."""
    monkeypatch.setenv(DATA_ONLY_ENV, "0")  # recorded, so teardown restores what was there
    monkeypatch.delenv(DATA_ONLY_ENV)
    seen: list[str | None] = []

    def built() -> None:
        seen.append(os.environ.get(DATA_ONLY_ENV))
        raise DataOnlyRefused("stop here")

    monkeypatch.setattr(sandbox_node, "sandbox_node", built)
    with pytest.raises(DataOnlyRefused, match="stop here"):
        sandbox_node.main(["--minutes", "1"])
    assert seen == ["1"]
    for minutes in ("0", "61"):
        with pytest.raises(SystemExit):
            sandbox_node.main(["--minutes", minutes])


def test_the_tester_is_upstreams_example_on_spy_without_a_book() -> None:
    config = sandbox_node.exec_tester_config()
    assert config.instrument_id == sandbox_node.INSTRUMENT_ID
    assert config.subscribe_book is False and config.subscribe_quotes is True
    assert config.cancel_orders_on_stop is True and config.close_positions_on_stop is True


def test_paper_data_reads_account_a_and_its_deadline_holds_the_stream_wait_start_up_run_and_stop() -> (
    None
):
    """The task (mise.toml): account A's service, the data-only flag, and a deadline 60 s inside mise's
    timeout that holds the stream lock's wait, uv's sync, the imports, the longest run and its stop."""
    task = tomllib.loads((SOURCE_ROOT / "mise.toml").read_text())["tasks"]["paper:data"]
    for item in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        assert f"-s canon-alpaca-paper -a {item} -w" in task["env"][item]["value"]
    assert task["env"]["CANON_ALPACA_DATA_ONLY"] == "1"
    bound = {
        k: int(v)
        for k, v in re.findall(r"^(deadline|sync|lockwait)=(\d+)$", task["run"], re.MULTILINE)
    }
    assert task["timeout"] == "90m" and bound["deadline"] == 90 * 60 - 60
    assert bound["deadline"] >= (
        bound["lockwait"]
        + bound["sync"]
        + START_UP_SECS
        + sandbox_node.MAX_MINUTES * 60
        + sandbox_node.STOP_TIMEOUT_SECS
    )
    assert (
        'python -m "$module"' in task["run"] and "module=canon_trading.sandbox_node" in task["run"]
    )


@pytest.mark.parametrize(
    ("left", "status"),
    [
        ({"open_orders": 0, "positions_open": 0}, 0),
        ({"open_orders": 1, "positions_open": 0}, 1),
        ({"open_orders": 0, "positions_open": 1}, 1),
    ],
)
def test_a_run_that_does_not_end_flat_fails(monkeypatch, left, status) -> None:
    """`main` exits 1 when the tester's stop left an order or a position open in the sandbox's cache, and
    disposes of the node either way."""
    monkeypatch.setenv(DATA_ONLY_ENV, "0")  # recorded, so teardown restores what was there
    disposed: list[bool] = []

    class Node:
        cache = None

        def dispose(self) -> None:
            disposed.append(True)

    async def ran(node, seconds) -> None:
        return None

    monkeypatch.setattr(sandbox_node, "sandbox_node", Node)
    monkeypatch.setattr(sandbox_node, "run_for", ran)
    monkeypatch.setattr(sandbox_node, "summary", lambda cache: {"orders": 2, "filled": 1, **left})
    assert sandbox_node.main(["--minutes", "1"]) == status
    assert disposed == [True]
