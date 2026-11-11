"""The mover strategy: ranked names in slots, a limit entry on the first rising run, a trailing sell that only rises.

A composition of upstream parts (ADR 0083, ADR 0088). What each part is, at NautilusTrader v2.0.0rc8
(`nautilus-trader@d53849b216e2`), read 2026-11-11:

* **The ranking** reaches the strategy as upstream's signal: `subscribe_signal(name)` and `on_signal`
  (`python/nautilus_trader/trading/__init__.pyi:696`, `:633`; concepts/message_bus, "Signal
  publish/subscribe": "The value is converted to a string on publish"). The ranking actor publishes
  `RANK_SIGNAL` each minute (ADR 0087); its value is `format_rank`'s string, the ranked instrument IDs best
  first, each with its move from the last regular close, and its `ts_event` the time the ranking was
  computed. Nothing here ranks, and no vendor's list is read.
* **The entry** is `OrderFactory.limit` (`python/nautilus_trader/common/__init__.pyi:1388`), time in force
  DAY, at the ask of the latest quote in the cache (`entry_ticks` through it, none by default), in every
  session; it is never a market order and is not re-priced: it rests until `entry_timeout_secs`.
* **The size** is `FixedRiskSizer.calculate(entry, stop_loss, equity, risk, hard_limit=...)`
  (`python/nautilus_trader/risk/__init__.pyi:16-29`; `crates/risk/src/sizing.rs:39-117`): `risk` of the
  account's total balance over the distance from the entry to the lowest price the first sale is planned
  at (`planned_stop`: the first trigger, less the released limit's give, less the chase's cap), in whole
  shares, capped by `hard_limit`: the largest entry (`max_notional`, and `max_shares`), the free balance
  and `flow_share` of the dollar volume of the last `flow_bars` bars. No entry is sent into a quote whose
  bid is already under that planned sale: the quote is then wider than the plan the size rests on.
* **The protection** is the engine's order emulator holding a `TRAILING_STOP_LIMIT` sell
  (`OrderFactory.trailing_stop_limit`, `common/__init__.pyi:1523-1547`; concepts/orders/emulated and
  trailing_stop_limit): its trigger follows the bid (`BID_ASK`) or the last trade (`LAST_PRICE`) up and
  never down, and at the trigger the emulator releases a `LIMIT` through the risk engine. A release
  keeps the submit command's params (`crates/execution/src/order_emulator/emulator.rs:1368` and
  `:1522-1531`; upstream's test of it, `:4513`, covers stop orders), so the `extended_hours` mark is given
  once, on the submit.
* **The chase** of a released limit that rests is `cancel_order`, the cancel seen terminal at a later
  step, then a new limit for all that is held (`chase="resend"`, the default). No order is modified: the
  lead's decision of 2026-11-11, until the adapter's replace chain has passed two clean outside reviews.
  `chase="modify"` is `Strategy.modify_order(client_order_id, price=...)` (concepts/strategies,
  "Modifying orders") and stays unused until then. The cost of a re-price is two requests and a moment
  with no working sale.
* **Prices** are ticks of upstream's `TieredTickScheme` stating Alpaca's two bands (`PRICES`;
  `crates/model/src/instruments/tick_scheme.rs`), through `next_ask_price` and `next_bid_price`.
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
   `LAST_PRICE`) is in the cache; when the held quantity changes it is canceled and made again, from
   the trigger it had reached, so the trigger never falls. Its limit offset is a whole number of
   cents, so its release is a price Alpaca takes on either side of a dollar (`offsets`). At the release
   (`on_order_released`) a still-working entry is canceled, since nothing more is bought of a name
   that is being sold; a sale that covers less than is held is canceled and the next covers all of it,
   so no share is without its sale and none has two. A limit that still rests after `chase_secs` is
   re-priced `exit_ticks` through the bid, never below the position's floor: `chase_cap` under its
   first sale price, kept whether that first sale filled, rests or was refused. Below it the sale rests.
   A sale the risk engine or the venue refuses is sent again every `chase_secs`.
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
order ID. The risk engine's per-order notional cap holds for a sale as for a buy
(`crates/risk/src/engine/mod.rs:2491-2507`; its one exemption, `:667-720`, is a full exit of a crypto
derivative), so the entry is bounded here and a node's cap is an outer bound (`mover_window`). A live
node builds a bar for every clock minute, a paused one too, flat and without volume
(`time_bars_build_with_no_updates`, on by default): a repeated close is not a rise, so a pause restarts
the run, and its minute counts as no flow.

Not built, and said so: a re-priced entry; the gain lock; a breach that must persist before the sale (the emulator
releases on the first quote at the trigger); a broker-held stop as an outage backstop (a second sell for
the same shares is refused while the first works); slots reserved for the newest movers; no order into
a halt (ADR 0083, gap 4). Known limits: the strategy does not know the session calendar (the run checks
it); a partly filled entry is protected only from the next step.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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
from nautilus_trader.model import OrderStatus
from nautilus_trader.model import Price
from nautilus_trader.model import TieredTickScheme
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
REFUSED = (OrderStatus.DENIED, OrderStatus.REJECTED)
"""An order the risk engine denied or the venue rejected."""


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


PRICES = TieredTickScheme(
    "ALPACA_US_EQUITY_LIMIT",
    [(0.0001, 1.0, 0.0001), (1.0, 5000.0, 0.01)],
    4,
    1_000_000,
)
"""Alpaca's limit prices as upstream's tiered tick scheme (`nautilus_trader.model.TieredTickScheme`,
`crates/model/src/instruments/tick_scheme.rs`): four decimals under a dollar, two from a dollar up, to
5,000 dollars. Every limit price this strategy computes is one of its ticks, so a price that crosses a
dollar lands on the other band's step. The adapter's instrument has one increment, the finer, and no
scheme of its own (`canon_alpaca.constants.PRICE_INCREMENT`)."""


NUDGE = 0.00001
"""A tenth of the finest tick. The scheme takes a float, and the float nearest a four-decimal price can
sit a hair under it (10.29 is 10.28999...), which the scheme would read as between two ticks. A price
is moved this much toward its own side first: up for a bid, which is then floored, down for an ask."""


def entry_price(ask: Decimal, ticks: int) -> Decimal:
    """A buy limit `ticks` ticks through the ask: the ask, or the next price above it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_ask_price`)."""
    price = PRICES.next_ask_price(float(ask) - NUDGE, ticks)
    if price is None:
        raise ValueError(f"no limit price {ticks} ticks above {ask} in {PRICES.name}")
    return price.as_decimal()


def exit_price(bid: Decimal, ticks: int, floor: Decimal | None) -> Decimal:
    """A sell limit `ticks` ticks through the bid: the bid, or the next price below it Alpaca takes, and
    `ticks` more (`TieredTickScheme.next_bid_price`), the lowest tick at least; never below `floor`."""
    found = PRICES.next_bid_price(float(bid) + NUDGE, ticks)
    price = PRICES.min_price.as_decimal() if found is None else found.as_decimal()
    return price if floor is None else max(price, floor)


def chase_floor(first: Decimal, chase_cap: Decimal) -> Decimal:
    """The lowest price a position's sale is chased to: `chase_cap` under its first sale price, up to
    the next price Alpaca takes."""
    price = PRICES.next_ask_price(float(first * (1 - chase_cap)) - NUDGE, 0)
    return first if price is None else price.as_decimal()


def offsets(entry: Decimal, trail: Decimal, exit_through: Decimal) -> tuple[Decimal, Decimal]:
    """The trailing offset and the limit offset, as prices. The trailing offset is `trail` of the entry
    up to the entry's step: it places the trigger, which the emulator keeps to itself. The limit offset
    is that plus `exit_through` of the entry, up to a whole cent and a cent over the trailing offset at
    least: it places the released limit, the one price that reaches the venue. A whole number of cents
    under the highest bid is a price Alpaca takes in either band, since a bid at or above a dollar is
    in cents and under a dollar any four-decimal price is taken; so the release is valid when the bid
    has crossed a dollar between two steps (the emulator prices it on the instrument's own 0.0001
    increment, `crates/execution/src/trailing.rs:182`). The cost is a release up to a cent lower than
    `exit_through` asks for a name under a dollar, which `planned_stop` and so the size take in."""
    step = tick(entry)
    trailing = max((entry * trail).quantize(step, rounding=ROUND_CEILING), step)
    limit = (trailing + entry * exit_through).quantize(CENT, rounding=ROUND_CEILING)
    return trailing, max(limit, (trailing + SUB_DOLLAR).quantize(CENT, rounding=ROUND_CEILING))


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
    "max_shares": (int, NoneType),
    "entries": (int, NoneType),
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

    `max_notional` (the largest notional of an entry, in dollars) and `max_move` (the no-chase cap on
    the ranking's move, a fraction) have no default: the run states them. The entry is bounded here,
    where it is sized; the risk engine's own per-order cap is the node's outer bound, a multiple of
    `max_notional`, since that cap holds for a sale as for a buy and no cap shapes or refuses a sale
    (ADR 0088). `max_shares` bounds an entry in shares, and `entries` how many entries a run may fill
    (None: no bound). `bar_spec`
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
        max_shares: int | None = None,
        entries: int | None = None,
        bar_spec: str = "1-MINUTE-LAST-EXTERNAL",
        slots: int = 1,
        watch: int = 3,
        risk: Decimal = Decimal("0.01"),
        run_bars: int = 3,
        trail: Decimal = Decimal("0.02"),
        exit_through: Decimal = Decimal("0.005"),
        chase_cap: Decimal = Decimal("0.02"),
        chase_secs: int = 5,
        chase: str = "resend",
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
        for name in ("max_shares", "entries"):
            if fields[name] is not None and fields[name] <= 0:
                raise ValueError(f"{name} must be positive, got {fields[name]}")
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
        self.max_shares = max_shares
        self.entries = entries
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
    """The lowest price this position's sale is chased to, from its first sale price (`chase_floor`). It
    belongs to the position: a sale that is refused or re-sent keeps it."""
    trail_trigger: Decimal | None = None
    """The trigger a trailing sell had reached when it was canceled to be made again: the next one starts
    from it, so the trigger never falls."""
    priced_ns: int = 0
    was_held: bool = False


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
        self.rank_said: tuple[int, str, int] | None = None
        """The ranking the log last named: its time, its value and the malformed ones counted then."""
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
        self.live_bars = 0
        self.history_bars = 0
        self.history_dropped = 0
        """It requests no history: a window's report reads these of every strategy."""
        self.sold: dict[InstrumentId, tuple[int, Decimal | None]] = {}
        """When each name was last sold, and at what price: the name's, until its next sale, so that
        neither a slot closed and opened again nor an entry that never filled loses the cooldown and
        the price the next run must close above."""
        self.notes: dict[InstrumentId, list[str]] = {}
        """What was noted of each name, for the run's report; kept when its slot is closed."""
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

    def record(self) -> dict[str, Any]:
        """What a run's report keeps of the strategy, in plain values: its rows, the ranking it last
        had, each fill, each order refused, the orders the emulator released, and each slot's notes."""
        return {
            "rows": {name: str(getattr(self.settings, name)) for name in OWN_FIELDS},
            "rank": format_rank(self.rank),
            "rank_ns": self.rank_ns,
            "rank_faults": self.rank_faults,
            "fills": [list(fill) for fill in self.fills],
            "refused": [list(refused) for refused in self.refused],
            "released": sorted(self.released),
            "notes": {str(i): list(notes) for i, notes in self.notes.items()},
        }

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
        self.live_bars += 1
        close = bar.close.as_decimal()
        before = slot.closes[-1] if slot.closes else None
        slot.closes.append(close)
        slot.dollars.append(close * bar.volume.as_decimal())
        rising = 0  # the closes in a row, to this one, each above the one before
        for later, earlier in pairwise(reversed(slot.closes)):
            if not later > earlier:
                break
            rising += 1
        wanted = self.settings.run_bars
        if rising >= wanted:
            slot.run_ns, slot.run_close = bar.ts_event, close
            self._say(f"{slot.instrument_id}: run qualified, {wanted} rising closes to {close}")
        elif before is not None and rising == 0:
            self._say(f"{slot.instrument_id}: run restarted, {close} is not above {before}")
        else:
            after = "" if before is None else f" after {before}"
            self._say(
                f"{slot.instrument_id}: run building, {rising} of {wanted} rising closes "
                f"({close}{after})"
            )

    def _wind_down(self, event: TimeEvent) -> None:  # the alert only marks it; the step acts
        self.winding_down = True

    def _say(self, text: str) -> None:
        """One line in the node's log, from which a run's record reads which rule decided. Called from
        the step and from `on_bar` only: a handler of an order event or a signal never logs (module
        header, "Order events")."""
        self.log.info(text)

    def _say_ranking(self, now: int) -> None:
        """The ranking, once each time it arrives new or changed, with its age; a malformed one, once."""
        heard = (self.rank_ns, format_rank(self.rank), self.rank_faults)
        said = self.rank_said or (0, "", 0)
        if heard == said:
            return
        if heard[2] > said[2]:
            self._say(f"Ranking: malformed ({heard[2]} so far); no entry until one parses")
        if heard[0] and heard[:2] != said[:2]:
            age = (now - heard[0]) / SECOND_NS
            self._say(f"Ranking: {heard[1] or 'no name'}; computed {age:.0f} s ago")
        self.rank_said = heard

    def _on_step(self, event: TimeEvent) -> None:
        now = event.ts_event
        self._say_ranking(now)
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

    def _fresh(self, now: int) -> bool:
        """Whether a ranking has arrived and is no older than `rank_max_age_secs`."""
        return (
            bool(self.rank_ns) and now - self.rank_ns <= self.settings.rank_max_age_secs * SECOND_NS
        )

    def _ranked(self, now: int) -> dict[InstrumentId, tuple[int, Decimal]]:
        """Each ranked name's place and move; nothing when the ranking is stale, or names nobody."""
        if not self._fresh(now):
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
            if instrument_id in top or self._working(slot) or self._held(slot) or slot.was_held:
                continue  # `was_held`: its sale is recorded by `_flat` before the slot may go
            if self._fresh(now) or self.winding_down:  # a stale ranking unwatches nothing
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
        if slot.protect_id is not None and slot.protect_id.value in self.released:
            slot.exiting = True
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
        """Cancel an entry still working after its time (again after another, if it is open still); at
        once when the wind-down has begun or this name's sale has been released, since nothing more is
        bought of a name that is being sold."""
        timeout = self.settings.entry_timeout_secs * SECOND_NS
        due = (slot.entry_cancel_ns or entry.ts_init) + timeout
        at_once = self.winding_down or slot.exiting
        if (now < due and not at_once) or entry.is_pending_cancel or not entry.is_open:
            return
        if at_once and slot.entry_cancel_ns and now < slot.entry_cancel_ns + timeout:
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
            price = None if order is None or order.avg_px is None else Decimal(str(order.avg_px))
            self.sold[slot.instrument_id] = (now, price)
            slot.was_held = False
            slot.exiting = False
            slot.floor = slot.trail_trigger = None
            slot.entry_id = slot.protect_id = None
            slot.entry_cancel_ns = slot.priced_ns = 0
            slot.run_ns = slot.run_close = None

    def _protect(self, slot: Slot, held: Decimal, now: int) -> None:
        order = self._order(slot.protect_id)
        if slot.exiting and slot.floor is None and order is not None and order.price is not None:
            # the released sale's own price, whether it is open, filled or was refused
            slot.floor = chase_floor(order.price.as_decimal(), self.settings.chase_cap)
            if order.is_open:
                slot.priced_ns = now  # the chase's clock starts when the sale is seen working
        if order is None or order.is_closed:
            if (
                order is not None
                and order.status in REFUSED
                and now - slot.priced_ns < self.settings.chase_secs * SECOND_NS
            ):
                return  # a sale the engine or the venue refused is sent again, every `chase_secs`
            if slot.exiting:
                self._limit_sell(slot, held, now, slot.floor)
            else:
                self._trail(slot, held, now)
        elif order.is_emulated:
            # No order is modified. A trailing sell that no longer covers what is held is canceled,
            # and one for all of it is made from the trigger this one had reached.
            if order.quantity.as_decimal() != held and not order.is_pending_cancel:
                if order.trigger_price is not None:
                    slot.trail_trigger = order.trigger_price.as_decimal()
                self.cancel_order(order.client_order_id)
                # The emulator cancels its own order before the call returns, so the cancel is seen
                # terminal here and the new sell follows in this step, not a second later.
                canceled = self._order(slot.protect_id)
                if canceled is not None and canceled.is_closed:
                    self._trail(slot, held, now)
        elif slot.exiting:
            self._chase(slot, order, held, now, slot.floor)

    def _trail(self, slot: Slot, held: Decimal, now: int) -> None:
        """Submit the emulated trailing stop-limit for what is held, once the emulator has a price to
        start from (a quote for `BID_ASK`, a trade for `LAST_PRICE`)."""
        trigger = self.settings.trigger
        at = self._followed(slot)
        entry = self._order(slot.entry_id)
        if at is None or entry is None or entry.avg_px is None:
            return
        kept = slot.trail_trigger
        if kept is not None and at <= kept:
            # the market is at the trigger the canceled sell had reached: its sale is due now
            slot.exiting, slot.trail_trigger = True, None
            self._say(f"{slot.instrument_id}: at {at}, the trigger {kept} is met; selling by limit")
            self._limit_sell(slot, held, now, slot.floor)
            return
        instrument = self._instrument(slot.instrument_id)
        bought = Decimal(str(entry.avg_px))
        trailing, limit = offsets(bought, self.settings.trail, self.settings.exit_through)
        self._say(
            f"{slot.instrument_id}: trailing SELL {held}, its trigger {trailing} under the highest "
            f"{'bid' if trigger == TriggerType.BID_ASK else 'trade'}, its limit {limit} under it"
            + ("" if kept is None else f", from the trigger {kept} already reached")
        )
        order = self.order_factory.trailing_stop_limit(
            slot.instrument_id,
            OrderSide.SELL,
            instrument.make_qty(held),
            None,
            limit_offset=limit,
            trailing_offset=trailing,
            trailing_offset_type=TrailingOffsetType.PRICE,
            trigger_price=None if kept is None else instrument.make_price(kept),
            trigger_type=trigger,
            time_in_force=TimeInForce.DAY,
            emulation_trigger=trigger,
        )
        slot.protect_id = order.client_order_id
        slot.priced_ns = now
        slot.trail_trigger = None
        self.submit_order(order, params=self.submit_params)

    def _followed(self, slot: Slot) -> Decimal | None:
        """The price the trailing sell's trigger follows, as the cache has it now: the bid for `BID_ASK`,
        the last trade for `LAST_PRICE`; None before the first."""
        if self.settings.trigger == TriggerType.BID_ASK:
            quote = self.cache.quote(slot.instrument_id)
            return None if quote is None else quote.bid_price.as_decimal()
        trade = self.cache.trade(slot.instrument_id)
        return None if trade is None else trade.price.as_decimal()

    def _chase(
        self, slot: Slot, order: Any, held: Decimal, now: int, floor: Decimal | None
    ) -> None:
        """Re-price a sell limit that still rests after `chase_secs`: `exit_ticks` through the bid, not
        below `floor`, and only downwards."""
        if not order.is_open or order.is_pending_update or order.is_pending_cancel:
            return
        if order.leaves_qty.as_decimal() < held:
            # more was bought after this sale was sent: cancel it, and the next step sells all that is
            # held in one order, so no share is without its sale and none has two
            self.cancel_order(order.client_order_id)
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
        self._say(f"{slot.instrument_id}: SELL {held} limit {price} (floor {floor})")
        self.submit_order(order, params=self.submit_params)

    def _sell_out(self, slot: Slot, held: Decimal, now: int) -> None:
        """The wind-down outside the session: withdraw the entry and the trailing sell, then sell what
        is held with a limit through the bid, re-priced every `chase_secs`, with no floor."""
        slot.exiting = True  # `_time_out` cancels a working entry at once; what it still buys is
        order = self._order(
            slot.protect_id
        )  # covered by `_chase`, which re-sends for all that is held
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
            self._say("Winding down: upstream's market exit for what is held and what works")
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
                self._say(f"{slot.instrument_id}: no entry: {why}")

    def _entered(self) -> int:
        """The entries this run has filled, in whole or in part, and those still working."""
        bought = {cid for cid, side, *_ in self.fills if side == OrderSide.BUY.name}
        for slot in self.slots.values():
            entry = self._order(slot.entry_id)
            if entry is not None and not entry.is_closed:
                bought.add(entry.client_order_id.value)
        return len(bought)

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
        if not self._fresh(now):
            return "the ranking is stale" if self.rank_ns else "no ranking yet"
        if slot.instrument_id not in ranked:
            return "no longer ranked"
        move = ranked[slot.instrument_id][1]
        if move > settings.max_move:
            return f"moved {move}, over the no-chase cap of {settings.max_move}"
        if slot.instrument_id in self.sold:
            sold_ns, sold_px = self.sold[slot.instrument_id]
            if now - sold_ns < settings.cooldown_secs * SECOND_NS:
                return "inside the cooldown after its sale"
            if sold_px is not None and run_close is not None and run_close <= sold_px:
                return f"its run closed at {run_close}, not above the sale at {sold_px}"
        if self._busy() >= settings.slots:
            return "no slot is free"
        if settings.entries is not None and self._entered() >= settings.entries:
            return f"the run's {settings.entries} entr{'y is' if settings.entries == 1 else 'ies are'} used"
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
        try:
            price = entry_price(quote.ask_price.as_decimal(), settings.entry_ticks)
        except ValueError as e:
            return str(e)
        stop = planned_stop(price, settings.trail, settings.exit_through, settings.chase_cap)
        if stop <= 0:
            return f"no room for the trail under {price}"
        bid = quote.bid_price.as_decimal()
        if bid < stop:
            return (
                f"the bid {bid} is under the planned stop {stop} of an entry at {price}: the quote "
                "is wider than the plan"
            )
        free = account.balance_free(instrument.quote_currency)
        if free is None:
            return "no free balance to size from"
        cap = min(settings.max_notional, free.as_decimal()) // price
        if len(slot.dollars) > 0:
            cap = min(cap, (settings.flow_share * sum(slot.dollars)) // price)
        if settings.max_shares is not None:
            cap = min(cap, Decimal(settings.max_shares))
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
        self.notes.setdefault(slot.instrument_id, []).append(f"entered at move {move}")
        self._say(
            f"{slot.instrument_id}: BUY {quantity} limit {price} (bid {bid}, planned stop {stop}, "
            f"move {move})"
        )
        self.submit_order(order, params=self.submit_params)
        return None

    def _note(self, slot: Slot, text: str) -> None:
        notes = self.notes.setdefault(slot.instrument_id, [])
        if text not in notes:
            notes.append(text)
            self.dropped.append(text)
