"""The mover strategy replayed over a tape: stage one's shape, offline (ADR 0088).

`replay` and `stage_one` run in `scripts/ci` on a generated tape, here; `tests/test_paper_mover_day.py`
loads one recorded symbol-day and runs the same two on it.

**The replay** follows `tests/test_mover.py` and the upstream parts it names (NautilusTrader v2.0.0rc8,
`nautilus-trader@d53849b216e2`): a `BacktestEngine` with one simulated venue, NETTING and CASH, zero fees
(ADR 0067), and `bar_execution=False`, since with bars and quotes together the simulated venue also fills
a resting limit from the bar's earlier path (measured at the pin, `canon_trading.mover`'s header); fills
come from quotes. `canon_trading.mover.Mover` runs as shipped, with `Recording` keeping each release's
time as a plain event value (`canon_trading.mover`, "Order events"). After the run, the replay reads each
trigger from the cached order's `events()` (`nt:python/nautilus_trader/model/__init__.pyi`,
`OrderInitialized.trigger_price`, `OrderUpdated.trigger_price`, `LimitOrder.events`): the emulator
stores every update, but sends it to the risk engine's logging-only handler rather than the strategy
(`nt:crates/execution/src/order_emulator/emulator.rs:1676-1686,1823-1826`,
`crates/risk/src/engine/mod.rs:1991-1997`). Order creation time is the original `init_event.ts_init`,
which survives the emulator's release transformation (`nt:crates/execution/src/order_emulator/emulator.rs:1404,1480`,
`crates/model/src/orders/mod.rs:1649-1656`). The ranking is upstream's signal (`publish_signal`,
concepts/message_bus, "Signal publish/subscribe"), published once a minute by `MoveRanker` with
`format_rank`: the one name and its move from the prior day's close, `close / prior - 1`, from the latest
minute bar. That is the score's move term only; the score itself is gap 1 of ADR 0083, unbuilt.

**What is reported** for the day: every order, every fill, the strategy's `dropped` list and its refused
orders, and for each round trip its counterfactuals against the tape (`Exit`): the sale as managed, the
same shares held on to the replay's last close, and flat (no trade, zero). The strategy names the three
(`Mover.fills`); their arithmetic here is this file's, unregistered and UNCALIBRATED, for a reader.

**Stage one's shape** (`stage_one`), each a check of the day's orders and fills: every buy is a DAY
limit; every trailing sell's trigger only rises, across remakes until flat; each released sell leaves
the position flat within `FAST_EXIT_SECS`; every round trip that lasted beyond `FAST_EXIT_SECS` was
protected by a trailing sell; nothing is sold beyond what is held; flat at the end. A day with no entry
says so and is not a failure of the harness.

Registered rows of this file, UNCALIBRATED: `BALANCE`, `FAST_EXIT_SECS`; `MAX_NOTIONAL`, `MAX_MOVE` and
the one entry a run are a session window's (`canon_trading.mover_window`), and the strategy's other rows
are `MoverConfig`'s defaults (ADR 0088).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import TYPE_CHECKING
from typing import Any

import pytest
from canon_alpaca.data import NEW_YORK
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderInitialized
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderUpdated
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import StrategyId
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType
from nautilus_trader.model import Venue
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy

from canon_trading.mover import RANK_SIGNAL
from canon_trading.mover import Mover
from canon_trading.mover import MoverConfig
from canon_trading.mover import format_rank
from canon_trading.mover_window import ENTRIES
from canon_trading.mover_window import ENTRY_NOTIONAL_USD
from canon_trading.mover_window import NO_CHASE_CAP
from canon_trading.mover_window import OUTER_BOUND_USD

if TYPE_CHECKING:
    from nautilus_trader.common import TimeEvent

USD = Currency.from_str("USD")
MINUTE_NS = 60_000_000_000
SECOND_NS = 1_000_000_000
MOVER_ID = StrategyId("Mover-001")
RANKER_ID = StrategyId("Ranker-001")
MARKET_EXIT_TAG = "MARKET_EXIT"
"""The tag upstream's `market_exit` gives its closing orders (observed in `tests/test_mover.py`)."""

BALANCE = 100_000
"""The simulated account's cash."""
MAX_NOTIONAL = ENTRY_NOTIONAL_USD
"""The largest entry: a session window's row (`canon_trading.mover_window`), with the risk engine's
per-order cap at that window's outer bound (`OUTER_BOUND_USD`) and its entries, which have no bound (`ENTRIES`)."""
MAX_MOVE = NO_CHASE_CAP
"""The no-chase cap on the ranking's move: a session window's row, which does not bind."""
FAST_EXIT_SECS = 60
"""How soon after its release a sell must leave the position flat to count as a fast exit."""


# --- the replay ---------------------------------------------------------------------------------------


class MoveRankerConfig(StrategyConfig):
    def __init__(self, **_kwargs: object) -> None:
        super().__init__()


class MoveRanker(Strategy):
    """Publishes `RANK_SIGNAL` once a minute, as the ranking actor does (ADR 0107): one name and its move
    from `prior_close` at the latest minute bar's close, in `format_rank`'s string. Nothing before the
    first bar. Build it with `move_ranker`."""

    bar_type: BarType | None = None
    prior_close = Decimal(1)
    close: Decimal | None = None

    def on_start(self) -> None:
        assert self.bar_type is not None
        self.subscribe_bars(self.bar_type)
        self.clock.set_timer("move-ranker", timedelta(minutes=1), callback=self._publish)

    def on_bar(self, bar: Bar) -> None:
        self.close = bar.close.as_decimal()

    def _publish(self, event: TimeEvent) -> None:
        if self.close is None or self.bar_type is None:
            return
        move = (self.close / self.prior_close - 1).quantize(Decimal("0.0001"))
        value = format_rank([(self.bar_type.instrument_id, move)])
        self.publish_signal(RANK_SIGNAL, value, ts_event=event.ts_event)


def move_ranker(bar_type: BarType, prior_close: Decimal) -> MoveRanker:
    made = MoveRanker(MoveRankerConfig(strategy_id=RANKER_ID, order_id_tag="RNK"))
    made.bar_type, made.prior_close = bar_type, prior_close
    return made


class Recording(Mover):
    """`Mover` as shipped, keeping each release's time; the cache holds its trigger events."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.release_ns: dict[str, int] = {}

    def on_order_released(self, event: Any) -> None:
        super().on_order_released(event)
        self.release_ns[event.client_order_id.value] = event.ts_event


@dataclass(frozen=True)
class OrderLine:
    id: str
    side: str
    kind: str
    status: str
    tif: str
    price: Decimal | None
    qty: Decimal
    filled: Decimal
    ts_init: int
    """Original initialization time, before the emulator transforms a released order."""
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Exit:
    """One round trip from flat to flat, and its counterfactuals against the tape."""

    opened_ns: int
    flat_ns: int
    qty: Decimal
    bought_px: Decimal
    sold_px: Decimal
    how: str
    """`released` (the trailing sell's limit, or its chase), `wind-down` (the end) or `limit`."""
    release_to_flat_secs: float | None
    protected: bool
    held_on: Decimal
    """The replay's last close: what the shares would have been worth held on."""
    high_after: Decimal | None
    """The highest high of the bars opened after the sale, if any."""

    @property
    def managed(self) -> Decimal:
        return (self.sold_px - self.bought_px) * self.qty

    @property
    def held_on_pnl(self) -> Decimal:
        return (self.held_on - self.bought_px) * self.qty

    @property
    def flat_pnl(self) -> Decimal:
        return Decimal(0)


@dataclass
class Replay:
    orders: list[OrderLine]
    fills: list[tuple[str, str, str, str, int]]
    dropped: list[str]
    refused: list[tuple[str, str]]
    triggers: dict[str, list[Decimal]]
    exits: list[Exit]
    flat: bool
    open_orders: int
    oversold: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"orders ({len(self.orders)}):"]
        lines += [
            f"  {o.id} {o.side} {o.kind} {o.tif} {o.status} qty {o.qty} filled {o.filled} "
            f"price {o.price} at {stamp(o.ts_init)} {list(o.tags)}"
            for o in self.orders
        ]
        lines.append(f"fills ({len(self.fills)}):")
        lines += [f"  {i} {s} {q} @ {p} at {stamp(t)}" for i, s, q, p, t in self.fills]
        lines.append(f"dropped ({len(self.dropped)}):")
        lines += [f"  {why}" for why in self.dropped]
        if self.refused:
            lines.append(f"refused: {self.refused}")
        for order_id, triggers in self.triggers.items():
            lines.append(
                f"trigger of {order_id}: {triggers[0]} to {triggers[-1]} ({len(triggers)})"
            )
        for e in self.exits:
            lines.append(
                f"exit {stamp(e.opened_ns)} to {stamp(e.flat_ns)}: {e.qty} bought {e.bought_px} sold "
                f"{e.sold_px} by {e.how} (release to flat {e.release_to_flat_secs} s); managed "
                f"{e.managed}, held on to {e.held_on} {e.held_on_pnl}, flat {e.flat_pnl}; highest "
                f"after the sale {e.high_after}"
            )
        if not any(o.side == "BUY" for o in self.orders):
            lines.append("no entry on this day: the strategy sent no buy (its reasons are above)")
        lines.append(f"flat at the end: {self.flat}; open orders at the end: {self.open_orders}")
        return "\n".join(lines)


def stamp(ts: int) -> str:
    return f"{datetime.fromtimestamp(ts / 1e9, UTC).astimezone(NEW_YORK):%H:%M:%S}"


def replay(
    instrument: Equity,
    data: list,
    prior_close: Decimal,
    *,
    wind_down_at: datetime | None,
    **changes: object,
) -> Replay:
    """Run `Mover` over `data` (minute bars and quotes of `instrument`) with `MoveRanker` from
    `prior_close`, in a `BacktestEngine` that fills on quotes only, and report what it did."""
    venue = Venue(str(instrument.id.venue))
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            risk_engine=RiskEngineConfig(
                max_notional_per_order={str(instrument.id): OUTER_BOUND_USD}
            ),
        )
    )
    try:
        engine.add_venue(
            venue,
            OmsType.NETTING,
            AccountType.CASH,
            [Money(BALANCE, USD)],
            fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
            bar_execution=False,
        )
        engine.add_instrument(instrument)
        engine.add_data(data)
        settings: dict[str, Any] = {
            "max_notional": MAX_NOTIONAL,
            "max_move": MAX_MOVE,
            "max_shares": None,
            "entry_arm": "bridge",  # this order/protection replay has no prior-session Day context
            "entries": ENTRIES,
            "wind_down_at": wind_down_at,
            "strategy_id": MOVER_ID,
            "order_id_tag": "001",
            # the in-session wind-down as `tests/test_mover.py` runs it
            "market_exit_reduce_only": False,
            "market_exit_time_in_force": TimeInForce.DAY,
        }
        settings.update(changes)
        strategy = Recording(MoverConfig(**settings))
        bar_type = BarType.from_str(f"{instrument.id}-{strategy.spec_suffix}")
        # first, so it has subscribed before the ranking's first publish
        engine.add_strategy(strategy)
        engine.add_strategy(move_ranker(bar_type, prior_close))
        engine.run()
        cache = engine.cache
        orders = sorted(
            cache.orders(strategy_id=MOVER_ID),
            key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
        )
        lines = [
            OrderLine(
                id=o.client_order_id.value,
                side=o.side.name,
                kind=o.order_type.name,
                status=o.status.name,
                tif=o.time_in_force.name,
                price=None if getattr(o, "price", None) is None else o.price.as_decimal(),
                qty=o.quantity.as_decimal(),
                filled=o.filled_qty.as_decimal(),
                # The emulator gives the transformed limit a new ts_init; prepended events keep
                # the original (nt:emulator.rs:1404,1455-1480; orders/mod.rs:1649-1656).
                ts_init=o.init_event.ts_init,
                tags=tuple(o.tags or ()),
            )
            for o in orders
        ]
        # Antigravity 2: the cache retains sub-second updates and the seed of a stop that releases
        # during submit. Timer samples and on_order_updated do not cover these at this pin.
        triggers: dict[str, list[Decimal]] = {}
        for order in orders:
            values = [
                event.trigger_price.as_decimal()
                for event in order.events()
                if isinstance(event, (OrderInitialized, OrderUpdated))
                and event.trigger_price is not None
            ]
            if values:
                triggers[order.client_order_id.value] = values
        flat = not cache.positions_open(strategy_id=MOVER_ID)
        open_orders = sum(1 for o in orders if not o.is_closed)
    finally:
        engine.dispose()
    bars = [d for d in data if isinstance(d, Bar)]
    exits, oversold = round_trips(strategy, lines, bars)
    return Replay(
        orders=lines,
        fills=list(strategy.fills),
        dropped=list(strategy.dropped),
        refused=list(strategy.refused),
        triggers=triggers,
        exits=exits,
        flat=flat,
        open_orders=open_orders,
        oversold=oversold,
    )


def round_trips(
    strategy: Recording, orders: list[OrderLine], bars: list[Bar]
) -> tuple[list[Exit], list[str]]:
    """Each flat-to-flat run of the fills, and any sale beyond what was held."""
    by_id = {o.id: o for o in orders}
    last_close = bars[-1].close.as_decimal() if bars else Decimal(0)
    exits: list[Exit] = []
    oversold: list[str] = []
    held = Decimal(0)
    trip: list[tuple[str, str, Decimal, Decimal, int]] = []
    for order_id, side, qty_s, px_s, ts in sorted(strategy.fills, key=lambda f: f[4]):
        qty, px = Decimal(qty_s), Decimal(px_s)
        held += qty if side == "BUY" else -qty
        trip.append((order_id, side, qty, px, ts))
        if held < 0:
            oversold.append(f"{order_id} sold {qty} at {stamp(ts)} with {held + qty} held")
        if held <= 0:
            exits.append(close_trip(strategy, by_id, trip, last_close, bars))
            trip, held = [], Decimal(0)
    return exits, oversold


def close_trip(
    strategy: Recording,
    by_id: dict[str, OrderLine],
    trip: list[tuple[str, str, Decimal, Decimal, int]],
    last_close: Decimal,
    bars: list[Bar],
) -> Exit:
    buys = [(q, p) for _, s, q, p, _ in trip if s == "BUY"]
    sells = [(i, q, p) for i, s, q, p, _ in trip if s == "SELL"]
    qty = sum((q for q, _ in buys), Decimal(0))
    bought = sum((q * p for q, p in buys), Decimal(0)) / qty
    sold = sum((q * p for _, q, p in sells), Decimal(0)) / sum((q for _, q, _ in sells), Decimal(0))
    opened, flat = trip[0][4], trip[-1][4]
    released = [ns for i, ns in strategy.release_ns.items() if opened <= ns <= flat]
    if any(MARKET_EXIT_TAG in by_id[i].tags for i, _, _ in sells):
        how = "wind-down"
    elif released:
        how = "released"
    else:
        how = "limit"
    protected = any(
        o.kind == "TRAILING_STOP_LIMIT" or o.id in strategy.release_ns
        for o in by_id.values()
        if o.side == "SELL" and opened <= o.ts_init <= flat
    )
    after = [b.high.as_decimal() for b in bars if b.ts_event >= flat]  # opened after the sale
    return Exit(
        opened_ns=opened,
        flat_ns=flat,
        qty=qty,
        bought_px=bought,
        sold_px=sold,
        how=how,
        release_to_flat_secs=(flat - min(released)) / SECOND_NS if released else None,
        protected=protected,
        held_on=last_close,
        high_after=max(after) if after else None,
    )


def stage_one(result: Replay) -> list[str]:
    """What of stage one's shape the replay broke (the module header), each named; empty if none."""
    broken: list[str] = []
    for o in result.orders:
        if o.side == "BUY" and (o.kind, o.tif) != ("LIMIT", "DAY"):
            broken.append(f"{o.id}: a buy as {o.kind} {o.tif}, not a DAY limit")
    for order_id, triggers in result.triggers.items():
        fell = [(a, b) for a, b in pairwise(triggers) if b < a]
        if fell:
            broken.append(f"{order_id}: the trigger fell, {fell[0][0]} to {fell[0][1]}")
    # Muse's finding 6 at b28f12c: a remake has a new ID, but its trigger belongs to the same
    # holding. This replay has one instrument; a completed trip starts a new trigger history.
    last_by_trip: dict[int, tuple[str, Decimal]] = {}
    for order in sorted(result.orders, key=lambda o: o.ts_init):
        triggers = result.triggers.get(order.id, [])
        if not triggers:
            continue
        # A stop can be created, released and filled at the same timestamp: its own flat belongs
        # to this holding, not the next one (Antigravity 1).
        trip = sum(e.flat_ns < order.ts_init for e in result.exits)
        previous = last_by_trip.get(trip)
        if previous is not None and triggers[0] < previous[1]:
            broken.append(
                f"{order.id}: the trigger fell across the remake, {previous[1]} to {triggers[0]} "
                f"after {previous[0]}"
            )
        last_by_trip[trip] = (order.id, triggers[-1])
    for e in result.exits:
        when = stamp(e.opened_ns)
        secs = e.release_to_flat_secs
        if secs is not None and secs > FAST_EXIT_SECS:
            broken.append(f"the trip from {when}: flat {secs} s after the release, by {e.how}")
        held_for = (e.flat_ns - e.opened_ns) / SECOND_NS
        if not e.protected and held_for > FAST_EXIT_SECS:
            broken.append(f"the trip from {when}: held {held_for} s with no trailing sell")
    broken += [f"sold beyond what was held: {o}" for o in result.oversold]
    if not result.flat or result.open_orders:
        broken.append(f"not flat at the end: flat {result.flat}, {result.open_orders} orders open")
    return broken


# --- offline: the replay on a generated tape (runs in scripts/ci) ------------------------------------

AAA = InstrumentId.from_str("AAA.ALPACA")
T0 = 1_762_785_000_000_000_000  # 2025-11-10T14:30:00Z, 09:30 New York
RISING = [10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70]
FALL = [10.46, 10.00, 9.90, 9.80]
AFTER = [9.70] * 6


def generated_instrument() -> Equity:
    return Equity(
        instrument_id=AAA,
        raw_symbol=Symbol("AAA"),
        currency=USD,
        price_precision=4,
        price_increment=Price.from_str("0.0001"),
        ts_event=0,
        ts_init=0,
    )


def generated_tape(closes: list[float], *, spread: float = 0.01) -> list:
    """One bar a minute from `T0` with these closes, and the quote of each close a nanosecond before it,
    as `tests/test_mover.py` builds its tape."""
    bar_type = BarType.from_str(f"{AAA}-1-MINUTE-LAST-EXTERNAL")
    data: list = []
    for i, close in enumerate(closes):
        opened = closes[i - 1] if i else close
        ts_close = T0 + (i + 1) * MINUTE_NS
        data.append(
            Bar(
                bar_type,
                Price(opened, 4),
                Price(max(opened, close), 4),
                Price(min(opened, close), 4),
                Price(close, 4),
                Quantity.from_int(100_000),
                T0 + i * MINUTE_NS,
                ts_close,
            )
        )
        data.append(
            QuoteTick(
                AAA,
                Price(close - spread, 4),
                Price(close + spread, 4),
                Quantity.from_int(10_000),
                Quantity.from_int(10_000),
                ts_close - 1,
                ts_close - 1,
            )
        )
    return data


def test_the_replay_shows_the_stage_one_shape_on_a_generated_mover() -> None:
    """A first leg, a fall through the trigger and a lower tail, from a prior close of 8.00: one limit
    buy at the ask, a trigger that only rises, the released limit sold within seconds, flat at the end,
    and the exit's counterfactuals against the tape."""
    closes = RISING + FALL + AFTER
    wind_down = datetime.fromtimestamp((T0 + (len(closes) - 1) * MINUTE_NS) / 1e9, tz=UTC)
    result = replay(
        generated_instrument(), generated_tape(closes), Decimal("8.00"), wind_down_at=wind_down
    )
    print(result.report())
    assert stage_one(result) == []
    assert [(o.side, o.kind, o.status) for o in result.orders] == [
        ("BUY", "LIMIT", "FILLED"),
        ("SELL", "LIMIT", "FILLED"),
    ]
    assert result.orders[0].price == Decimal("10.41")
    (exit_,) = result.exits
    assert exit_.how == "released" and exit_.protected
    assert exit_.release_to_flat_secs is not None and exit_.release_to_flat_secs <= 2
    assert exit_.held_on == Decimal("9.70")
    assert exit_.managed == (exit_.sold_px - exit_.bought_px) * exit_.qty
    assert exit_.held_on_pnl == (Decimal("9.70") - exit_.bought_px) * exit_.qty
    assert exit_.flat_pnl == 0
    assert result.flat


def test_a_day_with_no_rising_run_gives_no_entry_and_says_so() -> None:
    closes = [10.00, 9.90, 9.95, 9.85, 9.90, 9.80]
    result = replay(
        generated_instrument(), generated_tape(closes), Decimal("8.00"), wind_down_at=None
    )
    assert result.orders == []
    assert stage_one(result) == []
    assert "no entry on this day" in result.report()


@pytest.mark.parametrize(
    ("remade_trigger", "flat_between", "fell", "flat_second"),
    [
        ("10.30", False, True, 10),
        ("10.40", False, False, 10),
        ("10.45", False, False, 10),
        ("10.30", True, False, 10),
        ("10.30", False, True, 4),  # created, released and filled at the same timestamp
    ],
)
def test_stage_one_checks_a_remade_sells_trigger_until_the_position_is_flat(
    remade_trigger: str, flat_between: bool, fell: bool, flat_second: int
) -> None:
    """Muse's finding 6 at b28f12c: each order's trigger only rises, but the remake can start
    below its predecessor's last trigger. A new entry after flat starts a separate history."""
    orders = [
        OrderLine(
            id=order_id,
            side="SELL",
            kind="TRAILING_STOP_LIMIT",
            status="CANCELED",
            tif="DAY",
            price=None,
            qty=Decimal(1),
            filled=Decimal(0),
            ts_init=T0 + second * SECOND_NS,
            tags=(),
        )
        for order_id, second in (("S-2", 4), ("S-1", 1))
    ]
    trips = ((0, 3), (4, flat_second)) if flat_between else ((0, flat_second),)
    result = Replay(
        orders=orders,
        fills=[],
        dropped=[],
        refused=[],
        # The order list and this mapping are deliberately not chronological.
        triggers={
            "S-2": [Decimal(remade_trigger), Decimal("10.50")],
            "S-1": [Decimal("10.35"), Decimal("10.40")],
        },
        exits=[
            Exit(
                opened_ns=T0 + opened * SECOND_NS,
                flat_ns=T0 + flat * SECOND_NS,
                qty=Decimal(1),
                bought_px=Decimal("10.00"),
                sold_px=Decimal("10.50"),
                how="released",
                release_to_flat_secs=1.0,
                protected=True,
                held_on=Decimal("10.50"),
                high_after=None,
            )
            for opened, flat in trips
        ],
        flat=True,
        open_orders=0,
    )
    broken = stage_one(result)
    if fell:
        assert len(broken) == 1, broken
        assert "the trigger fell" in broken[0]
        assert all(value in broken[0] for value in ("S-1", "S-2", "10.40", "10.30"))
    else:
        assert broken == []


def test_a_released_sell_keeps_its_original_creation_time_in_the_replay() -> None:
    """Antigravity 1: the emulator replaces the cached order at release; its init event keeps
    creation time even when the released limit fills on that same quote."""
    result = replay(
        generated_instrument(), generated_tape(RISING + FALL + AFTER), Decimal(8), wind_down_at=None
    )
    (exit_,) = result.exits
    sell = result.orders[-1]
    assert sell.kind == "LIMIT" and sell.status == "FILLED"
    assert exit_.release_to_flat_secs == 0
    assert sell.ts_init < exit_.flat_ns


def test_a_released_sell_does_not_put_its_trigger_in_the_next_holding() -> None:
    """Antigravity 1: the first sale releases and fills at once; the next holding starts lower
    than its trigger, and ends by wind-down, with no fall inside either holding."""
    closes = [10.0, 10.1, 10.2, 10.3, 10.4, 11.0, 11.8, 12.0, 11.76, 11.4]
    closes += [11.45, 11.5, 11.6, 11.7, 11.8, 11.9, 12.0, 12.1]
    wind_down = datetime.fromtimestamp((T0 + (len(closes) - 1) * MINUTE_NS) / 1e9, tz=UTC)
    result = replay(
        generated_instrument(),
        generated_tape(closes),
        Decimal(8),
        wind_down_at=wind_down,
        entries=2,
        cooldown_secs=60,
    )
    assert len(result.exits) == 2, result.report()
    assert result.exits[0].release_to_flat_secs == 0
    assert result.exits[1].how == "wind-down"
    assert stage_one(result) == [], result.report()


@pytest.fixture
def liquid_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real engine with the venue's documented liquidity consumption enabled for partial fills
    (`nt:python/nautilus_trader/backtest/__init__.pyi`, `BacktestEngine.add_venue`)."""
    engine_type = BacktestEngine

    class LiquidEngine:
        def __init__(self, config: BacktestEngineConfig) -> None:
            self.engine = engine_type(config)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.engine, name)

        def add_venue(self, *args: Any, **kwargs: Any) -> None:
            self.engine.add_venue(*args, **kwargs, liquidity_consumption=True)

    monkeypatch.setitem(replay.__globals__, "BacktestEngine", LiquidEngine)


def generated_quote(seconds: float, bid: str, ask: str, *, ask_size: int = 10_000) -> QuoteTick:
    ns = T0 + int(seconds * SECOND_NS)
    return QuoteTick(
        AAA,
        Price.from_str(f"{Decimal(bid):.4f}"),
        Price.from_str(f"{Decimal(ask):.4f}"),
        Quantity.from_int(10_000),
        Quantity.from_int(ask_size),
        ns,
        ns,
    )


@pytest.mark.parametrize(
    ("peak_at", "refill_at", "bad_seed"),
    [(302.0, 304.0, None), (302.2, 302.4, Decimal("10.25"))],
    ids=["sampled-peak", "between-steps-peak"],
)
def test_the_replay_names_a_lower_remake_that_releases_and_fills_at_once(
    liquid_replay: None,
    monkeypatch: pytest.MonkeyPatch,
    peak_at: float,
    refill_at: float,
    bad_seed: Decimal | None,
) -> None:
    """Antigravity 1/2: real partial entry fills force a remake. Inject a lost or lowered kept
    trigger; the replay must catch it even if the last peak happened between timer steps."""
    trail = Recording._trail

    def lower_seed(self: Recording, slot: Any, held: Decimal, now: int) -> None:
        if slot.trail_trigger is not None:
            slot.trail_trigger = bad_seed
        trail(self, slot, held, now)

    monkeypatch.setattr(Recording, "_trail", lower_seed)
    data = generated_tape(RISING[:5])
    data[-1] = generated_quote(300 - 1e-9, "10.39", "10.41", ask_size=1)
    data += [
        generated_quote(peak_at, "10.59", "10.61"),
        generated_quote(refill_at, "10.39", "10.41", ask_size=2),
        generated_quote(306, "10.39", "10.60"),
        generated_quote(308, "10.17" if bad_seed is None else "10.24", "10.60"),
        generated_quote(310, "10.00", "10.60"),
    ]
    result = replay(
        generated_instrument(), data, Decimal(8), wind_down_at=None, max_notional=Decimal("31.5")
    )
    sells = [o for o in result.orders if o.side == "SELL"]
    assert len(sells) == 2, result.report()
    first, remade = sells
    assert (first.qty, remade.qty) == (Decimal(1), Decimal(3)), result.report()
    assert remade.kind == "LIMIT" and remade.status == "FILLED"
    assert result.exits[0].release_to_flat_secs == 0
    assert result.triggers[first.id][-1] == Decimal("10.38"), result.report()
    broken = stage_one(result)
    assert len(broken) == 1 and "the trigger fell" in broken[0], result.report()
    assert first.id in broken[0] and remade.id in broken[0]


def test_the_replay_records_a_seeded_trigger_released_during_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Antigravity 2: an emulator stop that matches during submit is never seen as emulated by
    the next timer sample; its initialization event still carries its seed."""

    def seeded_trail(self: Recording, slot: Any, held: Decimal, now: int) -> None:
        order = self.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            Quantity.from_int(int(held)),
            None,
            limit_offset=Decimal("0.27"),
            trailing_offset=Decimal("0.21"),
            trigger_price=Price.from_str("10.40"),
            trigger_type=TriggerType.BID_ASK,
            time_in_force=TimeInForce.DAY,
            emulation_trigger=TriggerType.BID_ASK,
        )
        slot.protect_id = order.client_order_id
        self.submit_order(order)

    monkeypatch.setattr(Recording, "_trail", seeded_trail)
    data = generated_tape(RISING[:5]) + [generated_quote(303, "10.39", "10.60")]
    result = replay(
        generated_instrument(), data, Decimal(8), wind_down_at=None, max_notional=Decimal("31.5")
    )
    sell = result.orders[-1]
    assert sell.side == "SELL" and sell.kind == "LIMIT" and sell.status == "FILLED"
    assert result.exits[0].release_to_flat_secs == 0
    assert Decimal("10.40") in result.triggers.get(sell.id, []), result.report()


def test_stage_one_names_a_falling_trigger_a_slow_exit_and_an_open_position() -> None:
    result = Replay(
        orders=[],
        fills=[],
        dropped=[],
        refused=[],
        triggers={"O-2": [Decimal("10.40"), Decimal("10.30")]},
        exits=[
            Exit(
                opened_ns=T0,
                flat_ns=T0 + 600 * SECOND_NS,
                qty=Decimal(10),
                bought_px=Decimal(10),
                sold_px=Decimal(9),
                how="wind-down",
                release_to_flat_secs=300.0,
                protected=False,
                held_on=Decimal(9),
                high_after=None,
            )
        ],
        flat=False,
        open_orders=1,
    )
    broken = stage_one(result)
    assert len(broken) == 4, broken
    assert "the trigger fell" in broken[0]
    assert "300.0 s after the release" in broken[1]
    assert "no trailing sell" in broken[2]
    assert "not flat" in broken[3]
