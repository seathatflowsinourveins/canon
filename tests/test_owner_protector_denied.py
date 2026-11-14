"""C1's P2-1, the ruled design (the lead, 2026-11-13 19:11): a protective sell the venue refuses
(a 403 or 422, OrderRejected or OrderDenied) is definitive and never sent again in this run. The
holding is frozen with the venue's own reason, an ALERT line says it once, the frozen state reaches
the book, and the next start re-arms that holding from the book."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from tests.test_owner_protector import AAA
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import book_rows
from tests.test_owner_protector import restart_harness
from tests.test_owner_protector_chase import refused


def armed_and_sent(path: Path) -> Any:
    strategy = restart_harness(path)
    strategy.cached.latest = None
    strategy.on_start()
    for n, bid in ((180, "1.05"), (181, "1.20"), (182, "1.20")):
        strategy.observe(bid, T0 + n * SECOND)
    assert len(strategy.sent) == 1, strategy.said
    return strategy


def refuse(strategy: Any) -> None:
    order = strategy.sent[0]
    at = T0 + 183 * SECOND
    refused(order, at)
    # The helper's OrderRejected carries the venue reason "refused by the venue".
    assert order.status.name == "REJECTED"
    strategy.observe("1.21", at)


def test_a_refused_protective_sell_freezes_its_holding_with_the_venues_reason(
    tmp_path: Path,
) -> None:
    strategy = armed_and_sent(tmp_path / "levels.json")
    refuse(strategy)
    frozen = [n for n in strategy.said if n.startswith(f"{AAA}: frozen:")]
    assert len(frozen) == 1, strategy.said
    assert "refused by the venue" in frozen[0] and "REJECTED" in frozen[0], frozen
    for n in range(184, 190):
        strategy.observe("1.21", T0 + n * SECOND)
        strategy.test_clock.set_time(T0 + n * SECOND)
        strategy._on_step(cast(Any, SimpleNamespace()))
    assert len(strategy.sent) == 1, "a refused protective sell was sent again in the same run"
    row = book_rows(tmp_path / "levels.json")["AAA.ALPACA"]
    assert row["state"] == "frozen" and "refused by the venue" in row["reason"], row


def test_a_refused_protective_sell_raises_one_alert_line(tmp_path: Path) -> None:
    strategy = armed_and_sent(tmp_path / "levels.json")
    refuse(strategy)
    for n in range(184, 187):
        strategy.observe("1.21", T0 + n * SECOND)
    alerts = [n for n in strategy.said if n.startswith(f"{AAA}: ALERT:")]
    assert len(alerts) == 1, strategy.said
    assert "refused by the venue" in alerts[0] and "unprotected" in alerts[0], alerts


def test_the_next_start_rearms_a_refused_holding_from_the_book(tmp_path: Path) -> None:
    path = tmp_path / "levels.json"
    first = armed_and_sent(path)
    level = first.slots[AAA].level
    refuse(first)
    first.on_stop()
    second = restart_harness(path)
    second.settings.run = "shape-after-refusal"
    second.on_start()
    assert second.slots[AAA].level == level, second.said
    assert AAA not in second.frozen or not second.frozen[AAA].startswith("our protective"), (
        second.frozen
    )


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Synthetic only, as the sibling owner-protector files: never inspect a credential environment
    or a paper lock (tests/conftest.py's autouse fixture reads the key variables)."""
