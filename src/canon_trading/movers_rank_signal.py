"""The movers ranking's signal value: built by the ranking actor, read by the mover strategy, one function each.

Contract: NautilusTrader's signal API at the pin (v2.0.0rc8): `DataActor.publish_signal(name, value,
ts_event)`, `DataActor.subscribe_signal(name)` and `DataActor.on_signal(signal)`
(`nautilus_trader/common/__init__.pyi`, the installed package's stubs, class `DataActor`; `Strategy` has the
same three). The value is carried as text: the pyo3 binding takes `str(value)`, and the `Signal` it
publishes holds `name`, `value: String`, `ts_event` and `ts_init` (`crates/common/src/python/actor.rs`,
`py_publish_signal`, and `crates/common/src/signal.rs` at the pinned commit). `ts_event` is the time the
ranking was computed; a value of 0 makes upstream take the clock's time. A signal is not kept for a
subscriber that arrives later: one published before a subscription is never delivered to it (observed at
v2.0.0rc8 in a backtest engine, 2026-11-11), so a reader with no signal yet has no ranking.

The value's form is this repository's own, decided by the lead for the movers work on 2026-11-11 (record 0087, landing with
the ranking actor, states it):
at most `MAX_ENTRIES` entries, best first, joined by `;`, each `<instrument id>=<move>`, for example
`NXXT.ALPACA=9.5094;NCT.ALPACA=1.3639`. An instrument id is NautilusTrader's `InstrumentId` on the
`ALPACA` venue (`canon_alpaca.constants.VENUE`); a move is written in Python's shortest round-trip form
(`repr` of the float), so reading a value back gives the same floats and the same text. An empty ranking
is the empty string. Anything else is refused by name, never repaired: a reader that cannot parse the value
has no ranking, and no ranking means no entry. A wrong type is a `TypeError`; every other refusal is a
`ValueError`. How old a value may be is the reader's own rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from canon_alpaca.constants import VENUE
from nautilus_trader.model import InstrumentId

if TYPE_CHECKING:
    from collections.abc import Sequence

SIGNAL_NAME = "movers-rank"
MAX_ENTRIES = 20
ENTRY_SEPARATOR = ";"
MOVE_SEPARATOR = "="


@dataclass(frozen=True)
class RankEntry:
    """One ranked instrument and its move; one that could not cross the bus is refused when built."""

    instrument_id: InstrumentId
    move: float

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, InstrumentId):
            raise TypeError(f"{self.instrument_id!r} is not an InstrumentId")
        text = str(self.instrument_id)
        if any(c.isspace() for c in text):
            raise ValueError(f"{text!r} holds whitespace")
        if not text.isprintable():
            raise ValueError(f"{text!r} holds a control character")
        if ENTRY_SEPARATOR in text or MOVE_SEPARATOR in text:
            raise ValueError(
                f"{text} holds a separator ({ENTRY_SEPARATOR!r} or {MOVE_SEPARATOR!r})"
            )
        if self.instrument_id.venue != VENUE:
            raise ValueError(f"{text} is on venue {self.instrument_id.venue}, not {VENUE}")
        if type(self.move) is not float:  # an int or a bool would not read back as itself
            raise TypeError(f"{text} has a move that is not a float: {self.move!r}")
        if not math.isfinite(self.move):
            raise ValueError(f"{text} has a move that is not finite: {self.move}")


def _checked(count: int) -> None:
    if count > MAX_ENTRIES:
        raise ValueError(
            f"a ranking holds at most {MAX_ENTRIES} entries; this one has {count} entries"
        )


def _distinct(entries: Sequence[RankEntry]) -> None:
    seen: set[InstrumentId] = set()
    for e in entries:
        if e.instrument_id in seen:
            raise ValueError(f"the ranking holds {e.instrument_id} twice")
        seen.add(e.instrument_id)


def encode_ranking(entries: Sequence[RankEntry]) -> str:
    """Build the signal value from entries, best first; refuse what `decode_ranking` would not read back."""
    _checked(len(entries))
    _distinct(entries)
    parts = [f"{e.instrument_id}{MOVE_SEPARATOR}{e.move!r}" for e in entries]
    return ENTRY_SEPARATOR.join(parts)


def decode_ranking(value: str) -> tuple[RankEntry, ...]:
    """Read a signal value back into entries, best first, refusing any value `encode_ranking` would not build."""
    if value == "":
        return ()
    texts = value.split(ENTRY_SEPARATOR)
    _checked(len(texts))
    entries = []
    for text in texts:
        id_text, sep, move_text = text.partition(MOVE_SEPARATOR)
        if not sep or not id_text or not move_text:
            raise ValueError(f"entry {text!r} is not <instrument id>{MOVE_SEPARATOR}<move>")
        try:
            move = float(move_text)
        except ValueError:
            raise ValueError(f"entry {text!r} has a move that is not a number") from None
        if math.isfinite(move) and repr(move) != move_text:
            raise ValueError(
                f"entry {text!r} does not write its move in the shortest form, {move!r}"
            )
        try:
            instrument_id = InstrumentId.from_str(id_text)
        except ValueError as e:
            raise ValueError(f"entry {text!r} has no instrument id: {e}") from None
        if str(instrument_id) != id_text:
            raise ValueError(f"entry {text!r} does not read back as {instrument_id}")
        entries.append(RankEntry(instrument_id, move))
    result = tuple(entries)
    _distinct(result)
    return result
