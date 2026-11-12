"""The risk limits of `canon_trading.paper_node`, proved offline on upstream's simulated venue
(ADR 0037, ADR 0049).

No test here opens a network connection or reads a credential. The venue is NautilusTrader's own
`BacktestEngine` with a simulated `ALPACA` venue, a cash account with $100,000 and no fees, the settings of
`canon_trading.backtest.simulated_alpaca_venue` written out for the low-level API. Its risk engine is
the same engine a live node runs, and it takes the same keywords (`risk_limits`), so what is denied here is what the paper node denies. The quotes are invented,
two cents wide around $750, one every 100 ms; the instrument is built with the adapter's constants.

What upstream documents and this file checks (`crates/risk/src/engine/mod.rs` and
`crates/model/src/events/order/denied_reason.rs` at the pinned commit; concepts/execution "Risk engine"):
a submit over `max_order_submit_rate` gets `OrderDenied` with reason `RATE_LIMIT_EXCEEDED` and never
reaches the venue; a modify over `max_order_modify_rate` gets `OrderModifyRejected` with "Exceeded
MAX_ORDER_MODIFY_RATE"; an order over `max_notional_per_order` gets `OrderDenied` with
`NOTIONAL_EXCEEDS_MAX_PER_ORDER: max=..., notional=...`. A denied order does not use up the rate.
"""

import inspect
import math
from decimal import Decimal
from itertools import pairwise

from canon_alpaca.constants import PRICE_INCREMENT
from canon_alpaca.constants import PRICE_PRECISION
from canon_alpaca.constants import USD
from canon_alpaca.constants import VENUE
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.backtest import BacktestEngineConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.live import LiveRiskEngineConfig
from nautilus_trader.model import AccountType
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import QuoteTick
from nautilus_trader.model import Symbol
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.risk import RiskEngineConfig
from nautilus_trader.trading import Strategy

from canon_trading.paper_node import EMA_CROSS
from canon_trading.paper_node import EMA_CROSS_ID
from canon_trading.paper_node import MAX_NOTIONAL_PER_ORDER_USD
from canon_trading.paper_node import MAX_ORDER_MODIFY_RATE
from canon_trading.paper_node import MAX_ORDER_SUBMIT_RATE
from canon_trading.paper_node import REQUEST_CEILING
from canon_trading.paper_node import RiskLimits
from canon_trading.paper_node import ema_cross_config
from canon_trading.paper_node import ema_cross_paper_node
from canon_trading.paper_node import risk_limits

SPY = InstrumentId.from_str("SPY.ALPACA")
T0 = 1_762_790_400_000_000_000  # 2025-11-10T16:00:00Z
STEP_NS = 100_000_000
FAR_BELOW = Price.from_str("1.00")
"""A resting buy far below the market: it is accepted and never fills, and costs a dollar of notional."""


def quotes(count: int, mid=lambda i: 750.0) -> list[QuoteTick]:
    return [
        QuoteTick(
            SPY,
            Price(mid(i) - 0.01, 4),
            Price(mid(i) + 0.01, 4),
            Quantity.from_int(100),
            Quantity.from_int(100),
            T0 + i * STEP_NS,
            T0 + i * STEP_NS,
        )
        for i in range(count)
    ]


def engine(limits: RiskLimits) -> BacktestEngine:
    eng = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("TESTER-001"), risk_engine=RiskEngineConfig(**limits)
        )
    )
    eng.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
    )
    eng.add_instrument(
        Equity(
            instrument_id=SPY,
            raw_symbol=Symbol("SPY"),
            currency=USD,
            price_precision=PRICE_PRECISION,
            price_increment=PRICE_INCREMENT,
            ts_event=0,
            ts_init=0,
        )
    )
    return eng


class Script(Strategy):
    """Run `actions[n]` on the n-th quote (from 1); each action gets the strategy."""

    def __init__(self, actions: dict) -> None:
        super().__init__()
        self.actions = actions
        self.seen = 0

    def on_start(self) -> None:
        self.subscribe_quotes(SPY)

    def on_quote(self, quote) -> None:  # the quote only times the script
        self.seen += 1
        action = self.actions.get(self.seen)
        if action is not None:
            action(self)

    def buy_far_below(self, count: int = 1) -> None:
        for _ in range(count):
            self.submit_order(
                self.order_factory.limit(
                    SPY,
                    OrderSide.BUY,
                    Quantity.from_int(1),
                    FAR_BELOW,
                    time_in_force=TimeInForce.DAY,
                )
            )


def outcomes(eng: BacktestEngine) -> list[tuple[str, str]]:
    """Each order's last event and its reason, in submit order (the client order ID counts up)."""
    result = []
    for order in sorted(
        eng.cache.orders(), key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1])
    ):
        last = order.events()[-1]
        result.append((type(last).__name__, str(getattr(last, "reason", ""))))
    return result


def test_the_configs_report_the_values_set() -> None:
    for config in (
        LiveRiskEngineConfig(**risk_limits(SPY)),
        RiskEngineConfig(**risk_limits(SPY)),
    ):
        assert config.bypass is False
        assert config.max_order_submit_rate == MAX_ORDER_SUBMIT_RATE == "180/00:01:00"
        assert config.max_order_modify_rate == MAX_ORDER_MODIFY_RATE == "180/00:01:00"
        assert {k: Decimal(str(v)) for k, v in config.max_notional_per_order.items()} == {
            "SPY.ALPACA": MAX_NOTIONAL_PER_ORDER_USD
        }
    run = LiveRiskEngineConfig(**risk_limits(SPY, max_order_submit_rate="1/00:05:00"))
    assert run.max_order_submit_rate == "1/00:05:00"
    assert run.max_order_modify_rate == MAX_ORDER_MODIFY_RATE


def test_submits_over_the_rate_are_denied_before_the_venue() -> None:
    """181 submits at one instant: 180 reach the venue, the 181st is denied. 70 s later the window has
    moved and a submit passes again."""
    eng = engine(risk_limits(SPY))
    eng.add_data(quotes(800))
    eng.add_strategy(Script({1: lambda s: s.buy_far_below(181), 701: lambda s: s.buy_far_below()}))
    eng.run()
    result = outcomes(eng)
    eng.dispose()
    assert result[:180] == [("OrderAccepted", "")] * 180
    assert result[180] == ("OrderDenied", "RATE_LIMIT_EXCEEDED")
    assert result[181] == ("OrderAccepted", "")


def test_seven_mover_slots_at_full_turnover_are_never_throttled_by_the_engine() -> None:
    """The lead's decision of 2026-11-11: the engine's throttle can never be what refuses a sale. Seven
    slots at Mover's real cadence (entry, sale, buy-back and flat ending, and a re-priced sale every
    six seconds: 14 submits a slot) send 98 submits in one minute, spread over it; none is denied.
    What holds entries back is the request count: Mover's soft line sits under the window's request
    ceiling, which is the submit limit, and every submit is at least one request."""
    from canon_trading import mover
    from canon_trading import session_window

    per_slot, slots = 14, 7
    eng = engine(risk_limits(SPY))
    eng.add_data(quotes(700))
    # a submit for every slot every 4.2 s, 14 times: 98 within 55 s of quotes 100 ms apart
    steps = {1 + i * 42: lambda s: s.buy_far_below(slots) for i in range(per_slot)}
    eng.add_strategy(Script(steps))
    eng.run()
    result = outcomes(eng)
    eng.dispose()
    assert len(result) == slots * per_slot
    assert result == [("OrderAccepted", "")] * (slots * per_slot)
    soft_line = inspect.signature(mover.MoverConfig).parameters["soft_line"].default
    submits = int(MAX_ORDER_SUBMIT_RATE.split("/")[0])
    assert soft_line < session_window.REQUEST_CEILING == REQUEST_CEILING == submits


def test_the_paper_runs_budget_lets_one_order_through() -> None:
    eng = engine(risk_limits(SPY, max_order_submit_rate="1/00:05:00"))
    eng.add_data(quotes(20))
    eng.add_strategy(Script({1: lambda s: s.buy_far_below(3)}))
    eng.run()
    result = outcomes(eng)
    eng.dispose()
    assert result == [("OrderAccepted", "")] + [("OrderDenied", "RATE_LIMIT_EXCEEDED")] * 2


def test_an_order_over_the_notional_cap_is_denied_and_does_not_use_the_rate() -> None:
    """The paper test's probe: a one-share sell limit at $1,000.01, a cent over the cap. It is denied with
    the configured cap in the reason, and the one order the run's budget allows still goes through."""
    probe_price = Price.from_str("1000.01")

    def probe(s: Script) -> None:
        s.submit_order(
            s.order_factory.limit(
                SPY,
                OrderSide.SELL,
                Quantity.from_int(1),
                probe_price,
                time_in_force=TimeInForce.DAY,
            )
        )
        s.buy_far_below()

    eng = engine(risk_limits(SPY, max_order_submit_rate="1/00:05:00"))
    eng.add_data(quotes(20))
    eng.add_strategy(Script({1: probe}))
    eng.run()
    result = outcomes(eng)
    eng.dispose()
    assert result == [
        ("OrderDenied", "NOTIONAL_EXCEEDS_MAX_PER_ORDER: max=1000.00 USD, notional=1000.01 USD"),
        ("OrderAccepted", ""),
    ]


def test_modifies_over_the_rate_are_rejected() -> None:
    """181 modifies of one resting order at one instant: 180 apply, the 181st is rejected."""

    def modify(s: Script) -> None:
        [order] = s.cache.orders_open(instrument_id=SPY)
        for i in range(181):
            s.modify_order(order.client_order_id, price=Price.from_str(f"{1.01 + i / 100:.2f}"))

    eng = engine(risk_limits(SPY))
    eng.add_data(quotes(30))
    eng.add_strategy(Script({1: lambda s: s.buy_far_below(), 10: modify}))
    eng.run()
    [order] = eng.cache.orders()
    names = [type(e).__name__ for e in order.events()]
    reasons = [str(e.reason) for e in order.events() if type(e).__name__ == "OrderModifyRejected"]
    eng.dispose()
    assert names.count("OrderUpdated") == 180, names
    assert reasons == ["Exceeded MAX_ORDER_MODIFY_RATE"], names


def test_ema_cross_trades_one_share_an_order_under_the_limits() -> None:
    """Upstream's `EmaCross`, as the paper node configures it, over a slow sine of mids: it waits for 50
    quotes, then sends one market order of one share on each cross, alternating sides. Its first cross is
    a sell, which the simulated cash venue rejects as a short sale ("Short selling not permitted on a CASH
    account", observed), so the next buy opens a long and every later order alternates. Alpaca's paper
    account allowed the short instead (ADR 0037)."""

    eng = engine(risk_limits(SPY))
    eng.add_data(quotes(2000, mid=lambda i: 750 + 2 * math.sin(i / 40.0)))
    eng.add_builtin_strategy(EMA_CROSS, ema_cross_config(SPY))
    eng.run()
    orders = sorted(
        eng.cache.orders(strategy_id=EMA_CROSS_ID),
        key=lambda o: int(o.client_order_id.value.rsplit("-", 1)[1]),
    )
    sides = [o.side for o in orders]
    statuses = [o.status.name for o in orders]
    quantities = {int(o.quantity) for o in orders}
    eng.dispose()
    assert len(orders) >= 4, statuses
    assert quantities == {1}
    assert all(a != b for a, b in pairwise(sides)), sides
    assert statuses[0] == "REJECTED" and set(statuses[1:]) == {"FILLED"}, statuses


def test_the_paper_node_builds_without_connecting() -> None:
    """Building the node registers the clients and the strategy and opens no connection."""
    node = ema_cross_paper_node(SPY)
    try:
        assert node.trader_id == TraderId("TESTER-001")
        assert not node.is_running
    finally:
        node.dispose()


def test_the_paper_node_wires_its_limits_strategy_cap_and_reconciliation(monkeypatch) -> None:
    """Review finding: the offline tests could not fail if the node stopped wiring its limits or strategy
    in. Each piece is built through a module function the node must call; a spy on each records it."""
    from canon_trading import paper_node

    calls: list[str] = []
    for name in ("risk_limits", "ema_cross_config", "exec_engine_config", "position_cap_config"):
        real = getattr(paper_node, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(paper_node, name, spy)
    node = paper_node.ema_cross_paper_node(SPY)
    node.dispose()
    assert sorted(calls) == [
        "ema_cross_config",
        "exec_engine_config",
        "position_cap_config",
        "risk_limits",
    ]


def test_the_paper_node_scopes_start_up_reconciliation() -> None:
    """Review finding: with the lookback unset, start-up reconciliation infers fills from the account's
    old filled orders and holds a position Alpaca does not (ADR 0037). It is scoped to the symbol, as
    upstream's Lighter tutorial scopes it, and to a minute of history, as the adapter's paper tests do."""
    from canon_trading.paper_node import exec_engine_config

    config = exec_engine_config(SPY)
    assert config.reconciliation_instrument_ids == ["SPY.ALPACA"]
    assert config.reconciliation_lookback_mins == 1
