"""The movers ranking actor on a `BacktestEngine`: what it publishes on the bus, and when it publishes nothing.

Contract: `canon_trading.movers_rank_actor.MoversRankActor` (ADR 0107), a `DataActor` added to upstream's
`BacktestEngine` with `add_actor`, beside a second `DataActor` that reads the signal with
`subscribe_signal` and `on_signal`, as upstream's own `examples/backtest/callback_borrow_reentrancy.py` pairs
them at the pin (`nautilus-trader@d53849b216e2`). Instruments come from an Alpaca asset payload through
`canon_alpaca.providers.parse_equity` and bars through `canon_alpaca.data.parse_bar`, as the node gets them
(`tests/conftest.py`). The reference reader is a stub and the baseline is a file written through
`movers_rank.write_baselines` into `tmp_path`, as the nightly job writes it: the actor's own REST reads are
tested against stub clients with alpaca-py's own response models (`BarSet`, `CorporateActionsSet`).

The prices and volumes are invented. Antigravity's review findings 1 to 7 are each taken by a test here:
a behaviour test that failed before the fix, or, where the code was already right, by an assertion that
fails when the behaviour it guards is mutated.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from alpaca.data.enums import Adjustment
from alpaca.data.enums import CorporateActionsType
from alpaca.data.enums import DataFeed
from alpaca.data.models import Bar as AlpacaBar
from alpaca.data.models import BarSet
from alpaca.data.models.corporate_actions import CorporateActionsSet
from alpaca.data.models.corporate_actions import ForwardSplit
from alpaca.data.models.corporate_actions import ReverseSplit
from alpaca.data.requests import CorporateActionsRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.models import Asset
from canon_alpaca.constants import STREAM_FEEDS
from canon_alpaca.data import named_feed
from canon_alpaca.data import parse_bar
from canon_alpaca.providers import parse_equity
from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.backtest import BacktestEngineConfig
from nautilus_trader.common import DataActor
from nautilus_trader.config import LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType
from nautilus_trader.model import ActorId
from nautilus_trader.model import BarType
from nautilus_trader.model import Currency
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import InstrumentStatus
from nautilus_trader.model import MarketStatusAction
from nautilus_trader.model import Money
from nautilus_trader.model import OmsType
from nautilus_trader.model import Symbol
from nautilus_trader.model import Venue

from canon_trading import movers_rank
from canon_trading import movers_rank_actor as actor_module
from canon_trading.movers_rank_actor import MoversRankActor
from canon_trading.movers_rank_actor import MoversRankActorConfig
from canon_trading.movers_rank_actor import ReferenceRead
from canon_trading.movers_rank_actor import read_references
from canon_trading.movers_rank_actor import read_splits
from canon_trading.movers_rank_actor import reference_cutoff
from canon_trading.movers_rank_actor import trade_date
from canon_trading.movers_rank_signal import SIGNAL_NAME
from canon_trading.movers_rank_signal import decode_ranking

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence
    from pathlib import Path

    from nautilus_trader.common import Signal
    from nautilus_trader.common import TimeEvent
    from nautilus_trader.model import Bar
    from nautilus_trader.model import ClientId

    from canon_trading.movers_rank_actor import ReferenceReader

NEW_YORK = ZoneInfo("America/New_York")
VENUE = Venue("ALPACA")
USD = Currency.from_str("USD")
SESSION = date(2026, 11, 11)
PREVIOUS = date(2026, 11, 10)
BASELINE_SESSIONS = tuple(date(2026, 11, d) for d in (4, 5, 6, 9, 10))
"""The five trading days before `SESSION`, the baseline's lookback."""
OPEN = datetime(2026, 11, 11, 9, 30, tzinfo=NEW_YORK)
ASSET: dict[str, object] = {
    "class": "us_equity",
    "exchange": "NASDAQ",
    "fractionable": True,
    "id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "marginable": True,
    "shortable": True,
    "status": "active",
    "tradable": True,
    "easy_to_borrow": True,
    "borrow_status": "easy_to_borrow",
}
NAMES = {
    "UPA": "Up A Inc. Common Stock",
    "UPB": "Up B Corp. American Depositary Shares",
    "FLAT": "Flat Co. Common Stock",
    "WRNT": "Up Warrant Corp. Warrants",
    "SPLT": "Split Co. Common Stock",
}
REFERENCE = 10.0
"""Every symbol's reference close."""
BASELINE_SEGMENT_DOLLARS = 1_000.0
"""The baseline's cumulative dollar volume added per 15-minute segment, for every symbol."""


def ns(t: datetime) -> int:
    return int(t.timestamp() * 1_000_000_000)


def equity(symbol: str) -> Equity:
    return parse_equity(Asset.model_validate({**ASSET, "symbol": symbol, "name": NAMES[symbol]}), 0)


def sim_equity(symbol: str) -> Equity:
    """The same symbol on a venue that is not Alpaca's, as another adapter's instrument in one cache."""
    alpaca = equity(symbol)
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue("SIM")),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=alpaca.price_precision,
        price_increment=alpaca.price_increment,
        ts_event=0,
        ts_init=0,
        info={**ASSET, "name": NAMES[symbol]},
    )


def status(instrument: Equity, action: MarketStatusAction, at: datetime) -> InstrumentStatus:
    """One `InstrumentStatus` event for `instrument`, available and received at `at`."""
    return InstrumentStatus(instrument.id, action, ns(at), ns(at))


def minute_bar(instrument: Equity, at: datetime, close: float, volume: int = 10_000) -> Bar:
    """One Alpaca minute bar opening at `at`, through the adapter's `parse_bar`."""
    bar_type = BarType.from_str(f"{instrument.id}-1-MINUTE-LAST-EXTERNAL")
    payload = {
        "t": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": volume,
        "n": 10,
        "vw": close,
    }
    return parse_bar(AlpacaBar(instrument.id.symbol.value, payload), bar_type, instrument)


CLOSES = {"UPA": 15.0, "UPB": 12.0, "FLAT": 10.0, "WRNT": 30.0, "SPLT": 40.0}
"""Each symbol's close in every minute: WRNT and SPLT move most, and each is one the filters take out."""

SPLIT_RECORD: dict[str, object] = {
    "id": "a7932b15-f816-4f83-921e-998b524487b1",
    "symbol": "SPLT",
    "cusip": "000000000",
    "new_rate": 4.0,
    "old_rate": 1.0,
    "process_date": SESSION.isoformat(),
    "ex_date": SESSION.isoformat(),
}
"""SPLT's forward split since the reference close, in alpaca-py's `CorporateActionsSet` shape."""
SPLIT_ACTION = ForwardSplit.model_validate(
    {"corporate_action_type": "forward_splits", **SPLIT_RECORD}
)
DEFAULT_SPLITS: tuple[ForwardSplit | ReverseSplit, ...] = (SPLIT_ACTION,)
"""The splits the default reference reader returns: SPLT is exactly the name the split filter takes out."""


def market(minutes: int, start: datetime = OPEN, closes: Mapping[str, float] = CLOSES) -> list[Bar]:
    instruments = {s: equity(s) for s in closes}
    return [
        minute_bar(instruments[s], start + timedelta(minutes=m), c)
        for m in range(minutes)
        for s, c in closes.items()
    ]


def stub_reader(
    splits: Sequence[ForwardSplit | ReverseSplit] = DEFAULT_SPLITS,
    calls: list[tuple[date, date]] | None = None,
) -> ReferenceReader:
    def read(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        if calls is not None:
            calls.append((cutoff, session))
        references = {s: movers_rank.Reference(close=REFERENCE, session=PREVIOUS) for s in symbols}
        return ReferenceRead(
            cutoff=cutoff,
            references=references,
            splits=movers_rank.SplitRead(start=PREVIOUS, end=session, actions=tuple(splits)),
            read_ns=0,
        )

    return read


BASELINE_CUMULATIVE = tuple(BASELINE_SEGMENT_DOLLARS * (i + 1) for i in range(movers_rank.SEGMENTS))
"""Every symbol's cumulative dollar volume at each 15-minute segment's end, from 04:00 New York."""


def write_stub_baseline(
    tmp_path: Path, session: date = SESSION, header: dict[str, object] | None = None
) -> Path:
    """The baseline for `session`, written through `movers_rank.write_baselines` as the nightly job writes
    it: the lookback's five earlier sessions, each name trading in every segment of each, so every name
    is established (ADR 0087's definition) and the actor loads a real file for the trade date it
    computes."""
    path = tmp_path / "baselines.json"
    movers_rank.write_baselines(
        path,
        session,
        {
            symbol: movers_rank.Baseline(
                cumulative=BASELINE_CUMULATIVE,
                sessions=BASELINE_SESSIONS,
                traded_sessions=(movers_rank.BASELINE_SESSIONS,) * movers_rank.SEGMENTS,
            )
            for symbol in NAMES
        },
        header=header,
    )
    return path


class Reader(DataActor):
    """Reads `movers-rank` as the strategy will: `subscribe_signal` in `on_start`, then `on_signal`."""

    def __init__(self) -> None:
        super().__init__()
        self.signals: list[tuple[str, str, int]] = []

    def on_start(self) -> None:
        self.subscribe_signal(SIGNAL_NAME)

    def on_signal(self, signal: Signal) -> None:
        self.signals.append((signal.name, signal.value, signal.ts_event))


def run(
    bars: Sequence[object],
    *,
    baseline_path: Path,
    reader: ReferenceReader | None = None,
    instruments: Sequence[Equity] | None = None,
    before: Sequence[DataActor] = (),
    inline_reads: bool = True,
    feed: str = "sip",
    max_data_age_secs: float | None = None,
    rank_without_baseline: bool = False,
) -> tuple[MoversRankActor, Reader]:
    engine = BacktestEngine(
        BacktestEngineConfig(logging=LoggerConfig(bypass_logging=True), run_analysis=False)
    )
    engine.add_venue(
        VENUE,
        OmsType.NETTING,
        AccountType.CASH,
        [Money(100_000, USD)],
        fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
    )
    for instrument in [equity(symbol) for symbol in NAMES] if instruments is None else instruments:
        if instrument.id.venue != VENUE:
            engine.add_venue(
                instrument.id.venue,
                OmsType.NETTING,
                AccountType.CASH,
                [Money(100_000, USD)],
                fee_model=MakerTakerFeeModel(maker_rate=Decimal(0), taker_rate=Decimal(0)),
            )
        engine.add_instrument(instrument)
    engine.add_data(list(bars))
    subscriber = Reader()
    engine.add_actor(subscriber)
    for helper in before:
        engine.add_actor(helper)
    age_kwargs: dict[str, float] = (
        {} if max_data_age_secs is None else {"max_data_age_secs": max_data_age_secs}
    )
    actor = MoversRankActor(
        MoversRankActorConfig(
            feed=feed,
            order_cap=1_000.0,
            baseline_path=str(baseline_path),
            reference_reader=reader or stub_reader(),
            inline_reads=inline_reads,
            **age_kwargs,
            rank_without_baseline=rank_without_baseline,
        )
    )
    engine.add_actor(actor)
    try:
        engine.run()
    finally:
        engine.dispose()
    return actor, subscriber


def published(subscriber: Reader) -> list[tuple[str, ...]]:
    return [
        tuple(e.instrument_id.symbol.value for e in decode_ranking(v))
        for _, v, _ in subscriber.signals
    ]


@pytest.mark.parametrize("feed", ["sip", "boats"])
def test_task_m_gap_3_subscriptions_name_the_feed_and_wildcard_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feed: str
) -> None:
    """Capture the actor's real subscription boundary and use the adapter's feed parser."""
    bar_commands: list[tuple[BarType | Sequence[BarType], dict | None]] = []
    status_commands: list[tuple[InstrumentId | Sequence[InstrumentId], dict | None]] = []

    def subscribe_bars(
        actor: MoversRankActor,
        bar_type: BarType | Sequence[BarType],
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        bar_commands.append((bar_type, params))
        DataActor.subscribe_bars(actor, bar_type, client_id=client_id, params=params)

    def subscribe_statuses(
        actor: MoversRankActor,
        instrument_id: InstrumentId | Sequence[InstrumentId],
        client_id: ClientId | None = None,
        params: dict | None = None,
    ) -> None:
        status_commands.append((instrument_id, params))
        DataActor.subscribe_instrument_status(
            actor, instrument_id, client_id=client_id, params=params
        )

    monkeypatch.setattr(MoversRankActor, "subscribe_bars", subscribe_bars)
    monkeypatch.setattr(MoversRankActor, "subscribe_instrument_status", subscribe_statuses)
    _, subscriber = run(market(4), baseline_path=write_stub_baseline(tmp_path), feed=feed)
    assert subscriber.signals
    assert len(bar_commands) == len(status_commands) == 1
    bar_types, bar_params = bar_commands[0]
    instrument_ids, status_params = status_commands[0]
    assert bar_params == {"feed": feed, "wildcard": True, "updated_bars": True}
    assert status_params == {"feed": feed, "wildcard": True}
    for params in (bar_params, status_params):
        parsed, explicit = named_feed(params, STREAM_FEEDS, "subscriptions")
        assert (parsed.value, explicit) == (feed, True)
    assert isinstance(bar_types, list)
    assert {str(bar_type) for bar_type in bar_types} == {
        f"{symbol}.ALPACA-1-MINUTE-LAST-EXTERNAL" for symbol in NAMES
    }
    assert isinstance(instrument_ids, list)
    assert {str(instrument_id) for instrument_id in instrument_ids} == {
        f"{symbol}.ALPACA" for symbol in NAMES
    }


def test_task_m_gap_4_a_fresh_symbol_does_not_refresh_a_stale_name(tmp_path: Path) -> None:
    bars = [*market(1, closes={"UPA": 15.0}), *market(8, closes={"UPB": 12.0})]
    actor, subscriber = run(bars, baseline_path=write_stub_baseline(tmp_path))
    rankings = published(subscriber)
    assert rankings
    assert "UPA" in rankings[0], "UPA was eligible while its bar was fresh"
    assert rankings[-1] == ("UPB",), actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["rows"] == len(NAMES)
    assert actor.last_record["stale_symbols"] == ["UPA"]


@pytest.mark.parametrize(("bound", "ranked_names"), [(10.0, ("UPB",)), (600.0, ("UPB", "UPA"))])
def test_task_m_gap_4_the_actor_applies_and_records_its_age_setting(
    tmp_path: Path, bound: float, ranked_names: tuple[str, ...]
) -> None:
    actor, subscriber = run(
        [*market(1, closes={"UPA": 15.0}), *market(8, closes={"UPB": 12.0})],
        baseline_path=write_stub_baseline(tmp_path),
        max_data_age_secs=bound,
    )
    assert published(subscriber)[-1] == ranked_names, actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["max_data_age_secs"] == bound


@pytest.mark.parametrize("bound", [0.0, -1.0, float("nan"), float("inf")])
def test_task_m_gap_4_the_config_refuses_an_invalid_age_setting(
    tmp_path: Path, bound: float
) -> None:
    with pytest.raises(ValueError, match="data age"):
        MoversRankActorConfig(
            feed="sip", order_cap=1_000.0, baseline_path=str(tmp_path), max_data_age_secs=bound
        )


@pytest.mark.parametrize(
    ("start_second", "first_minute", "first_second"),
    [(1, 0, 5), (5, 1, 5), (10, 1, 5), (59, 1, 5)],
)
def test_task_m_gap_9_first_tick_is_the_next_publish_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_second: int,
    first_minute: int,
    first_second: int,
) -> None:
    ticks: list[int] = []
    on_tick = MoversRankActor.on_tick

    def record_tick(actor: MoversRankActor, event: TimeEvent) -> None:
        ticks.append(event.ts_event)
        on_tick(actor, event)

    monkeypatch.setattr(MoversRankActor, "on_tick", record_tick)
    start = status(
        equity("UPA"), MarketStatusAction.TRADING, OPEN + timedelta(seconds=start_second)
    )
    actor, subscriber = run([start, *market(3)], baseline_path=write_stub_baseline(tmp_path))
    assert ticks[0] == ns(OPEN + timedelta(minutes=first_minute, seconds=first_second))
    assert subscriber.signals[0][2] == ns(OPEN + timedelta(minutes=1, seconds=5)), actor.last_record


def reference_reader_from_bars(
    bars: Mapping[str, list[dict[str, object]]],
    actions: Mapping[str, list[dict[str, object]]] | None = None,
    calls: list[tuple[date, date]] | None = None,
) -> ReferenceReader:
    """A `ReferenceReader` that reads through `read_references` and `read_splits` as the actor's own
    `alpaca_reference_reader` does, so `cutoff` decides which daily close is the reference."""

    def read(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        if calls is not None:
            calls.append((cutoff, session))
        references = read_references(StubBars(dict(bars)), symbols, cutoff)
        splits = read_splits(StubActions(dict(actions or {})), references, session)
        return ReferenceRead(cutoff=cutoff, references=references, splits=splits, read_ns=0)

    return read


def test_publishes_the_ranking_through_the_codec_once_an_interval_with_ts_event_the_generation_time(
    tmp_path: Path,
) -> None:
    actor, subscriber = run(market(10), baseline_path=write_stub_baseline(tmp_path))
    assert subscriber.signals, actor.last_record
    names = {name for name, _, _ in subscriber.signals}
    assert names == {SIGNAL_NAME}
    stamps = [ts for _, _, ts in subscriber.signals]
    assert stamps == sorted(set(stamps))
    assert all(
        b - a >= actor_module.PUBLISH_INTERVAL_SECS * 1_000_000_000
        for a, b in itertools.pairwise(stamps)
    )
    # every stamp is a timer tick, PUBLISH_OFFSET_SECS after a minute mark
    assert all((ts // 1_000_000_000) % 60 == actor_module.PUBLISH_OFFSET_SECS for ts in stamps)
    first = published(subscriber)[-1]
    assert first[:2] == ("UPA", "UPB")
    assert "FLAT" not in first  # it has not moved


def test_item_1_a_split_since_the_reference_and_a_warrant_are_not_ranked(tmp_path: Path) -> None:
    """Antigravity, item 1: a split or a non-common name crowds out real movers.

    The control reads no splits, so the same bars must rank both the split name and the warrant: an empty
    ranking on either side would hide a reader that publishes nothing.
    """
    bars = market(10)
    _, control = run(
        bars, baseline_path=write_stub_baseline(tmp_path), reader=stub_reader(splits=())
    )
    control_ranking = published(control)[-1]
    assert control_ranking == ("SPLT", "UPA", "UPB"), (
        "the control ranks the split name; the warrant is excluded by its name whatever the split read"
    )

    _, subscriber = run(bars, baseline_path=write_stub_baseline(tmp_path), reader=stub_reader())
    rankings = published(subscriber)
    assert rankings
    assert rankings[-1] == ("UPA", "UPB")


def test_item_2_an_updated_bar_invalidates_the_generation_and_nothing_is_published_for_it(
    tmp_path: Path,
) -> None:
    """Antigravity, item 2: an updated bar (Alpaca's `updatedBars`, the same minute again) is not ignored."""
    bars = market(6)
    late = minute_bar(equity("UPA"), OPEN + timedelta(minutes=2), 15.5)
    # it arrives with the minute after it, as an updated bar comes after the half-minute mark
    late_arrival = OPEN + timedelta(minutes=4)
    updated = type(late)(
        late.bar_type,
        late.open,
        late.high,
        late.low,
        late.close,
        late.volume,
        late.ts_event,
        ns(late_arrival),
    )
    _actor, subscriber = run([*bars, updated], baseline_path=write_stub_baseline(tmp_path))
    stamps = {ts for _, _, ts in subscriber.signals}
    tick_after = ns(late_arrival.replace(second=actor_module.PUBLISH_OFFSET_SECS))
    assert tick_after not in stamps
    assert stamps, "the generations before and after the invalid one are published"
    assert any(ts > tick_after for ts in stamps)


def test_a_failed_reference_read_publishes_nothing_and_names_the_read(tmp_path: Path) -> None:
    def failing(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        raise ConnectionError("no route")

    actor, subscriber = run(market(5), baseline_path=write_stub_baseline(tmp_path), reader=failing)
    assert subscriber.signals == []
    assert actor.last_record is not None
    reasons = actor.last_record["reasons"]
    assert isinstance(reasons, list)
    assert any("references and splits read failed" in r for r in reasons)


def test_a_missing_baseline_publishes_nothing(tmp_path: Path) -> None:
    actor, subscriber = run(market(5), baseline_path=tmp_path / "absent.json")
    assert subscriber.signals == []
    assert actor.last_record is not None
    reasons = actor.last_record["reasons"]
    assert isinstance(reasons, list)
    assert any("baseline not usable" in r for r in reasons)


def test_with_no_baseline_file_a_part_that_ranks_without_one_publishes_on_dollars(
    tmp_path: Path,
) -> None:
    """ultra-3a8613a/2: overnight (boats) has no baseline writer, so with `rank_without_baseline` a
    missing file ranks every name in the dollar tier and each record says so; an unreadable file
    still publishes nothing."""
    actor, subscriber = run(
        market(5), baseline_path=tmp_path / "absent.json", rank_without_baseline=True
    )
    assert subscriber.signals
    assert actor.last_record is not None and actor.last_record["published"] is True
    assert actor.last_record["baseline"] == "no baseline: dollar tier under the move condition"
    (tmp_path / "bad.json").write_text("{")
    actor, subscriber = run(
        market(5), baseline_path=tmp_path / "bad.json", rank_without_baseline=True
    )
    assert subscriber.signals == []


def test_a_gap_in_the_bars_makes_the_generation_stale_and_nothing_is_published(
    tmp_path: Path,
) -> None:
    """The tick at the last fresh bar publishes; a tick deep in the gap is stale. An empty run would show
    neither, so the freshness of the generation before the gap is asserted too."""
    before = market(3)  # 09:30-09:32 New York, the last bar ending 09:33
    later = status(equity("UPB"), MarketStatusAction.TRADING, OPEN + timedelta(minutes=20))
    actor, subscriber = run([*before, later], baseline_path=write_stub_baseline(tmp_path))
    stamps = {ts for _, _, ts in subscriber.signals}
    fresh = ns(OPEN + timedelta(minutes=3, seconds=actor_module.PUBLISH_OFFSET_SECS))
    assert fresh in stamps, "the tick at the last fresh bar published"
    assert actor.last_record is not None
    assert actor.last_record["published"] is False
    reasons = actor.last_record["reasons"]
    assert isinstance(reasons, list)
    assert any("stale:" in reason for reason in reasons)


def test_every_record_carries_the_latency_labels_and_leaves_the_strategys_unknown(
    tmp_path: Path,
) -> None:
    actor, _ = run(market(5), baseline_path=write_stub_baseline(tmp_path))
    record = actor.last_record
    assert record is not None
    assert record["published"] is True
    labels = record["latency_ns"]
    assert isinstance(labels, dict)
    assert set(labels) == {"publication", "receipt", "generation", "entry_eligible", "submission"}
    assert labels["entry_eligible"] is None
    assert labels["submission"] is None
    assert labels["publication"] <= labels["generation"]
    assert labels["receipt"] is not None
    assert record["feed"] == "sip"


def test_item_4b_the_reference_is_the_latest_completed_regular_close_in_every_part_of_the_day() -> (
    None
):
    """Antigravity, item 4b: a snapshot's previous daily bar is the wrong reference outside the session.

    `movers_rank.rank` refuses a reference close of the trade date or later (`r.session >= session`), so
    the close that settled at 16:15 is the reference of the *next* trade date, read from 20:00; until then
    the previous session's close stays the reference.
    """
    at = lambda h, m: ns(datetime(2026, 11, 11, h, m, tzinfo=NEW_YORK))
    assert reference_cutoff(at(4, 0)) == SESSION  # pre-market: the previous session's close
    assert reference_cutoff(at(15, 59)) == SESSION
    assert reference_cutoff(at(16, 15)) == SESSION  # settled, but it is this trade date's own close
    assert reference_cutoff(at(19, 59)) == SESSION
    assert reference_cutoff(at(20, 0)) == SESSION + timedelta(days=1)
    assert trade_date(at(19, 59)) == SESSION
    assert trade_date(at(20, 0)) == SESSION + timedelta(days=1)


def test_the_actor_publishes_after_the_close_with_the_previous_sessions_reference(
    tmp_path: Path,
) -> None:
    """Antigravity, findings 1 and 4b: from 16:15 the previous session's close is still the only reference
    `rank` accepts, so every tick after the close must keep publishing."""
    start = datetime(2026, 11, 11, 16, 5, tzinfo=NEW_YORK)
    calls: list[tuple[date, date]] = []
    daily_rows = {symbol: [daily(PREVIOUS, 10.0), daily(SESSION, 11.0)] for symbol in NAMES}
    actor, subscriber = run(
        market(15, start=start),
        baseline_path=write_stub_baseline(tmp_path),
        reader=reference_reader_from_bars(daily_rows, calls=calls),
    )
    after_close = ns(datetime(2026, 11, 11, 16, 15, 5, tzinfo=NEW_YORK))
    stamps = {ts for _, _, ts in subscriber.signals}
    assert after_close in stamps, actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["published"] is True, actor.last_record
    assert {cutoff for cutoff, _ in calls} == {SESSION}


def test_the_actor_refreshes_the_reference_and_splits_across_the_trade_date_roll(
    tmp_path: Path,
) -> None:
    """Antigravity, finding 2: a read taken while the session is still 2026-11-11 must not serve the trade
    date 2026-11-12, whose split window ends on the 12th; the roll has to re-read both."""
    start = datetime(2026, 11, 11, 19, 55, tzinfo=NEW_YORK)
    morning = datetime(2026, 11, 12, 9, 30, tzinfo=NEW_YORK)
    calls: list[tuple[date, date]] = []
    daily_rows = {symbol: [daily(PREVIOUS, 10.0), daily(SESSION, 11.0)] for symbol in NAMES}
    split: dict[str, object] = {
        "id": "a7932b15-f816-4f83-921e-998b524487b3",
        "symbol": "SPLT",
        "cusip": "000000000",
        "new_rate": 2.0,
        "old_rate": 1.0,
        "process_date": "2026-11-12",
        "ex_date": "2026-11-12",
    }
    actor, subscriber = run(
        [*market(5, start=start), *market(6, start=morning)],
        baseline_path=write_stub_baseline(tmp_path, session=SESSION + timedelta(days=1)),
        reader=reference_reader_from_bars(
            daily_rows, actions={"forward_splits": [split]}, calls=calls
        ),
    )
    assert [cutoff for cutoff, _ in calls] == [SESSION, SESSION + timedelta(days=1)]
    rankings = published(subscriber)
    assert rankings, actor.last_record
    assert rankings[-1][:2] == ("UPA", "UPB"), (
        f"a stale split read leaves every name's corporate action unknown: {actor.last_record}"
    )
    assert actor.last_record is not None
    assert actor.last_record["published"] is True


def test_a_failed_read_and_an_unusable_baseline_are_retried(tmp_path: Path) -> None:
    """Antigravity, finding 2: a failed reference read and a baseline that was not written yet are retried
    on a later tick, rather than latching for the rest of the trade date."""
    path = tmp_path / "baselines.json"
    calls: list[tuple[date, date]] = []

    def flaky(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        calls.append((cutoff, session))
        if len(calls) == 1:
            raise ConnectionError("no route")
        # the nightly job writes it between the first read and this retry
        write_stub_baseline(tmp_path)
        return stub_reader()(symbols, cutoff, session)

    actor, subscriber = run(market(5), baseline_path=path, reader=flaky)
    assert len(calls) >= 2, "the failed reference read was not retried"
    assert subscriber.signals, actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["published"] is True, actor.last_record


class StubBars:
    def __init__(self, data: dict[str, list[dict[str, object]]]) -> None:
        self.data = data
        self.requests: list[StockBarsRequest] = []

    def get_stock_bars(self, request_params: StockBarsRequest) -> BarSet:
        self.requests.append(request_params)
        symbols = request_params.symbol_or_symbols
        return BarSet({s: rows for s, rows in self.data.items() if s in symbols})


def daily(day: date, close: float) -> dict[str, object]:
    t = datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(UTC)
    return {
        "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": 1,
        "n": 1,
        "vw": close,
    }


def test_read_references_asks_for_split_adjusted_daily_sip_bars_and_takes_the_latest_before_the_cutoff() -> (
    None
):
    client = StubBars(
        {
            "UPA": [daily(date(2026, 11, 9), 9.0), daily(PREVIOUS, 10.0), daily(SESSION, 11.0)],
            "UPB": [daily(date(2026, 11, 6), 7.0)],
        }
    )
    found = read_references(client, ["UPA", "UPB", "FLAT"], SESSION)
    assert found == {
        "UPA": movers_rank.Reference(close=10.0, session=PREVIOUS),
        "UPB": movers_rank.Reference(close=7.0, session=date(2026, 11, 6)),
    }
    request = client.requests[0]
    assert request.timeframe.value == TimeFrame.Day.value
    assert request.adjustment == Adjustment.SPLIT
    assert request.feed == DataFeed.SIP


class StubActions:
    def __init__(self, raw: dict[str, list[dict[str, object]]]) -> None:
        self.raw = raw
        self.requests: list[CorporateActionsRequest] = []

    def get_corporate_actions(self, request_params: CorporateActionsRequest) -> CorporateActionsSet:
        self.requests.append(request_params)
        return CorporateActionsSet(self.raw)


def test_read_splits_is_unknown_when_the_read_may_be_cut_short() -> None:
    split: dict[str, object] = {
        "id": "a7932b15-f816-4f83-921e-998b524487b2",
        "symbol": "SPLT",
        "cusip": "000000000",
        "new_rate": 2.0,
        "old_rate": 1.0,
        "process_date": "2026-11-11",
        "ex_date": "2026-11-11",
    }
    references = {"SPLT": movers_rank.Reference(close=REFERENCE, session=PREVIOUS)}
    client = StubActions({"forward_splits": [split]})
    read = read_splits(client, references, SESSION)
    assert read is not None
    assert [a.symbol for a in read.actions] == ["SPLT"]
    assert read.start == PREVIOUS
    assert read.end == SESSION
    request = client.requests[0]
    assert request.types == [CorporateActionsType.FORWARD_SPLIT, CorporateActionsType.REVERSE_SPLIT]
    assert request.start == PREVIOUS
    assert request.end == SESSION
    assert request.limit == actor_module.SPLITS_LIMIT
    full = StubActions({"forward_splits": [split] * actor_module.SPLITS_LIMIT})
    assert read_splits(full, references, SESSION) is None
    assert read_splits(StubActions({}), {}, SESSION) is None


def test_a_bar_from_an_earlier_trade_date_after_the_roll_invalidates_the_generation(
    tmp_path: Path,
) -> None:
    """A bar of the trade date before the actor's, arriving after the 20:00 roll, invalidates the generation.

    The baseline is for the post-roll trade date: a baseline the actor cannot use suppresses every post-roll
    tick by itself, so a run that published nothing would not show whether the stray bar was noticed. The
    first run is the control: the same bars without the stray bar publish at the affected tick.
    """
    evening = datetime(2026, 11, 11, 19, 55, tzinfo=NEW_YORK)
    bars = market(8, start=evening, closes={"UPA": 15.0})
    stray = minute_bar(equity("UPB"), evening + timedelta(minutes=1), 12.0)
    stray_late = type(stray)(
        stray.bar_type,
        stray.open,
        stray.high,
        stray.low,
        stray.close,
        stray.volume,
        stray.ts_event,
        ns(evening + timedelta(minutes=7)),
    )
    # the stray bar's trade date is behind the rolled session, and it arrives at 20:02:00, before that tick
    affected = ns(evening + timedelta(minutes=7, seconds=actor_module.PUBLISH_OFFSET_SECS))
    baseline = write_stub_baseline(tmp_path, session=SESSION + timedelta(days=1))

    _actor, control = run(bars, baseline_path=baseline)
    assert affected in {ts for _, _, ts in control.signals}, (
        "the affected tick publishes without the stray"
    )

    actor, subscriber = run([*bars, stray_late], baseline_path=baseline)
    stamps = {ts for _, _, ts in subscriber.signals}
    before = ns(evening + timedelta(minutes=6, seconds=actor_module.PUBLISH_OFFSET_SECS))
    assert before in stamps, "the generation before the stray bar published"
    assert affected not in stamps, "the stray bar suppresses its affected tick"
    record = actor.last_record
    assert record is not None
    labels = record["latency_ns"]
    assert isinstance(labels, dict)
    assert labels["generation"] == affected, (
        "the last generation is the one the stray bar arrived before"
    )
    reasons = record["reasons"]
    assert isinstance(reasons, list)
    assert any(
        "invalid input: UPB: a bar opening" in reason and "is from trade date 2026-11-11" in reason
        for reason in reasons
    ), reasons


@pytest.mark.parametrize("feed", ["iex", "SIP", ""])
def test_the_config_refuses_a_feed_that_is_not_sip_or_boats(feed: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sip or boats"):
        MoversRankActorConfig(
            feed=feed,
            order_cap=1_000.0,
            baseline_path=str(tmp_path / "baselines.json"),
        )


def test_instrument_ids_in_the_ranking_are_alpaca_ids(tmp_path: Path) -> None:
    _, subscriber = run(market(5), baseline_path=write_stub_baseline(tmp_path))
    assert subscriber.signals
    entries = [entry for _, value, _ in subscriber.signals for entry in decode_ranking(value)]
    assert entries, "the ranking is not empty"
    assert all(entry.instrument_id.venue == VENUE for entry in entries)
    assert all(isinstance(entry.instrument_id, InstrumentId) for entry in entries)


def test_item_3_a_status_that_says_nothing_about_trading_does_not_erase_a_halt(
    tmp_path: Path,
) -> None:
    """Antigravity, finding 3: `HALT` then `SHORT_SELL_RESTRICTION_CHANGE` stays halted; the ranking that
    admits an unknown halt must not turn the second status into `None`."""
    upa = equity("UPA")
    statuses = [
        status(upa, MarketStatusAction.HALT, OPEN + timedelta(seconds=10)),
        status(
            upa,
            MarketStatusAction.SHORT_SELL_RESTRICTION_CHANGE,
            OPEN + timedelta(seconds=20),
        ),
    ]
    actor, subscriber = run([*market(5), *statuses], baseline_path=write_stub_baseline(tmp_path))
    rankings = published(subscriber)
    assert rankings, actor.last_record
    assert all("UPA" not in ranking for ranking in rankings)
    assert "UPB" in rankings[-1]


def test_item_3_trading_after_a_halt_makes_the_name_eligible_again(tmp_path: Path) -> None:
    upa = equity("UPA")
    statuses = [
        status(upa, MarketStatusAction.HALT, OPEN + timedelta(seconds=10)),
        status(upa, MarketStatusAction.TRADING, OPEN + timedelta(minutes=2)),
    ]
    _, subscriber = run([*market(5), *statuses], baseline_path=write_stub_baseline(tmp_path))
    rankings = published(subscriber)
    assert rankings
    assert "UPA" in rankings[-1]


def test_item_4_a_future_trade_date_bar_does_not_roll_the_session(tmp_path: Path) -> None:
    """Antigravity, finding 4: one bar whose event is next trade date is invalid input for its tick, not a
    roll that empties today's tallies and makes every later bar of today an invalid one."""
    future = minute_bar(equity("UPB"), datetime(2026, 11, 11, 20, 5, tzinfo=NEW_YORK), 12.0)
    malformed = type(future)(
        future.bar_type,
        future.open,
        future.high,
        future.low,
        future.close,
        future.volume,
        future.ts_event,
        ns(OPEN + timedelta(minutes=1)),
    )
    actor, subscriber = run([*market(6), malformed], baseline_path=write_stub_baseline(tmp_path))
    stamps = {ts for _, _, ts in subscriber.signals}
    assert stamps, "the malformed bar must not empty today's tallies"
    assert max(stamps) >= ns(OPEN + timedelta(minutes=4)), actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["published"] is True, actor.last_record


def test_item_5_the_config_accepts_the_base_actor_configs_keywords(tmp_path: Path) -> None:
    """Antigravity, finding 5: `DataActorConfig`'s own keywords reach the subclass's `__init__` too."""
    config = MoversRankActorConfig(
        feed="sip",
        order_cap=1_000.0,
        baseline_path=str(tmp_path / "baselines.json"),
        actor_id=ActorId("MOVERS-001"),
        log_events=False,
        log_commands=False,
    )
    assert config.actor_id == ActorId("MOVERS-001")
    assert config.log_events is False
    assert config.log_commands is False


@pytest.mark.parametrize("order_cap", [0.0, -1.0, float("inf"), float("nan")])
def test_item_5_the_config_refuses_an_order_cap_that_is_not_an_amount(
    order_cap: float, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="order cap"):
        MoversRankActorConfig(
            feed="sip",
            order_cap=order_cap,
            baseline_path=str(tmp_path / "baselines.json"),
        )


def test_the_config_still_refuses_a_keyword_that_is_nobodys(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="nonsense"):
        MoversRankActorConfig(
            feed="sip",
            order_cap=1_000.0,
            baseline_path=str(tmp_path / "baselines.json"),
            nonsense=1,
        )


def test_item_6_a_cancelled_read_is_reported_instead_of_raising() -> None:
    future: Future[ReferenceRead] = Future()
    future.cancel()
    reasons: list[str] = []
    assert MoversRankActor._result(future, "references and splits", reasons) is None
    assert any("cancelled" in reason for reason in reasons), reasons


def test_item_6_inline_reads_false_runs_the_reads_on_a_worker_thread(tmp_path: Path) -> None:
    """`inline_reads=False` is the live node's path: the reads run in the actor's worker, never the loop."""
    release = threading.Event()
    threads: list[str] = []

    def blocking(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        threads.append(threading.current_thread().name)
        assert release.wait(timeout=5)
        return stub_reader()(symbols, cutoff, session)

    class Releaser(DataActor):
        """A first bar's arrival releases the read, and a short sleep lets the worker finish before the
        tick that follows it."""

        def on_start(self) -> None:
            self.subscribe_bars([BarType.from_str(f"{equity('UPA').id}-1-MINUTE-LAST-EXTERNAL")])

        def on_bar(self, bar: Bar) -> None:
            release.set()
            time.sleep(0.05)

    actor, subscriber = run(
        market(5),
        baseline_path=write_stub_baseline(tmp_path),
        reader=blocking,
        before=[Releaser()],
        inline_reads=False,
    )
    assert threads and threads[0].startswith("movers-rank-reads"), threads
    assert subscriber.signals, actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["published"] is True


def test_item_7_only_alpaca_equities_are_subscribed(tmp_path: Path) -> None:
    """Antigravity, finding 7: another venue's `UPA.SIM` bar must not be subscribed to, overwrite
    `UPA.ALPACA`'s asset, or invalidate the generation as a bar on the wrong venue."""
    sim = sim_equity("UPA")
    sim_bar = minute_bar(sim, OPEN + timedelta(minutes=1), 12.0)
    actor, subscriber = run(
        [*market(5), sim_bar],
        baseline_path=write_stub_baseline(tmp_path),
        instruments=[*(equity(symbol) for symbol in NAMES), sim],
    )
    stamps = {ts for _, _, ts in subscriber.signals}
    assert stamps, "the SIM bar must not suppress the Alpaca ranking"
    assert max(stamps) > ns(OPEN + timedelta(minutes=2)), actor.last_record
    assert actor.last_record is not None
    assert actor.last_record["published"] is True, actor.last_record


@pytest.mark.parametrize(
    ("feeds", "overnight"), [(None, False), (["sip"], False), (["sip", "boats"], True)]
)
def test_the_actor_passes_whether_its_baseline_holds_the_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feeds: list[str] | None, overnight: bool
) -> None:
    """ADR 0087, the lead's numerator rule of 2026-11-11: the actor gives `rank` the file's own word on
    whether its baselines hold the overnight feed, so a sip-only file's day ratio counts from 04:00."""
    path = write_stub_baseline(tmp_path, header=None if feeds is None else {"feeds": feeds})
    seen: list[bool] = []
    real = movers_rank.rank

    def spy(**kwargs):
        seen.append(kwargs["overnight_baseline"])
        return real(**kwargs)

    monkeypatch.setattr(movers_rank, "rank", spy)
    run(market(10), baseline_path=path)
    assert seen and set(seen) == {overnight}


def test_each_generation_record_carries_its_ranked_table_and_symbol_count(tmp_path: Path) -> None:
    """The lead's ranked table (2026-11-11): every published generation's record names how many symbols
    the stream has delivered and its best ten in rank order, each with move, span dollars, normal dollars,
    ratio and span, tier and data age; none of it is a float JSON cannot carry."""
    start = datetime(2026, 11, 11, 16, 5, tzinfo=NEW_YORK)
    daily_rows = {symbol: [daily(PREVIOUS, 10.0), daily(SESSION, 11.0)] for symbol in NAMES}
    actor, _ = run(
        market(15, start=start),
        baseline_path=write_stub_baseline(tmp_path),
        reader=reference_reader_from_bars(daily_rows, calls=[]),
    )
    record = actor.last_record
    assert record is not None and record["published"] is True, record
    assert record["symbols"] == len(NAMES)
    top = record["top"]
    assert isinstance(top, list) and 0 < len(top) <= 10
    assert [row["rank"] for row in top] == list(range(1, len(top) + 1))
    assert {row["tier"] for row in top} <= {"ratio", "dollar"}
    assert all(row["data_age_secs"] is not None and row["data_age_secs"] >= 0 for row in top)
    json.dumps(record, allow_nan=False)


@pytest.mark.parametrize("unusable", ["no references", "no splits"])
def test_an_empty_or_unusable_reference_read_is_retried_and_publishes_nothing_meanwhile(
    tmp_path: Path, unusable: str
) -> None:
    """ultra-3a8613a/12, the lead's ruling of 2026-11-12: a reference or split read that comes back
    empty or unusable without raising counts as failed: it is retried, and the ranking is unpublished
    with a reason, never an empty ranking published as fresh."""
    calls: list[tuple[date, date]] = []

    def empty(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        calls.append((cutoff, session))
        good = stub_reader()(symbols, cutoff, session)
        if unusable == "no references":
            return replace(good, references={})
        return replace(good, splits=None)

    actor, subscriber = run(market(5), baseline_path=write_stub_baseline(tmp_path), reader=empty)
    assert subscriber.signals == []
    assert len(calls) >= 2, "the unusable read was not retried"
    assert actor.last_record is not None and actor.last_record["published"] is False
    reasons = actor.last_record["reasons"]
    assert isinstance(reasons, list)
    assert any("references and splits not usable" in r for r in reasons), reasons


def test_an_unusable_reference_read_that_comes_back_usable_publishes(tmp_path: Path) -> None:
    """ultra-3a8613a/12: once the retried read is usable, the actor publishes."""
    calls: list[tuple[date, date]] = []

    def late(symbols: Sequence[str], cutoff: date, session: date) -> ReferenceRead:
        calls.append((cutoff, session))
        good = stub_reader()(symbols, cutoff, session)
        return replace(good, references={}) if len(calls) == 1 else good

    actor, subscriber = run(market(5), baseline_path=write_stub_baseline(tmp_path), reader=late)
    assert len(calls) >= 2 and subscriber.signals
    assert actor.last_record is not None and actor.last_record["published"] is True


def test_an_absent_baseline_is_kept_on_the_worker_and_the_night_still_ranks(tmp_path: Path) -> None:
    """Review of the composition's fixes, high round, finding 1: an absent baseline file read as
    unusable, so with the reads on the worker (`inline_reads=False`, the live node's path) it was read
    again at every tick and the tick found the new read still pending: overnight never published."""

    class Sleeper(DataActor):
        """Each bar leaves the worker time to finish a read before the next tick."""

        def on_start(self) -> None:
            self.subscribe_bars([BarType.from_str(f"{equity('UPA').id}-1-MINUTE-LAST-EXTERNAL")])

        def on_bar(self, bar: Bar) -> None:
            time.sleep(0.05)

    actor, subscriber = run(
        market(5),
        baseline_path=tmp_path / "absent.json",
        rank_without_baseline=True,
        before=[Sleeper()],
        inline_reads=False,
    )
    assert subscriber.signals, actor.last_record
    assert actor.last_record is not None and actor.last_record["published"] is True
