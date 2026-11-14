"""A paper node scopes loading, reconciliation and risk to every window name, without connecting."""

from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model import InstrumentId

from canon_trading import paper_node

AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")


@pytest.fixture(autouse=True)
def alpaca_stream_lock() -> None:
    """Building nodes offline needs no credential-environment reader."""


def test_each_window_instrument_has_the_engine_notional_cap() -> None:
    limits = paper_node.risk_limits((AAA, BBB), max_notional_per_order=Decimal(5000))
    assert limits["max_notional_per_order"] == {
        str(AAA): Decimal(5000),
        str(BBB): Decimal(5000),
    }
    assert limits["bypass"] is False
    assert limits["max_order_submit_rate"] == paper_node.MAX_ORDER_SUBMIT_RATE


def test_reconciliation_includes_every_window_instrument_once() -> None:
    config = paper_node.exec_engine_config((AAA, BBB, AAA))
    assert config.reconciliation_instrument_ids == [str(AAA), str(BBB)]
    assert config.reconciliation is True
    assert config.reconciliation_lookback_mins == paper_node.RECONCILIATION_LOOKBACK_MINS
    assert config.open_check_interval_secs == paper_node.OPEN_CHECK_INTERVAL_SECS
    assert config.position_check_interval_secs == paper_node.POSITION_CHECK_INTERVAL_SECS


@pytest.mark.parametrize("instruments", [AAA, (AAA, BBB, AAA)])
def test_both_client_providers_load_every_window_name(monkeypatch, instruments) -> None:
    providers: list = []
    for factory in (paper_node.AlpacaDataClientFactory, paper_node.AlpacaExecutionClientFactory):
        original = factory.create

        def create(*, _original=original, **kwargs):
            providers.append(kwargs["config"].instrument_provider)
            return _original(**kwargs)

        monkeypatch.setattr(factory, "create", staticmethod(create))
    node = paper_node.alpaca_paper_builder("CANON-MULTI-INSTRUMENT-TEST", instruments).build()
    try:
        expected = [str(AAA)] if instruments == AAA else [str(AAA), str(BBB)]
        assert len(providers) == 2
        assert all(provider.load_ids == expected for provider in providers)
        assert all(provider.load_all is False for provider in providers)
    finally:
        node.dispose()


@pytest.mark.parametrize("configure", [paper_node.risk_limits, paper_node.exec_engine_config])
def test_an_empty_window_universe_is_refused(configure) -> None:
    with pytest.raises(ValueError, match="at least one instrument"):
        configure(())


def test_a_whole_market_data_client_loads_every_equity_and_execution_keeps_the_window(
    monkeypatch,
) -> None:
    """The lead's order for the 04:00 firing (2026-11-12): the data client loads every active US equity
    (`load_all`), so the ranking actor's wildcard bars are tallied for the whole market; the execution
    client keeps the window's instruments."""
    providers: dict[str, InstrumentProviderConfig] = {}
    for kind, factory in (
        ("data", paper_node.AlpacaDataClientFactory),
        ("exec", paper_node.AlpacaExecutionClientFactory),
    ):
        original = factory.create

        def create(*, _original=original, _kind=kind, **kwargs):
            providers[_kind] = kwargs["config"].instrument_provider
            return _original(**kwargs)

        monkeypatch.setattr(factory, "create", staticmethod(create))
    node = paper_node.alpaca_paper_builder(
        "CANON-WHOLE-MARKET-TEST", (AAA, BBB), data_load_all=True
    ).build()
    try:
        data, execution = providers["data"], providers["exec"]
        assert data.load_all is True
        assert execution.load_all is False
        assert execution.load_ids == [str(AAA), str(BBB)]
    finally:
        node.dispose()


def test_shared_builder_accepts_owner_execution_config_without_losing_provider(monkeypatch):
    from canon_trading.owner_protector import owner_execution_config

    configs = []
    original = paper_node.AlpacaExecutionClientFactory.create

    def create(**kwargs):
        configs.append(kwargs["config"])
        return original(**kwargs)

    monkeypatch.setattr(paper_node.AlpacaExecutionClientFactory, "create", staticmethod(create))
    node = paper_node.alpaca_paper_builder(
        "CANON-PROTECTOR-BUILDER-TEST", (AAA, BBB), execution_config=owner_execution_config
    ).build()
    try:
        assert len(configs) == 1
        assert configs[0].instrument_provider.load_ids == [str(AAA), str(BBB)]
        assert configs[0].reduce_only_sells is True
        assert configs[0].report_external_orders is True
    finally:
        node.dispose()


def test_close_only_builder_can_leave_notional_uncapped_without_bypassing_risk():
    limits = paper_node.risk_limits((AAA, BBB), max_notional_per_order=None)
    assert limits["max_notional_per_order"] == {}
    assert limits["bypass"] is False
    assert limits["max_order_submit_rate"] == paper_node.MAX_ORDER_SUBMIT_RATE
