"""A venue latency counted from each command's send, for the replays' simulated venue.

Contract: nautilus-trader v2.0.0rc8 at d53849b216e2. ``nautilus_trader.execution.LatencyModel`` is the
documented Python base for command-specific models: each method "returns the effective insertion
(modification, cancellation) delay in nanoseconds at current clock time ``ts_now``"
(crates/execution/src/python/latency.rs:31-60), and a Python subclass is accepted wherever a latency
model is (python/latency.rs:193-213). The simulated exchange sets a command's arrival at
``command.ts_init`` plus the returned delay (crates/backtest/src/exchange.rs:952).

Why not ``StaticLatencyModel``: the order emulator releases a triggered order by resending its original
SubmitOrder, which keeps its first ``ts_init`` (crates/execution/src/order_emulator/emulator.rs:1522-1594;
upstream's own test expects the released command equal to the original but for the order,
emulator.rs:4579 and :4636). A fixed delay is then long past by the release, and the released order
meets no latency at all. This model returns ``(ts_now - ts_init) + delay``, so every command arrives
``delay`` after it is sent: for a direct submit (``ts_now == ts_init``) exactly what
``StaticLatencyModel(0, insert, update, cancel)`` gives, and for a released order the same delay counted
from its release.

A command handed in with ``ts_now`` before its ``ts_init`` is refused by name. The engine latches the
error and fails the run (exchange.rs:343), so no command is ever given a negative delay.

The delays are a run's own (the spec's ``latency`` mapping, nanoseconds); this module holds no figure.
Absent, a replay passes no latency model, as before. Not shown: a venue's queueing under load, and that a
command settles only on its own instrument's data or a timer step (exchange.rs:99-107), which can hold a
thin name's command past its delay.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nautilus_trader.execution import LatencyModel

KEYS = ("insert_ns", "update_ns", "delete_ns")
# A bound far above any venue answer, and far inside upstream's u64 nanosecond delay and its
# checked ts_init addition (python/latency.rs, exchange.rs:952), so a figure no run means is
# refused with the spec rather than failing the engine mid-run.
MAX_NS = 60_000_000_000
KIND = "counted from send"


class SendLatency(LatencyModel):
    """Each command arrives its delay after ``ts_now``, the time the venue was handed it."""

    def __init__(self, insert_ns: int, update_ns: int, delete_ns: int) -> None:
        for name, value in zip(KEYS, (insert_ns, update_ns, delete_ns), strict=True):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_NS:
                raise ValueError(
                    f"latency {name} must be a whole number of nanoseconds from 0 to {MAX_NS}"
                )
        self.insert_ns = insert_ns
        self.update_ns = update_ns
        self.delete_ns = delete_ns

    @staticmethod
    def _from_send(command: Any, ts_now: int, delay: int) -> int:
        ts_init = int(command.ts_init)
        if ts_now < ts_init:
            raise ValueError(f"ts_now {ts_now} is before the command's ts_init {ts_init}")
        return (ts_now - ts_init) + delay

    def get_insert_latency(self, _command: Any, _ts_now: int) -> int:
        return self._from_send(_command, _ts_now, self.insert_ns)

    def get_update_latency(self, _command: Any, _ts_now: int) -> int:
        return self._from_send(_command, _ts_now, self.update_ns)

    def get_delete_latency(self, _command: Any, _ts_now: int) -> int:
        return self._from_send(_command, _ts_now, self.delete_ns)


def checked(value: Any) -> dict[str, int] | None:
    """The spec's ``latency`` mapping, refused unless it is exactly the three delays; None if absent."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(KEYS):
        raise ValueError(f"latency must map exactly {list(KEYS)} to nanoseconds")
    SendLatency(*(value[key] for key in KEYS))
    return {key: value[key] for key in KEYS}


def longest(value: Mapping[str, int] | None) -> int:
    """The largest of the three delays, 0 when the spec names none."""
    delays = checked(value)
    return 0 if delays is None else max(delays.values())


def model(value: Mapping[str, int] | None) -> SendLatency | None:
    """A fresh model for one venue, or None (no latency) when the spec names none."""
    delays = checked(value)
    return None if delays is None else SendLatency(*(delays[key] for key in KEYS))


def describe(value: Mapping[str, int] | None) -> dict[str, Any] | None:
    """The report's plain form of the model a venue was given."""
    delays = checked(value)
    return None if delays is None else {"model": "SendLatency", "kind": KIND, **delays}
