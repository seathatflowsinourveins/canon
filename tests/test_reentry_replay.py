"""Generated-bar checks for R3, ADR 0092's model and the lead's registered R3 rows."""

import json
import sqlite3
from dataclasses import replace
from datetime import date
from datetime import datetime
from pathlib import Path

import pytest

from canon_trading import early_entry_study as study
from canon_trading import reentry_replay as r3

DAY = date(2026, 8, 18)


def bars(closes: list[float], start: int = 570) -> tuple[study.Minute, ...]:
    # Flat OHLC isolates decisions on closes from the protection walk.
    return tuple(study.Minute(start + i, c, c, c, c, 100) for i, c in enumerate(closes))


def day(closes: list[float]) -> study.Day:
    quiet = (study.Minute(240, 10, 10, 10, 10, 1),)
    return study.Day("TEST", DAY, bars(closes), (quiet,), 10)


def test_holdout_is_refused_before_catalog_access(tmp_path: Path, monkeypatch) -> None:
    sessions = tmp_path / "sessions.txt"
    sessions.write_text("2026-08-18 development\n2026-10-16 development\n")
    with pytest.raises(ValueError, match="holdout"):
        r3.read_sessions(sessions)
    monkeypatch.setattr(r3, "ParquetDataCatalog", lambda *a: pytest.fail("catalog read"))
    with pytest.raises(ValueError, match="holdout"):
        r3.load_symbol(tmp_path, "TEST", [DAY, date(2026, 10, 16)], [DAY])


def test_population_is_top_baseline_tier_union_ignition_with_close_times_volume() -> None:
    top = study.Day("TOP", DAY, bars([10]), (bars([10_000], 240),), 10)
    quiet = study.Day("QUIET", DAY, bars([10]), (bars([10], 240),), 10)
    ignited = day([10.4, 10.5, 10.6])
    assert r3.population(top, 1000)["selected"]
    assert not r3.population(quiet, 1000)["selected"]
    assert r3.population(ignited, 1e9)["ignition"] == 571
    assert r3.population(ignited, 1e9)["selected"]
    assert r3.baseline_per_minute(top) == pytest.approx(1_000_000 / 720)


@pytest.mark.parametrize("fraction", [0.02, 0.002, 0.007])
def test_protection_reuses_the_study_walk_with_isolated_scaled_rows(fraction: float) -> None:
    after = (study.Minute(600, 10, 10.5, 10.1, 10.4, 1),)
    out = r3.protection(after, 10, 10, study.GROSS, fraction)
    assert out is not None and out.price == pytest.approx(10.5 - 10 * fraction)
    assert (study.TRAIL, study.EXIT_THROUGH, study.CHASE_CAP) == (0.02, 0.005, 0.02)
    if fraction == 0.02:
        assert out == study.protect(after, 10, 10, study.GROSS)


def test_gap_floor_scales_with_the_selected_trail_and_later_open_fills_it() -> None:
    after = bars([9, 9.99], 600)
    out = r3.protection(after, 10, 10, study.GROSS, 0.002)
    assert out is not None and out.minute == 602 and out.price == 9.99
    expected_floor = (9.98 - 10 * 0.002 / 4) * (1 - 0.002)
    assert out.price > expected_floor


def test_atr_uses_only_completed_bars_and_waits_fourteen() -> None:
    tape = (
        *bars([10] * 13),
        study.Minute(583, 10, 10.2, 9.8, 10, 100),
        study.Minute(584, 10, 50, 1, 10, 100),
    )
    values = r3.atr_values(tape)
    assert values[:13] == (None,) * 13
    assert values[13] == pytest.approx(0.4 / 14)
    assert r3.atr_values(tape[:14]) == values[:14]
    assert r3.trail_fraction("atr-1", 10, values[13], False) == pytest.approx(0.4 / 140)
    assert r3.trail_fraction("atr-3", 10, 1, False) == 0.02
    assert r3.trail_fraction("tier", 10, None, True) == 0.002
    assert r3.trail_fraction("atr-1", 10, None, False) is None


def test_reclaim_has_no_cooldown_but_never_enters_the_sale_bar() -> None:
    tape = bars([10.5, 10.6, 10.7])
    signal = r3.reentry_signal(tape, 0, 10, 0.2, "reclaim", None)
    assert signal is not None and signal.index == 1


def test_a_reclaim_without_a_fall_reports_zero_fall_depth() -> None:
    signal = r3.reentry_signal(bars([10.5, 10.6]), 0, 10, 0.2, "reclaim", None)
    assert signal is not None and signal.depth_trails == 0


def test_catalog_coverage_uses_the_existing_studys_known_close_boundary() -> None:
    assert r3._covered([(study._ns(DAY, 240) + 1, study._ns(DAY, 960))], DAY)
    assert not r3._covered([(study._ns(DAY, 242), study._ns(DAY, 960))], DAY)


@pytest.mark.parametrize("k", [1, 2, 3])
@pytest.mark.parametrize("depth", [1, 2])
def test_turn_counts_rising_closes_from_low_and_required_depth(k: int, depth: int) -> None:
    tape = bars([10, 9.5, 9.6, 9.7, 9.8])
    signal = r3.reentry_signal(tape, 0, 10, 0.2, f"turn-{k}-{depth}", None)
    assert signal is not None and signal.index == 1 + k
    assert signal.depth_trails == pytest.approx(2.5)
    assert (signal.fall_minutes, signal.turn_minutes) == (1, k + 1)
    assert r3.reentry_signal(bars([10, 9.9, 10]), 0, 10, 0.2, f"turn-{k}-{depth}", None) is None


def test_equal_or_falling_close_resets_turn_and_new_low_resets_its_clock() -> None:
    tape = bars([10, 9.5, 9.6, 9.6, 9.7, 9.4, 9.5, 9.6])
    signal = r3.reentry_signal(tape, 0, 10, 0.2, "turn-2-1", None)
    assert signal is not None and signal.index == 7
    assert signal.fall_minutes == 5 and signal.depth_trails == pytest.approx(3)


def test_vwap_weights_own_vw_and_refuses_missing_instead_of_using_typical_price() -> None:
    tape = (study.Minute(570, 10, 10, 10, 10, 100), study.Minute(571, 12, 12, 12, 12, 300))
    assert r3.session_vwap(tape, {570: 10, 571: 12}) == (10, 11.5)
    with pytest.raises(ValueError, match="vwap refused: missing own vw"):
        r3.session_vwap(tape, {570: 10})
    signal = r3.reentry_signal(bars([10, 9, 11]), 0, 10, 0.2, "vwap", (10, 10, 10))
    assert signal is not None and signal.index == 2
    assert r3.reentry_signal(bars([11, 12]), 0, 10, 0.2, "vwap", (10, 10)) is None


def test_replay_counts_repeated_sales_and_reentries_and_costs_per_side() -> None:
    today = day([10.4, 10.5, 10.6, 10.5, 10.3, 10.8, 10.7, 10.4, 10.9, 12])
    context = r3.ReplayContext(today, {}, False)
    gross = context.replay("fixed", "reclaim", 2, 0)
    assert gross["entries"] == 3 and gross["reentries"] == 2
    assert gross["stop_outs"] == 2 and gross["given_back_dollars"] > 0
    costly = context.replay("fixed", "reclaim", 2, 5)
    assert costly["net_dollars"] < gross["net_dollars"]
    assert gross["breakeven_half_spread_bps_frozen_path"] == pytest.approx(
        10_000 * gross["net_dollars"] / gross["turnover_dollars"]
    )


def test_missing_vw_is_a_named_arm_refusal_and_no_entry_counts_zero() -> None:
    context = r3.ReplayContext(day([10, 10]), {}, False)
    assert context.replay("fixed", "vwap", 2, 0)["refusal"].startswith("vwap refused")
    assert context.replay("fixed", "reclaim", 2, 0)["entries"] == 0
    assert context.replay("fixed", "reclaim", 2, 0)["net_dollars"] == 0


def test_opening_signal_is_causal_and_diagnostic_low_is_separate() -> None:
    tape = bars([10, 9.5, 9.6, 9.7, 9.0, 9.1, 9.2])
    signal = r3.opening_signal(tape, 15, 1, 2, (0.2,) * len(tape))
    assert signal is not None and signal.index == 3  # does not wait for the later lower low
    assert r3.opening_signal(tape, 3, 1, 2, (0.2,) * len(tape)) is None
    low = r3.opening_diagnostic(tape, 15)
    assert low["low_minute"] == 575 and low["depth_dollars"] == 1
    assert low["rise_after_low_dollars"] == pytest.approx(0.2)
    assert r3.opening_diagnostic(bars([9], 575), 15)["refusal"] == "no 09:30 opening bar"


def test_report_labels_diagnostics_tier_settings_and_uncalibrated_costs() -> None:
    header = r3.report_header("abc", [DAY])
    for word in ("DIAGNOSTIC", "UNCALIBRATED", "per tier", "per-symbol", "no quotes", "abc"):
        assert word in header


def test_cost_grid_refuses_an_entry_bid_under_the_scaled_planned_stop() -> None:
    # With the Mover's chase depth the grid's 100 bps clears a 0.002 trail's planned stop; 150 does not.
    context = r3.ReplayContext(day([10.4, 10.5, 10.6, 10.7]), {}, True)
    result = context.replay("tier", "reclaim", 2, 150)
    assert result["entries"] == 0
    assert result["guard_skips_count"] == 2
    assert result["refusal"] == "no guard-valid initial signal"
    assert all(s["bid"] < s["planned_stop"] for s in result["guard_skips"])


def test_initial_guard_skip_continues_to_the_first_valid_rising_close() -> None:
    tape = (*bars([10.4] * 13 + [10.401, 10.402]), study.Minute(585, 10.402, 13, 8, 10.5, 100))
    result = r3.ReplayContext(replace(day([]), bars=tape), {}, False).replay(
        "atr-1", "reclaim", 2, 150
    )
    assert result["entries"] == 1 and result["guard_skips_count"] == 1
    assert result["initial_signal"]["minute"] == 586
    skip = result["guard_skips"][0]
    assert (skip["kind"], skip["symbol"], skip["day"], skip["minute"]) == (
        "guard_skip",
        "TEST",
        str(DAY),
        585,
    )
    assert skip["bid"] < skip["planned_stop"]


def test_reentry_guard_skip_keeps_the_sale_low_and_continues() -> None:
    tape = (
        *(study.Minute(570 + i, 10.4, 10.6, 10.2, 10.4, 100) for i in range(13)),
        study.Minute(583, 10.4, 10.5, 10.4, 10.5, 100),
        study.Minute(584, 10.5, 10.6, 10.5, 10.6, 100),
        *bars([10.7] * 80, 585),
        *bars([10.46, 10.47], 665),
        study.Minute(667, 10.47, 11, 10, 10.5, 100),
    )
    result = r3.ReplayContext(replace(day([]), bars=tape), {}, False).replay(
        "atr-1", "reclaim", 2, 150
    )
    assert result["entries"] == 2 and result["reentries"] == 1
    assert result["guard_skips_count"] == 1
    assert result["guard_skips"][0]["minute"] == 667
    assert result["falls"][0]["turn_minutes"] == 2


def test_opening_arm_keeps_scanning_after_a_guard_invalid_turn() -> None:
    tape = bars([10, 9.5, 9.6, 9.7, 9.8])
    skipped = []

    def eligible(index):
        if index == 3:
            skipped.append(index)
            return False
        return True

    signal = r3.opening_signal(tape, 15, 1, 2, (0.2,) * len(tape), eligible)
    assert signal is not None and signal.index == 4 and skipped == [3]


def test_peak_giveback_never_uses_the_entry_bars_earlier_high() -> None:
    today = study.Day("TEST", DAY, (study.Minute(570, 10, 20, 9, 10, 100),), (), 10)
    result = r3.ReplayContext(today, {}, True).sale("fixed", 0, 0)
    assert result["given_back"] == 0


def test_generated_catalog_vw_join_and_outputs_are_complete(tmp_path, alpaca_bars) -> None:
    # Existing study's test helpers implement the adapter/catalog contract already in this repo.
    from tests.test_early_entry_study import minute_payloads
    from tests.test_early_entry_study import write_minutes

    before = date(2026, 8, 17)
    root = tmp_path / "catalog"
    payloads = {
        before: minute_payloads(before, 240, [10], 1) + minute_payloads(before, 959, [10], 1),
        DAY: minute_payloads(DAY, 570, [10.4, 10.5, 10.6, 10.3, 10.8], 100)
        + minute_payloads(DAY, 959, [11], 100),
    }
    # Helper's end date is its module DAY; explicit coverage remains before R3's cutoff.
    write_minutes(root, alpaca_bars, "TEST", payloads, 960)
    with sqlite3.connect(root / "bar-extras.sqlite") as conn:
        conn.execute("CREATE TABLE bar_extras (bar_type TEXT, ts_event INTEGER, vwap REAL)")
        for payload in payloads[DAY]:
            conn.execute(
                "INSERT INTO bar_extras VALUES (?, ?, ?)",
                (
                    "TEST.ALPACA-1-MINUTE-LAST-EXTERNAL",
                    int(datetime.fromisoformat(payload["t"]).timestamp() * 1_000_000_000),
                    payload["vw"],
                ),
            )
    loaded = r3.load_symbol(root, "TEST", [DAY], [before])
    assert len(loaded) == 1 and loaded[0][0].reference == 10
    assert loaded[0][0].bars[-1].start == 959
    assert loaded[0][1][570] == 10.4
    assert loaded[0][1][959] == 11
    alpaca_bars.write(
        root, [9], symbols=("TEST",), first=datetime(2026, 8, 17, tzinfo=study.NEW_YORK)
    )
    daily_loaded = r3.load_symbol(root, "TEST", [DAY], [before])
    assert daily_loaded[0][0].reference == 9
    assert daily_loaded[0][0].reference_from == f"daily bar of {before}"
    sessions = tmp_path / "sessions.txt"
    sessions.write_text(f"{before} lookback\n{DAY} development\n")
    output = tmp_path / "output"
    assert (
        r3.main(
            [
                "--catalog",
                str(root),
                "--sessions",
                str(sessions),
                "--output",
                str(output),
                "--name",
                "generated",
            ]
        )
        == 0
    )
    assert (output / "generated-summary.md").exists()
    symbol_table = (output / "generated-symbols.md").read_text()
    assert "DIAGNOSTIC" in symbol_table and "symbol:TEST" in symbol_table
    assert '"kind": "completion"' in (output / "generated.jsonl").read_text()
    assert '"guard_skips_count"' in (output / "generated.jsonl").read_text()
    common = ["--catalog", str(root), "--sessions", str(sessions), "--output", str(output)]
    for k in (0, 1):
        assert r3.main([*common, "--name", f"shard-{k}", "--shard", f"{k}/2"]) == 0
    shards = [str(output / f"shard-{k}.jsonl") for k in (0, 1)]
    assert r3.main(["--output", str(output), "--name", "merged", "--merge", *shards]) == 0
    merged = (output / "merged-summary.md").read_text()
    whole = (output / "generated-summary.md").read_text()
    rows = [line for line in whole.splitlines() if line.startswith("| tier:")]
    assert rows and rows == [line for line in merged.splitlines() if line.startswith("| tier:")]


def test_catalog_query_is_bounded_before_holdout_and_only_complete_days_load(
    tmp_path, monkeypatch
) -> None:
    calls = []

    class Catalog:
        def __init__(self, path):
            pass

        def get_intervals(self, data_type, identifier):
            return [(study._ns(DAY, 240), study._ns(DAY, 960))]

        def query(self, data_type, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(r3, "ParquetDataCatalog", Catalog)
    loaded = r3.load_symbol(tmp_path, "TEST", [DAY], [])
    assert len(loaded) == 1 and loaded[0][0].missing == "no minute bar in the day's window"
    assert all(c["end"] < study._ns(date(2026, 10, 16), 0) for c in calls)


# The lead's fix round on the second reading (2026-11-12): each test names the bias it removes.


def listed_at(tape: tuple[study.Minute, ...], minutes: set[int]):
    """A listing that holds only at the given bar-start minutes, in place of ADR 0092's thresholds."""

    def fake(day, rows):
        return tuple(
            study.Listed(b.start + 1, 0.0, 0.0, None, None, b.start in minutes) for b in day.bars
        )

    return fake


def test_a_top_tier_entry_does_not_wait_for_a_listing_that_comes_later_in_the_day(
    monkeypatch,
) -> None:
    # Hindsight in the gate: ignition was the day's first listing, so a name that listed at 14:00
    # skipped its 05:00 run, which a live rule could not know to skip.
    tape = (*bars([10, 10.1, 10.2], 300), *bars([10.2, 10.3, 10.4], 840))
    monkeypatch.setattr(study, "listing", listed_at(tape, {840, 841, 842}))
    context = r3.ReplayContext(replace(day([]), bars=tape), {}, True)
    signal = context.initial("fixed", 2, 0, None)
    assert signal is not None and tape[signal.index].start == 302


def test_an_other_tier_opening_entry_needs_the_name_listed_at_its_entry_bar(monkeypatch) -> None:
    # Survivorship: an other-tier name joins the population only by igniting at some point in the
    # day, and the opening arms bought it at 09:34 on an ignition that came at 13:00.
    tape = (*bars([10, 9.5, 9.6, 9.7, 9.8]), *bars([10.5, 10.6], 780))
    monkeypatch.setattr(study, "listing", listed_at(tape, {780, 781}))
    late = r3.ReplayContext(replace(day([]), bars=tape), {}, False)
    assert late.initial("fixed", 2, 0, (15, 1)) is None
    monkeypatch.setattr(study, "listing", listed_at(tape, {572, 573, 574}))
    listed = r3.ReplayContext(replace(day([]), bars=tape), {}, False)
    signal = listed.initial("fixed", 2, 0, (15, 1))
    assert signal is not None and tape[signal.index].start == 573


def test_the_chase_keeps_the_movers_depth_under_a_narrow_trail() -> None:
    # The chase cap was the trail; the Mover chases max(trail, chase_cap) (mover.py:2144, rule (ii)),
    # so a 0.002 trail at 25 bps was refused every entry and rested 0.2% under a gap.
    context = r3.ReplayContext(day([10.4, 10.5, 10.6, 10.7]), {}, True)
    assert context.guard_skip("tier", 3, 25) is None
    after = bars([9.9], 600)
    out = r3.protection(after, 10, 10, study.GROSS, 0.002)
    assert out is not None and (out.why, out.price) == ("gap", 9.9)


def test_the_cost_grid_charges_prices_and_never_moves_a_stop_or_a_reentry() -> None:
    # The re-entry tests compared raw closes with a sale net of h, so at h=100 a close of 10.25
    # reclaimed a sale at 10.30 (10.197 after cost); the cost rows mixed cost with a changed rule.
    today = day([10.4, 10.5, 10.6, 10.3, 10.25, 10.35, 10.5])
    context = r3.ReplayContext(today, {}, True)
    gross = context.replay("fixed", "reclaim", 2, 0)
    assert gross["entries"] == 2
    for h in (100, 25, 5):
        costly = context.replay("fixed", "reclaim", 2, h)
        expected = gross["net_dollars"] - h / 10_000 * gross["turnover_dollars"]
        assert costly["net_dollars"] == pytest.approx(expected), h
        assert (costly["entries"], costly["stop_outs"]) == (gross["entries"], gross["stop_outs"])
        assert costly["entry_minutes"] == gross["entry_minutes"] == [573, 576]


def test_a_released_limit_never_filled_is_a_stop_not_a_hold_to_the_close() -> None:
    # A gap under the trigger and the floor that never recovered returned None and was booked as a
    # hold to the close: stops were undercounted on exactly the crash days.
    tape = (*bars([10.4, 10.5, 10.6]), *bars([8.0, 8.1, 8.05], 573))
    context = r3.ReplayContext(replace(day([]), bars=tape), {}, True)
    sold = context.sale("fixed", 2, 0)
    assert sold["stop"] is True and sold["index"] == len(tape) - 1
    assert sold["sold"] == pytest.approx(8.05)


def test_the_run_records_the_hash_of_every_module_its_rows_come_from() -> None:
    # Only reentry_replay.py was hashed, while the exit walk and the listing are the study's.
    import hashlib

    hashes = r3.code_hashes()
    for module in (r3, study):
        path = Path(module.__file__)
        assert hashes[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()


PINNED = study.Minute
PROTECT_PIN = [
    ((PINNED(600, 10, 10.5, 10.1, 10.4, 1), PINNED(601, 10.4, 10.45, 10.2, 10.3, 1)),
     study.GROSS, study.Exit(601, 10.3, "trail", 9.8, 10.3, 1)),
    ((PINNED(600, 9.7, 9.9, 9.6, 9.8, 1),), study.GROSS,
     study.Exit(601, 9.7, "gap", 9.8, 9.8, 0)),
    ((PINNED(600, 9.0, 9.1, 8.9, 9.0, 1), PINNED(601, 9.7, 9.8, 9.6, 9.7, 1)), study.GROSS,
     study.Exit(602, 9.7, "floor", 9.8, 9.8, 0)),
    ((PINNED(600, 9.0, 9.1, 8.9, 9.0, 1), PINNED(601, 9.4, 9.7, 9.3, 9.5, 1)), study.GROSS,
     study.Exit(602, 9.555, "floor", 9.8, 9.8, 0)),
    ((PINNED(600, 9.0, 9.1, 8.9, 9.0, 1), PINNED(601, 9.0, 9.2, 8.8, 9.1, 1)), study.GROSS, None),
    ((PINNED(600, 10, 10.1, 9.9, 10.05, 1), PINNED(601, 10.05, 10.2, 10.0, 10.1, 1)),
     study.Costs(0.0005, 0.0005), None),
    ((PINNED(600, 9.85, 10.2, 9.79, 10.0, 1),), study.GROSS,
     study.Exit(601, 10.0, "trail", 9.8, 10.0, 1)),
]  # fmt: skip


@pytest.mark.parametrize(("tape", "costs", "expected"), PROTECT_PIN)
def test_the_shared_walk_is_unchanged_for_the_other_studies(tape, costs, expected) -> None:
    # The lead: protect keeps the pin's default order; these are its outputs before this change.
    assert study.protect(tape, 10.0, 10.0, costs) == expected
    assert study.planned_stop(10.0) == pytest.approx((10 - 10 * 0.025) * 0.98)


def test_the_adaptive_order_takes_the_low_first_when_the_open_is_nearer_it() -> None:
    # bar_adaptive_high_low_ordering (bar-execution.md; matching_engine/mod.rs:2400-2402 at the pin):
    # the open 9.85 is 0.06 over the low and 0.35 under the high, so the low comes first and the
    # stop sells at the first trigger 9.80, where open-high-low-close sold at the raised 10.00.
    tape = (study.Minute(600, 9.85, 10.2, 9.79, 10.0, 1),)
    default = study.protect(tape, 10, 10, study.GROSS)
    assert default is not None and default.price == 10.0
    low_first = study.protect(tape, 10, 10, study.GROSS, adaptive=True)
    assert low_first is not None and low_first.price == pytest.approx(9.8)
    high_first = (study.Minute(600, 10.1, 10.2, 9.79, 10.0, 1),)
    assert study.protect(high_first, 10, 10, study.GROSS, adaptive=True) == study.protect(
        high_first, 10, 10, study.GROSS
    )


def test_every_replay_reports_both_orders_and_per_entry_returns() -> None:
    today = day([10.4, 10.5, 10.6, 10.5, 10.3, 10.8, 10.7, 10.4, 10.9, 12])
    context = r3.ReplayContext(today, {}, True)
    result = context.replay("fixed", "reclaim", 2, 5)
    assert len(result["entry_returns_bps"]) == result["entries"]
    adaptive = context.replay("fixed", "reclaim", 2, 5, adaptive=True)
    assert adaptive["order"] == "adaptive" and result["order"] == "ohlc"
    totals = r3.new_aggregate(histogram=True)
    r3.add_to_aggregate(totals, result)
    returns = result["entry_returns_bps"]
    assert totals["returns_count"] == len(returns)
    assert totals["returns_bps_sum"] == pytest.approx(sum(returns))
    assert totals["returns_bps_sumsq"] == pytest.approx(sum(r * r for r in returns))
    assert sum(totals["returns_bps_bins"].values()) == len(returns)


def test_shards_partition_the_symbols_and_their_tier_sums_merge() -> None:
    symbols = [f"S{i}" for i in range(10)]
    parts = [r3.shard(symbols, f"{k}/3") for k in range(3)]
    assert sorted(s for part in parts for s in part) == symbols
    one, two = r3.new_aggregate(histogram=True), r3.new_aggregate(histogram=True)
    one.update(days=2, entries=3, returns_count=3, returns_bps_sum=-30.0)
    one["returns_bps_bins"] = {-10: 3}
    two.update(days=1, entries=1, returns_count=1, returns_bps_sum=5.0)
    two["returns_bps_bins"] = {0: 1}
    merged = r3.merge_aggregates([one, two])
    assert (merged["days"], merged["entries"], merged["returns_count"]) == (3, 4, 4)
    assert merged["returns_bps_sum"] == -25.0 and merged["returns_bps_bins"] == {-10: 3, 0: 1}


# The code review of the fix round (2026-11-12): each test names the bias it removes.


def test_a_reentry_needs_the_name_watched_at_its_entry_bar(monkeypatch) -> None:
    # Hindsight in the re-entry gate: an other-tier name that fell off the list re-entered on a bar
    # a live list would not have shown it.
    today = day([10.4, 10.5, 10.6, 10.3, 10.25, 10.35, 10.5])
    monkeypatch.setattr(study, "listing", listed_at(today.bars, {570, 571, 572, 573, 574}))
    dropped = r3.ReplayContext(today, {}, False).replay("fixed", "reclaim", 2, 0)
    assert dropped["entry_minutes"] == [573]
    monkeypatch.setattr(study, "listing", listed_at(today.bars, set(range(570, 577))))
    listed = r3.ReplayContext(today, {}, False).replay("fixed", "reclaim", 2, 0)
    assert listed["entry_minutes"] == [573, 576]


def shard_file(path: Path, spec: str, **metadata) -> Path:
    head = {
        "kind": "metadata",
        "base_commit": "abc",
        "code_sha256": {"reentry_replay.py": "1"},
        "shard": spec,
        "sessions_sha256": "s",
        "development": [str(DAY)],
        "lookback": [],
        "catalog": "c",
        **metadata,
    }
    tail = {"kind": "completion", "counts": {"symbols_scanned": 1}, "days_used": {str(DAY): 1}}
    path.write_text(json.dumps(head) + "\n" + json.dumps(tail) + "\n")
    return path


def test_a_merge_refuses_shards_that_repeat_miss_or_ran_on_other_inputs(tmp_path) -> None:
    # Overlapping or missing shards were summed silently, and shards of other sessions merged.
    zero, one = (shard_file(tmp_path / f"{k}.jsonl", f"{k}/2") for k in (0, 1))
    with pytest.raises(ValueError, match="shard"):
        r3.merge([zero, zero, one], tmp_path, "repeated")
    with pytest.raises(ValueError, match="shard"):
        r3.merge([zero], tmp_path, "missing")
    with pytest.raises(ValueError, match="shard"):
        r3.merge([zero, shard_file(tmp_path / "x.jsonl", "1/3")], tmp_path, "mixed")
    other = shard_file(tmp_path / "o.jsonl", "1/2", sessions_sha256="t")
    with pytest.raises(ValueError, match="sessions_sha256"):
        r3.merge([zero, other], tmp_path, "other")
    summary = r3.merge([zero, one], tmp_path, "whole").read_text()
    assert f'"{DAY}": 2' in summary


def test_the_tier_table_gives_every_figure_the_bar_order_moves_as_a_range() -> None:
    # Only net, mean and median were paired; the breakeven half-spread came from the favourable order.
    ohlc, adaptive = r3.new_aggregate(), r3.new_aggregate()
    ohlc.update(entries=2, stop_outs=1, reentries=1, net_dollars=1.0, turnover_dollars=100.0)
    adaptive.update(entries=3, stop_outs=2, reentries=2, net_dollars=0.5, turnover_dollars=100.0)
    key = ("tier:top", "run-2", "fixed", "reclaim", 2, 0)
    row = r3.table_row(
        r3.aggregate_record((*key, "ohlc"), ohlc), r3.aggregate_record((*key, "adaptive"), adaptive)
    ).split(" | ")
    assert "2; 3" in row and "1; 2" in row and "100.000; 50.000" in row


def test_an_adaptive_sale_on_the_low_never_counts_the_high_after_it_as_given_back() -> None:
    # The peak took the sale bar's high on every trail exit, but the low-first order sells first.
    tape = (*bars([10]), study.Minute(571, 9.85, 10.2, 9.79, 10.0, 1))
    context = r3.ReplayContext(replace(day([]), bars=tape), {}, True)
    assert context.sale("fixed", 0, 0, adaptive=True)["given_back"] == pytest.approx(0.2)
    assert context.sale("fixed", 0, 0)["given_back"] == pytest.approx(0.2)


def test_the_guard_plans_its_stop_from_the_trail_the_walk_uses() -> None:
    # The guard sized an ATR trail from the entry after h, a factor (1+h) narrower than the walk's.
    tape = tuple(study.Minute(570 + i, 10, 10.15, 10, 10, 100) for i in range(15))
    context = r3.ReplayContext(replace(day([]), bars=tape), {}, False)
    fraction = r3.trail_fraction("atr-1", 10, context.atr[14], False)
    assert fraction is not None and fraction < 0.02
    skip = context.guard_skip("atr-1", 14, 300)
    assert skip is not None
    assert skip["planned_stop"] == pytest.approx(study.planned_stop(10.3, **r3.plan(fraction)))


def test_an_opening_fall_is_timed_at_the_close_of_its_low_bar() -> None:
    # The fall was timed at the low bar's open and the turn at its bar's close: one minute early.
    tape = bars([10, 9.5, 9.6, 9.7, 9.0])
    signal = r3.opening_signal(tape, 15, 1, 2, (0.2,) * len(tape))
    assert signal is not None and (signal.fall_minutes, signal.turn_minutes) == (2, 4)


class _Value(float):
    def as_decimal(self):
        return self


def alpaca_minute(on: date, minute: int):
    """A catalog bar stand-in: a flat 10.0 minute opening at `minute` New York time."""

    class Bar:
        ts_event = int(
            datetime(
                on.year, on.month, on.day, minute // 60, minute % 60, tzinfo=study.NEW_YORK
            ).timestamp()
            * 1_000_000_000
        )
        open = high = low = close = _Value(10.0)
        volume = _Value(100.0)

    return Bar()


def test_a_day_with_no_known_reference_is_refused_by_name(tmp_path, monkeypatch) -> None:
    # The study refuses a day with no reference; the replay kept it and ran it on its scaffold.
    class Catalog:
        def __init__(self, path):
            pass

        def get_intervals(self, data_type, identifier):
            return [(study._ns(DAY, 240), study._ns(DAY, 960))]

        def query(self, data_type, **kwargs):
            if "DAY" in kwargs["identifiers"][0]:
                return []
            return [alpaca_minute(DAY, 600)]

    monkeypatch.setattr(r3, "ParquetDataCatalog", Catalog)
    loaded = r3.load_symbol(tmp_path, "TEST", [DAY], [])
    assert loaded[0][0].bars and loaded[0][0].reference is None
    assert loaded[0][0].missing == "no bar before the day: the reference close is unknown"


def test_an_adaptive_replay_line_keeps_its_own_guard_skips(
    tmp_path, monkeypatch, alpaca_bars
) -> None:
    # The JSONL wrote an empty guard_skips list for every adaptive result and kept only the count.
    from tests.test_early_entry_study import minute_payloads
    from tests.test_early_entry_study import write_minutes

    before = date(2026, 8, 17)
    root = tmp_path / "catalog"
    payloads = {
        before: minute_payloads(before, 240, [10], 1) + minute_payloads(before, 959, [10], 1),
        DAY: minute_payloads(DAY, 570, [10.4, 10.5, 10.6, 10.7], 100)
        + minute_payloads(DAY, 959, [11], 100),
    }
    write_minutes(root, alpaca_bars, "TEST", payloads, 960)
    sessions = tmp_path / "sessions.txt"
    sessions.write_text(f"{before} lookback\n{DAY} development\n")
    monkeypatch.setattr(r3, "HALF_SPREAD_BPS", (300,))
    common = ["--catalog", str(root), "--sessions", str(sessions), "--output", str(tmp_path)]
    assert r3.main([*common, "--name", "skips"]) == 0
    lines = [json.loads(x) for x in (tmp_path / "skips.jsonl").read_text().splitlines()]
    adaptive = [c for x in lines if x["kind"] == "replay" for c in x["costs"]]
    adaptive = [c for c in adaptive if c["order"] == "adaptive" and c["guard_skips_count"]]
    assert adaptive
    assert all(len(c["guard_skips"]) == c["guard_skips_count"] for c in adaptive)
