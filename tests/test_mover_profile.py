"""Amendment 10 through T1/T2's offline engine and order seam; no paper account or credential."""

import hashlib
import json
import re
from collections import deque
from dataclasses import replace
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from nautilus_trader.common import Logger
from nautilus_trader.common import logging_sync_to_disk
from nautilus_trader.model import Bar
from nautilus_trader.model import OrderSide
from nautilus_trader.model import Price

from canon_trading import mover
from canon_trading import movers_rank
from canon_trading import session_window
from canon_trading import trail
from canon_trading import trail_profile
from tests.test_mover_trail import AAA
from tests.test_mover_trail import MINUTE
from tests.test_mover_trail import RISING
from tests.test_mover_trail import SECOND
from tests.test_mover_trail import T0
from tests.test_mover_trail import Seam
from tests.test_mover_trail import bar_type
from tests.test_mover_trail import engine
from tests.test_mover_trail import quote
from tests.test_mover_trail import rank
from tests.test_mover_trail import ranker
from tests.test_mover_trail import run
from tests.test_mover_trail import seam_slot
from tests.test_mover_trail import strategy
from tests.test_mover_trail import tape
from tests.test_mover_trail import warmed
from tests.test_mover_window import MoverPick
from tests.test_mover_window import WindowConfig
from tests.test_trail import BASE
from tests.test_trail import absent_long_document
from tests.test_trail import arithmetic
from tests.test_trail import profile_document

D = Decimal


@pytest.fixture(autouse=True)
def windows_account(monkeypatch):
    """Each window here runs on paper account B, the windows' account, so a Mover window has its loss
    share (S2: a Mover window with no named account refuses before any request)."""
    monkeypatch.setenv(session_window.ACCOUNT_ENV, "windows")


def profile_strategy(**rows):
    s = strategy(trail_mode="profile", **rows)
    s.trail_profile = profile_document()
    return s


def test_profile_is_registered_without_changing_the_strategy_default():
    assert "profile" in mover.TRAIL_SETTINGS["trail_mode"]["arms"]
    config = mover.MoverConfig(max_notional=D(100), max_move=D(5))
    assert config.trail_mode == "fixed"
    for field, default in BASE.items():
        assert getattr(config, field) == default and field in mover.OWN_FIELDS
    s = profile_strategy()
    assert s.rdc_1 == {}
    assert all(field in s.record()["rows"] for field in BASE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_k", D(4)),
        ("profile_k", D("NaN")),
        ("profile_pctl", "p99"),
        ("profile_floor", "p90"),
        ("profile_multipliers", (D(1), D(2), D(3))),
        ("profile_multipliers", (D(1), D("NaN"), D(1))),
    ],
)
def test_both_configs_refuse_unregistered_profile_settings(field, value):
    with pytest.raises(ValueError, match=field):
        strategy(**{field: value})
    with pytest.raises(ValueError, match=field):
        WindowConfig(**{field: value})


def test_the_entry_record_names_profile_inputs_even_with_a_cold_atr():
    eng = engine()
    s = profile_strategy(atr_n=30)
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        [entry] = s.record()["entries"]
        assert any(o.side == OrderSide.BUY and o.filled_qty.is_positive() for o in orders)
        assert entry["trail_mode"] == entry["trail_source"] == "profile"
        assert D(entry["trail"]) == D("0.007") and entry["trail_bound"] == "profile"
        assert entry["profile"] == {
            "symbol": "AAA",
            "segment": 60,
            "K": "2",
            "pctl": "p75",
            "R": "0.003",
            "r_window": "recent",
            "ATR": None,
            "floor_setting": "median",
            "floor": "median",
            "F": "0.007",
            "f_window": "recent",
            "ratio": "unknown",
            "bucket": "under 3",
            "V": "1",
            "multipliers": ["1", "1", "1"],
            "distance": "0.007",
            "reason": None,
        }
        assert entry["trail_changes"] == []
        assert "profile_tightenings" not in entry
        assert D(entry["exit_through"]) == D(entry["trail"]) / 4
        assert D(entry["chase_cap"]) == max(D(entry["trail"]), s.settings.chase_cap)
        json.dumps(s.record())
    finally:
        eng.dispose()


@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize(
    ("thin", "reason"),
    [
        ("absent", "not in the profile"),
        ("sessions", "4 recent sessions"),
        ("range", "no true_range statistic"),
    ],
)
def test_thin_entry_records_the_existing_warm_or_cold_path_and_reason(cold, thin, reason):
    eng = engine()
    s = profile_strategy(atr_n=30 if cold else 3)
    doc = s.trail_profile
    if thin == "absent":
        doc["symbols"] = {}
    else:
        for window in ("recent", "long"):
            for row in doc["symbols"]["AAA"][window]:
                if thin == "sessions" and window == "recent":
                    row["sessions"] = 4
                if thin == "range":
                    row["true_range"]["p75"] = None
    try:
        run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        [entry] = s.record()["entries"]
        source = "cold_atr_fallback" if cold else "atr"
        assert entry["trail_source"] == source  # categorical; the reason is in the calculation
        assert entry["profile"]["reason"] == reason
        assert entry["profile"]["distance"] is None
        if cold:
            assert entry["trail"] == "0.02"
        else:
            assert D(entry["trail"]) == D(entry["atr"]) / D(
                str(next(o.avg_px for o in eng.cache.orders() if o.side == OrderSide.BUY))
            )
    finally:
        eng.dispose()


@pytest.mark.parametrize("baseline", ["1000000", "2000000"])
def test_the_top_tier_keeps_tier_under_profile(baseline):
    s, slot, latest = warmed("tier", baseline=baseline)
    s.settings.trail_mode = "profile"
    s.trail_profile = profile_document()
    s.rdc_1 = {}
    plan = s._entry_trail(slot, D(10), latest, now=T0)
    assert (plan.trail, plan.bound, plan.source) == (D("0.002"), "tier", "tier")


@pytest.mark.parametrize(
    ("spread", "r", "expected", "bound"),
    [("0.05", 0.003, "0.01", "spread floor"), ("0.005", 0.03, "0.02", "0.02 cap")],
)
def test_the_existing_bounds_apply_to_profile(spread, r, expected, bound):
    s, slot, latest = warmed("atr", spread=spread)
    s.settings.trail_mode = "profile"
    s.trail_profile = profile_document()
    s.rdc_1 = {}
    s.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = r
    plan = s._entry_trail(slot, D(10), latest, now=T0)
    assert (plan.trail, plan.bound, plan.source) == (D(expected), bound, "profile")


def test_profile_preserves_the_unusable_spread_and_impossible_floor_cases():
    s, slot, _ = warmed("atr")
    s.settings.trail_mode = "profile"
    s.trail_profile = profile_document()
    s.rdc_1 = {}
    plan = s._entry_trail(slot, D(10), quote("0"), now=T0)
    assert plan.bound == "fixed" and "no usable quoted spread" in plan.note
    with pytest.raises(ValueError, match="spread floor.*0.02 cap"):
        s._entry_trail(slot, D(10), quote("0.11"), now=T0)


def test_profile_without_a_document_refuses_every_entry_by_name():
    eng = engine()
    s = strategy(trail_mode="atr")
    s.settings.trail_mode = "profile"
    s.trail_profile = None
    s.baseline_dollar_volume_per_minute[AAA] = D(2_000_000)
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        assert not any(o.side == OrderSide.BUY for o in orders)
        assert any("profile document is missing" in why for why in s.dropped)
    finally:
        eng.dispose()


class ProfileSeam(Seam):
    """T1/T2's same cancel/remake seam, including a synchronous emulator cancellation."""

    def __init__(self, bid, **rows):
        super().__init__(bid, **rows)
        self.trail_profile = profile_document()
        self.rdc_1 = {}
        self.baseline_dollar_volume_per_minute = {}

    def _profile_tighten(self, slot, now):
        return mover.Mover._profile_tighten(cast(Any, self), slot, now)

    def _trail_change(self, slot, now, before, plan, why, profile=None):
        mover.Mover._trail_change(cast(Any, self), slot, now, before, plan, why, profile)

    def _opening_run(self, slot, bar):
        mover.Mover._opening_run(cast(Any, self), slot, bar)

    def _top_tier(self, slot):
        return mover.Mover._top_tier(cast(Any, self), slot)

    def is_exiting(self):
        return False  # no upstream market exit runs in the seam

    def _profile_skip(self, slot, now, minute_ns, reason):
        return mover.Mover._profile_skip(cast(Any, self), slot, now, minute_ns, reason)

    def cancel_order(self, client_order_id):
        super().cancel_order(client_order_id)
        order = self._order(client_order_id)
        assert order is not None
        order.is_closed, order.is_emulated = True, False

    def _instrument(self, _instrument_id):
        instrument = super()._instrument(_instrument_id)
        instrument.price_increment = Price.from_str("0.0100")
        return instrument


def holding(sized_loss=None, **rows):
    """A profile holding bought at 10.00 on a 0.02 plan; with `sized_loss`, a budget-sized entry whose
    first trailing sell trails by that L, as every paper window's entry is (S2)."""
    if sized_loss is not None:
        rows.update(size_from_budget=True, loss_share=mover.PAPER_LOSS_SHARE)
    seam = ProfileSeam("10.50", trail_mode="profile", **rows)
    slot = seam_slot(seam)
    slot.sized_loss = None if sized_loss is None else D(sized_loss)
    slot.bar_type = bar_type(AAA)
    slot.dollars = deque(maxlen=5)
    slot.closes = deque([D(10)], maxlen=4)
    slot.run_open_ns = T0
    slot.atr = cast(Any, SimpleNamespace(initialized=True, value=0.01))
    slot.trail_plan = slot.initial_trail = mover.TrailPlan(
        D("0.02"), D("0.005"), D("0.02"), None, D("0.005"), "profile", source="profile"
    )
    slot.entry_record = {"client_order_id": "E-1", "trail_changes": []}
    seam._protect(slot, D(5), T0)
    seam.sent[0].trigger_price = Price.from_str("10.4000")
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str(seam.bid), ask_price=Price.from_str(str(D(seam.bid) + D("0.005")))
    )
    return seam, slot


def test_narrower_profile_remakes_the_sell_from_its_reached_trigger_and_records_it():
    seam, slot = holding()
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 2 and seam.canceled == [seam.sent[0].client_order_id.value]
    assert seam.sent[1].trigger_price == Price.from_str("10.4000")
    assert seam.sent[1].trailing_offset == D("0.07")
    assert slot.trail_plan.trail == D("0.007")
    assert slot.trail_plan.exit_through == D("0.00175")
    assert slot.trail_plan.chase_cap == D("0.02")
    assert slot.exit_record.trail_distance == D("0.07")
    assert slot.entry_record["trail_changes"] == [
        {
            "when_ns": T0 + 2 * MINUTE,
            "from": "0.02",
            "to": "0.007",
            "bound": "profile",
            "atr": "0.01",
            "why": "profile segment 60",
            "profile": trail.profile_record(slot.trail_plan.profile),
        }
    ]
    assert slot.ratcheted is False


def test_a_profile_tighten_refreshes_the_records_calculation_and_keeps_the_one_at_entry():
    """L4's review of the 09:30 join, item 7: after a tighten the entry's `profile` is the calculation
    in force, the change carries it too, and the calculation the entry was made from is kept."""
    seam, slot = holding()
    at_entry = {"segment": 59, "distance": "0.02", "reason": None}
    slot.entry_record["profile"] = dict(at_entry)
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    record = slot.entry_record
    assert record["profile"] == trail.profile_record(slot.trail_plan.profile)
    assert (record["profile"]["segment"], record["profile"]["distance"]) == (60, "0.007")
    assert record["trail_changes"][0]["profile"] == record["profile"]
    assert record["profile_at_entry"] == at_entry
    json.dumps(record)
    slot.run_open_ns = T0 + 2 * MINUTE
    seam.sent[1].is_emulated, seam.sent[1].is_closed = True, False
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.002
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["pullback"]["median"] = 0.001
    seam._protect(slot, D(5), T0 + 3 * MINUTE)
    assert len(record["trail_changes"]) == 2
    assert record["profile"] == record["trail_changes"][1]["profile"]
    assert record["profile_at_entry"] == at_entry


@pytest.mark.parametrize("kind", ["equal", "wider", "thin", "unusable spread", "floor over cap"])
def test_a_non_narrower_or_unusable_update_leaves_the_working_sell_alone(kind):
    seam, slot = holding()
    slot.trail_plan = replace(slot.trail_plan, trail=D("0.007"))
    if kind == "wider":
        seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.03
    elif kind == "thin":
        seam.trail_profile["symbols"]["AAA"]["recent"][60]["sessions"] = 4
    elif kind in ("unusable spread", "floor over cap"):
        seam.cache.quote = lambda _: quote("0" if kind == "unusable spread" else "0.11")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.trail_plan.trail == D("0.007")
    assert slot.entry_record["trail_changes"] == []


def test_profile_evaluates_once_per_complete_bar_and_uses_that_bars_segment(monkeypatch):
    seam, slot = holding()
    seen = []
    real = arithmetic().profile_distance

    def measure(*args, **kwargs):
        seen.append((args[2], args[4]))
        return real(*args, **kwargs)

    monkeypatch.setattr(mover, "profile_distance", measure)
    slot.run_open_ns = T0 + 15 * MINUTE
    seam.rdc_1[AAA] = D(3)
    for second in range(10):
        seam._protect(slot, D(5), T0 + 16 * MINUTE + second * SECOND)
    assert seen == [(61, D(3))]
    assert slot.entry_record["trail_changes"][0]["why"] == "profile segment 61"
    slot.run_open_ns += MINUTE
    seam._protect(slot, D(5), T0 + 17 * MINUTE)
    assert seen == [(61, D(3)), (61, D(3))]


def test_a_fraction_change_that_does_not_save_a_price_increment_does_not_remake():
    seam, slot = holding()
    slot.trail_plan = replace(slot.trail_plan, trail=D("0.007"))
    seam.settings.profile_floor = "off"
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.003495
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.entry_record["trail_changes"] == []


def test_profile_does_not_tighten_the_top_tier():
    seam, slot = holding()
    slot.trail_plan = slot.initial_trail = replace(
        slot.trail_plan, trail=D("0.002"), bound="tier", source="tier"
    )
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 1 and seam.canceled == []


def test_turn_measures_d_in_the_actual_profile_price_distance(monkeypatch):
    seam, slot = holding()
    seam.settings.reentry_arm, seam.settings.turn_d = "turn", D(2)
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    s = profile_strategy(reentry_arm="turn", turn_d=D(2))
    s.slots[AAA] = slot
    s.rank, s.rank_ns = [(AAA, D("0.25"))], T0 + 3 * MINUTE
    s.sold[AAA] = (T0 + 2 * MINUTE, D(10))
    state = mover.ReentryState(
        T0 + 2 * MINUTE, D(10), slot.exit_record.trail_distance, low=D("9.87"), rises=2
    )
    s.reentry_states[AAA] = state
    monkeypatch.setattr(s, "_busy", lambda: 0)
    reason = s._refusal(slot, D(10), {AAA: (0, D("0.25"))}, 0, T0 + 3 * MINUTE)
    assert "turn depth" in reason  # 0.13 is less than 2 * the actual 0.07 PRICE distance.
    state.low = D("9.86")
    assert s._refusal(slot, D(10), {AAA: (0, D("0.25"))}, 0, T0 + 3 * MINUTE) is None


def node_case(monkeypatch, tmp_path, *, mode="profile", doc=None, **rows):
    path = tmp_path / "trail-profile.json"
    if doc is not None:
        path.write_text(json.dumps(doc), encoding="utf-8")
    built = []
    node = SimpleNamespace(add_strategy=lambda _: None, add_actor=lambda _: None)

    class Builder:
        def with_logging(self, _):
            return self

        def build(self):
            built.append(True)
            return node

    monkeypatch.setattr(session_window, "alpaca_paper_builder", lambda *a, **k: Builder())
    monkeypatch.setattr(session_window, "window_rank", lambda *a, **k: object())
    config = WindowConfig(
        symbol="AAA",
        strategy="Mover",
        mover=True,
        log_dir=tmp_path,
        trail_mode=mode,
        trail_profile_path=path,
        **rows,
    )
    ends = datetime(2026, 11, 12, 10, tzinfo=session_window.NEW_YORK)
    pick = MoverPick("AAA", D(10), {"inputs": {"move": 0.25}}, D(10))

    def build():
        return session_window.window_node(
            config,
            wind_down_at=ends - timedelta(seconds=session_window.FLATTEN_SECS),
            log_name="profile-test",
            feed="sip",
            pick=pick,
            ends=ends,
            entry_notional=D(1000),
            loss_share=mover.PAPER_LOSS_SHARE,
        )

    return build, built, pick, path


def test_window_defaults_to_fixed_and_has_the_registered_profile_cli_rows(tmp_path):
    config = WindowConfig()
    assert config.trail_mode == "fixed" and config.trail_profile_path is None
    defaults = session_window._args(["--log-dir", str(tmp_path)])
    # a flag not given stays unset (ADR 0106, 2026-11-12/13); a non-Mover window keeps the fixed trail
    assert defaults.trail_mode is None
    assert session_window.window_config(defaults, "regular").trail_mode == "fixed"
    args = session_window._args(
        [
            "--strategy",
            "Mover",
            "--mover",
            "--trail-mode",
            "profile",
            "--profile-k",
            "3",
            "--profile-pctl",
            "p90",
            "--profile-floor",
            "off",
            "--profile-multipliers",
            "1",
            "1.25",
            "1.5",
            "--trail-profile-path",
            "profiles/today.json",
            "--log-dir",
            str(tmp_path),
        ]
    )
    for field, value in {
        "trail_mode": "profile",
        "profile_k": D(3),
        "profile_pctl": "p90",
        "profile_floor": "off",
        "profile_multipliers": (D(1), D("1.25"), D("1.5")),
        "trail_profile_path": Path("profiles/today.json"),
    }.items():
        assert getattr(args, field) == value


def test_window_reads_once_before_build_and_records_the_exact_file_and_counts(
    monkeypatch, tmp_path
):
    build, built, pick, path = node_case(monkeypatch, tmp_path, doc=profile_document())
    raw = path.read_bytes()
    reads = []
    byte_reads = []
    real = trail_profile.read_profile
    real_bytes = Path.read_bytes

    def read(path, *, as_of):
        reads.append((str(path), as_of))
        assert built == []
        return real(path, as_of=as_of)

    monkeypatch.setattr(trail_profile, "read_profile", read)

    def read_bytes(path):
        byte_reads.append(str(path))
        return real_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    _, s, _ = build()
    assert reads == [(str(path), date(2026, 11, 12))]
    assert byte_reads == [str(path)] * 2  # hashed before and after the strict reader's one read
    assert s.trail_profile == profile_document() and s.rdc_1 == {}
    assert all(getattr(s.settings, name) == base for name, base in BASE.items())
    assert pick.record["trail_profile"] == {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "as_of": "2025-11-10",
        "written_at": "2025-11-09T23:00:00+00:00",
        "symbols": 1,
        "windows": "recent and long",
        "window_instruments": 1,
    }
    assert pick.record["rdc_1"] == "absent"


@pytest.mark.parametrize("kind", ["missing", "future"])
def test_missing_or_future_profile_refuses_before_the_node_can_place_an_order(
    monkeypatch, tmp_path, kind
):
    doc = None
    if kind == "future":
        doc = profile_document()
        doc["as_of"] = "2026-11-13"
    build, built, _, path = node_case(monkeypatch, tmp_path, doc=doc)
    with pytest.raises(
        session_window.WindowRefused, match="trail profile.*refused|trail-profile.json.*refused"
    ) as refused:
        build()
    assert str(path) in str(refused.value) and built == []
    assert ("later" if kind == "future" else "No such file") in str(refused.value)


def test_tier_does_not_open_a_profile_file(monkeypatch, tmp_path):
    build, built, _, _ = node_case(monkeypatch, tmp_path, mode="tier")
    monkeypatch.setattr(
        trail_profile, "read_profile", lambda *a, **k: pytest.fail("profile opened")
    )
    _, s, _ = build()
    assert built == [True] and s.trail_profile is None


def test_default_profile_path_is_resolved_like_the_baseline():
    config = WindowConfig()
    assert (
        session_window.mover_trail_profile_path(config)
        == (session_window.SOURCE_ROOT / "catalog/movers-baseline/trail-profile-v1.json").resolve()
    )
    config = replace(config, trail_profile_path=Path("elsewhere/today.json"))
    assert (
        session_window.mover_trail_profile_path(config)
        == (session_window.SOURCE_ROOT / "elsewhere/today.json").resolve()
    )


def test_a_real_emulator_holding_tightens_at_the_new_segment_and_flattens():
    eng = engine()
    s = profile_strategy(atr_n=30, entries=1)
    for window in ("recent", "long"):
        s.trail_profile["symbols"]["AAA"][window][61]["true_range"] = {
            "median": 0.001,
            "p75": 0.002,
            "p90": 0.003,
        }
        s.trail_profile["symbols"]["AAA"][window][61]["pullback"]["median"] = 0.003
    closes = [10 + i / 100 for i in range(25)] + [10.15, 10.10, 10.00, 9.90]
    try:
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes, spread=0.001))
        buys = [o for o in orders if o.side == OrderSide.BUY]
        sales = [o for o in orders if o.side == OrderSide.SELL]
        assert len(buys) == 1 and buys[0].filled_qty.is_positive()
        assert len(sales) >= 2 and sales[0].status.name == "CANCELED"
        assert s.triggers and s.triggers == sorted(s.triggers)
        [entry] = s.record()["entries"]
        assert entry["trail_changes"] == [
            {
                "when_ns": T0 + 16 * MINUTE,
                "from": "0.007",
                "to": "0.004",
                "bound": "profile",
                "atr": None,
                "why": "profile segment 61",
                "profile": entry["profile"],
            }
        ]
        assert (entry["profile"]["segment"], entry["profile"]["distance"]) == (61, "0.004")
        assert (entry["profile_at_entry"]["segment"], entry["profile_at_entry"]["distance"]) == (
            60,
            "0.007",
        )
        json.dumps(s.record())
        [exit_row] = s.record()["exits"]
        assert (
            D(exit_row["trail_distance"])
            == mover.offsets(D(str(buys[0].avg_px)), D("0.004"), D("0.001"))[0]
        )
        assert not eng.cache.positions_open()
    finally:
        eng.dispose()


@pytest.mark.parametrize("internal", [False, True])
def test_each_complete_bar_uses_the_shared_protection_path_before_the_next_step(
    monkeypatch, internal
):
    seam, slot = holding()
    seam.settings.entry_arm = "bridge"
    slot.atr = None
    seam = cast(Any, seam)
    seam.slots, seam.live_bars, seam.reentry_states, seam.sold = {AAA: slot}, 0, {}, {}
    seam._held = lambda _: D(5)
    seen = []
    real = mover.profile_distance

    def measure(*args, **kwargs):
        seen.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(mover, "profile_distance", measure)
    bars = [b for b in tape(AAA, [10.0] * 3) if isinstance(b, Bar)]
    if internal:
        slot.bar_type = mover.BarType.from_str("AAA.ALPACA-1-MINUTE-LAST-INTERNAL")
        slot.subscribed_ns = T0 + MINUTE // 2
        bars = [
            Bar(slot.bar_type, b.open, b.high, b.low, b.close, b.volume, b.ts_init, b.ts_init)
            for b in bars
        ]
        mover.Mover.on_bar(seam, bars[0])
        assert seen == [] and seam.live_bars == 0  # partial first INTERNAL minute
    for bar in bars[1:]:
        mover.Mover.on_bar(seam, bar)
    assert seen == [60, 60] and seam.live_bars == 2
    for second in range(5):
        seam._protect(slot, D(5), T0 + 3 * MINUTE + second * SECOND)
    assert seen == [60, 60]


@pytest.mark.parametrize("kind", ["valid", "missing", "future", "tier"])
def test_the_running_window_reads_before_selection_and_records_before_any_node(
    monkeypatch, tmp_path, kind, references_in
):
    """Exercise run_window's actual composition, stopping at its initial record before a node exists.
    Its references are in before the node (`references_in`, main's #323)."""
    at = datetime(2026, 11, 12, 9, 30, tzinfo=session_window.NEW_YORK)
    path = tmp_path / "trail-profile.json"
    if kind in ("valid", "future"):
        doc = profile_document()
        if kind == "future":
            doc["as_of"] = "2026-11-13"
        path.write_text(json.dumps(doc), encoding="utf-8")
    config = WindowConfig(
        strategy="Mover",
        mover=True,
        slots=1,
        spares=0,
        log_dir=tmp_path,
        trail_mode="tier" if kind == "tier" else "profile",
        trail_profile_path=path,
    )
    calendar = [
        SimpleNamespace(
            date=(at - timedelta(days=i)).date(), open=at, close=at + timedelta(hours=6)
        )
        for i in (1, 0)
    ]
    monkeypatch.setattr(
        session_window, "session_part", lambda *a: (at + timedelta(hours=6), "regular")
    )
    monkeypatch.setattr(session_window, "warm_up_start", lambda *a, **k: at - timedelta(minutes=30))
    monkeypatch.setattr(session_window, "leftovers", lambda _: {})
    monkeypatch.setattr(session_window, "venue_no_shorting", lambda _: True)
    monkeypatch.setattr(
        movers_rank, "read_baselines", lambda *a: SimpleNamespace(problem=None, absent=False)
    )
    monkeypatch.setattr(
        session_window,
        "mover_allocation",
        lambda *a: session_window.MoverAllocation(D(1000), D(1000), 1, D(1000)),
    )
    pick = MoverPick("AAA", D(10), {"inputs": {"move": 0.25}}, D(10))
    selected = []

    def choose(*args, **kwargs):
        selected.append(True)
        return pick

    monkeypatch.setattr(session_window, "choose_mover", choose)
    monkeypatch.setattr(session_window, "window_data", lambda: object())
    monkeypatch.setattr(session_window, "mover_entry_limit", lambda *a, **k: D(10))
    monkeypatch.setattr(session_window, "mover_entry_arm", lambda _: ("bridge", "synthetic test"))
    monkeypatch.setattr(session_window, "request_record", lambda *a, **k: {})
    monkeypatch.setattr(session_window, "window_node", lambda *a, **k: pytest.fail("node built"))
    records = []

    def initial_record(_path, report):
        records.append(report)
        return session_window.WindowRefused("synthetic test reached the initial record")

    monkeypatch.setattr(session_window, "write_record", initial_record)
    reads = []
    real = trail_profile.read_profile

    def read(path, *, as_of):
        assert selected == [] and records == []
        reads.append((str(path), as_of))
        return real(path, as_of=as_of)

    monkeypatch.setattr(trail_profile, "read_profile", read)
    with pytest.raises(session_window.WindowRefused) as refused:
        session_window.run_window(
            config,
            trading=cast(Any, object()),
            market=(cast(Any, SimpleNamespace(timestamp=at)), cast(Any, calendar)),
        )
    if kind in ("missing", "future"):
        assert records == selected == []
        assert str(path) in str(refused.value)
        assert ("No such file" if kind == "missing" else "later") in str(refused.value)
    else:
        assert "initial record" in str(refused.value)
        [report] = records
        assert report.mover["trail_mode"] == ("tier" if kind == "tier" else "profile")
        assert report.mover["rdc_1"] == "absent"
        if kind == "valid":
            assert report.mover["trail_profile"]["window_instruments"] == 1
            assert report.mover["trail_profile"]["symbols"] == 1
            assert (
                report.mover["trail_profile"]["sha256"]
                == hashlib.sha256(path.read_bytes()).hexdigest()
            )
        else:
            assert "trail_profile" not in report.mover
    assert reads == ([] if kind == "tier" else [(str(path), date(2026, 11, 12))])


def test_the_command_line_passes_profile_rows_to_the_window(monkeypatch, tmp_path):
    captured = []

    def run_window(config, **kwargs):
        captured.append(config)
        return session_window.WindowReport("regular", "AAA", started="synthetic profile CLI")

    monkeypatch.setattr(session_window, "run_window", run_window)
    monkeypatch.setattr(session_window, "write_heartbeat", lambda *a: None)
    args = session_window._args(
        [
            "--strategy",
            "Mover",
            "--mover",
            "--trail-mode",
            "profile",
            "--profile-k",
            "3",
            "--profile-pctl",
            "p90",
            "--profile-floor",
            "off",
            "--profile-multipliers",
            "1",
            "1",
            "1",
            "--trail-profile-path",
            "profiles.json",
            "--log-dir",
            str(tmp_path),
        ]
    )
    assert session_window._main(args) == 0
    [config] = captured
    assert config.trail_mode == "profile" and config.trail_profile_path == Path("profiles.json")
    assert (
        config.profile_k == D(3) and config.profile_pctl == "p90" and config.profile_floor == "off"
    )
    assert config.profile_multipliers == (D(1), D(1), D(1))  # RDc_1 absent: only the default runs


def test_a_complete_bar_does_not_protect_a_slot_with_no_holding(monkeypatch):
    seam, slot = holding()
    seam = cast(Any, seam)
    seam.settings.entry_arm = "bridge"
    seam.slots, seam.live_bars, seam.reentry_states, seam.sold = {AAA: slot}, 0, {}, {}
    seam._held = lambda _: D(0)
    slot.atr = None
    monkeypatch.setattr(seam, "_protect", lambda *a: pytest.fail("protection called while flat"))
    bar = next(b for b in tape(AAA, [10.0]) if isinstance(b, Bar))
    mover.Mover.on_bar(seam, bar)


@pytest.mark.parametrize("release_afterward", [False, True])
def test_tightening_sends_no_trading_command_out_of_the_native_emulator(capfd, release_afterward):
    """rc8 emulator.rs:1811–1820 logs every outgoing risk/exec TradingCommand.

    Count those records at the native boundary, excluding data subscriptions. A later triggered
    release is the positive control: the same capture must observe its outbound SubmitOrder.
    Python MessageBus.register targets Any endpoints, so it cannot count this typed Rust route.
    """
    eng = engine()
    s = profile_strategy(atr_n=30, entries=1)
    for window in ("recent", "long"):
        row = s.trail_profile["symbols"]["AAA"][window][61]
        row["true_range"] = {"median": 0.001, "p75": 0.002, "p90": 0.003}
        row["pullback"]["median"] = 0.003
    closes = [10 + i / 100 for i in range(25)]
    if release_afterward:
        closes += [10.15, 10.10, 10.00, 9.90]
    logger = Logger("ProfileEmulatorCommandCounter")

    def through_marker(marker):
        logger.info(marker)
        # rc8 logger.rs:1136 flushes stdout before acknowledging Sync, even with no file writer.
        assert logging_sync_to_disk()
        captured = capfd.readouterr().out
        assert marker in captured, "native logger did not acknowledge the command-counter marker"
        return captured

    try:
        through_marker("PROFILE_COMMAND_COUNTER_BEGIN")
        orders = run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, closes, spread=0.001))
        logged = re.sub(r"\x1b\[[0-9;]*m", "", through_marker("PROFILE_COMMAND_COUNTER_END"))
        outgoing = [
            line
            for line in logged.splitlines()
            if "nautilus_execution::order_emulator::emulator:" in line
            and "[CMD]-->" in line
            and re.search(
                r"\b(?:SubmitOrder(?:List)?|ModifyOrder|Cancel(?:Order|Orders|AllOrders))\(", line
            )
        ]
        sales = [o for o in orders if o.side == OrderSide.SELL]
        assert len(sales) >= 2 and sales[0].status.name == "CANCELED"
        tightened_at = datetime.fromtimestamp((T0 + 16 * MINUTE) / SECOND, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        assert not [line for line in outgoing if tightened_at + "." in line], outgoing
        if release_afterward:
            assert any("SubmitOrder(" in line for line in outgoing), (
                "release positive control was not counted"
            )
            assert not eng.cache.positions_open()
        else:
            assert outgoing == [], (
                f"tightening sent a trading request out of the emulator: {outgoing}"
            )
            assert sales[1].order_type.name == "TRAILING_STOP_LIMIT"
            assert eng.cache.positions_open()
    finally:
        eng.dispose()


@pytest.mark.parametrize(
    "flags",
    [
        ["--profile-k", "abc"],
        ["--profile-k", "4"],
        ["--profile-k", "NaN"],
        ["--profile-multipliers", "1", "x", "3"],
        ["--profile-multipliers", "1", "2", "3"],
        ["--profile-multipliers", "1", "1.25"],
    ],
)
def test_a_profile_flag_that_is_not_a_registered_value_is_a_usage_error(flags, tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        session_window._args([*flags, "--log-dir", str(tmp_path)])
    assert stopped.value.code == 2
    assert flags[0] in capsys.readouterr().err


def test_the_profile_k_flag_offers_exactly_the_registered_values(tmp_path):
    row = trail.PROFILE_SETTINGS["profile_k"]
    for value in (row["default"], *row["alternates"]):
        # a profile flag is Mover's, refused on another strategy's window like any trail flag
        argv = ["--strategy", "Mover", "--mover", "--profile-k", str(value)]
        args = session_window._args([*argv, "--log-dir", str(tmp_path)])
        assert args.profile_k == value


def test_a_profile_replaced_while_it_is_read_refuses_the_window(monkeypatch, tmp_path):
    path = tmp_path / "trail-profile.json"
    path.write_text(json.dumps(profile_document()), encoding="utf-8")
    config = WindowConfig(
        strategy="Mover", mover=True, trail_mode="profile", trail_profile_path=path
    )
    real = trail_profile.read_profile

    def read_then_replace(read_path, *, as_of):
        document = real(read_path, as_of=as_of)
        path.write_text(json.dumps(profile_document()) + "\n", encoding="utf-8")
        return document

    monkeypatch.setattr(trail_profile, "read_profile", read_then_replace)
    with pytest.raises(session_window.WindowRefused, match="changed while it was read"):
        session_window.read_window_trail_profile(config, as_of=date(2026, 11, 12))


def test_the_recorded_hash_is_the_files_whatever_way_the_reader_opens_it(monkeypatch, tmp_path):
    path = tmp_path / "trail-profile.json"
    path.write_text(json.dumps(profile_document()), encoding="utf-8")
    config = WindowConfig(
        strategy="Mover", mover=True, trail_mode="profile", trail_profile_path=path
    )

    def read_by_open(read_path, *, as_of):
        assert type(read_path) is type(path)
        with open(read_path, encoding="utf-8") as stream:
            return json.load(stream)

    monkeypatch.setattr(trail_profile, "read_profile", read_by_open)
    _, details = session_window.read_window_trail_profile(config, as_of=date(2026, 11, 12))
    assert details["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_record_names_the_windows_a_recent_only_profile_carries(monkeypatch, tmp_path):
    build, _, pick, _ = node_case(monkeypatch, tmp_path, doc=absent_long_document())
    _, s, _ = build()
    assert s.trail_profile == absent_long_document()
    assert pick.record["trail_profile"]["windows"] == "recent only; long absent"
    assert pick.record["trail_profile"]["symbols"] == 1


@pytest.mark.parametrize("cold", [False, True])
def test_a_recent_only_profile_with_a_thin_recent_row_enters_on_atr_by_name(cold):
    eng = engine()
    s = profile_strategy(atr_n=30 if cold else 3)
    s.trail_profile = absent_long_document()
    for row in s.trail_profile["symbols"]["AAA"]["recent"]:
        row["true_range"]["p75"] = None
    try:
        run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        [entry] = s.record()["entries"]
        source = "cold_atr_fallback" if cold else "atr"
        assert entry["trail_source"] == source
        assert entry["profile"]["reason"] == "no true_range statistic"
        assert entry["profile"]["distance"] is None
    finally:
        eng.dispose()


def test_a_recent_only_profile_with_its_recent_statistics_enters_on_the_profile():
    eng = engine()
    s = profile_strategy()
    s.trail_profile = absent_long_document()
    try:
        run(eng, s, ranker(rank((AAA, "0.25"))), tape(AAA, RISING))
        [entry] = s.record()["entries"]
        assert entry["trail_source"] == "profile"
        assert (entry["profile"]["r_window"], entry["profile"]["f_window"]) == ("recent", "recent")
    finally:
        eng.dispose()


def test_a_top_tier_opening_entry_on_a_cold_atr_is_not_tightened_by_the_profile():
    # The opening's cold-ATR entry takes the fixed plan, a top-tier name included (cold_atr_fallback),
    # and only the warmed ATR moves it on, to tier: the profile never tightens a top-tier name.
    seam, slot = holding()
    seam.baseline_dollar_volume_per_minute = {AAA: seam.settings.liquidity_tier_cut}
    slot.atr = cast(Any, SimpleNamespace(initialized=False, value=0.0))
    slot.trail_plan = slot.initial_trail = replace(
        slot.trail_plan, bound="fixed", source="cold_atr_fallback"
    )
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.trail_plan.source == "cold_atr_fallback"
    assert slot.entry_record["trail_changes"] == []


def bar_seam():
    seam, slot = holding()
    seam.settings.entry_arm = "bridge"
    slot.atr = None
    seam = cast(Any, seam)
    seam.slots, seam.live_bars, seam.reentry_states, seam.sold = {AAA: slot}, 0, {}, {}
    seam._held = lambda _: D(5)
    return seam, slot


def test_once_the_wind_down_has_begun_a_complete_bar_neither_tightens_nor_remakes_the_sell(
    monkeypatch,
):
    # The lead's rule (2): nothing remakes or tightens a sell once the wind-down has begun.
    seam, slot = bar_seam()
    seam.winding_down = True
    seen = []
    real = mover.profile_distance

    def measure(*args, **kwargs):
        seen.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(mover, "profile_distance", measure)
    for bar in [b for b in tape(AAA, [10.0] * 3) if isinstance(b, Bar)][1:]:
        mover.Mover.on_bar(seam, bar)
    assert seen == [] and len(seam.sent) == 1 and seam.canceled == []
    assert slot.entry_record["trail_changes"] == []


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("equal", "not narrower than the trail in force"),
        ("wider", "not narrower than the trail in force"),
        ("thin", "profile: 4 recent sessions"),
        ("unusable spread", "no usable quoted spread"),
        ("floor over cap", "the spread floor exceeds the 0.02 cap"),
        ("increment", "narrower by less than one price increment"),
        ("exiting", "the sale is being chased"),
        ("no document", "no profile document"),
        ("no anchor", "no protection anchor"),
    ],
)
def test_a_skipped_tighten_is_recorded_with_its_reason(kind, reason):
    # The lead's rule (3): each evaluated minute that does not tighten says why, in the entry record.
    seam, slot = holding()
    slot.trail_plan = replace(slot.trail_plan, trail=D("0.007"))
    if kind == "wider":
        seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.03
    elif kind == "thin":
        seam.trail_profile["symbols"]["AAA"]["recent"][60]["sessions"] = 4
    elif kind in ("unusable spread", "floor over cap"):
        seam.cache.quote = lambda _: quote("0" if kind == "unusable spread" else "0.11")
    elif kind == "increment":  # as in the fraction-change test above
        seam.settings.profile_floor = "off"
        seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.003495
    elif kind == "exiting":
        slot.exiting = True
    elif kind == "no document":
        seam.trail_profile = None
    elif kind == "no anchor":
        slot.protection_anchor = None
    slot.run_open_ns = T0 + MINUTE
    seam._profile_tighten(slot, T0 + 2 * MINUTE)
    skips = slot.entry_record["profile_skips"]
    assert len(skips) == 1 and skips[0]["when_ns"] == T0 + 2 * MINUTE
    assert skips[0]["minute_ns"] == T0 + MINUTE
    assert skips[0]["reason"].startswith(reason), skips[0]["reason"]
    seam._profile_tighten(slot, T0 + 2 * MINUTE + SECOND)
    assert len(slot.entry_record["profile_skips"]) == 1  # one row per minute, not per step


def test_a_tier_holding_records_no_profile_skips():
    seam, slot = holding()
    slot.trail_plan = replace(slot.trail_plan, bound="tier", source="tier")
    slot.run_open_ns = T0 + MINUTE
    seam._profile_tighten(slot, T0 + 2 * MINUTE)
    assert slot.entry_record.get("profile_skips", []) == []


@pytest.mark.parametrize("multipliers", [(D(1), D(1), D(1)), (D(1), D("1.25"), D("1.5"))])
def test_the_record_writes_the_multipliers_as_a_json_list_of_decimal_text(multipliers):
    s = profile_strategy(profile_multipliers=multipliers)
    rows = json.loads(json.dumps(s.record()["rows"]))
    assert rows["profile_multipliers"] == [str(value) for value in multipliers]
    assert rows["profile_k"] == "2"
    assert all(
        isinstance(value, str)
        or (isinstance(value, list) and all(isinstance(v, str) for v in value))
        for value in rows.values()
    )
    assert not any("Decimal(" in str(value) for value in rows.values())


def test_the_share_caps_row_is_written_as_a_json_mapping_of_text():
    s = profile_strategy(share_caps={AAA: 50})
    rows = json.loads(json.dumps(s.record()["rows"]))
    assert rows["share_caps"] == {str(AAA): "50"}


def test_profile_rows_keep_the_existing_trial_key_and_fixed_replay_contract():
    from canon_trading.ledger import config_key
    from canon_trading.ledger import trial_config
    from canon_trading.tuning_contract import BARS_REPLAY_FIXED_ROWS
    from canon_trading.tuning_contract import RankedConfiguration
    from canon_trading.tuning_study import BarsReplayMover
    from tests.test_tuning_study import p0_trial

    cfg = p0_trial()
    assert {name: BARS_REPLAY_FIXED_ROWS[name] for name in BASE} == BASE
    original = trial_config([cfg], starting_balances=["100000 USD"])
    assert config_key(original) == (
        "213b37b6d3bba04c68c7c7d5b8bb91b73e74a8476c6f5a46179c18b0eec2cf88"
    )
    assert not set(BASE) & set(original["strategies"][0]["config"])
    decoded = RankedConfiguration.from_config(original).mover_config
    replay = BarsReplayMover(cfg.settings)
    assert {name: getattr(decoded, name) for name in BASE} == BASE
    assert {name: getattr(replay.settings, name) for name in BASE} == BASE
    settings = mover.MoverConfig(max_notional=D(1000), max_move=D(1000), trail_mode="profile")
    with pytest.raises(ValueError, match="trail_mode.*profile"):
        BarsReplayMover(settings)
    with pytest.raises(ValueError, match="trail_mode.*profile"):
        trial_config([replace(cfg, settings=settings)], starting_balances=["100000 USD"])


def test_the_fixed_default_does_not_open_a_profile_file(monkeypatch, tmp_path):
    build, built, _, _ = node_case(monkeypatch, tmp_path, mode="fixed")
    monkeypatch.setattr(
        trail_profile, "read_profile", lambda *a, **k: pytest.fail("profile opened")
    )
    _, s, _ = build()
    assert built == [True] and s.settings.trail_mode == "fixed" and s.trail_profile is None


def test_the_profile_arm_preserves_hold_to_close_on_complete_bars(monkeypatch):
    seam, _slot = bar_seam()
    seam.settings.protect = False
    monkeypatch.setattr(seam, "_protect", lambda *a: pytest.fail("hold-to-close protected"))
    for bar in [b for b in tape(AAA, [10.0] * 3) if isinstance(b, Bar)][1:]:
        mover.Mover.on_bar(seam, bar)
    assert len(seam.sent) == 1 and seam.canceled == []


def test_a_warmed_atr_that_moves_a_cold_entry_onto_the_profile_records_that_calculation():
    """Review of L8's 61e82cd on the join: a cold fallback entry that the warmed ATR moves onto a
    profile plan carries the profile in force, as a profile tighten does."""
    seam, slot = holding()
    slot.trail_plan = slot.initial_trail = replace(
        slot.trail_plan, bound="fixed", source="cold_atr_fallback", profile=None
    )
    slot.entry_record["profile"] = None
    assert mover.Mover._warm_tighten(cast(Any, seam), slot, T0 + MINUTE) is True
    assert slot.trail_plan.source == "profile"
    record = slot.entry_record
    [change] = record["trail_changes"]
    assert change["profile"] == trail.profile_record(slot.trail_plan.profile)
    assert record["profile"] == change["profile"] and record["profile"] is not change["profile"]
    assert record["profile_at_entry"] is None


@pytest.mark.parametrize("exiting", [False, True])
def test_a_complete_bar_leaves_protection_to_the_market_exit_while_it_runs(monkeypatch, exiting):
    """The bar path holds the step path's rule: while upstream's market exit runs it alone sells."""
    seam, slot = holding()
    seam = cast(Any, seam)
    seam.settings.entry_arm = "bridge"
    seam.slots, seam.live_bars, seam.reentry_states, seam.sold = {AAA: slot}, 0, {}, {}
    seam._held = lambda _: D(5)
    seam.is_exiting = lambda: exiting
    seam._order = lambda _: SimpleNamespace(is_emulated=True)
    slot.atr = None
    calls = []
    monkeypatch.setattr(seam, "_protect", lambda *a: calls.append(a))
    bar = next(b for b in tape(AAA, [10.0]) if isinstance(b, Bar))
    mover.Mover.on_bar(seam, bar)
    assert len(calls) == (0 if exiting else 1)


@pytest.mark.parametrize("fault", ["zero close", "runtime"])
def test_a_profile_re_evaluation_that_cannot_compute_is_named_not_raised(monkeypatch, fault):
    """Raised from the step timer or a bar it would stop every slot, whatever the error's class."""
    seam, slot = holding()
    slot.run_open_ns = T0 + MINUTE
    if fault == "zero close":
        slot.closes = deque([D(0)], maxlen=4)
    else:

        def broken(*a, **k):
            raise RuntimeError("synthetic profile fault")

        monkeypatch.setattr(mover, "profile_distance", broken)
    assert seam._profile_tighten(slot, T0 + 2 * MINUTE) is False
    [skip] = slot.entry_record["profile_skips"]
    assert skip["reason"].startswith("profile not re-evaluated")


def test_a_window_refuses_multipliers_while_rdc_1_is_not_built():
    """With no RDc_1 every name takes the first bucket, so a non-default row would trade as 1."""
    with pytest.raises(ValueError, match="RDc_1"):
        WindowConfig(
            strategy="Mover",
            mover=True,
            trail_mode="profile",
            profile_multipliers=(D(1), D("1.25"), D("1.5")),
        )
    default = WindowConfig(strategy="Mover", mover=True, trail_mode="profile")
    assert default.profile_multipliers == (D(1), D(1), D(1))


def test_the_selection_time_counts_the_profile_read(monkeypatch, tmp_path):
    """A slow profile read must not leave the pick timed at the clock read before it."""
    at = datetime(2026, 11, 12, 9, 30, tzinfo=session_window.NEW_YORK)
    path = tmp_path / "trail-profile.json"
    path.write_text(json.dumps(profile_document()), encoding="utf-8")
    config = WindowConfig(
        strategy="Mover",
        mover=True,
        slots=1,
        spares=0,
        log_dir=tmp_path,
        trail_mode="profile",
        trail_profile_path=path,
    )
    calendar = [
        SimpleNamespace(
            date=(at - timedelta(days=i)).date(), open=at, close=at + timedelta(hours=6)
        )
        for i in (1, 0)
    ]
    monkeypatch.setattr(
        session_window, "session_part", lambda *a: (at + timedelta(hours=6), "regular")
    )
    monkeypatch.setattr(session_window, "warm_up_start", lambda *a, **k: at - timedelta(minutes=30))
    monkeypatch.setattr(session_window, "leftovers", lambda _: {})
    monkeypatch.setattr(session_window, "venue_no_shorting", lambda _: True)
    monkeypatch.setattr(
        movers_rank, "read_baselines", lambda *a: SimpleNamespace(problem=None, absent=False)
    )
    monkeypatch.setattr(
        session_window,
        "mover_allocation",
        lambda *a: session_window.MoverAllocation(D(1000), D(1000), 1, D(1000)),
    )
    monkeypatch.setattr(session_window, "start_reference_read", lambda *a: None)
    ticks = [100.0]
    monkeypatch.setattr(
        session_window, "time", SimpleNamespace(monotonic=lambda: ticks[0], time=lambda: 0.0)
    )
    real = trail_profile.read_profile

    def slow_read(path, *, as_of):
        ticks[0] += 5  # the read took five seconds
        return real(path, as_of=as_of)

    monkeypatch.setattr(trail_profile, "read_profile", slow_read)
    chosen = []

    def choose(_trading, when, *a, **k):
        chosen.append(when)
        raise session_window.WindowRefused("synthetic test reached the selection")

    monkeypatch.setattr(session_window, "choose_mover", choose)
    with pytest.raises(session_window.WindowRefused, match="reached the selection"):
        session_window.run_window(
            config,
            trading=cast(Any, object()),
            market=(cast(Any, SimpleNamespace(timestamp=at)), cast(Any, calendar)),
            market_read_at=100.0,
        )
    assert chosen == [at + timedelta(seconds=5)]


def test_an_auto_run_s_other_strategy_part_keeps_the_fixed_trail_and_says_so(tmp_path, capsys):
    """An auto run's SessionMaCross part is not failed by the Mover's trail rows; it names them."""
    args = session_window._args(
        [
            "--session",
            "auto",
            "--mover",
            "--regular-strategy",
            "SessionMaCross",
            "--trail-mode",
            "profile",
            "--profile-k",
            "3",
            "--log-dir",
            str(tmp_path),
        ]
    )
    args.strategy = "SessionMaCross"  # what _main resolves for the regular part
    config = session_window.window_config(args, "regular")
    assert config.trail_mode == "fixed" and config.profile_k == D(2)
    assert "the trail rows are Mover's" in capsys.readouterr().err
    args.strategy = "Mover"
    assert session_window.window_config(args, "extended").trail_mode == "profile"


# --- the lead's ruling of 2026-11-12, 21:01: a recorded tightening hands the trigger over ---------------


def test_a_profile_tighten_on_a_sized_entry_remakes_the_sale_at_the_profiles_offset_not_l():
    """The lead's ruling of 2026-11-12, 21:01: after every remake of a holding's sale, the distance the
    working sale trails by is the distance the record says. A budget-sized entry at 10.00 trails by
    L = 0.20; the profile tightens its 0.02 plan to 0.007, so the remade sale trails by 0.07, not by L."""
    seam, slot = holding(sized_loss="0.20")
    assert seam.sent[0].trailing_offset == D("0.20")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    [change] = slot.entry_record["trail_changes"]
    assert (change["from"], change["to"]) == ("0.02", "0.007")
    assert len(seam.sent) == 2 and seam.canceled == [seam.sent[0].client_order_id.value]
    remade = seam.sent[1]
    assert remade.trailing_offset == D("0.07")
    assert remade.trigger_price == Price.from_str("10.4000")
    assert slot.exit_record.trail_distance == D("0.07")


def test_a_profile_tighten_wider_than_l_in_force_leaves_the_sized_sale_alone():
    """The lead's ruling of 2026-11-12, 21:01: a tightening only moves the trigger closer. The profile's
    0.007 is narrower than the 0.02 plan, but its 0.07 is wider than the 0.05 L in force, so the sale
    is not remade, nothing is recorded as a change, and the minute's skip says why."""
    seam, slot = holding(sized_loss="0.05")
    assert seam.sent[0].trailing_offset == D("0.05")
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert len(seam.sent) == 1 and seam.canceled == []
    assert slot.trail_plan.trail == D("0.02")
    assert slot.entry_record["trail_changes"] == []
    [skip] = slot.entry_record["profile_skips"]
    assert skip["reason"].startswith("not narrower than L"), skip["reason"]


def test_a_sized_profile_tighten_to_the_spread_floor_trails_at_the_floor_never_under_it():
    """The lead's ruling of 2026-11-12, 21:01: a tightening never goes past what the spread floor
    allows. A quote 0.04 wide puts the floor at 2 spreads, 0.008 of the 10.00 anchor, over the profile's
    0.007; the remade sale trails by the floor's 0.08, under L's 0.20 and not under 2 spreads."""
    seam, slot = holding(sized_loss="0.20")
    seam.cache.quote = lambda _: SimpleNamespace(
        bid_price=Price.from_str("10.5000"), ask_price=Price.from_str("10.5400")
    )
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert (slot.trail_plan.trail, slot.trail_plan.bound) == (D("0.008"), "spread floor")
    remade = seam.sent[-1]
    assert len(seam.sent) == 2
    assert remade.trailing_offset == D("0.08") == seam.settings.spread_floor_m * D("0.04")


def test_under_own_a_profile_tighten_is_clamped_to_the_cap_a_known_limit():
    """The lead's note (1) of 2026-11-12, 21:01, pinned and not changed: under `own` the profile's
    re-evaluation still clamps to `TRAIL_CAP`, so a holding on its own 0.05 tightens to 0.02, not to
    the profile's own 0.03 (`mover.TRAIL_CAP_DEFAULT`'s known limit 1)."""
    seam, slot = holding(trail_cap="own")
    slot.trail_plan = slot.initial_trail = replace(slot.trail_plan, trail=D("0.05"))
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.015
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.trail_plan.profile["distance"] == D("0.03")
    assert (slot.trail_plan.trail, slot.trail_plan.bound) == (D("0.02"), "0.02 cap")
    assert seam.sent[-1].trailing_offset == D("0.20")
    assert "(1) a profile re-evaluation is clamped to the cap" in cap_row_note()


def test_under_own_an_openings_cold_atr_entry_is_not_raised_to_a_floor_over_the_cap_a_known_limit():
    """The lead's note (2) of 2026-11-12, 21:01, pinned and not changed: under `own` a cold-ATR entry
    outside the opening is raised to a spread floor over the fixed trail, but the opening's cold-ATR
    path keeps the fixed 0.02 under its 0.022 floor (`mover.TRAIL_CAP_DEFAULT`'s known limit 2)."""
    s, slot, latest = warmed(
        "atr",
        spread="0.11",
        trail_cap="own",
        size_from_budget=True,
        loss_share=mover.PAPER_LOSS_SHARE,
    )
    slot.atr.reset()
    slot.run_arm = "opening"
    plan = s._entry_trail(slot, D(10), latest, now=T0)
    assert (plan.trail, plan.bound, plan.source) == (D("0.02"), "fixed", "cold_atr_fallback")
    assert plan.floor == D("0.022") > plan.trail
    assert "(2) an opening's cold-ATR entry is not raised" in cap_row_note()


def cap_row_note() -> str:
    """The text documenting the `trail_cap` row: the docstring under `TRAIL_CAP_DEFAULT`."""
    import ast
    import inspect
    import itertools

    body = ast.parse(inspect.getsource(mover)).body
    for node, after in itertools.pairwise(body):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "TRAIL_CAP_DEFAULT"
            and isinstance(after, ast.Expr)
            and isinstance(after.value, ast.Constant)
        ):
            return " ".join(str(after.value.value).split())
    raise AssertionError("TRAIL_CAP_DEFAULT has no docstring")


def test_an_own_cold_fallback_raised_to_the_floor_keeps_the_profiles_calculation():
    """A merge-union loss on the join of batch A and S2: under `own`, a profile entry whose row is thin
    and whose ATR is cold is raised to a spread floor over the fixed trail; its plan, and so the entry
    record's `profile`, keeps the calculation and its thin reason, as every other plan does."""
    s, slot, latest = warmed(
        "atr",
        spread="0.11",
        trail_cap="own",
        size_from_budget=True,
        loss_share=mover.PAPER_LOSS_SHARE,
    )
    s.settings.trail_mode = "profile"
    s.trail_profile = profile_document()
    s.trail_profile["symbols"] = {}
    s.rdc_1 = {}
    slot.atr.reset()
    plan = s._entry_trail(slot, D(10), latest, now=T0)
    assert (plan.trail, plan.bound, plan.source) == (
        D("0.022"),
        "spread floor",
        "cold_atr_fallback",
    )
    assert plan.profile is not None
    assert trail.profile_record(plan.profile)["reason"] == "not in the profile"


def tightened_at_l(seam_bid="10.50"):
    """A budget-sized holding at 10.00 trailing by L = 0.21, whose profile tightens its 0.05 plan to
    0.02 (0.20 at 10.00, closer than L); the entry then fills further, its average 10.80."""
    seam, slot = holding(sized_loss="0.21")
    slot.trail_plan = slot.initial_trail = replace(slot.trail_plan, trail=D("0.05"))
    seam.trail_profile["symbols"]["AAA"]["recent"][60]["true_range"]["p75"] = 0.01
    slot.run_open_ns = T0 + MINUTE
    seam._protect(slot, D(5), T0 + 2 * MINUTE)
    assert slot.trail_plan.trail == D("0.02") and slot.tightened is True
    assert seam.sent[-1].trailing_offset == D("0.20")
    seam.orders["E-1"].avg_px = 10.80
    seam.bid = "11.00"
    return seam, slot


def test_a_tightened_sized_sale_remade_at_a_higher_average_never_trails_wider_than_l():
    """The high read of the ruling's fix, item 1: a tightening accepted against L at one average is
    never wider than L after a later fill. At the 10.80 average the plan's 0.02 is 0.22, over L's
    0.21, so the sale remade for all that is held trails by L."""
    seam, slot = tightened_at_l()
    seam._protect(slot, D(10), T0 + 2 * MINUTE + SECOND)
    remade = seam.sent[-1]
    assert str(remade.quantity) == "10"
    assert len(seam.sent) == 3
    assert remade.trailing_offset == D("0.21")
    assert slot.exit_record.trail_distance == D("0.21")


def test_a_tightened_sized_holdings_held_stop_is_never_wider_than_l():
    """The high read of the ruling's fix, item 1, for the held stop: with no followed price the hold
    of a tightened sized holding at the 10.80 average is L = 0.21 under it, not the plan's 0.22."""
    seam, slot = tightened_at_l()
    slot.closes = deque([D(11)], maxlen=4)
    seam._hold(slot, D(10), T0 + 3 * MINUTE)
    assert slot.held_stop == D("10.59")
