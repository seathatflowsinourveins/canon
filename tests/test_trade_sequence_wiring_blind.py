"""L8 blind contracts P1-P5, written against base 3c7de6c3470a (ADR 0134).

Upstream: NautilusTrader d53849b216e2, docs/developer_guide/python_adapters.md,
"Define adapter configuration" / "Serialize configuration", and the live builder's
add_data_client(name, factory, config) registration contract. Capture registrations at that
public seam: no real node or client is built, started or connected.
"""

from __future__ import annotations

import ast
import inspect
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import Mock

import pytest
from canon_alpaca.data import AlpacaDataClientConfig
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.live import LiveNodeBuilder
from nautilus_trader.model import InstrumentId

from canon_trading import paper_node
from canon_trading import sandbox_node

SETTING = "validate_trade_sequence"
ROOT = Path(__file__).resolve().parents[1]
AAA = InstrumentId.from_str("AAA.ALPACA")
BBB = InstrumentId.from_str("BBB.ALPACA")


@dataclass
class BuilderProbe:
    kind: str
    constructor: Callable[..., object]
    invoke: Callable[..., object]
    builder: MagicMock
    registrations: list[tuple[str, object, DataClientConfig]]
    client_factories: tuple[Mock, ...]

    def data_config(self) -> DataClientConfig:
        assert len(self.registrations) == 1, "register exactly one Alpaca data client"
        name, factory, config = self.registrations[0]
        assert name == "ALPACA"
        assert factory is paper_node.AlpacaDataClientFactory
        for create in self.client_factories:
            create.assert_not_called()
        return config

    def expected_provider(self, *, load_all: bool) -> InstrumentProviderConfig:
        if load_all:
            return InstrumentProviderConfig(load_all=True)
        ids = [str(AAA), str(BBB)] if self.kind == "paper" else [str(sandbox_node.INSTRUMENT_ID)]
        return InstrumentProviderConfig(load_ids=ids)


@pytest.fixture(params=["paper", "sandbox"])
def probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> BuilderProbe:
    """Replace the upstream builder, preserving real configuration construction and serialization."""
    kind = str(request.param)
    # The future keyword is deliberately absent at the base; signature assertions test it at runtime.
    constructor = cast(
        Callable[..., object],
        paper_node.alpaca_paper_builder if kind == "paper" else sandbox_node.sandbox_node,
    )
    builder = MagicMock(spec=LiveNodeBuilder)
    for method in (
        "with_risk_engine_config",
        "with_exec_engine_config",
        "with_reconciliation",
        "with_delay_post_stop_secs",
        "with_timeout_connection",
        "with_timeout_disconnection_secs",
        "with_delay_shutdown_secs",
        "add_exec_client",
        "add_simulated_exec_client",
    ):
        getattr(builder, method).return_value = builder
    builder.build.return_value = MagicMock(spec=LiveNode)
    live_node = Mock(spec=LiveNode)
    live_node.builder.return_value = builder
    monkeypatch.setattr(paper_node, "LiveNode", live_node)
    monkeypatch.setattr(sandbox_node, "LiveNode", live_node)
    monkeypatch.setattr(sandbox_node, "data_only", lambda: True)
    monkeypatch.setattr(sandbox_node, "refuse_alpaca_execution", Mock())

    # A direct factory call must fail too, rather than accidentally constructing a real client.
    data_create = Mock(side_effect=AssertionError("a real data client must not be built"))
    exec_create = Mock(side_effect=AssertionError("a real execution client must not be built"))
    sandbox_create = Mock(side_effect=AssertionError("a sandbox client must not be built"))
    monkeypatch.setattr(paper_node.AlpacaDataClientFactory, "create", data_create)
    monkeypatch.setattr(paper_node.AlpacaExecutionClientFactory, "create", exec_create)
    sandbox_factory = Mock()
    sandbox_factory.create = sandbox_create
    monkeypatch.setattr(
        sandbox_node, "SandboxExecutionClientFactory", Mock(return_value=sandbox_factory)
    )

    registrations: list[tuple[str, object, DataClientConfig]] = []

    def register(name: str, factory: object, config: DataClientConfig) -> MagicMock:
        registrations.append((name, factory, config))
        return builder

    builder.add_data_client.side_effect = register

    def invoke(*, load_all: bool = False, **settings: object) -> object:
        if kind == "paper":
            return constructor(
                "CANON-L8-BLIND", (AAA, BBB, AAA), data_load_all=load_all, **settings
            )
        return constructor("CANON-L8-BLIND", load_all=load_all, **settings)

    return BuilderProbe(
        kind,
        constructor,
        invoke,
        builder,
        registrations,
        (data_create, exec_create, sandbox_create),
    )


def require_setting(constructor: Callable[..., object]) -> inspect.Parameter:
    parameter = inspect.signature(constructor).parameters.get(SETTING)
    assert parameter is not None, f"the builder must declare {SETTING}"
    return parameter


def test_builder_declares_a_bool_keyword_defaulting_to_false(probe: BuilderProbe) -> None:
    """P1/P3: this is a declared keyword, with a bool annotation and an actual False default."""
    parameter = require_setting(probe.constructor)
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.annotation in (bool, "bool")
    assert parameter.default is False


@pytest.mark.parametrize("load_all", [False, True], ids=["listed", "whole-market"])
@pytest.mark.parametrize("explicit_false", [False, True], ids=["omitted", "explicit-false"])
def test_default_and_false_preserve_plain_config_and_serialized_fields(
    probe: BuilderProbe, load_all: bool, explicit_false: bool
) -> None:
    """P1/P3: both provider modes retain the base config's exact type and serialized field values."""
    settings = {SETTING: False} if explicit_false else {}
    probe.invoke(load_all=load_all, **settings)
    config = probe.data_config()
    expected = DataClientConfig(instrument_provider=probe.expected_provider(load_all=load_all))
    assert type(config) is DataClientConfig
    actual_fields = json.loads(config.json())
    assert actual_fields == json.loads(expected.json())
    assert SETTING not in actual_fields


@pytest.mark.parametrize("load_all", [False, True], ids=["listed", "whole-market"])
def test_true_registers_adapter_config_with_only_the_requested_setting_changed(
    probe: BuilderProbe, load_all: bool
) -> None:
    """P2/P3: True reaches the registered adapter config; every other field retains its default."""
    probe.invoke(load_all=load_all, validate_trade_sequence=True)
    config = probe.data_config()
    assert isinstance(config, AlpacaDataClientConfig)
    assert config.validate_trade_sequence is True
    expected = AlpacaDataClientConfig(
        instrument_provider=probe.expected_provider(load_all=load_all), validate_trade_sequence=True
    )
    assert json.loads(config.json()) == json.loads(expected.json())


@pytest.mark.parametrize(
    "value",
    [None, 0, 1, 0.0, 1.0, "False", "True", [], {}, object()],
    ids=[
        "none",
        "zero",
        "one",
        "zero-float",
        "one-float",
        "false-text",
        "true-text",
        "list",
        "dict",
        "object",
    ],
)
def test_non_bool_is_refused_by_name_before_any_client_is_built(
    probe: BuilderProbe, value: object
) -> None:
    """P4: test value validation, not Python's rejection of an unknown keyword at the base."""
    require_setting(probe.constructor)
    with pytest.raises(TypeError, match=SETTING):
        probe.invoke(validate_trade_sequence=value)
    probe.builder.build.assert_not_called()
    for create in probe.client_factories:
        create.assert_not_called()


# Changed by the owning lane after the tester's hand, on the lead's order of 2026-11-13 that a session
# window run with the setting on: the session window module may now name the setting (its config row,
# its --validate-trade-sequence flag, its record line). The tester's case allowed the two builders only
# and was named test_setting_occurs_only_inside_the_two_builders_in_src.
WINDOW = "canon_trading/session_window.py"


def test_setting_occurs_only_inside_the_two_builders_and_the_session_window_in_src() -> None:
    """P5: even other functions in the builder modules must not pass or expose the new setting."""
    source_root = ROOT / "src"
    allowed = {
        "canon_trading/paper_node.py": "alpaca_paper_builder",
        "canon_trading/sandbox_node.py": "sandbox_node",
    }
    outside: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
        if relative == WINDOW:
            continue
        source = path.read_text()
        permitted_lines: range = range(0)
        if relative in allowed:
            function = next(
                node
                for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == allowed[relative]
            )
            assert function.end_lineno is not None
            permitted_lines = range(function.lineno, function.end_lineno + 1)
        for line_number, line in enumerate(source.splitlines(), start=1):
            if (
                SETTING in line or "--validate-trade-sequence" in line
            ) and line_number not in permitted_lines:
                outside.append(f"{relative}:{line_number}")
    assert outside == [], f"trade-sequence wiring escaped the two builders: {outside}"


def test_no_mise_task_passes_the_setting() -> None:
    """P5: the task definitions neither pass the Python keyword nor introduce a window CLI flag."""
    tasks = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    outside = [
        name
        for name, task in tasks.items()
        if SETTING in str(task) or "--validate-trade-sequence" in str(task)
    ]
    assert outside == [], f"tasks opt into the setting: {outside}"
