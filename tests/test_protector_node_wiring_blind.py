"""Blind wiring: what each unit added to the protector's config must reach the strategy.

Every case builds the node through `protector_node`, the builder the task calls, with the task's own
arguments and a stand-in for the level book's trading reader. The node is built but never connected:
nothing reaches Alpaca, and no level book, catalog or ledger is written. Each case is named after the
config field it proves; the expected value is what the task passes in, never what the builder does
with it.

At the base (e217e3ed) the builder has no `book` or `session_part` keyword and `OwnerProtectorConfig`
has none of these fields, so the cases fail at that seam. That is the property not being built yet,
not a fault in a case.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nautilus_trader.model import InstrumentId

AAA = InstrumentId.from_str("AAA.ALPACA")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """No credential-environment reader for these offline builds."""


def subject() -> Any:
    """The runner module, looked up at the base's own name; the composed tree keeps it."""
    return importlib.import_module("canon_trading.protector_node")


@pytest.fixture
def controls(tmp_path: Path) -> tuple[Path, Path]:
    holds, inbox = tmp_path / "holds", tmp_path / "inbox"
    holds.mkdir()
    inbox.write_text("")
    return holds, inbox


@pytest.fixture
def built(controls: tuple[Path, Path]) -> Iterator[Callable[..., Any]]:
    """Build one node the way the task does and dispose it; never connect it."""
    nodes: list[Any] = []

    def build(
        *,
        book: dict[str, Any] | None = None,
        session_part: str = "regular",
        feed: str = "sip",
    ) -> Any:
        module = subject()
        config = module.ProtectorRun(
            account="windows",
            row="OWN1",
            holds_dir=controls[0],
            inbox=controls[1],
        )
        node, strategy = module.protector_node(
            config, (AAA,), feed=feed, book=book, session_part=session_part
        )
        nodes.append(node)
        return strategy

    yield build
    for node in nodes:
        node.dispose()


def test_account_label_reaches_the_strategy_config(built: Callable[..., Any]) -> None:
    strategy = built(book={"account_label": "windows"})
    assert strategy.settings.account_label == "windows"


def test_level_book_path_reaches_the_strategy_config(
    built: Callable[..., Any], tmp_path: Path
) -> None:
    path = tmp_path / "windows-book.json"
    strategy = built(book={"account_label": "windows", "level_book_path": str(path)})
    settings: Any = strategy.settings
    assert settings.level_book_path is not None
    assert Path(settings.level_book_path) == path


def test_run_reaches_the_strategy_config(built: Callable[..., Any]) -> None:
    strategy = built(book={"run": "wiring-blind-20261113T192000Z"})
    assert strategy.settings.run == "wiring-blind-20261113T192000Z"


@pytest.mark.parametrize("part", ["regular", "pre-market", "after-hours", "overnight"])
def test_session_part_reaches_the_strategy_config(built: Callable[..., Any], part: str) -> None:
    strategy = built(session_part=part)
    assert strategy.settings.session_part == part


def test_trading_reader_reaches_the_strategy_config(built: Callable[..., Any]) -> None:
    reader = SimpleNamespace(name="trading-reader")
    strategy = built(book={"trading": reader})
    assert strategy.settings.trading is reader


def test_every_level_book_setting_arrives_in_one_build(
    built: Callable[..., Any], tmp_path: Path
) -> None:
    reader = SimpleNamespace(name="trading-reader")
    path = tmp_path / "windows-book.json"
    strategy = built(
        book={
            "account_label": "windows",
            "level_book_path": str(path),
            "run": "wiring-blind-20261113T192000Z",
            "trading": reader,
        },
        session_part="overnight",
    )
    settings: Any = strategy.settings
    assert settings.account_label == "windows"
    assert Path(settings.level_book_path) == path
    assert settings.run == "wiring-blind-20261113T192000Z"
    assert settings.trading is reader
    assert settings.session_part == "overnight"


def test_by_hand_run_leaves_the_level_book_settings_absent(
    controls: tuple[Path, Path],
) -> None:
    """`paper:protector` by hand passes no book: persistence is off and the session part is regular."""
    module = subject()
    config = module.ProtectorRun(
        account="windows",
        row="OWN1",
        holds_dir=controls[0],
        inbox=controls[1],
    )
    node, strategy = module.protector_node(config, (AAA,), feed="sip")
    try:
        settings: Any = strategy.settings
        assert settings.account_label is None
        assert settings.level_book_path is None
        assert settings.run == ""
        assert settings.trading is None
        assert settings.session_part == "regular"
    finally:
        node.dispose()


def test_lock_wait_reaches_the_task_arguments(controls: tuple[Path, Path]) -> None:
    parsed = subject().parse_args(
        [
            "windows",
            "--row",
            "OWN1",
            "--holds-dir",
            str(controls[0]),
            "--inbox",
            str(controls[1]),
            "--lock-wait",
            "30",
        ]
    )
    assert parsed.lock_wait == 30
