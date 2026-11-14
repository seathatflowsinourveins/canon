"""Unit 10's ROW, blind: the owner protector's trailing distance is the registered row's, exactly as
`paper:protect` plans it. No implementation of `canon_trading.owner_protector` was read: the module is
imported inside each case, so a missing seam fails each property separately, and an import failure is
the expected result at the base.

What this file reads and why:

* `src/canon_trading/protect.py`: `ROWS` (:172), `planner_config` (:181-185), `Distance` and its
  `source()` (:264-316), `row_distance` (:384-418) and `plan_distance` (:421-426). Every expected
  distance is computed by `protect.row_distance` on a `protect.Book` built from the same bid, ask and
  ATR the harness feeds, so this file never re-implements the task's arithmetic.
* `tests/test_mover.py`'s offline engine, instruments and tape. A seed strategy is registered before
  `OwnerProtector`; its simulated fill creates a real EXTERNAL long in the cache, which is the state
  the protector must find. This is the mechanism in
  `~/.claude/canon/deputy-eebefdab/reviews/handoff/u10-blind-h/REPORT-HARNESS.md` (read this session):
  every strategy is registered before the engine's first run because only the engine's own start gives
  a strategy a clock (`crates/backtest/src/engine.rs:832` at the pin), and the Python `Cache` has no
  public position insert.
* The row field of `OwnerProtectorConfig` is unknown. This file reads the public constructor signature
  and picks the field whose name says row (`row`, `trail_row`, `arm`, ...); it never guesses another
  config name and never reads or writes an implementation. All data is offline: no adapter, paper
  state, catalog, ledger, credential or network.

The task's distance source names are (`atr`, `floor`, `cold`), as `protect.Distance.source()` prints
them: `ATR 0.0475...` for a warm ATR, `from spread floor` when the floor binds, `ATR cold` otherwise.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from decimal import Decimal
from importlib import import_module
from typing import Any

import pytest
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import Bar
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from canon_trading import protect
from canon_trading.price_step import up_on_step
from tests.test_mover import AAA
from tests.test_mover import MINUTE
from tests.test_mover import T0
from tests.test_mover import engine
from tests.test_mover import tape
from tests.test_owner_protector import alpaca_stream_lock  # noqa: F401 -- offline autouse fixture

EXTERNAL = StrategyId("EXTERNAL")
POSITION = 10
BID = Decimal("9.9999")
ASK = Decimal("10.0000")
TICK = Decimal("0.0001")
BAR_CLOSES = [10.00, 10.05] * 10
"""Twenty one-minute bars whose true range is 0.05 after the first: a warm ATR of about 0.05, so
OWN1's trail is about 0.5% and OWN3's about 1.5%, both under the 0.02 cap."""

ROW_FIELDS = ("row", "trail_row", "arm", "trail_arm")

F2_REFUSED = pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason=(
        "the lead's ruling of 2026-11-13, 15:21: the owner protector refuses row F2 by name at start, "
        "since a holding's distance is per symbol; OWN1 and OWN3 carry P1 and P4's property"
    ),
)


class ExternalInventory(Strategy):
    """Buy the already-held long through the simulated exchange, so the cache takes a real fill."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.entered = False

    def on_start(self) -> None:
        self.subscribe_quotes(AAA)

    def on_quote(self, quote: QuoteTick) -> None:
        if self.entered:
            return
        self.entered = True
        self.submit_order(
            self.order_factory.limit(
                AAA,
                OrderSide.BUY,
                Quantity.from_int(POSITION),
                quote.ask_price,
                time_in_force=TimeInForce.GTC,
            )
        )


class RecordingLog:
    """Every level a strategy logs, in order, without reaching the trader's logger."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def record(self, message: str, *args: Any, **_kwargs: Any) -> None:
        self.lines.append(" ".join([str(message), *(str(arg) for arg in args)]))

    trace = record
    debug = record
    info = record
    warning = record
    error = record
    exception = record


class Replay:
    def __init__(self, eng: Any) -> None:
        self.engine = eng
        self.closed = False

    def close(self) -> None:
        """Stop through the engine, which owns the strategies' clocks and on_stop path."""
        if not self.closed:
            self.engine.end()
            self.closed = True

    def external_quantity(self) -> Decimal:
        return sum(
            (
                position.signed_qty
                for position in self.engine.cache.positions_open(
                    instrument_id=AAA, strategy_id=EXTERNAL
                )
            ),
            Decimal(0),
        )


def quote_tick(minute: int, bid: Decimal, ask: Decimal) -> QuoteTick:
    timestamp = T0 + minute * MINUTE
    return QuoteTick(
        AAA,
        Price.from_str(f"{bid:.4f}"),
        Price.from_str(f"{ask:.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(10_000),
        timestamp,
        timestamp,
    )


def bar_ticks() -> list[Bar]:
    """The one-minute bars the protector's ATR is warmed from, one a minute from the open."""
    return [event for event in tape(AAA, BAR_CLOSES) if isinstance(event, Bar)]


@contextmanager
def replay(strategy: Any, *, warm: bool = True, bid: Decimal = BID, ask: Decimal = ASK):
    """Start the seed and `strategy` in one offline engine, with an EXTERNAL long created by the
    seed's fill before the quote the protector acts on. `warm=False` feeds no one-minute bars, the
    task's cold-ATR input."""
    eng = engine(liquid=True)
    harness: Replay | None = None
    try:
        seed = ExternalInventory(StrategyConfig(strategy_id=EXTERNAL))
        eng.add_strategy(seed)
        eng.add_strategy(strategy)
        # A third quote: the first one received after the arming is guarded (the lead's
        # ruling, 2026-11-13, 15:55; L6's change to the blind file).
        data = [
            *(bar_ticks() if warm else []),
            *(quote_tick(minute, bid, ask) for minute in (21, 22, 23)),
        ]
        eng.add_data(data)
        eng.run(streaming=True)
        harness = Replay(eng)
        yield harness
    finally:
        try:
            if harness is not None:
                harness.close()
        finally:
            eng.dispose()


def owner_type() -> tuple[Any, Any]:
    """Import inside each case, so missing the named seam fails each property separately."""
    module = import_module("canon_trading.owner_protector")
    assert issubclass(module.OwnerProtector, Strategy)

    class Recording(module.OwnerProtector):
        # Answer the added history requests from this replay's existing cache; native
        # orders/emulation and the blind distance oracle are unchanged.
        from tests.test_owner_protector import Harness

        request_bars = Harness.request_bars
        request_quotes = Harness.request_quotes

        def __init__(self, config: Any) -> None:
            self.sent: list[Any] = []
            self.lines: list[str] = []
            self.recorded_log = RecordingLog(self.lines)
            super().__init__(config)

        @property
        def log(self) -> RecordingLog:  # ty: ignore[incompatible-property-override]
            return self.recorded_log

        def submit_order(self, order, position_id=None, client_id=None, params=None):
            self.sent.append(order)
            return super().submit_order(order, position_id, client_id, params)

    return module, Recording


def row_field(module: Any) -> str:
    """The one public `OwnerProtectorConfig` field whose name says which registered row the run
    plans on. Read from the signature, never from the implementation."""
    # The landing lane's one change: the config overrides `__new__(**kwargs)` to default NETTING, so
    # the class's own signature hides its fields; `__init__` names them.
    parameters = inspect.signature(module.OwnerProtectorConfig.__init__).parameters
    names = [name for name in parameters if name != "self"]
    for wanted in ROW_FIELDS:
        if wanted in names:
            return wanted
    found = [name for name in names if "row" in name.lower() or "arm" in name.lower()]
    assert len(found) == 1, (
        f"OwnerProtectorConfig must name its row in one field; its fields are {names}, and {found} "
        "look like the row"
    )
    return found[0]


def configured(module: Any, row: str) -> Any:
    return module.OwnerProtectorConfig(**{row_field(module): row})


def protective_sell(strategy: Any) -> Any:
    sells = [order for order in strategy.sent if order.side == OrderSide.SELL]
    assert sells, "the protector armed no protective sell"
    return sells[-1]


def distances(order: Any, bid: Decimal, ask: Decimal) -> list[Decimal]:
    """The distance the armed sell carries: its trailing offset where the seam gives one, else the
    gap from either price `paper:protect` names as its reference (the bid less the distance, or the
    reference price the distance is planned against, `up_on_step` of the ask). Both references are
    one tick apart in the narrow quote, so accepting either costs one tick and no more."""
    offset = getattr(order, "trailing_offset", None)
    if offset is not None:
        return [Decimal(str(offset))]
    trigger = getattr(order, "trigger_price", None)
    if trigger is None:
        trigger = getattr(order, "price", None)
    assert trigger is not None, f"the armed sell carries no price or trailing offset: {order}"
    level = Decimal(str(trigger))
    entry = up_on_step(ask)
    return [abs(bid - level), abs(entry - level)]


def matched(candidates: list[Decimal], expected: Decimal, what: str) -> Decimal:
    for candidate in candidates:
        if abs(candidate - expected) <= TICK:
            return candidate
    raise AssertionError(
        f"{what}: the armed distance is {candidates}, but protect.row_distance plans {expected}"
    )


def expected_distance(
    row: str, *, warm: bool = True, bid: Decimal = BID, ask: Decimal = ASK
) -> Decimal:
    """The distance protect plans for `row` on the same bid and ask and the window U-h warms its ATR
    on: the session day's last `protect.ATR_BARS` complete one-minute bars (L3's rule, as
    `Mover._warm_atr` feeds), through `protect.distance_for`, the one function the task and the node
    share. Every bar here is complete when the protector arms."""
    bars = bar_ticks()[-protect.ATR_BARS :] if warm else []
    distance, _ = protect.distance_for("AAA", row, bid, ask, bars)
    return distance


def armed(
    row: str, *, warm: bool = True, bid: Decimal = BID, ask: Decimal = ASK
) -> tuple[list[Decimal], list[str]]:
    module, recording = owner_type()
    strategy = recording(configured(module, row))
    with replay(strategy, warm=warm, bid=bid, ask=ask) as harness:
        assert harness.external_quantity() == POSITION, (
            "setup must create a real EXTERNAL long before the protector acts"
        )
        order = protective_sell(strategy)
        return distances(order, bid, ask), list(strategy.lines)


@pytest.mark.parametrize("row", [pytest.param("F2", marks=F2_REFUSED), "OWN1", "OWN3"])
def test_the_armed_distance_is_the_rows_distance(row: str) -> None:
    """P1: same bid, ask and ATR; the armed sell's distance equals `protect.row_distance` for the row."""
    expected = expected_distance(row)
    seen, _lines = armed(row)
    matched(seen, expected, f"{row}'s armed distance")


@pytest.mark.parametrize("row", ["OWN1", "OWN3"])
def test_cold_atr_uses_the_tasks_cold_distance(row: str) -> None:
    """P3: with no usable ATR the distance is `paper:protect`'s cold-path distance for that row, not a
    refusal and not a wait for the ATR to warm."""
    expected = expected_distance(row, warm=False)
    seen, _lines = armed(row, warm=False)
    matched(seen, expected, f"{row}'s cold-ATR distance")


def test_a_config_without_a_row_is_refused_by_name_before_any_order() -> None:
    """P2: no row means no start. The refusal names the row and no order is sent."""
    module, recording = owner_type()
    try:
        settings = module.OwnerProtectorConfig()
    except (TypeError, ValueError, RuntimeError) as exc:  # construction is before any order
        assert "row" in str(exc).lower(), f"the refusal must name the row: {exc}"
        return
    try:
        strategy = recording(settings)
    except (TypeError, ValueError, RuntimeError) as exc:
        assert "row" in str(exc).lower(), f"the refusal must name the row: {exc}"
        return
    try:
        with replay(strategy):
            pass
    except (TypeError, ValueError, RuntimeError) as exc:
        assert "row" in str(exc).lower(), f"the refusal must name the row: {exc}"
        assert not strategy.sent, "a config without a row must send no order"
        return
    assert not strategy.sent, "a config without a row must send no order"
    assert any("row" in line.lower() for line in strategy.lines), (
        "a config without a row must be refused by name; it logged nothing naming the row"
    )


@F2_REFUSED
def test_a_fixed_fraction_is_not_the_distance_across_rows() -> None:
    """P4: two rows with different planned distances carry different armed distances, in the same
    ratio the task plans them; one fixed fraction for every row fails here."""
    expected_f2 = expected_distance("F2")
    expected_own3 = expected_distance("OWN3")
    assert expected_own3 < expected_f2, "the test bars must make OWN3 tighter than F2"
    seen_f2, _lines = armed("F2")
    seen_own3, _lines = armed("OWN3")
    observed_f2 = matched(seen_f2, expected_f2, "F2's armed distance")
    observed_own3 = matched(seen_own3, expected_own3, "OWN3's armed distance")
    assert observed_f2 != observed_own3, "one fixed fraction cannot be every row's distance"
    expected_ratio = expected_own3 / expected_f2
    observed_ratio = observed_own3 / observed_f2
    assert abs(observed_ratio - expected_ratio) <= Decimal("0.002"), (
        f"the rows' distances differ as {observed_ratio}, not as the task plans {expected_ratio}"
    )


@pytest.mark.parametrize(
    ("row", "warm", "bid", "ask", "source"),
    [
        pytest.param("OWN1", True, BID, ASK, "atr", id="atr"),
        pytest.param("OWN1", True, Decimal("9.9100"), ASK, "floor", id="floor"),
        pytest.param("OWN1", False, BID, ASK, "cold", id="cold"),
    ],
)
def test_the_distance_is_logged_with_its_source(
    row: str, warm: bool, bid: Decimal, ask: Decimal, source: str
) -> None:
    """P5: the distance is logged with its source, as `protect.Distance.source()` names it: a warm
    ATR, the spread floor, or a cold ATR."""
    expected = expected_distance(row, warm=warm, bid=bid, ask=ask)
    seen, lines = armed(row, warm=warm, bid=bid, ask=ask)
    matched(seen, expected, f"the {source} distance")
    said = [line for line in lines if source in line.lower()]
    assert said, f"a {source} distance must be logged with its source; the log was {lines}"
    if source == "atr":
        assert all("cold" not in line.lower() for line in said), (
            f"a warm ATR must not be logged as cold: {said}"
        )
