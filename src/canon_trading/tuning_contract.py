"""N2/N3 complete ranked configuration contract (coordinator, 2026-11-11 22:07).

JSON is Python's documented json contract; keys use ledger.config_key (ADR 0053).
The proposed bars replay record (ADR 0101) names execution/sizing/cost assumptions.
No window may silently drop an arm row or replace a ranked window row on promotion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from typing import Literal
from typing import TypedDict

from nautilus_trader.model import TriggerType

from .early_entry_study import Rows
from .mover import OWN_FIELDS
from .mover import PERSISTENCE_FIELDS
from .mover import MoverConfig

BARS_REPLAY_FIXED_ROWS: dict[str, Any] = {
    "trail_mode": "fixed",
    "atr_k": Decimal(1),
    "atr_n": 14,
    "spread_floor_m": Decimal(2),
    "liquidity_tier_cut": Decimal(1_000_000),
    "ratchet_r": Decimal(2),
    "ratchet_g": Decimal("0.5"),
    "ratchet_base": "atr",
    "trail_cap": "clamp",
    "planned_loss": None,
    "loss_share": None,
}
"""Window-only settings excluded from bars trial keys; the study does not tune these rows. The cap arm
and the planned loss (S2) act only on a non-fixed trail and on budget sizing, neither of which the bars
replay runs, so a trial's key is what it was before them."""


def check_bars_replay_modes(settings: MoverConfig) -> None:
    """The bars contract covers only the pre-port fixed trail and clock re-entry, by name."""
    if settings.size_from_budget:
        # the replay sizes buying_power / slots; budget sizing and its planned loss are the window's
        raise ValueError("bars replay refuses size_from_budget=True; it sizes buying_power / slots")
    for name in ("trail_mode",):
        value = getattr(settings, name)
        if value != BARS_REPLAY_FIXED_ROWS[name]:
            raise ValueError(
                f"bars replay refuses {name}={value!r}; requires {BARS_REPLAY_FIXED_ROWS[name]!r}"
            )


SCHEMA: Literal["canon.mover-ranked.v1"] = "canon.mover-ranked.v1"


class RankedPayload(TypedDict):
    schema: Literal["canon.mover-ranked.v1"]
    config_key: str
    config: dict[str, Any]


@dataclass(frozen=True)
class RankedConfiguration:
    """Every keyed row: strategy/arm/window, execution/sizing, venue/cost and bar specification.

    Consumers persist encode() whole and decode() with the expected ledger key; mover_config
    and rows are typed views, never a reduced promotion payload. A consumer that cannot apply
    a row must refuse promotion instead of claiming the changed configuration was ranked.
    """

    config: dict[str, Any]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RankedConfiguration:
        copied = json.loads(json.dumps(config, allow_nan=False))
        if not isinstance(copied, dict) or set(copied) != {"strategies", "venue", "data"}:
            raise ValueError("ranked configuration needs strategies, venue and data")
        if not isinstance(copied["strategies"], list) or not copied["strategies"]:
            raise ValueError("ranked configuration needs at least one strategy")
        candidate = cls(copied)
        _ = candidate.mover_config, candidate.rows, candidate.part
        return candidate

    @property
    def fields(self) -> dict[str, Any]:
        return self.config["strategies"][0]["config"]

    @property
    def mover_config(self) -> MoverConfig:
        """`Mover`'s own fields only: a base `StrategyConfig` field the key holds is not in this view,
        and a consumer must name it as a row it cannot apply (N3's window, which is not built yet)."""
        decoded: dict[str, Any] = {}
        for name, kind in OWN_FIELDS.items():
            if name in BARS_REPLAY_FIXED_ROWS and name not in self.fields:
                decoded[name] = BARS_REPLAY_FIXED_ROWS[name]
                continue
            if name in PERSISTENCE_FIELDS and name not in self.fields:
                continue  # keyed with both arms off, which leaves the rows out: `MoverConfig`'s off
            value = self.fields[name]
            if kind is Decimal:
                if not isinstance(value, str):
                    raise ValueError(f"{name}: Decimal must be text")
                value = Decimal(value)
            elif name == "trigger":
                value = TriggerType.from_str(value)
            elif name == "wind_down_at" and value is not None:
                value = datetime.fromisoformat(value)
            decoded[name] = value
        settings = MoverConfig(**decoded)
        check_bars_replay_modes(settings)
        return settings

    @property
    def rows(self) -> Rows:
        return Rows(**self.fields["replay"]["rows"])

    @property
    def part(self) -> str:
        part = self.fields["replay"]["part"]
        if part not in ("pre-market", "regular", "after-hours", "overnight"):
            raise ValueError(f"unknown ranked part {part!r}")
        return part

    @property
    def key(self) -> str:
        from .ledger import config_key

        return config_key(self.config)

    def encode(self) -> RankedPayload:
        return {
            "schema": SCHEMA,
            "config_key": self.key,
            "config": json.loads(json.dumps(self.config, allow_nan=False)),
        }

    @classmethod
    def decode(cls, payload: object, *, expected_key: str | None = None) -> RankedConfiguration:
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("unsupported ranked configuration schema")
        candidate = cls.from_config(payload["config"])
        if payload.get("config_key") != candidate.key or expected_key not in (None, candidate.key):
            raise ValueError("promoted configuration differs from the ranked key")
        return candidate
