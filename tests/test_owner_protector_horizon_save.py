"""A venue-reading retry that changes only the horizon is saved (rd-owner-protector F3).

The Codex second reader's finding on main 1b052804, held as the lead's ruling of 2026-11-14 10:15:
a successful retry of the venue reading changed only the entry's horizon, which `_reported` left
out, so the step wrote its heartbeat and kept the on-disk entry without one. A process lost before
another reported change left the next run to read that holding UNKNOWN and arm it below the
pre-restart level. The two cases are the reader's own probes, their assertions kept; process loss
is modelled by omitting on_stop. All prices and holdings are synthetic.
"""

from __future__ import annotations

import json

from nautilus_trader.model import Price

from tests import test_owner_protector as h
from tests.test_owner_protector_growth import step


def test_a_retried_venue_horizon_must_be_saved_without_a_price_change(tmp_path):
    venue = h.VenueOrders(failure=RuntimeError("synthetic first-read failure"))
    path = tmp_path / "levels.json"
    strategy = h.restart_harness(path, trading=venue)
    strategy.on_start()
    strategy.observe("1.05", h.T0 + 181 * h.SECOND)
    strategy.observe("1.05", h.T0 + 182 * h.SECOND)
    assert len(strategy.sent) == 1
    assert "horizon_ns" not in json.loads(path.read_text())["entries"][0]
    venue.failure = None
    step(strategy, h.T0 + 241 * h.SECOND)
    assert h.AAA in strategy.readings, "the retry itself must have succeeded"
    row = json.loads(path.read_text())["entries"][0]
    assert "horizon_ns" in row, (
        "step persisted its heartbeat but omitted the successful venue reading",
        row,
    )


def test_restart_after_successful_retry_must_keep_the_pre_restart_level(tmp_path):
    venue = h.VenueOrders(failure=RuntimeError("synthetic first-read failure"))
    path = tmp_path / "restart-levels.json"
    strategy = h.restart_harness(path, trading=venue)
    strategy.on_start()
    strategy.observe("1.05", h.T0 + 181 * h.SECOND)
    strategy.observe("1.05", h.T0 + 182 * h.SECOND)
    original_level = strategy.slots[h.AAA].level
    assert original_level == Price.from_str("1.01")
    assert original_level is not None
    venue.failure = None
    step(strategy, h.T0 + 241 * h.SECOND)
    assert h.AAA in strategy.readings
    # Simulate a process loss: no on_stop/final save; a new instance reads the completed step's file.
    successor = h.restart_harness(path, trading=h.VenueOrders())
    successor.cached.history = [
        b for b in successor.cached.history if b.ts_event < h.T0 + 180 * h.SECOND
    ]
    successor.cached.latest = h.quote("1.00", h.T0 + 242 * h.SECOND)
    successor.test_clock.set_time(h.T0 + 242 * h.SECOND)
    successor.on_start()
    level = successor.slots[h.AAA].level
    assert level is not None, successor.said
    assert level.as_decimal() >= original_level.as_decimal(), (
        "a successful retry was not durable: same holding restarted below its retained level",
        original_level,
        level,
        successor.said,
    )
