"""The owner's word for one symbol (the lead's gate unit for the protect account, 2026-11-13 23:50).

Each case is one property of the unit:
1. the word is one file named for the symbol in the instance's symbol-holds folder;
2. it is read at the start and at every step. A held symbol is not armed at the start. A new hold
   cancels only our own sell of that symbol within one step and sells nothing. Every other holding
   stays protected;
3. the hold is named in the node's line, in the END record and in the book's state;
4. removing the hold re-arms the symbol at its level at the next step, never lowered, and a breach
   during the hold is acted on then under the count rule;
5. the hold never touches an order the owner placed.

Offline: the Harness of tests/test_owner_protector.py (real order factory, stand-in cache and
transport), with two holdings and a quote per instrument.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import PositionId
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity

from canon_trading import symbol_hold
from tests.test_owner_protector import AAA
from tests.test_owner_protector import BBB
from tests.test_owner_protector import PROTECTOR
from tests.test_owner_protector import SECOND
from tests.test_owner_protector import T0
from tests.test_owner_protector import Cache
from tests.test_owner_protector import Harness
from tests.test_owner_protector import position
from tests.test_owner_protector import quote
from tests.test_owner_protector import restart_harness
from tests.test_owner_protector import restart_record
from tests.test_owner_protector import working_sell

ROOT = Path(__file__).resolve().parents[1]
HELD = symbol_hold.HELD


class TwoCache(Cache):
    """AAA (10 shares) and BBB (7 shares), each with its own latest quote."""

    def __init__(self) -> None:
        super().__init__()
        other = position("7")
        other.instrument_id, other.id = BBB, PositionId("BBB.ALPACA-EXTERNAL")
        self.held.append(other)
        self.latest_by = {AAA: quote("10.00", T0), BBB: quote("20.00", T0, iid=BBB)}

    def quote(self, iid):
        return self.latest_by.get(iid)

    def quotes(self, iid):
        tick = self.latest_by.get(iid)
        return [] if tick is None else [tick]


def two(folder: Path, **settings: Any) -> Harness:
    strategy = Harness(
        levels={AAA: Price.from_str("9.90"), BBB: Price.from_str("19.80")},
        symbol_holds_dir=folder,
        **settings,
    )
    strategy.cached = TwoCache()
    return strategy


def tick(strategy: Harness, iid, bid: str, at: int) -> None:
    q = quote(bid, at, iid=iid)
    cache = strategy.cached
    assert isinstance(cache, TwoCache)
    cache.latest_by[iid] = q
    strategy.test_clock.set_time(max(at, strategy.test_clock.timestamp_ns()))
    strategy.on_quote(q)


def step(strategy: Harness, at: int) -> None:
    strategy.test_clock.set_time(max(at, strategy.test_clock.timestamp_ns()))
    strategy._on_step(cast(Any, SimpleNamespace(ts_event=at)))


def ours(strategy: Harness, iid) -> list[Any]:
    return [order for order in strategy.sent if order.instrument_id == iid]


def acknowledge_cancel(strategy: Harness, cid: ClientOrderId, iid, qty: str, trigger: str) -> None:
    """Our cancel's terminal acknowledgement, with the trigger the emulator had raised it to."""
    strategy.cached.working = [o for o in strategy.cached.working if o.client_order_id != cid]
    strategy.cached.working.append(
        working_sell(
            str(cid),
            qty=qty,
            filled="0",
            instrument_id=iid,
            strategy_id=PROTECTOR,
            status=OrderStatus.CANCELED,
            is_closed=True,
            trigger_price=Price.from_str(trigger),
        )
    )


def armed(folder: Path) -> Harness:
    """Both holdings armed, each with one sell of ours."""
    strategy = two(folder)
    strategy.on_start()
    tick(strategy, AAA, "10.00", T0 + SECOND // 2)
    tick(strategy, BBB, "20.00", T0 + SECOND // 2)
    assert len(ours(strategy, AAA)) == 1 and len(ours(strategy, BBB)) == 1
    return strategy


def test_property_1_the_word_is_one_file_named_for_the_symbol(tmp_path: Path) -> None:
    folder = tmp_path / "canon" / "protector" / "protect" / symbol_hold.FOLDER
    assert symbol_hold.write_hold(folder, "AAA") is True
    assert symbol_hold.write_hold(folder, "AAA") is False, "a second write changes nothing"
    assert [p.name for p in folder.iterdir()] == ["AAA"], "a plain create, no temporary name"
    (folder / ".DS_Store").write_text("")
    (folder / "notes.txt").write_text("")  # a lowercase name: the Mac folds AAA and aaa
    held, strays = symbol_hold.read_holds(folder)
    assert held == {"AAA"} and strays == (".DS_Store", "notes.txt")
    assert symbol_hold.remove_hold(folder, "AAA") is True
    assert symbol_hold.remove_hold(folder, "AAA") is False
    for bad in ("BRK/B", "../X", "", "aapl"):
        try:
            symbol_hold.check_symbol(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be refused by name")
    assert symbol_hold.instance_holds("protect", home=tmp_path) == folder


def test_property_1_the_task_only_writes_or_removes_and_names_c_as_the_owners_alone() -> None:
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"].get("paper:hold-symbol")
    assert task is not None, "paper:hold-symbol is not registered"
    assert "canon_trading.symbol_hold" in task["run"]
    assert "security" not in task["run"], "the task reads no credential"
    assert "owner's alone" in task["description"] and "no lane" in task["description"]
    assert "paper:hold-symbol" in (ROOT / "AGENTS.md").read_text()


def test_property_2_a_symbol_held_at_the_start_is_not_armed_and_is_named(tmp_path: Path) -> None:
    symbol_hold.write_hold(tmp_path, "AAA")
    strategy = two(tmp_path)
    strategy.on_start()
    for at in (T0 + SECOND // 2, T0 + SECOND, T0 + 2 * SECOND):
        tick(strategy, AAA, "10.00", at)
        tick(strategy, BBB, "20.00", at)
        step(strategy, at)
    assert ours(strategy, AAA) == [], "a held symbol is not armed"
    assert len(ours(strategy, BBB)) == 1, "every other holding stays protected"
    assert any(f"{AAA}: {HELD}" in line for line in strategy.said), strategy.said


def test_property_2_a_new_hold_cancels_only_our_sell_of_that_symbol_within_one_step(
    tmp_path: Path,
) -> None:
    strategy = armed(tmp_path)
    aaa, bbb = ours(strategy, AAA)[0], ours(strategy, BBB)[0]
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    assert strategy.canceled == [aaa.client_order_id], "within one step, only our AAA sell"
    acknowledge_cancel(strategy, aaa.client_order_id, AAA, "10", "9.90")
    # Quotes advancing through the level and steps: nothing is sent, modified or sold for AAA.
    for n, bid in enumerate(("9.80", "9.70", "9.60", "10.50", "9.50"), start=2):
        tick(strategy, AAA, bid, T0 + n * SECOND)
        tick(strategy, BBB, "20.05", T0 + n * SECOND)
        step(strategy, T0 + n * SECOND)
    assert ours(strategy, AAA) == [aaa], "a held symbol is sold by nothing"
    assert ours(strategy, BBB) == [bbb] and bbb.client_order_id not in strategy.canceled


def test_property_4_removing_the_hold_rearms_at_the_level_at_the_next_step(tmp_path: Path) -> None:
    strategy = armed(tmp_path)
    aaa = ours(strategy, AAA)[0]
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    # The emulator had raised the trail to 9.95 before the cancel ended it.
    acknowledge_cancel(strategy, aaa.client_order_id, AAA, "10", "9.95")
    step(strategy, T0 + 2 * SECOND)
    assert ours(strategy, AAA) == [aaa], "nothing is placed while the hold stands"
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, T0 + 3 * SECOND)
    rearmed = ours(strategy, AAA)[1:]
    assert len(rearmed) == 1, "re-armed at the next step, with no new quote needed"
    assert rearmed[0].trigger_price == Price.from_str("9.95"), "never lowered"
    assert rearmed[0].quantity == Quantity.from_int(10)


def test_property_4_a_breach_during_the_hold_is_acted_on_under_the_count_rule(
    tmp_path: Path,
) -> None:
    strategy = armed(tmp_path)
    aaa = ours(strategy, AAA)[0]
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    acknowledge_cancel(strategy, aaa.client_order_id, AAA, "10", "9.95")
    tick(strategy, AAA, "9.90", T0 + 2 * SECOND)  # through the level, during the hold
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, T0 + 3 * SECOND)
    assert ours(strategy, AAA) == [aaa], "a breached level is not sent on a step"
    tick(strategy, AAA, "9.89", T0 + 4 * SECOND)
    assert ours(strategy, AAA) == [aaa], "one observation cannot elect"
    tick(strategy, AAA, "9.88", T0 + 5 * SECOND)
    rearmed = ours(strategy, AAA)[1:]
    assert len(rearmed) == 1, "the second observation sends"
    assert rearmed[0].trigger_price == Price.from_str("9.95"), "at the retained level"


def test_property_5_the_hold_never_touches_an_order_the_owner_placed(tmp_path: Path) -> None:
    strategy = armed(tmp_path)
    owner = working_sell("owner-order", qty="7", filled="0", instrument_id=BBB)
    strategy.cached.working.append(owner)
    owner_aaa = working_sell("owner-buy-later", qty="3", filled="0", instrument_id=AAA)
    owner_aaa.side = type(owner_aaa.side).BUY
    strategy.cached.working.append(owner_aaa)
    symbol_hold.write_hold(tmp_path, "AAA")
    symbol_hold.write_hold(tmp_path, "BBB")
    step(strategy, T0 + SECOND)
    symbol_hold.remove_hold(tmp_path, "AAA")
    symbol_hold.remove_hold(tmp_path, "BBB")
    step(strategy, T0 + 2 * SECOND)
    strategy.on_stop()
    sent = {order.client_order_id for order in strategy.sent}
    assert set(strategy.canceled) <= sent, "only IDs this instance sent are ever canceled"
    assert owner.client_order_id not in strategy.canceled
    assert owner_aaa.client_order_id not in strategy.canceled


def test_property_3_the_end_record_names_a_held_symbol(tmp_path: Path) -> None:
    strategy = armed(tmp_path)
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    strategy.on_stop()
    (aaa_end,) = [line for line in strategy.said if line.startswith(f"{AAA}: END ")]
    (bbb_end,) = [line for line in strategy.said if line.startswith(f"{BBB}: END ")]
    assert HELD in aaa_end and "owner holds=1" in aaa_end, aaa_end
    assert HELD not in bbb_end and "owner holds=0" in bbb_end, bbb_end


def test_property_3_and_4_the_book_names_the_hold_and_keeps_the_level_across_starts(
    tmp_path: Path,
) -> None:
    """Back to back on one book, as the runner starts them: held, held again, then released."""
    holds = tmp_path / "symbol-holds"
    symbol_hold.write_hold(holds, "AAA")
    book = tmp_path / "levels.json"
    book.write_text(json.dumps({"schema": 1, "entries": [restart_record()]}))
    for run in ("held-1", "held-2"):
        strategy = restart_harness(book, symbol_holds_dir=holds, run=run)
        strategy.on_start()
        step(strategy, T0 + 181 * SECOND)
        assert not strategy.sent, f"{run}: a held symbol is not armed at the start"
        strategy.on_stop()
        (row,) = json.loads(book.read_text())["entries"]
        assert row["state"] == "held", (run, row)
        assert HELD in row["reason"], (run, row)
        assert row["last_level"] == "1.10", f"{run}: the book level is never lowered or retired"
    symbol_hold.remove_hold(holds, "AAA")
    strategy = restart_harness(book, symbol_holds_dir=holds, run="released")
    strategy.on_start()
    assert strategy.slots[AAA].level == Price.from_str("1.10"), "released: armed at the book level"


def test_property_2_an_unread_folder_mid_run_keeps_the_last_holds_and_is_named(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "symbol-holds"
    strategy = armed(folder)
    symbol_hold.write_hold(folder, "AAA")
    step(strategy, T0 + SECOND)
    folder.rename(tmp_path / "moved")
    step(strategy, T0 + 2 * SECOND)
    step(strategy, T0 + 3 * SECOND)
    unread = [line for line in strategy.said if "symbol-holds" in line and "unread" in line]
    assert len(unread) >= 2, "named at every step"
    assert len(ours(strategy, AAA)) == 1, "the last holds read stand: AAA stays held"


def test_property_2_a_stray_entry_is_named_at_every_step(tmp_path: Path) -> None:
    strategy = armed(tmp_path)
    (tmp_path / ".hidden").write_text("")
    step(strategy, T0 + SECOND)
    step(strategy, T0 + 2 * SECOND)
    named = [line for line in strategy.said if "'.hidden'" in line and "hold nothing" in line]
    assert len(named) >= 2


def test_no_folder_supplied_is_said_at_the_start() -> None:
    strategy = Harness()
    strategy.on_start()
    assert any("no symbol-holds folder" in line for line in strategy.said), strategy.said


# Review round (/code-review high on the unit's first tree, 2026-11-14): each case fails before its fix.


def test_review_f4_the_nodes_start_read_stands_when_the_strategys_own_read_fails(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "symbol-holds"
    folder.write_text("not a folder")  # the strategy's own read fails at on_start
    strategy = two(folder, symbol_holds=frozenset({"AAA"}))
    strategy.on_start()
    for at in (T0 + SECOND // 2, T0 + SECOND, T0 + 2 * SECOND):
        tick(strategy, AAA, "10.00", at)
        tick(strategy, BBB, "20.00", at)
        step(strategy, at)
    assert ours(strategy, AAA) == [], "the node's start read is the hold that stands"
    assert len(ours(strategy, BBB)) == 1


def test_review_f5_a_counting_observation_does_not_survive_the_hold(tmp_path: Path) -> None:
    strategy = two(tmp_path)
    strategy.on_start()
    tick(strategy, AAA, "9.89", T0 + SECOND // 2)  # one observation through the level: guarded
    assert ours(strategy, AAA) == []
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, T0 + 2 * SECOND)
    tick(strategy, AAA, "9.88", T0 + 3 * SECOND)
    assert ours(strategy, AAA) == [], "one new observation after the hold cannot elect"
    tick(strategy, AAA, "9.87", T0 + 4 * SECOND)
    assert len(ours(strategy, AAA)) == 1, "the second new observation sends"


def test_review_f6_a_freeze_is_not_hidden_and_a_resting_sell_is_not_called_unarmed(
    tmp_path: Path,
) -> None:
    strategy = armed(tmp_path)
    aaa = ours(strategy, AAA)[0]
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    assert strategy._holding_state(AAA, strategy.slots[AAA])[0] != "held", (
        "our sell still rests, its cancel pending"
    )
    acknowledge_cancel(strategy, aaa.client_order_id, AAA, "10", "9.90")
    step(strategy, T0 + 2 * SECOND)
    assert strategy._holding_state(AAA, strategy.slots[AAA])[0] == "held"
    strategy.frozen[AAA] = "frozen: our protective sell ended REJECTED: venue said no"
    state, _, reason = strategy._holding_state(AAA, strategy.slots[AAA])
    assert state == "frozen" and reason is not None
    assert "REJECTED" in reason and HELD in reason, reason


def test_review_f9_the_release_line_says_what_the_protector_will_do(tmp_path: Path) -> None:
    strategy = two(tmp_path)
    strategy.on_start()
    tick(strategy, AAA, "9.89", T0 + SECOND // 2)  # guarded, no sell placed
    symbol_hold.write_hold(tmp_path, "AAA")
    step(strategy, T0 + SECOND)
    symbol_hold.remove_hold(tmp_path, "AAA")
    step(strategy, T0 + 2 * SECOND)
    (line,) = [text for text in strategy.said if "hold removed" in text]
    assert "placed at this step" not in line and "count rule" in line, line
    placed = armed(tmp_path / "other")
    symbol_hold.write_hold(tmp_path / "other", "AAA")
    step(placed, T0 + SECOND)
    symbol_hold.remove_hold(tmp_path / "other", "AAA")
    step(placed, T0 + 2 * SECOND)
    (line,) = [text for text in placed.said if "hold removed" in text]
    assert "placed at this step" in line, line


def test_review_f2_a_folder_never_made_is_none_held_and_said_once(tmp_path: Path) -> None:
    strategy = armed(tmp_path / "symbol-holds")
    step(strategy, T0 + SECOND)
    step(strategy, T0 + 2 * SECOND)
    assert not [line for line in strategy.said if "ALERT" in line and "symbol-holds" in line]
    assert any("none held" in line for line in strategy.said), strategy.said


def test_review_f3_and_f10_the_task_writes_only_into_an_installed_instance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    folder = symbol_hold.instance_holds("protect")
    assert symbol_hold.main(["protect", "ABC", "on"]) != 0, "no instance: nothing written"
    assert not folder.parent.exists(), "no folder is made where no runner reads"
    assert "no protector instance" in capsys.readouterr().err
    folder.parent.mkdir(parents=True)
    (folder.parent / "instance").write_text("account=protect\nrow=OWN1\n")
    assert symbol_hold.main(["protect", "ABC", "off"]) == 0, "off with no hold is not a failure"
    assert "held on protect: none" in capsys.readouterr().out
    assert symbol_hold.main(["protect", "ABC", "on"]) == 0
    assert (folder / "ABC").is_file()
    try:
        symbol_hold.main(["protect", "ABC", "on", "--folder", "relative/x"])
    except SystemExit:
        pass
    assert not Path("relative/x").exists(), "a relative folder is refused before any write"
