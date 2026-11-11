"""The mover strategy: ranked names in slots, a limit entry on the first rising run, a trailing sell that only rises.

A composition of upstream parts (ADR 0083, ADR 0088). What each part is, at NautilusTrader v2.0.0rc8
(`nautilus-trader@d53849b216e2`), read 2026-11-11:

* **The ranking** reaches the strategy as upstream's signal: `subscribe_signal(name)` and `on_signal`
  (`python/nautilus_trader/trading/__init__.pyi:365`, `:388`; concepts/message_bus, "Signal
  publish/subscribe": "The value is converted to a string on publish"). The ranking actor publishes
  `RANK_SIGNAL` each minute (ADR 0087); its value is `format_rank`'s string, the ranked instrument IDs best
  first, each with its move from the last regular close, and its `ts_event` the time the ranking was
  computed. Nothing here ranks, and no vendor's list is read.
* **The entry** is `OrderFactory.limit` (`python/nautilus_trader/common/__init__.pyi:1388`), time in force
  DAY, at the ask of the latest quote in the cache (`entry_ticks` through it, none by default), in every
  session; it is never a market order and is not re-priced: it rests until `entry_timeout_secs`.
* **The size** is `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, hard_limit=...)`
  (`python/nautilus_trader/risk/__init__.pyi:16-29`; `crates/risk/src/sizing.rs:39-113`): `risk` of the
  account's total balance over the distance from the entry to the lowest price the first sale is planned
  at (`planned_stop`: the first trigger, less the released limit's give, less the chase's cap), in whole
  shares, capped by `hard_limit`: the notional cap, the free balance and `flow_share` of the dollar
  volume of the last `flow_bars` bars.
* **The protection** is the engine's order emulator holding a `TRAILING_STOP_LIMIT` sell
  (`OrderFactory.trailing_stop_limit`, `common/__init__.pyi:1523-1541`; concepts/orders/emulated and
  trailing_stop_limit): its trigger follows the bid (`BID_ASK`) or the last trade (`LAST_PRICE`) up and
  never down, and at the trigger the emulator releases a `LIMIT` through the risk engine. A release
  keeps the submit command's params (`crates/execution/src/order_emulator/emulator.rs:1368`, and its own
  test at `:4577`), so the `extended_hours` mark is given once, on the submit.
* **The chase** of a released limit that rests is `Strategy.modify_order(client_order_id, price=...)`
  (concepts/strategies, "Modifying orders"), or `cancel_order` and a new limit (`chase="resend"`).
* **The end** in the session is upstream's `market_exit()` (concepts/strategies, "Market exit"), as
  `session_ma_cross` uses it; outside the session a market close would only queue, so it is a limit.
* **Timers** are the clock's `set_timer` and `set_time_alert` with a callback (concepts/strategies,
  "Timers").

Alpaca's rules it works within (https://docs.alpaca.markets/us/docs/orders-at-alpaca.md, read
2026-11-11): outside the regular session "Only limit orders with `time_in_force` set to `day` or `gtc`
orders are accepted as extended hours eligible", so every order there is a DAY limit submitted with
`params={"extended_hours": True}` (ADR 0038); "Limit price >=$1.00: Max Decimals= 2", "Limit price <$1.00:
Max Decimals = 4" (`tick`).

What is written here, since no upstream ships it (ADR 0083, gap 3): the rule that reads a rising run from
bars, the slots, the re-entry and rotation rules, the chase's price and its cap, and the yield of entries
to the request budget. Every number of them is a registered row, UNCALIBRATED (ADR 0088).

**How it runs.** `on_signal` keeps the latest ranking. Once a second (`STEP_SECS`) a timer steps:

1. *Watch.* The top `watch` names of a fresh ranking whose instruments are in the cache get a slot:
   quotes and trades are subscribed first, on `feed` (ADR 0055), then the bars (`bar_spec`). A name that
   has left the ranking loses its slot once it holds nothing and nothing of it works.
2. *Each slot.* An entry not filled within `entry_timeout_secs` is canceled. What is held is protected:
   the emulated trailing stop-limit is submitted for the held quantity once a quote (or a trade, for
   `LAST_PRICE`) is in the cache, and its quantity follows the position. After the release
   (`on_order_released`), a limit that still rests after `chase_secs` is re-priced `exit_ticks` through
   the bid, never below `chase_cap` under its first price; below that it rests.
3. *Entries.* A slot whose bars have just closed `run_bars` times in a row above the close before is a
   candidate. Candidates are taken best-ranked first while a slot is free, each refused by name
   (`dropped`) when the ranking is older than `rank_max_age_secs`, the name is no longer in it, its move
   is over `max_move` (the no-chase cap), no quote is in the cache, the size comes to nothing, or the
   requests of the last 60 seconds have reached `soft_line`. A name sold before is bought back only
   after `cooldown_secs` and on a run whose last close is above the sale's price (a reclaim).

Exits never wait for the ranking or the budget. The strategy is long only: a sell is sized from what is
held, one sell works at a time for a name, and nothing is sold while flat.

**Order events.** At the pin, an event that the strategy's own `submit_order`, `cancel_order` or
`modify_order` publishes reaches its handler while the strategy object is mutably borrowed
(`crates/trading/src/python/strategy.rs:2016-2037`), and reading `cache`, `portfolio`, `clock` or `log`
there raises "Already mutably borrowed"; a signal can reenter an actor too (concepts/message_bus,
"Signal callback reentry"). So every handler of an order event or a signal only stores what the event
carries, in plain attributes, and all reading of the engine and every command happens in the timer's
step and in `on_bar`.

Measured at the pin, offline, 2026-11-11 (ADR 0088): on bars alone the emulator cannot activate a
trailing stop ("no market price available"); it needs a quote for `BID_ASK` or a trade for `LAST_PRICE`.
With a limit offset smaller than the trailing offset the released sell limit sits above a market that
has fallen through the trigger and does not fill; so the limit offset here is the trailing offset plus
`exit_through`. An emulated order is not `is_open`; `modify_order` and `cancel_order` take the client
order ID.

Not built, and said so: a re-priced entry; the gain lock; a breach that must persist before the sale (the emulator
releases on the first quote at the trigger); a broker-held stop as an outage backstop (a second sell for
the same shares is refused while the first works); slots reserved for the newest movers; no order into
a halt (ADR 0083, gap 4). Known limits: the strategy does not know the session calendar (the run checks
it); a partly filled entry is protected only from the next step.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from decimal import InvalidOperation
from itertools import pairwise
from types import NoneType
from typing import TYPE_CHECKING
from typing import Any

from canon_alpaca.providers import TRADING_API
from canon_alpaca.providers import request_counts
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import BarType
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TrailingOffsetType
from nautilus_trader.model import TriggerType
from nautilus_trader.risk import FixedRiskSizer
from nautilus_trader.trading import Strategy

from .ma_cross import refuse_another_type
from .ma_cross import refuse_unknown_keywords

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nautilus_trader.common import Signal
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar

RANK_SIGNAL = "movers-rank"
"""The signal the ranking actor publishes (ADR 0087): `format_rank`'s string, `ts_event` when computed."""
RANK_MOST = 20
"""The most names a ranking carries."""
EXTENDED_HOURS = {"extended_hours": True}
"""The submit params that mark an order as extended-hours eligible for the adapter (ADR 0038)."""
STEP_SECS = 1
"""How often the strategy steps: reads the cache and acts."""
STEP_TIMER = "mover-step"
WIND_DOWN_ALERT = "mover-wind-down"
CENT = Decimal("0.01")
SUB_DOLLAR = Decimal("0.0001")
SECOND_NS = 1_000_000_000
BUDGET_SPAN_NS = 60 * SECOND_NS
CHASES = ("modify", "resend")
TRIGGERS = (TriggerType.BID_ASK, TriggerType.LAST_PRICE)
"""The triggers the local emulator accepts for an order it holds (concepts/orders/emulated)."""


def format_rank(entries: Sequence[tuple[InstrumentId, Decimal]]) -> str:
    """The ranking as the signal's one string: `ID=move` joined by `;`, best first."""
    return ";".join(f"{instrument_id}={move}" for instrument_id, move in entries)


def parse_rank(value: str) -> list[tuple[InstrumentId, Decimal]]:
    """The ranking a `RANK_SIGNAL` value carries, best first; `ValueError` naming what is wrong with it.

    An empty value is an empty ranking. A name twice, a move that is no number, more than `RANK_MOST`
    names or an ID without a venue is refused whole: a ranking read in part would rank other names.
    """
    if not value:
        return []
    parts = value.split(";")
    if len(parts) > RANK_MOST:
        raise ValueError(
            f"{RANK_SIGNAL}: {len(parts)} names, where a ranking carries at most {RANK_MOST}"
        )
    ranked: list[tuple[InstrumentId, Decimal]] = []
    for part in parts:
        name, eq, move = part.partition("=")
        symbol, dot, venue = name.rpartition(".")
        if not (eq and symbol and dot and venue):
            raise ValueError(f"{RANK_SIGNAL}: {part!r} is not <symbol>.<venue>=<move>")
        try:
            moved = Decimal(move)
        except InvalidOperation:
            raise ValueError(f"{RANK_SIGNAL}: the move in {part!r} is not a number") from None
        if not moved.is_finite():
            raise ValueError(f"{RANK_SIGNAL}: the move in {part!r} is not finite")
        instrument_id = InstrumentId.from_str(name)
        if any(instrument_id == seen for seen, _ in ranked):
            raise ValueError(f"{RANK_SIGNAL}: {name} is ranked twice")
        ranked.append((instrument_id, moved))
    return ranked


def tick(price: Decimal) -> Decimal:
    """Alpaca's smallest limit-price step at `price`: a cent at or above $1.00, a hundredth of one below."""
    return CENT if price >= 1 else SUB_DOLLAR


def entry_price(ask: Decimal, ticks: int) -> Decimal:
    """A buy limit `ticks` through the ask, on Alpaca's step for that price, rounded up to it."""
    step = tick(ask)
    return (ask + ticks * step).quantize(step, rounding=ROUND_CEILING)


def exit_price(bid: Decimal, ticks: int, floor: Decimal | None) -> Decimal:
    """A sell limit `ticks` through the bid, on Alpaca's step, rounded down to it; never below `floor`."""
    step = tick(bid)
    price = (bid - ticks * step).quantize(step, rounding=ROUND_FLOOR)
    price = max(price, step)
    return price if floor is None else max(price, floor)


def offsets(entry: Decimal, trail: Decimal, exit_through: Decimal) -> tuple[Decimal, Decimal]:
    """The trailing offset and the limit offset, as prices on the entry's step: `trail` of the entry up
    to a step, and that plus `exit_through` of the entry up to a step (a step at least), so the
    released limit is below the trigger and marketable when the trigger is met."""
    step = tick(entry)
    trailing = max((entry * trail).quantize(step, rounding=ROUND_CEILING), step)
    through = max((entry * exit_through).quantize(step, rounding=ROUND_CEILING), step)
    return trailing, trailing + through


def planned_stop(
    entry: Decimal, trail: Decimal, exit_through: Decimal, chase_cap: Decimal
) -> Decimal:
    """The lowest price the first sale is planned at, which the size is risked against: the first
    trigger less what the released limit may give (`offsets`), less the chase's cap under that."""
    step = tick(entry)
    _, limit = offsets(entry, trail, exit_through)
    return ((entry - limit) * (1 - chase_cap)).quantize(step, rounding=ROUND_FLOOR)


def requests_sent() -> int:
    """Every trading-API request this process has sent so far, by the adapter's own count."""
    return sum(count.total for count in request_counts() if count.api == TRADING_API)


OWN_FIELDS: dict[str, type | tuple[type, ...]] = {
    "max_notional": Decimal,
    "max_move": Decimal,
    "bar_spec": str,
    "slots": int,
    "watch": int,
    "risk": Decimal,
    "run_bars": int,
    "trail": Decimal,
    "exit_through": Decimal,
    "chase_cap": Decimal,
    "chase_secs": int,
    "chase": str,
    "flow_share": Decimal,
    "flow_bars": int,
    "entry_ticks": int,
    "exit_ticks": int,
    "entry_timeout_secs": int,
    "cooldown_secs": int,
    "rank_max_age_secs": int,
    "soft_line": int,
    "protect": bool,
    "trigger": TriggerType,
    "extended_hours": bool,
    "wind_down_at": (datetime, NoneType),
    "feed": (str, NoneType),
}
"""`MoverConfig`'s own fields and the type each takes."""


class MoverConfig(StrategyConfig):
    """Parameters of `Mover`. Every number is a registered row, UNCALIBRATED (ADR 0088).

    `max_notional` (dollars an order, the node's cap for every instrument it may trade) and `max_move`
    (the no-chase cap on the ranking's move, a fraction) have no default: the run states them. `bar_spec`
    is the bars' specification and source after the instrument ID, `1-MINUTE-LAST-EXTERNAL` in a replay
    and `1-MINUTE-LAST-INTERNAL` on the adapter, which serves quotes and trades and no bars. `slots` is
    how many names are held at once and `watch` how many of the ranking's top are followed. `risk` is
    `FixedRiskSizer`'s fraction of the balance. `trail`, `exit_through` and `chase_cap` are fractions of
    a price; `flow_share` of the last `flow_bars` bars' dollar volume caps the size. `protect=False` is
    the hold-to-close row: no trailing sell, sold at the wind-down. `wind_down_at` is an aware
    `datetime`. Any other keyword must be a base `StrategyConfig` field, and each field takes one type.
    """

    def __init__(
        self,
        *,
        max_notional: Decimal,
        max_move: Decimal,
        bar_spec: str = "1-MINUTE-LAST-EXTERNAL",
        slots: int = 1,
        watch: int = 3,
        risk: Decimal = Decimal("0.01"),
        run_bars: int = 3,
        trail: Decimal = Decimal("0.02"),
        exit_through: Decimal = Decimal("0.005"),
        chase_cap: Decimal = Decimal("0.02"),
        chase_secs: int = 5,
        chase: str = "modify",
        flow_share: Decimal = Decimal("0.05"),
        flow_bars: int = 5,
        entry_ticks: int = 0,
        exit_ticks: int = 1,
        entry_timeout_secs: int = 30,
        cooldown_secs: int = 300,
        rank_max_age_secs: int = 90,
        soft_line: int = 140,
        protect: bool = True,
        trigger: TriggerType = TriggerType.BID_ASK,
        extended_hours: bool = False,
        wind_down_at: datetime | None = None,
        feed: str | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        refuse_unknown_keywords(self, _kwargs)
        fields = {name: value for name, value in locals().items() if name in OWN_FIELDS}
        refuse_another_type(self, fields, OWN_FIELDS)
        for name in ("max_notional", "max_move", "trail", "flow_share"):
            if not fields[name].is_finite() or fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        for name in ("exit_through", "chase_cap"):
            if not fields[name].is_finite() or not 0 <= fields[name] < 1:
                raise ValueError(f"{name} must be a fraction from 0 to under 1, got {fields[name]}")
        if not risk.is_finite() or not 0 < risk < 1:
            raise ValueError(f"risk must be a fraction over 0 and under 1, got {risk}")
        if trail >= 1:
            raise ValueError(f"trail must be a fraction under 1, got {trail}")
        for name in ("slots", "run_bars", "flow_bars", "chase_secs", "entry_timeout_secs",
                     "rank_max_age_secs", "soft_line"):  # fmt: skip
            if fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
        for name in ("entry_ticks", "exit_ticks", "cooldown_secs"):
            if fields[name] < 0:
                raise ValueError(f"{name} must not be negative, got {fields[name]}")
        if not slots <= watch <= RANK_MOST:
            raise ValueError(f"watch must be from slots ({slots}) to {RANK_MOST}, got {watch}")
        if chase not in CHASES:
            raise ValueError(f"chase must be one of {CHASES}, got {chase!r}")
        if trigger not in TRIGGERS:
            raise ValueError(
                f"trigger must be BID_ASK or LAST_PRICE, which the emulator accepts, got {trigger}"
            )
        if wind_down_at is not None and wind_down_at.tzinfo is None:
            raise ValueError("wind_down_at must be an aware datetime")
        try:
            BarType.from_str(f"X.X-{bar_spec}")
        except ValueError as e:
            raise ValueError(
                f"bar_spec {bar_spec!r} is not <step>-<aggregation>-<price>-<source>: {e}"
            ) from e
        self.max_notional = max_notional
        self.max_move = max_move
        self.bar_spec = bar_spec
        self.slots = slots
        self.watch = watch
        self.risk = risk
        self.run_bars = run_bars
        self.trail = trail
        self.exit_through = exit_through
        self.chase_cap = chase_cap
        self.chase_secs = chase_secs
        self.chase = chase
        self.flow_share = flow_share
        self.flow_bars = flow_bars
        self.entry_ticks = entry_ticks
        self.exit_ticks = exit_ticks
        self.entry_timeout_secs = entry_timeout_secs
        self.cooldown_secs = cooldown_secs
        self.rank_max_age_secs = rank_max_age_secs
        self.soft_line = soft_line
        self.protect = protect
        self.trigger = trigger
        self.extended_hours = extended_hours
        self.wind_down_at = wind_down_at
        self.feed = feed


@dataclass
class Slot:
    """What the strategy keeps for one watched name. Plain values: the cache holds the orders."""

    instrument_id: InstrumentId
    bar_type: BarType
    closes: deque[Decimal]
    dollars: deque[Decimal]
    run_ns: int | None = None
    """When the last rising run completed (the bar's `ts_event`), until an entry has been decided on."""
    run_close: Decimal | None = None
    entry_id: ClientOrderId | None = None
    entry_cancel_ns: int = 0
    protect_id: ClientOrderId | None = None
    """The one sell of this name: the trailing stop-limit, then the limit it became, or a plain limit."""
    exiting: bool = False
    """The trigger has been met, or the wind-down has begun: from here every sell is a plain limit."""
    floor: Decimal | None = None
    priced_ns: int = 0
    was_held: bool = False
    sold_ns: int | None = None
    sold_px: Decimal | None = None
    notes: list[str] = field(default_factory=list)


class Mover(Strategy):
    """Hold up to `slots` of the ranked names, each bought on its first rising run and sold by a trailing
    limit, as the module header describes. It makes no claim to an edge: the gate measures that."""

    def __init__(self, config: MoverConfig) -> None:
        super().__init__(config)
        self.settings = config
        self.spec_suffix = config.bar_spec
        self.submit_params = dict(EXTENDED_HOURS) if config.extended_hours else None
        self.feed_params = {"feed": config.feed} if config.feed else None
        self.wind_down_at = config.wind_down_at
        self.manage_stop_set = config.manage_stop
        self.slots: dict[InstrumentId, Slot] = {}
        self.rank: list[tuple[InstrumentId, Decimal]] = []
        """The latest ranking, best first; empty until one arrives and after a malformed one."""
        self.rank_ns = 0
        self.rank_faults = 0
        self.released: set[str] = set()
        """Client order IDs the emulator has released, as `on_order_released` saw them."""
        self.fills: list[tuple[str, str, str, str, int]] = []
        """Each fill as its event gave it: client order ID, side, quantity, price, time. The session's
        reflection reads a sale's counterfactuals (held on, flat, managed) from these and the tape."""
        self.refused: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        """Why each candidate entry was not sent, for the run's report."""
        self.quotes = 0
        self.trades = 0
        self.winding_down = False
        self.sent: deque[tuple[int, int]] = deque()
        """(time, the adapter's request total) at each step of the last 60 seconds."""

    # --- handlers that only store what they are given (module header, "Order events") ---------------

    def on_signal(self, signal: Signal) -> None:
        if signal.name != RANK_SIGNAL:
            return
        try:
            self.rank = parse_rank(signal.value)
        except ValueError:
            self.rank = []
            self.rank_faults += 1
            return
        self.rank_ns = signal.ts_event

    def on_order_released(self, event: Any) -> None:
        self.released.add(event.client_order_id.value)

    def on_order_filled(self, event: Any) -> None:
        self.fills.append(
            (
                event.client_order_id.value,
                event.order_side.name,
                str(event.last_qty),
                str(event.last_px),
                event.ts_event,
            )
        )

    def on_order_denied(self, event: Any) -> None:
        self.refused.append((event.client_order_id.value, str(event.reason)))

    def on_order_rejected(self, event: Any) -> None:
        self.refused.append((event.client_order_id.value, str(event.reason)))

    def on_quote(self, quote: Any) -> None:
        self.quotes += 1

    def on_trade(self, trade: Any) -> None:
        self.trades += 1

    # --- start, bars, the step -------------------------------------------------------------------------

    def on_start(self) -> None:
        self.subscribe_signal(RANK_SIGNAL)
        self.clock.set_timer(STEP_TIMER, timedelta(seconds=STEP_SECS), callback=self._on_step)
        if self.wind_down_at is not None:
            self.clock.set_time_alert(
                WIND_DOWN_ALERT, self.wind_down_at, self._wind_down, allow_past=True
            )

    def on_bar(self, bar: Bar) -> None:
        """Keep the bar's close and dollar volume; note a rising run that this bar completes."""
        slot = self.slots.get(bar.bar_type.instrument_id)
        if slot is None or bar.bar_type != slot.bar_type:
            return
        close = bar.close.as_decimal()
        slot.closes.append(close)
        slot.dollars.append(close * bar.volume.as_decimal())
        if len(slot.closes) == slot.closes.maxlen and all(a < b for a, b in pairwise(slot.closes)):
            slot.run_ns, slot.run_close = bar.ts_event, close

    def _wind_down(self, event: TimeEvent) -> None:  # the alert only marks it; the step acts
        self.winding_down = True

    def _on_step(self, event: TimeEvent) -> None:
        now = event.ts_event
        recent = self._recent_requests(now)
        self._watch(now)
        for slot in list(self.slots.values()):
            self._manage(slot, now)
        if self.winding_down:
            self._end()
        else:
            self._enter(now, recent)

    def on_stop(self) -> None:
        """Cancel what works; what is then still held is the window's to find and report. With
        `manage_stop` upstream's market exit has already run before this is called."""
        if self.clock.timer_names().count(STEP_TIMER):
            self.clock.cancel_timer(STEP_TIMER)
        if self.manage_stop_set:
            return
        for slot in self.slots.values():
            if self._working(slot):
                self.cancel_all_orders(slot.instrument_id)

    # --- reading the cache ---------------------------------------------------------------------------

    def _order(self, client_order_id: ClientOrderId | None) -> Any | None:
        """The cached order (one seam, so a test can stand in for the cache)."""
        return None if client_order_id is None else self.cache.order(client_order_id)

    def _instrument(self, instrument_id: InstrumentId) -> Any:
        """The cached instrument of a watched name; a slot is opened only for one that is there."""
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Mover: instrument {instrument_id} has left the cache")
        return instrument

    def _held(self, slot: Slot) -> Decimal:
        """This strategy's signed open quantity in the slot's instrument."""
        return sum(
            (
                p.signed_qty
                for p in self.cache.positions_open(
                    instrument_id=slot.instrument_id, strategy_id=self.strategy_id
                )
            ),
            Decimal(0),
        )

    def _working(self, slot: Slot) -> bool:
        """Whether an order of this slot is not closed: emulated, in flight or open."""
        return any(
            order is not None and not order.is_closed
            for order in (self._order(slot.entry_id), self._order(slot.protect_id))
        )

    def _ranked(self, now: int) -> dict[InstrumentId, tuple[int, Decimal]]:
        """Each ranked name's place and move, or nothing when the ranking is stale."""
        if now - self.rank_ns > self.settings.rank_max_age_secs * SECOND_NS:
            return {}
        return {
            instrument_id: (place, move) for place, (instrument_id, move) in enumerate(self.rank)
        }

    # --- watch ---------------------------------------------------------------------------------------

    def _watch(self, now: int) -> None:
        ranked = self._ranked(now)
        top = [i for i in ranked if self.cache.instrument(i) is not None][: self.settings.watch]
        if not self.winding_down:
            for instrument_id in top:
                if instrument_id not in self.slots:
                    self._open_slot(instrument_id)
        for instrument_id, slot in list(self.slots.items()):
            if instrument_id in top or self._working(slot) or self._held(slot):
                continue
            if ranked or self.winding_down:  # a stale ranking unwatches nothing
                self._close_slot(slot)

    def _open_slot(self, instrument_id: InstrumentId) -> None:
        bar_type = BarType.from_str(f"{instrument_id}-{self.spec_suffix}")
        self.slots[instrument_id] = Slot(
            instrument_id=instrument_id,
            bar_type=bar_type,
            closes=deque(maxlen=self.settings.run_bars + 1),
            dollars=deque(maxlen=self.settings.flow_bars),
        )
        # Quotes and trades first, on the feed: the data engine hands the client only an instrument's
        # first subscription (ADR 0055), and the emulator's own subscription names no feed.
        self.subscribe_quotes(instrument_id, params=self.feed_params)
        self.subscribe_trades(instrument_id, params=self.feed_params)
        self.subscribe_bars(bar_type)

    def _close_slot(self, slot: Slot) -> None:
        del self.slots[slot.instrument_id]
        self.unsubscribe_bars(slot.bar_type)
        self.unsubscribe_trades(slot.instrument_id)
        self.unsubscribe_quotes(slot.instrument_id)

    # --- one slot ------------------------------------------------------------------------------------

    def _manage(self, slot: Slot, now: int) -> None:
        entry = self._order(slot.entry_id)
        if entry is not None and not entry.is_closed:
            self._time_out(slot, entry, now)
        held = self._held(slot)
        if held < 0:
            self._note(
                slot,
                f"{slot.instrument_id}: short {held}, which this strategy never opens; nothing sent",
            )
            return
        if held == 0:
            self._flat(slot, now)
            return
        slot.was_held = True
        if self.winding_down:
            if self.settings.extended_hours:
                self._sell_out(slot, held, now)
        elif self.settings.protect or slot.exiting:
            self._protect(slot, held, now)

    def _time_out(self, slot: Slot, entry: Any, now: int) -> None:
        """Cancel an entry still working after its time (again after another, if it is open still)."""
        timeout = self.settings.entry_timeout_secs * SECOND_NS
        due = (slot.entry_cancel_ns or entry.ts_init) + timeout
        if (now < due and not self.winding_down) or entry.is_pending_cancel or not entry.is_open:
            return
        if self.winding_down and slot.entry_cancel_ns and now < slot.entry_cancel_ns + timeout:
            return
        slot.entry_cancel_ns = now
        self.cancel_order(entry.client_order_id)

    def _flat(self, slot: Slot, now: int) -> None:
        """Nothing held: withdraw a sell that can no longer sell, and mark the sale once all is closed."""
        order = self._order(slot.protect_id)
        if order is not None and not order.is_closed:
            if not slot.was_held:
                return
            if (order.is_emulated or order.is_open) and not order.is_pending_cancel:
                self.cancel_order(order.client_order_id)
            return
        if slot.was_held and not self._working(slot):
            slot.sold_ns = now
            if order is not None and order.avg_px is not None:
                slot.sold_px = Decimal(str(order.avg_px))
            slot.was_held = False
            slot.exiting = False
            slot.floor = None
            slot.entry_id = slot.protect_id = None
            slot.entry_cancel_ns = slot.priced_ns = 0
            slot.run_ns = slot.run_close = None

    def _protect(self, slot: Slot, held: Decimal, now: int) -> None:
        order = self._order(slot.protect_id)
        if slot.protect_id is not None and slot.protect_id.value in self.released:
            slot.exiting = True
        if order is None or order.is_closed:
            if slot.exiting:
                self._limit_sell(slot, held, now, slot.floor)
            else:
                self._trail(slot, held)
        elif order.is_emulated:
            if order.quantity.as_decimal() != held and not order.is_pending_update:
                instrument = self._instrument(slot.instrument_id)
                self.modify_order(order.client_order_id, quantity=instrument.make_qty(held))
        elif slot.exiting:
            if slot.floor is None and order.price is not None:
                first = order.price.as_decimal()
                step = tick(first)
                slot.floor = (first * (1 - self.settings.chase_cap)).quantize(
                    step, rounding=ROUND_CEILING
                )
                slot.priced_ns = now
            self._chase(slot, order, held, now, slot.floor)

    def _trail(self, slot: Slot, held: Decimal) -> None:
        """Submit the emulated trailing stop-limit for what is held, once the emulator has a price to
        start from (a quote for `BID_ASK`, a trade for `LAST_PRICE`)."""
        trigger = self.settings.trigger
        seen = (
            self.cache.quote(slot.instrument_id)
            if trigger == TriggerType.BID_ASK
            else self.cache.trade(slot.instrument_id)
        )
        entry = self._order(slot.entry_id)
        if seen is None or entry is None or entry.avg_px is None:
            return
        instrument = self._instrument(slot.instrument_id)
        trailing, limit = offsets(
            Decimal(str(entry.avg_px)), self.settings.trail, self.settings.exit_through
        )
        order = self.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            None,
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_type=trigger,
            time_in_force=TimeInForce.DAY,
            emulation_trigger=trigger,
        )
        slot.protect_id = order.client_order_id
        self.submit_order(order, params=self.submit_params)

    def _chase(
        self, slot: Slot, order: Any, held: Decimal, now: int, floor: Decimal | None
    ) -> None:
        """Re-price a sell limit that still rests after `chase_secs`: `exit_ticks` through the bid, not
        below `floor`, and only downwards."""
        if not order.is_open or order.is_pending_update or order.is_pending_cancel:
            return
        if now - slot.priced_ns < self.settings.chase_secs * SECOND_NS:
            return
        quote = self.cache.quote(slot.instrument_id)
        if quote is None or order.price is None:
            return
        price = exit_price(quote.bid_price.as_decimal(), self.settings.exit_ticks, floor)
        if price >= order.price.as_decimal():
            return
        slot.priced_ns = now
        if self.settings.chase == "modify":
            instrument = self._instrument(slot.instrument_id)
            self.modify_order(
                order.client_order_id,
                price=Price.from_str(f"{price:.{instrument.price_precision}f}"),
            )
        else:  # the next step finds it closed and sends the new limit
            self.cancel_order(order.client_order_id)

    def _limit_sell(self, slot: Slot, held: Decimal, now: int, floor: Decimal | None) -> None:
        """A plain DAY limit for what is held, `exit_ticks` through the bid and not below `floor`."""
        quote = self.cache.quote(slot.instrument_id)
        if quote is None:
            return
        instrument = self._instrument(slot.instrument_id)
        price = exit_price(quote.bid_price.as_decimal(), self.settings.exit_ticks, floor)
        order = self.order_factory.limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            Price.from_str(f"{price:.{instrument.price_precision}f}"),
            time_in_force=TimeInForce.DAY,
        )
        slot.protect_id = order.client_order_id
        slot.priced_ns = now
        self.submit_order(order, params=self.submit_params)

    def _sell_out(self, slot: Slot, held: Decimal, now: int) -> None:
        """The wind-down outside the session: withdraw the entry and the trailing sell, then sell what
        is held with a limit through the bid, re-priced every `chase_secs`, with no floor."""
        slot.exiting = True
        entry = self._order(slot.entry_id)
        if entry is not None and not entry.is_closed:
            return  # `_time_out` cancels it at once in the wind-down
        order = self._order(slot.protect_id)
        if order is None or order.is_closed:
            self._limit_sell(slot, held, now, None)
        elif order.is_emulated:
            if not order.is_pending_cancel:
                self.cancel_order(order.client_order_id)
        else:
            self._chase(slot, order, held, now, None)

    # --- the end -------------------------------------------------------------------------------------

    def _end(self) -> None:
        """The wind-down in the session: upstream's market exit, once, for every slot."""
        if self.settings.extended_hours or self.is_exiting():
            return
        if any(self._held(slot) or self._working(slot) for slot in self.slots.values()):
            self.market_exit()

    # --- entries -------------------------------------------------------------------------------------

    def _enter(self, now: int, recent: int) -> None:
        candidates = [slot for slot in self.slots.values() if slot.run_ns is not None]
        if not candidates:
            return
        ranked = self._ranked(now)
        candidates.sort(key=lambda slot: ranked.get(slot.instrument_id, (RANK_MOST, 0))[0])
        for slot in candidates:
            run_close = slot.run_close
            slot.run_ns = slot.run_close = None  # a run is decided on once
            if slot.was_held or self._working(slot) or self._held(slot):
                continue
            why = self._refusal(slot, run_close, ranked, recent, now)
            if why is None:
                why = self._buy(slot, ranked[slot.instrument_id][1])
            if why is not None:
                self.dropped.append(f"{slot.instrument_id}: {why}")

    def _busy(self) -> int:
        return sum(1 for slot in self.slots.values() if slot.was_held or self._working(slot))

    def _refusal(
        self,
        slot: Slot,
        run_close: Decimal | None,
        ranked: dict[InstrumentId, tuple[int, Decimal]],
        recent: int,
        now: int,
    ) -> str | None:
        """Why this candidate is not entered now, or None."""
        settings = self.settings
        if not ranked:
            return "the ranking is stale" if self.rank_ns else "no ranking yet"
        if slot.instrument_id not in ranked:
            return "no longer ranked"
        move = ranked[slot.instrument_id][1]
        if move > settings.max_move:
            return f"moved {move}, over the no-chase cap of {settings.max_move}"
        if slot.sold_ns is not None:
            if now - slot.sold_ns < settings.cooldown_secs * SECOND_NS:
                return "inside the cooldown after its sale"
            if slot.sold_px is not None and run_close is not None and run_close <= slot.sold_px:
                return f"its run closed at {run_close}, not above the sale at {slot.sold_px}"
        if self._busy() >= settings.slots:
            return "no slot is free"
        if recent >= settings.soft_line:
            return f"{recent} requests in the last 60 s, at the soft line of {settings.soft_line}"
        return None

    def _recent_requests(self, now: int) -> int:
        """The adapter's requests over the last 60 seconds, from its running total sampled each step."""
        total = requests_sent()
        self.sent.append((now, total))
        while self.sent and now - self.sent[0][0] > BUDGET_SPAN_NS:
            self.sent.popleft()
        return total - self.sent[0][1]

    def _buy(self, slot: Slot, move: Decimal) -> str | None:
        """Send the entry; return why not, if it was not sent."""
        settings = self.settings
        quote = self.cache.quote(slot.instrument_id)
        if quote is None:
            return "no quote yet"
        instrument = self._instrument(slot.instrument_id)
        account = self.portfolio.account(instrument.id.venue)
        balance = None if account is None else account.balance_total(instrument.quote_currency)
        if balance is None:
            return "no account balance to size from"
        price = entry_price(quote.ask_price.as_decimal(), settings.entry_ticks)
        stop = planned_stop(price, settings.trail, settings.exit_through, settings.chase_cap)
        if stop <= 0:
            return f"no room for the trail under {price}"
        free = account.balance_free(instrument.quote_currency)
        if free is None:
            return "no free balance to size from"
        cap = min(settings.max_notional, free.as_decimal()) // price
        if len(slot.dollars) > 0:
            cap = min(cap, (settings.flow_share * sum(slot.dollars)) // price)
        if cap < 1:
            return f"the caps leave no share at {price}"
        precision = instrument.price_precision
        quantity = FixedRiskSizer(instrument).calculate(
            Price.from_str(f"{price:.{precision}f}"),
            Price.from_str(f"{stop:.{precision}f}"),
            balance,
            settings.risk,
            hard_limit=cap,
        )
        if not quantity.is_positive():
            return f"the size from risk came to nothing at {price}"
        order = self.order_factory.limit(
            slot.instrument_id,
            OrderSide.BUY,
            quantity,
            Price.from_str(f"{price:.{precision}f}"),
            time_in_force=TimeInForce.DAY,
        )
        slot.entry_id = order.client_order_id
        slot.entry_cancel_ns = 0
        slot.sold_ns = slot.sold_px = None
        slot.notes.append(f"entered at move {move}")
        self.submit_order(order, params=self.submit_params)
        return None

    def _note(self, slot: Slot, text: str) -> None:
        if text not in slot.notes:
            slot.notes.append(text)
            self.dropped.append(text)
