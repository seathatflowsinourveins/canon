"""Replay an owner's protection over local catalogs with native engine reports.

Contracts read at NautilusTrader d53849b216e2 (v2.0.0rc8):
* examples/backtest/fx_ema_cross_audusd_ticks.py:49-117: low-level engine and native reports.
  crates/backtest/src/python/engine.rs:790-816 and python/nautilus_trader/analysis/reporter.py:
  generate_orders_report, generate_order_fills_report, generate_fills_report and
  generate_positions_report. Order fills summarize orders; fills preserves each partial fill.
* docs/concepts/backtesting/apis-and-runs.md:22-95: a fresh BacktestEngine per segment,
  add_venue/instrument/strategy/data, run(streaming=True), end, dispose.
* docs/concepts/backtesting/trade-execution.md:1-90 and fill-prices-and-matching.md:38-64,
  165-206: L1 quote/trade execution and liquidity consumption, with their limitations;
  trade-execution.md:115-140: queue_position with trade_execution.
* python/nautilus_trader/persistence/__init__.pyi:232-240,409-445: catalog instruments and
  bounded queries; backtest/__init__.pyi:445-511: venue flags and engine lifetime.
* trading/__init__.pyi:635-683,963-1002 and crates/trading/src/strategy/mod.rs:1444-1464:
  data and order hooks; on_order_event follows the specific callback. The observer forwards
  the protector's callbacks unchanged and reads its _holding_state table and _own_level.
  crates/trading/src/python/strategy.rs:1735-1758 returns shared cache/clock handles; retain
  them at start so observation during a synchronous submit/modify callback does not borrow
  the native strategy again (:2016-2036 takes a mutable strategy borrow while submitting).
  crates/execution/src/order_emulator/emulator.rs:1657-1686 updates the cached order for
  a native trail raise and sends a risk event, without publishing a strategy callback.
  Read these OrderUpdated events from order.events(), retaining their own timestamps.
* docs/concepts/execution/reconciliation.md:305-335 and
  crates/execution/src/matching_engine/mod.rs:3583-3667,5545-5560,5949-5965: the execution
  engine's EXTERNAL fill fallback is not the simulated venue's position selection, which
  refuses a sale of a long another strategy holds at every native venue setting.

The two stand-ins follow tests/test_owner_protector.py's Harness and VenueOrders:
cold-start requests answer only from the engine cache; the identity reader reports no fills
between runs. An EXTERNAL owner buys anew in each segment. Neither stand-in changes matching,
custody, the guard, row arithmetic, gain lock or chase. Bars replay before the first quote to
warm the cache; their ts_init controls availability, so the request cannot see future bars.

Report serialization: pandas 3dcc6fbbbebc, core/generic.py's to_json and core/frame.py's
reset_index preserve the native report index and ISO timestamps at nanosecond precision.

CLI: python -m canon_trading.tape_replay --spec spec.json --report report.json
The report also writes report.md. Required spec keys: quotes_root, trades_root, bars_root
(local catalog directories), holdings ({"LLL": {"buy_time": timestamp, "quantity": 10}}), row
(OWN1, OWN3, or F2 for the native refusal), segments (consecutive [start, end) pairs),
level_book_path, run_label. Optional fill_arm defaults to tape (alpaca-paper-sim-v1);
touch is a named departure without trades, consumption or queue tracking. Optional booking
defaults to foreign; own buys from the protector's hook as a bounded experiment,
without changing its custody decisions. An own run is a measurement, never a counted proof.
Timestamps are integer nanoseconds, decimal
nanosecond strings, or timezone-aware ISO strings with at most microsecond precision. Paths
are local; relative paths in a CLI spec are relative to that spec's directory. Segments use
the catalog/engine replay clock, ts_init, without retiming the data's ts_event. The book is
written by OwnerProtector itself. No environment, credential, adapter client or network read.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.config import BacktestEngineConfig
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import NautilusDataType
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TraderId
from nautilus_trader.model import Venue
from nautilus_trader.persistence import ParquetDataCatalog
from nautilus_trader.trading import Strategy

from . import protect
from .backtest import FEE_RATES
from .backtest import VENUE_SETTINGS
from .owner_protector import OwnerProtector
from .owner_protector import OwnerProtectorConfig
from .owner_protector import _own_level

SECOND = 1_000_000_000
STARTING_BALANCE = "100000 USD"
INTRODUCTION = (
    "The fill is not shown: the simulated venue refuses a sale of a long another strategy "
    "holds. What follows the release in this run is the protector's refusal path."
)
OWN_INTRODUCTION = (
    "The buy was booked under the protector's ID so that the simulated venue would fill its sale; "
    "up to the release its decisions equal the foreign run's."
)
LIMITATIONS = (
    "Not shown by a simulation: the venue's own rules (wash-trade refusal, extended-hours order "
    "rules), the paper venue's fills, our own weight in a thin name. Our own weight in that name "
    "is not modelled, so what the tape absorbed is an upper bound."
)
LIMITS = (
    (
        "The EXTERNAL long is bought again at the first quote at or after its buy time in each "
        "fresh segment; it is not reconciled inventory and its new ask sets its simulated entry."
    ),
    (
        "The holding identity reader is a stand-in reporting no intervening buys or sells, not "
        "evidence from a venue; a restart therefore tests book continuity for the same quantity."
    ),
    (
        "Cold-start bars and quotes come from the engine cache, not a historical endpoint; bars "
        "warm that cache before the segment's quotes. Discovery uses the protector's normal timer."
    ),
    (
        "State snapshots read the protector's table after its callbacks within the requested "
        "segment; earlier bars only warm the cache. Native trail raises "
        "which publish no strategy callback are read from cached order history at their event "
        "times; no historical table state is invented for those cache-only events."
    ),
    (
        "The ALERT-once path for a refusal before a sale begins is not reachable here: the emulated "
        "trail reaches the venue only as its released LIMIT. Each observed ALERT is counted as said."
    ),
    (
        "A tape print and liquidity consumption do not show a protective fill: the venue refuses the "
        "EXTERNAL sale before matching. No HEDGING, mirrored order or ownership change is used."
    ),
    (
        "Starting cash is the profile's synthetic default, 100000 USD, not account equity. "
        "Zero latency; no market-hours or market-impact model and no performance claim. "
        "Regulatory pass-through fees are not modelled. Queue tracking estimates displayed "
        "quantity ahead, not our actual priority."
    ),
)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _stamp(value: Any) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isdigit():
        result = int(value)
    elif isinstance(value, str):
        # Do not silently discard sub-microsecond ISO precision; integer ns keeps it exactly.
        fraction = value.split(".", 1)[1] if "." in value else ""
        digits = "".join(c for c in fraction.split("+")[0].split("-")[0] if c.isdigit())
        if len(digits) > 6:
            raise ValueError("timestamp ISO precision exceeds microseconds; use integer ns")
        at = datetime.fromisoformat(value)
        if at.tzinfo is None:
            raise ValueError("timestamp must have a timezone")
        result = (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1000
    else:
        raise ValueError("timestamp must be integer nanoseconds or timezone-aware ISO text")
    if result < 0:
        raise ValueError("timestamp must be nonnegative")
    return result


def _path(value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip() or "://" in value:
        raise ValueError("catalog and level book paths must name local files, not URLs")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _checked(spec: Any, base: Path) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise TypeError("spec must be a JSON object")
    required = {
        "quotes_root",
        "trades_root",
        "bars_root",
        "holdings",
        "row",
        "segments",
        "level_book_path",
        "run_label",
    }
    allowed = required | {"fill_arm", "booking"}
    if not required <= spec.keys() or spec.keys() - allowed:
        raise ValueError(
            f"spec keys: missing {sorted(required - spec.keys())}; unknown {sorted(spec.keys() - allowed)}"
        )
    spec = {"fill_arm": "tape", "booking": "foreign", **spec}
    if spec["fill_arm"] not in ("touch", "tape"):
        raise ValueError(f"unknown fill arm {spec['fill_arm']!r}; use touch or tape")
    if spec["booking"] not in ("foreign", "own"):
        raise ValueError(f"unknown booking {spec['booking']!r}; use foreign or own")
    if not isinstance(spec["run_label"], str) or not spec["run_label"].strip():
        raise ValueError("run_label must be nonempty text")
    pairs = spec["segments"]
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("segments must be a nonempty list of [start, end) pairs")
    segments = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("segments must contain [start, end) pairs")
        start, end = map(_stamp, pair)
        if start >= end:
            raise ValueError("segment start must precede end")
        if segments and segments[-1][1] != start:
            problem = "overlap" if start < segments[-1][1] else "gap"
            raise ValueError(f"segments {problem}: shared bounds must be identical")
        segments.append((start, end))
    rows = spec["holdings"]
    if not isinstance(rows, dict) or not rows:
        raise ValueError("holdings must map symbols to buy_time and quantity")
    holdings = {}
    for symbol, row in rows.items():
        if not isinstance(symbol, str) or not symbol or any(c in symbol for c in "/\\ \t\n"):
            raise ValueError("holding symbol must be nonempty text without whitespace or slashes")
        if not isinstance(row, dict) or row.keys() != {"buy_time", "quantity"}:
            raise ValueError(f"holding {symbol} needs buy_time and quantity")
        quantity = Decimal(str(row["quantity"]))
        if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValueError(f"holding {symbol} quantity must be positive whole shares")
        holdings[InstrumentId.from_str(f"{symbol}.ALPACA")] = {
            "buy_time": _stamp(row["buy_time"]),
            "quantity": int(quantity),
        }
    roots = {key: _path(spec[key], base) for key in ("quotes_root", "trades_root", "bars_root")}
    for key, path in roots.items():
        if not path.is_dir():
            raise ValueError(f"{key}: local catalog directory is missing")
    return {
        **spec,
        **roots,
        "holdings": holdings,
        "segments": segments,
        "level_book_path": _path(spec["level_book_path"], base),
    }


class _IdentityReader:
    """VenueOrders' empty /orders answer: explicitly assumed identity, never a client."""

    def get(self, route: str, params: Any = None) -> list[Any]:
        if route != "/orders" or params is None:
            raise ValueError(f"offline identity reader cannot answer {route}")
        return []


def _config(spec: dict[str, Any], index: int) -> OwnerProtectorConfig:
    return OwnerProtectorConfig(
        strategy_id=StrategyId("OwnerProtector-001"),
        row=spec["row"],
        feed="sip",
        account_label="tape-replay",
        level_book_path=spec["level_book_path"],
        run=f"{spec['run_label']}:{index + 1}",
        session_part="regular",
        trading=_IdentityReader(),
    )


def _batch(
    catalog: ParquetDataCatalog, kind: Any, ids: list[str], start: int, end: int
) -> list[Any]:
    # Query endpoints are inclusive at the pin. Filter replay timestamps as [start, end).
    return [
        item
        for item in catalog.query(kind, identifiers=ids, start=start, end=end - 1)
        if start <= item.ts_init < end
    ]


def _load(spec: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    catalogs = {
        kind: ParquetDataCatalog(str(spec[f"{kind}_root"])) for kind in ("quotes", "trades", "bars")
    }
    ids = [str(iid) for iid in spec["holdings"]]
    definitions: dict[str, Any] = {}
    for catalog in catalogs.values():
        for instrument in catalog.instruments(instrument_ids=ids):
            key = str(instrument.id)
            if key in definitions:
                before = definitions[key].to_dict()
                after = instrument.to_dict()
                for stamp in ("ts_event", "ts_init"):
                    before.pop(stamp, None)
                    after.pop(stamp, None)
                if before != after:
                    raise ValueError(f"conflicting instrument definition: {key}")
            definitions[key] = instrument
    missing = set(ids) - definitions.keys()
    if missing:
        raise ValueError(
            f"holding has missing definition in the roots: {', '.join(sorted(missing))}"
        )
    bars = [f"{iid}-1-MINUTE-LAST-EXTERNAL" for iid in ids]
    batches = []
    for start, end in spec["segments"]:
        day = protect.session_start(datetime.fromtimestamp(start // SECOND, UTC))
        history_start = _stamp(day.isoformat())
        batches.append(
            {
                "start": start,
                "end": end,
                "quotes": _batch(catalogs["quotes"], NautilusDataType.QuoteTick, ids, start, end),
                "trades": (
                    _batch(catalogs["trades"], NautilusDataType.TradeTick, ids, start, end)
                    if spec["fill_arm"] == "tape"
                    else []
                ),
                "bars": _batch(catalogs["bars"], NautilusDataType.Bar, bars, history_start, end),
            }
        )
    return list(definitions.values()), batches


def _unfiled_alerts(lines: list[dict[str, Any]]) -> int:
    """Count the ALERT lines filed under no holding, such as a book-wide one."""
    return sum(1 for line in lines if line["symbol"] is None and "ALERT" in line["text"])


class _Trace:
    """Read the protector's table after callbacks; do not derive a substitute state machine."""

    def __init__(
        self, protector: _RecordedProtector, holdings: dict[InstrumentId, Any], start: int
    ) -> None:
        self.protector = protector
        self.start = start
        self.ids = list(holdings)
        self.lines: list[dict[str, Any]] = []
        self.last_level: dict[InstrumentId, str] = {}
        self.seen_events: set[str] = set()
        self.rows: dict[str, dict[str, Any]] = {
            iid.symbol.value: {
                "arming": [],
                "level_changes": [],
                "releases": [],
                "refusals": [],
                "order_events": [],
                "owner_order_events": [],
                "owner_fills": [],
                "states": [],
            }
            for iid in self.ids
        }

    def capture(self, event: str) -> None:
        protector = self.protector
        at = protector.clock.timestamp_ns()
        if at < self.start:
            return  # Warm-up bars precede the requested segment and no scripted buy is possible yet.
        for iid in self.ids:
            slot = protector.slots.get(iid)
            state, count, reason = protector._holding_state(iid, slot)
            order = (
                None
                if slot is None or slot.protect_id is None
                else protector.cache.order(slot.protect_id)
            )
            if order is not None:
                for native_event in order.events():
                    self._order_record(native_event, order, owner=False)
            level = None if slot is None else _text(_own_level(slot, order))
            row = self.rows[iid.symbol.value]
            row["states"].append(
                {
                    "at": at,
                    "event": event,
                    "state": state,
                    "count": count,
                    "reason": reason,
                    "level": level,
                    "distance": None
                    if slot is None or slot.distance is None
                    else str(slot.distance.distance),
                    "limit_offset": None if slot is None else _text(slot.limit_offset),
                    "order_status": None if order is None else order.status.name,
                }
            )
            old = self.last_level.get(iid)
            if level is not None:
                if old is not None and Decimal(old) != Decimal(level):
                    row["level_changes"].append(
                        {"at": at, "old": old, "new": level, "event": event}
                    )
                self.last_level[iid] = level

    def order(self, event: Any, *, owner: bool = False) -> None:
        order = self.protector.cache.order(event.client_order_id)
        if order is not None:
            for native_event in order.events():
                self._order_record(native_event, order, owner=owner)
        self._order_record(event, order, owner=owner)
        self.capture(("owner " if owner else "") + type(event).__name__)

    def _order_record(self, event: Any, order: Any, *, owner: bool) -> None:
        event_id = str(event.event_id)
        if event_id in self.seen_events:
            return
        self.seen_events.add(event_id)
        row = self.rows[event.instrument_id.symbol.value]
        record = {
            "event_id": event_id,
            "event": type(event).__name__,
            "at": event.ts_event,
            "received_at": event.ts_init,
            "client_order_id": str(event.client_order_id),
            "strategy_id": str(event.strategy_id),
            "quantity": None if order is None else str(order.quantity),
        }
        for field in (
            "reason",
            "price",
            "trigger_price",
            "released_price",
            "last_px",
            "last_qty",
            "commission",
        ):
            value = getattr(event, field, None)
            if value is not None:
                record[field] = str(value)
        row["owner_order_events" if owner else "order_events"].append(record)
        if owner and record["event"] == "OrderFilled":
            row["owner_fills"].append(record.copy())
        if not owner and record["event"] == "OrderReleased":
            row["releases"].append(
                {**record, "price": str(event.released_price), "lines_seen": len(self.lines)}
            )
        if "reason" in record:
            row["refusals"].append({**record, "source": "owner" if owner else "protector"})
        if not owner and (trigger := getattr(event, "trigger_price", None)) is not None:
            old = self.last_level.get(event.instrument_id)
            if old is not None and trigger.as_decimal() > Decimal(old):
                level = str(trigger)
                row["level_changes"].append(
                    {"at": event.ts_event, "old": old, "new": level, "event": record["event"]}
                )
                self.last_level[event.instrument_id] = level

    def finish(self) -> dict[str, Any]:
        # Stops can close orders after native strategy callback dispatch has stopped.
        for order in self.protector.cache.orders():
            for event in order.events():
                self._order_record(event, order, owner=order.side == OrderSide.BUY)
        for symbol, row in self.rows.items():
            for key in ("order_events", "owner_order_events", "releases", "refusals"):
                row[key].sort(key=lambda event: event["at"])
            counts = Counter(line["text"] for line in self.lines if line["symbol"] == symbol)
            row["line_counts"] = [{"text": text, "count": count} for text, count in counts.items()]
            row["alert_count"] = sum(count for text, count in counts.items() if "ALERT" in text)
            row["gain_lock_events"] = [
                event.copy()
                for event in self.protector.gain_lock_events
                if event["instrument_id"] == f"{symbol}.ALPACA"
            ]
        return {
            "holdings": self.rows,
            "lines": self.lines,
            "unfiled_alert_count": _unfiled_alerts(self.lines),
        }


class _TapeOwner(Strategy):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self.holdings: dict[InstrumentId, Any] = {}
        self.bought: set[InstrumentId] = set()
        self.trace: _Trace

    def on_start(self) -> None:
        for iid in self.holdings:
            self.subscribe_quotes(iid)

    def on_quote(self, quote: Any) -> None:
        iid = quote.instrument_id
        holding = self.holdings[iid]
        if iid not in self.bought and quote.ts_event >= holding["buy_time"]:
            self.bought.add(iid)
            self.submit_order(
                self.order_factory.market(
                    iid, OrderSide.BUY, Quantity.from_int(holding["quantity"])
                )
            )
        self.trace.capture("owner QuoteTick")

    def on_order_event(self, event: Any) -> None:
        self.trace.order(event, owner=True)

    def on_position_event(self, event: Any) -> None:
        self.trace.capture(type(event).__name__)


class _RecordedProtector(OwnerProtector):
    """Observe without replacing callbacks, row arithmetic, emulator or venue behavior."""

    trace: _Trace
    scripted_buys: dict[InstrumentId, Any]
    bought: set[InstrumentId]

    @property
    def cache(self) -> Any:
        return self._observed_cache

    @property
    def clock(self) -> Any:
        return self._observed_clock

    def _say(self, text: str) -> None:
        # A holding's line starts with "<instrument>: ", or, for an ALERT written as
        # "ALERT: <instrument>: ...", has the instrument right after that prefix. A line that
        # names no instrument there is filed under no symbol and counted in the segment's
        # unfiled_alert_count when it is an ALERT, never matched in its body.
        head = text.removeprefix("ALERT: ")
        symbol = next(
            (iid.symbol.value for iid in self.trace.ids if head.startswith(f"{iid}:")), None
        )
        self.trace.lines.append({"at": self.clock.timestamp_ns(), "symbol": symbol, "text": text})

    def request_bars(
        self, bar_type, start=None, end=None, limit=None, client_id=None, params=None, callback=None
    ) -> str:
        self.on_historical_bars(self.cache.bars(bar_type) or [])
        if callback is not None:
            callback("tape-bars")
        return "tape-bars"

    def request_quotes(
        self,
        instrument_id,
        start=None,
        end=None,
        limit=None,
        client_id=None,
        params=None,
        callback=None,
    ) -> str:
        quote = self.cache.quote(instrument_id)
        self.on_historical_quotes([] if quote is None else [quote])
        if callback is not None:
            callback("tape-quotes")
        return "tape-quotes"

    def _plan_holding(self, instrument_id, slot, tick) -> bool:
        planned = super()._plan_holding(instrument_id, slot, tick)
        if planned:
            plan = slot.distance
            now = self.clock.timestamp_ns()
            earliest = _stamp(
                protect.session_start(datetime.fromtimestamp(now // SECOND, UTC)).isoformat()
            )
            bar_type = BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")
            bars = dict(self.history_bars.get(instrument_id, {}))
            bars.update({bar.ts_event: bar for bar in self.cache.bars(bar_type) or []})
            stamps = [at for at in sorted(bars) if earliest <= at and at + 60 * SECOND <= now][
                -protect.ATR_BARS :
            ]
            state, count, _ = self._holding_state(instrument_id, slot)
            self.trace.rows[instrument_id.symbol.value]["arming"].append(
                {
                    "at": now,
                    "quote_at": tick.ts_event,
                    "level": str(slot.level),
                    "distance": str(plan.distance),
                    "limit_offset": str(slot.limit_offset),
                    "guard_count": count,
                    "state": state,
                    "inputs": {
                        "row": self.settings.row,
                        "bid": str(tick.bid_price),
                        "ask": str(tick.ask_price),
                        "price": str(plan.price),
                        "bars": plan.bars,
                        "bar_stamps": stamps,
                        "atr": _text(plan.plan.atr),
                        "spread": _text(plan.plan.spread),
                        "floor": _text(plan.floor),
                        "planned": str(plan.planned),
                        "bound": plan.plan.bound,
                        "fallback": plan.fallback,
                        "flat": plan.flat,
                        "source": plan.source(),
                    },
                }
            )
            self.trace.capture("Arming")
        return planned

    def on_start(self) -> None:
        self._observed_cache = super().cache
        self._observed_clock = super().clock
        super().on_start()
        for iid in self.scripted_buys:
            self.subscribe_quotes(iid)
        self.trace.capture("Start")

    def on_quote(self, quote) -> None:
        iid = quote.instrument_id
        holding = self.scripted_buys.get(iid)
        if holding is not None and iid not in self.bought and quote.ts_event >= holding["buy_time"]:
            self.bought.add(iid)
            self.submit_order(
                self.order_factory.market(
                    iid, OrderSide.BUY, Quantity.from_int(holding["quantity"])
                )
            )
        super().on_quote(quote)
        self.trace.capture("QuoteTick")

    def on_bar(self, bar) -> None:
        super().on_bar(bar)
        self.trace.capture("Bar")

    def _on_step(self, event) -> None:
        super()._on_step(event)
        self.trace.capture("TimeEvent")

    def on_order_event(self, event) -> None:
        # Native dispatch already called the specific callback, including on_order_released.
        super().on_order_event(event)
        order = self.cache.order(event.client_order_id)
        self.trace.order(event, owner=order is not None and order.side == OrderSide.BUY)

    def on_stop(self) -> None:
        self.trace.capture("BeforeStop")
        super().on_stop()
        self.trace.capture("Stop")


def _venue(arm: str) -> dict[str, Any]:
    return {
        **VENUE_SETTINGS,
        "bar_execution": False,
        "trade_execution": arm == "tape",
        "liquidity_consumption": arm == "tape",
        "queue_position": arm == "tape",
        "fill_model": None,
        "latency_model": None,
        "use_reduce_only": True,
        "reject_stop_orders": True,
        "support_contingent_orders": True,
        "use_position_ids": True,
        "use_message_queue": True,
    }


def _records(frame: Any) -> list[dict[str, Any]]:
    """Native report rows, retaining their index and nanosecond timestamp precision."""
    return json.loads(
        frame.reset_index().to_json(orient="records", date_format="iso", date_unit="ns")
    )


def _segment(
    spec: dict[str, Any], instruments: list[Any], batch: dict[str, Any], index: int
) -> dict[str, Any]:
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"),
            bypass_logging=True,
            cache=CacheConfig(bar_capacity=max(CacheConfig().bar_capacity, len(batch["bars"]))),
        )
    )
    try:
        engine.add_venue(
            venue=Venue("ALPACA"),
            starting_balances=[Money.from_str(STARTING_BALANCE)],
            fee_model=MakerTakerFeeModel(
                maker_rate=FEE_RATES["maker_rate"], taker_rate=FEE_RATES["taker_rate"]
            ),
            **_venue(spec["fill_arm"]),
        )
        for instrument in instruments:
            engine.add_instrument(instrument)
        protector = _RecordedProtector(_config(spec, index))
        protector.scripted_buys = spec["holdings"] if spec["booking"] == "own" else {}
        protector.bought = set()
        trace = _Trace(protector, spec["holdings"], batch["start"])
        protector.trace = trace
        if spec["booking"] == "foreign":
            owner = _TapeOwner(
                StrategyConfig(strategy_id=StrategyId("EXTERNAL"), oms_type=OmsType.NETTING)
            )
            owner.holdings, owner.trace = spec["holdings"], trace
            engine.add_strategy(owner)
        engine.add_strategy(protector)
        for kind in ("bars", "quotes", "trades"):
            if batch[kind]:
                engine.add_data(batch[kind])
        engine.run(end=batch["end"] - 1, streaming=True)
        engine.end()
        path = spec["level_book_path"]
        book = json.loads(path.read_text()) if path.exists() else None
        return {
            "index": index + 1,
            "start": batch["start"],
            "end": batch["end"],
            "data_counts": {kind: len(batch[kind]) for kind in ("bars", "quotes", "trades")},
            **trace.finish(),
            "book": book,
            "engine_reports": {
                "orders": _records(engine.generate_orders_report()),
                "order_fills": _records(engine.generate_order_fills_report()),
                "fills": _records(engine.generate_fills_report()),
                "positions": _records(engine.generate_positions_report()),
            },
        }
    finally:
        engine.dispose()


def _report() -> dict[str, Any]:
    return {
        "introduction": INTRODUCTION,
        "status": "refused",
        "segments": [],
        "refusals": [],
        "limits": list(LIMITS),
        "limitations": LIMITATIONS,
    }


def replay(spec: Any, *, base: Path | None = None) -> dict[str, Any]:
    """Validate and read all segments before constructing an engine; return a JSON-ready report."""
    report = _report()
    try:
        checked = _checked(spec, Path.cwd() if base is None else base)
    except (ValueError, TypeError, ArithmeticError, OSError) as exc:
        report["refusals"].append({"source": "spec", "reason": str(exc)})
        return report
    report["run_label"] = checked["run_label"]
    report["row"] = checked["row"]
    report["fill_arm"] = checked["fill_arm"]
    report["booking"] = checked["booking"]
    if checked["booking"] == "own":
        report["introduction"] = OWN_INTRODUCTION
        report["counted_proof"] = False
        report["equivalence"] = (
            "UNVERIFIED: the fixed opening describes the experiment's hypothesis, not a result. "
            "Compare arming, level changes, release and lines against an independent foreign run. "
            "The base protector discovers only EXTERNAL inventory; its custody is unchanged here."
        )
        report["limits"][0] = (
            "The scripted long is bought again under the protector's ID at the first eligible "
            "quote of each fresh segment. It is not reconciled inventory."
        )
        report["limits"][5] = (
            "Own booking is only a measurement and changes where the buy is submitted. "
            "It never changes custody, the row, gain lock, guard, matching or chase. "
            "No protective fill or equivalence is presumed."
        )
    report["venue"] = {
        "name": "ALPACA",
        "trader_id": "TESTER-001",
        "profile": "alpaca-paper-sim-v1",
        "departures": (
            ["touch: trade_execution=False, liquidity_consumption=False, queue_position=False"]
            if checked["fill_arm"] == "touch"
            else []
        ),
        "starting_balances": [STARTING_BALANCE],
        **{
            key: value.name if hasattr(value, "name") else value
            for key, value in _venue(checked["fill_arm"]).items()
        },
        "fee_rates": {key: str(value) for key, value in FEE_RATES.items()},
        "fill_model": "DefaultFillModel",
        "fee_model": "MakerTakerFeeModel",
        "latency_ns": 0,
    }
    try:
        _config(checked, 0)  # F2's refusal belongs to OwnerProtectorConfig, before any engine.
    except (ValueError, TypeError) as exc:
        report["refusals"].append({"source": "OwnerProtectorConfig", "reason": str(exc)})
        return report
    try:
        instruments, batches = _load(checked)
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        report["refusals"].append({"source": "catalog", "reason": str(exc)})
        return report
    for index, batch in enumerate(batches):
        try:
            report["segments"].append(_segment(checked, instruments, batch, index))
        except Exception as exc:  # noqa: BLE001 - keep a named failed run in the report.
            report["status"] = "failed"
            report["refusals"].append(
                {"source": f"segment {index + 1}", "reason": f"{type(exc).__name__}: {exc}"}
            )
            return report
    report["status"] = "completed"
    return report


def _table(rows: list[dict[str, Any]], keys: list[str]) -> str:
    def cell(value: Any) -> str:
        text = (
            json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        )
        return text.replace("|", "\\|").replace("\n", "<br>")

    return "\n".join(
        [
            "| " + " | ".join(keys) + " |",
            "| " + " | ".join("---" for _ in keys) + " |",
            *("| " + " | ".join(cell(row.get(key)) for key in keys) + " |" for row in rows),
        ]
    )


def render_markdown(report: dict[str, Any]) -> str:
    """The same evidence, without truncating repeated lines, refusals or holding states."""
    parts = [
        report["introduction"],
        report.get(
            "equivalence", "Foreign booking: observe releases and refusals, not sale fills."
        ),
        f"Status: {report['status']}",
        "## Venue",
        _table(
            [{"setting": k, "value": v} for k, v in report.get("venue", {}).items()],
            ["setting", "value"],
        ),
        "## Harness limits",
        "\n".join(f"- {limit}" for limit in report["limits"]),
    ]
    if report["refusals"]:
        parts.extend(["## Run refusals", _table(report["refusals"], ["source", "reason"])])
    for segment in report["segments"]:
        parts.extend(
            [
                f"## Segment {segment['index']}: [{segment['start']}, {segment['end']})",
                f"Data counts: {json.dumps(segment['data_counts'], sort_keys=True)}",
                f"ALERT lines naming no holding: {segment['unfiled_alert_count']}",
            ]
        )
        parts.append("### Native engine reports")
        for name, records in segment["engine_reports"].items():
            keys = list(dict.fromkeys(key for record in records for key in record))
            parts.extend([f"#### {name}", _table(records, keys) if records else "None observed."])
        for symbol, row in segment["holdings"].items():
            parts.extend([f"### {symbol}", f"Observed ALERT count: {row['alert_count']}"])
            for name in (
                "arming",
                "level_changes",
                "releases",
                "refusals",
                "gain_lock_events",
                "order_events",
                "owner_order_events",
                "owner_fills",
                "states",
                "line_counts",
            ):
                records = row[name]
                keys = list(dict.fromkeys(key for record in records for key in record))
                parts.extend(
                    [f"#### {name}", _table(records, keys) if records else "None observed."]
                )
        parts.extend(
            [
                "### Every protector line",
                _table(segment["lines"], ["at", "symbol", "text"]),
                "### Book after engine.end()",
                "```json\n" + json.dumps(segment["book"], indent=2) + "\n```",
            ]
        )
    parts.append(LIMITATIONS)
    return "\n\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text())
        report = replay(spec, base=args.spec.resolve().parent)
    except (OSError, ValueError) as exc:
        report = _report()
        report["refusals"].append({"source": "spec", "reason": str(exc)})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    args.report.with_suffix(".md").write_text(render_markdown(report))
    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
