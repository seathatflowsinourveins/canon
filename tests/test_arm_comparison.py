"""Synthetic checks of ADR 0104's diagnostic through arch 9.0.0's public SPA/StepM API.

The daily CSV is the input contract of `canon_trading.arm_comparison`: one return per arm
and session day, not dollar P&L per symbol and day. No catalog, network, ledger or account is used.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest
from arch.bootstrap import SPA
from arch.bootstrap import StepM

from canon_trading import arm_comparison as comparison

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def returns() -> pd.DataFrame:
    """A varying, dominant return and its opposite around the same benchmark."""
    rng = np.random.default_rng(431)
    base = rng.normal(0.0002, 0.01, 120)
    advantage = rng.uniform(0.008, 0.012, len(base))
    return pd.DataFrame(
        {"base": base, "dominant": base + advantage, "inferior": base - advantage},
        index=pd.bdate_range("2025-03-03", periods=len(base)),
    )


def write_returns(path: Path, returns: pd.DataFrame) -> Path:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["session_day", *returns.columns])
        days = pd.DatetimeIndex(returns.index).strftime("%Y-%m-%d")
        for day, row in zip(days, returns.to_numpy(), strict=True):
            writer.writerow([day, *row])
    return path


def compare(path: Path, **settings):
    return comparison.compare_arms(
        path,
        "base",
        **{"block_size": 5, "reps": 199, "seed": 27, **settings},
    )


def test_dominant_arm_is_superior(tmp_path, returns):
    report = compare(write_returns(tmp_path / "daily.csv", returns))
    assert report.stepm_superior == ("dominant",)
    assert report.spa_pvalues["consistent"] is not None
    assert report.spa_pvalues["consistent"] < 0.05
    assert report.tested_arms == ("dominant", "inferior")


def test_equal_arms_are_excluded_and_never_claimed_superior(tmp_path, returns):
    returns["dominant"] = returns["base"]
    returns["inferior"] = returns["base"]
    report = compare(write_returns(tmp_path / "equal.csv", returns))
    assert report.stepm_superior is None
    assert report.tested_arms == ()
    assert set(report.excluded_arms) == {"dominant", "inferior"}
    assert all("constant loss differential" in reason for reason in report.excluded_arms.values())
    assert all("excluded by arm_comparison" in reason for reason in report.excluded_arms.values())
    assert report.spa_pvalues == {"lower": None, "consistent": None, "upper": None}
    assert "not computed" in report.summary()


def test_equal_mean_arms_with_varying_differentials_are_not_superior(tmp_path, returns):
    difference = np.tile([0.01, -0.01], len(returns) // 2)
    returns["dominant"] = returns["base"] + difference
    returns["inferior"] = returns["base"] - difference
    report = compare(write_returns(tmp_path / "equal-mean.csv", returns))
    assert report.stepm_superior == ()
    assert report.tested_arms == ("dominant", "inferior")
    assert report.excluded_arms == {}
    assert report.spa_pvalues["consistent"] is not None
    assert report.spa_pvalues["consistent"] > 0.05


def test_equal_arm_is_named_without_removing_other_candidates(tmp_path, returns):
    returns["equal"] = returns["base"]
    report = compare(write_returns(tmp_path / "mixed.csv", returns))
    assert report.stepm_superior == ("dominant",)
    assert list(report.excluded_arms) == ["equal"]
    assert report.arms == ("base", "dominant", "inferior", "equal")


def test_constant_nonzero_differential_is_also_excluded(tmp_path):
    frame = pd.DataFrame(
        {"base": [0.0, 0.125, -0.125, 0.25], "offset": [0.5, 0.625, 0.375, 0.75]},
        index=pd.bdate_range("2025-03-03", periods=4),
    )
    report = compare(write_returns(tmp_path / "offset.csv", frame), block_size=2)
    assert report.stepm_superior is None
    assert "offset" in report.excluded_arms


def test_returns_are_sign_reversed_and_match_the_public_loss_api(tmp_path, returns):
    path = write_returns(tmp_path / "sign.csv", returns)
    report = compare(path)
    # Use the exact floats serialized to the file, without pandas' CSV float parser.
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    values = np.array([[float(value) for value in row[1:]] for row in rows])
    spa = SPA(
        -values[:, 0],
        -values[:, 1:],
        seed=27,
        block_size=5,
        reps=199,
        bootstrap="stationary",
        studentize=True,
        nested=False,
    )
    spa.compute()
    stepm = StepM(
        -values[:, 0],
        -values[:, 1:],
        size=0.05,
        seed=27,
        block_size=5,
        reps=199,
        bootstrap="stationary",
        studentize=True,
        nested=False,
    )
    stepm.compute()
    assert report.spa_pvalues == {name: float(value) for name, value in spa.pvalues.items()}
    assert report.stepm_superior == tuple(returns.columns[1:][stepm.superior_models])
    reversed_report = compare(write_returns(tmp_path / "reversed.csv", -returns))
    assert reversed_report.stepm_superior == ("inferior",)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "not-a-return", ""])
def test_nonfinite_or_missing_return_names_the_arm_and_day(tmp_path, bad):
    path = tmp_path / "bad.csv"
    path.write_text(
        "session_day,base,candidate\n"
        f"2025-03-03,0.01,{bad}\n2025-03-04,0.02,0.03\n2025-03-05,-0.01,0.04\n"
    )
    with pytest.raises(ValueError, match="candidate.*2025-03-03"):
        compare(path, block_size=1)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_fewer_than_three_session_days_is_refused_by_name(tmp_path, returns, count):
    path = write_returns(tmp_path / "short.csv", returns.iloc[:count])
    with pytest.raises(ValueError, match=r"short.csv.*T.*at least 3"):
        compare(path, block_size=1)


def test_three_session_days_are_accepted(tmp_path, returns):
    report = compare(write_returns(tmp_path / "three.csv", returns.iloc[:3]), block_size=1)
    assert report.observations == 3
    assert all(value is not None for value in report.spa_pvalues.values())


def test_seed_reproduces_the_entire_report(tmp_path, returns):
    rng = np.random.default_rng(423)
    returns["dominant"] = returns["base"] + rng.normal(0.0004, 0.01, len(returns))
    path = write_returns(tmp_path / "seed.csv", returns)
    assert compare(path).as_dict() == compare(path).as_dict()
    assert compare(path).spa_pvalues != compare(path, seed=81).spa_pvalues


def test_settings_hash_and_gate_boundary_are_recorded(tmp_path, returns):
    path = write_returns(tmp_path / "settings.csv", returns)
    report = compare(path, block_size=7, reps=109, seed=913, size=0.1)
    assert report.settings == {
        "arch_version": "9.0.0",
        "bootstrap": "stationary",
        "block_size": 7,
        "block_size_unit": "session_days",
        "reps": 109,
        "seed": 913,
        "studentize": True,
        "nested": False,
        "size": 0.1,
        "loss_transform": "negative_returns",
    }
    assert report.input_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report.observations == len(returns)
    assert report.first_day == returns.index[0].date().isoformat()
    assert report.last_day == returns.index[-1].date().isoformat()
    assert report.diagnostic_only is True
    assert report.dsr == dict.fromkeys(returns.columns)
    assert "ranking.rank" in report.dsr_note
    assert "ledger" in report.dsr_note
    assert "0.95" in report.summary()


def test_report_labels_mean_daily_loss_comparison_beside_dsr(tmp_path, returns):
    report = compare(write_returns(tmp_path / "measure.csv", returns))
    assert report.as_dict()["comparison_measure"] == (
        "mean per-session-day loss differential (benchmark loss minus candidate loss)"
    )
    assert "SPA and StepM compare mean per-session-day loss differentials" in report.summary()
    assert "DSR unavailable" in report.summary()
    assert report.dsr == dict.fromkeys(returns.columns)
    assert "ADR 0010" in report.dsr_note


def test_no_entry_day_must_have_an_explicit_zero(tmp_path):
    path = tmp_path / "no-entry.csv"
    path.write_text(
        "session_day,base,candidate\n"
        "2025-03-03,0.01,0.0\n2025-03-04,-0.02,0.03\n2025-03-05,0.0,-0.01\n"
    )
    report = compare(path, block_size=1)
    assert report.observations == 3
    # A missing value on the same no-entry day is refused, never imputed as zero.
    path.write_text(path.read_text().replace("2025-03-03,0.01,0.0", "2025-03-03,0.01,"))
    with pytest.raises(ValueError, match="candidate.*2025-03-03"):
        compare(path, block_size=1)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("day,base,arm\n", "session_day"),
        ("session_day,base,base\n", "distinct"),
        ("session_day,base,\n", "arm name"),
        ("session_day,base\n", "benchmark and at least one"),
        ("session_day,base,arm\n2025-03-03,0.1\n", "columns"),
        ("session_day,base,arm\n2025-03-03,0.1,0.2,0.3\n", "columns"),
        ("session_day,base,arm\n20250303,0.1,0.2\n", "YYYY-MM-DD"),
        ("session_day,base,arm\n2025-03-03,0.1,0.2\n2025-03-03,0.2,0.1\n", "increasing"),
        ("session_day,base,arm\n2025-03-04,0.1,0.2\n2025-03-03,0.2,0.1\n", "increasing"),
        ('{"kind":"replay","day":"2025-03-03"}\n', "session_day"),
    ],
)
def test_reader_refuses_ambiguous_shapes(tmp_path, content, message):
    path = tmp_path / "shape.csv"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        compare(path, block_size=1)


def test_missing_benchmark_is_refused_by_name(tmp_path, returns):
    path = write_returns(tmp_path / "benchmark.csv", returns.rename(columns={"base": "other"}))
    with pytest.raises(ValueError, match="base.*benchmark"):
        compare(path)


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"block_size": 0}, "block_size"),
        ({"block_size": 121}, "block_size"),
        ({"block_size": 1.5}, "block_size"),
        ({"reps": 0}, "reps"),
        ({"seed": -1}, "seed"),
        ({"seed": None}, "seed"),
        ({"size": 0.0}, "size"),
        ({"size": 1.0}, "size"),
        ({"size": float("nan")}, "size"),
    ],
)
def test_invalid_settings_are_refused_even_when_all_arms_are_equal(
    tmp_path, returns, settings, message
):
    returns["dominant"] = returns["base"]
    returns["inferior"] = returns["base"]
    path = write_returns(tmp_path / "settings.csv", returns)
    with pytest.raises(ValueError, match=message):
        compare(path, **settings)


def test_cli_writes_external_json_and_prints_a_summary(tmp_path, returns):
    path = write_returns(tmp_path / "cli.csv", returns)
    output = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "canon_trading.arm_comparison",
            "--input",
            str(path),
            "--benchmark",
            "base",
            "--block-size",
            "5",
            "--reps",
            "199",
            "--seed",
            "27",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text())
    assert payload["stepm_superior"] == ["dominant"]
    assert payload["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["settings"]["studentize"] is True
    assert payload["diagnostic_only"] is True
    assert "mean per-session-day loss differential" in payload["comparison_measure"]
    assert "SPA and StepM compare mean per-session-day loss differentials" in completed.stdout
    assert "SPA" in completed.stdout and "StepM" in completed.stdout
    assert "DSR unavailable" in completed.stdout


def test_cli_refuses_output_inside_repository(tmp_path, returns, capsys):
    path = write_returns(tmp_path / "cli.csv", returns)
    written = comparison.CHECKOUT / "bad.json"
    try:
        with pytest.raises(SystemExit) as stopped:
            comparison.main(cli(path, written))
        assert stopped.value.code == 2
        assert "outside the repository" in capsys.readouterr().err
        assert not written.exists()
    finally:
        written.unlink(missing_ok=True)  # a failing run must not leave a report in the checkout


@pytest.mark.parametrize("missing", ["--block-size", "--reps", "--seed"])
def test_cli_requires_each_bootstrap_setting(tmp_path, missing, capsys):
    arguments = {
        "--input": "unused.csv",
        "--benchmark": "base",
        "--block-size": "1",
        "--reps": "19",
        "--seed": "27",
        "--output": str(tmp_path / "unused.json"),
    }
    del arguments[missing]
    with pytest.raises(SystemExit) as stopped:
        comparison.main([value for pair in arguments.items() for value in pair])
    assert stopped.value.code == 2
    assert missing in capsys.readouterr().err


def three_days(
    candidate: tuple[str, str, str], base: tuple[str, str, str] = ("0.01", "0.02", "-0.01")
):
    days = ("2025-03-03", "2025-03-04", "2025-03-05")
    rows = "".join(f"{day},{b},{c}\n" for day, b, c in zip(days, base, candidate, strict=True))
    return "session_day,base,candidate\n" + rows


def cli(path: Path, output: Path) -> list[str]:
    return [
        *("--input", str(path), "--benchmark", "base", "--block-size", "5"),
        *("--reps", "19", "--seed", "27", "--output", str(output)),
    ]


def test_untested_stepm_is_null_and_not_an_empty_superior_set(tmp_path, returns):
    returns["dominant"] = returns["base"]
    returns["inferior"] = returns["base"]
    report = compare(write_returns(tmp_path / "equal.csv", returns))
    assert report.stepm_superior is None
    assert report.as_dict()["stepm_superior"] is None
    assert "StepM superior set: not computed" in report.summary()
    assert "StepM superior set: none" not in report.summary()


def test_constant_differential_reason_gives_its_sign_and_size(tmp_path, returns):
    returns["dominant"] = returns["base"] + 0.5
    returns["inferior"] = returns["base"] - 0.25
    returns["equal"] = returns["base"]
    report = compare(write_returns(tmp_path / "offsets.csv", returns))
    assert "of +0.5 per session day" in report.excluded_arms["dominant"]
    assert "of -0.25 per session day" in report.excluded_arms["inferior"]
    assert "of +0 per session day" in report.excluded_arms["equal"]


@pytest.mark.parametrize("cell", ["1_0", " 0.02", "0.02 ", "0x1p-3", "１"])
def test_a_cell_that_is_not_a_plain_decimal_is_refused(tmp_path, cell):
    path = tmp_path / "lax.csv"
    path.write_text(three_days((cell, "0.03", "0.04")), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate.*2025-03-03"):
        compare(path, block_size=1)


def test_a_byte_order_mark_is_refused_by_name(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf" + three_days(("0.02", "0.03", "0.04")).encode())
    with pytest.raises(ValueError, match="byte order mark"):
        compare(path, block_size=1)


@pytest.mark.parametrize("size", ["0.05", None, True])
def test_a_size_that_is_not_a_number_is_a_value_error(tmp_path, returns, size):
    path = write_returns(tmp_path / "size.csv", returns)
    with pytest.raises(ValueError, match="size"):
        compare(path, size=size)


def test_a_statistic_arch_warns_about_is_a_named_value_error(tmp_path):
    path = tmp_path / "huge.csv"
    path.write_text(three_days(("1e200", "2e200", "3e200")))
    with pytest.raises(ValueError, match=r"huge\.csv.*arch"):
        compare(path, block_size=1)


def test_a_differential_too_small_to_studentize_is_refused_by_name(tmp_path):
    path = tmp_path / "tiny.csv"
    path.write_text(three_days(("2e-300", "5e-300", "1e-300"), base=("1e-300", "2e-300", "3e-300")))
    with pytest.raises(ValueError, match="candidate.*variance"):
        compare(path, block_size=1)


def test_cli_refuses_an_output_in_the_checkout_spelled_in_another_case(tmp_path, returns, capsys):
    other_case = type(comparison.CHECKOUT)(str(comparison.CHECKOUT).swapcase())
    if not other_case.exists():
        pytest.skip("this filesystem distinguishes case")
    path = write_returns(tmp_path / "cli.csv", returns)
    written = comparison.CHECKOUT / "bad-case.json"
    try:
        with pytest.raises(SystemExit) as stopped:
            comparison.main(cli(path, other_case / "bad-case.json"))
        assert stopped.value.code == 2
        assert "outside the repository" in capsys.readouterr().err
        assert not written.exists()
    finally:
        written.unlink(missing_ok=True)  # a failing run must not leave a report in the checkout


def test_cli_refuses_an_existing_output_before_reading_the_input(tmp_path, capsys):
    output = tmp_path / "report.json"
    output.write_text("kept")
    with pytest.raises(SystemExit) as stopped:
        comparison.main(cli(tmp_path / "absent.csv", output))
    assert stopped.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert output.read_text() == "kept"


@pytest.mark.parametrize("offset", [0.00001, 0.0001, 0.0003])
def test_a_small_fixed_offset_is_excluded_as_constant_and_never_superior(tmp_path, returns, offset):
    # Rounded returns plus a small offset differ by rounding noise only, which is large against
    # the offset and small against the returns: arch's own criterion measures it against the offset.
    base = returns["base"].round(6)
    frame = pd.DataFrame({"base": base, "offset": base + offset, "dominant": returns["dominant"]})
    report = compare(write_returns(tmp_path / "offset.csv", frame))
    assert "constant loss differential" in report.excluded_arms["offset"]
    assert f"of {offset:+.6g} per session day" in report.excluded_arms["offset"]
    assert report.tested_arms == ("dominant",)
    assert report.stepm_superior == ("dominant",)


def test_an_arm_equal_to_the_benchmark_up_to_rounding_is_excluded(tmp_path, returns):
    nudged = returns["base"].to_numpy().copy()
    nudged[::2] = np.nextafter(nudged[::2], np.inf)  # one unit in the last place, every other day
    frame = pd.DataFrame({"base": returns["base"], "nudged": nudged}, index=returns.index)
    report = compare(write_returns(tmp_path / "nudged.csv", frame))
    assert report.tested_arms == ()
    assert "constant loss differential" in report.excluded_arms["nudged"]
    assert report.stepm_superior is None


def test_a_standard_error_at_archs_floor_is_refused_by_name(tmp_path):
    # Not constant, and its variance is not zero, yet sqrt(variance / T) is below machine epsilon,
    # where arch floors the standard error and the statistic is no longer studentized.
    path = tmp_path / "floor.csv"
    path.write_text(three_days(("2e-17", "5e-17", "1e-17"), base=("1e-17", "2e-17", "3e-17")))
    with pytest.raises(ValueError, match="candidate.*variance.*studentize"):
        compare(path, block_size=1)


def test_cli_leaves_no_report_when_the_write_fails_at_close(tmp_path, returns, capsys, monkeypatch):
    path = write_returns(tmp_path / "cli.csv", returns)
    output = tmp_path / "report.json"
    real_open = type(output).open

    def open_then_fail_at_close(self, mode="r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if mode != "x":
            return stream
        real_close = stream.close

        def close():
            real_close()
            raise OSError(28, "No space left on device")

        stream.close = close
        return stream

    monkeypatch.setattr(type(output), "open", open_then_fail_at_close)
    with pytest.raises(SystemExit) as stopped:
        comparison.main(cli(path, output))
    assert stopped.value.code == 2
    assert "No space left" in capsys.readouterr().err
    assert not output.exists()  # a partial report would block the rerun


def test_cli_refuses_an_output_in_another_checkout(tmp_path, returns, capsys):
    path = write_returns(tmp_path / "cli.csv", returns)
    other = tmp_path / "other-checkout"
    (other / "ledger").mkdir(parents=True)
    (other / ".git").write_text("gitdir: elsewhere\n")  # what a linked worktree holds
    with pytest.raises(SystemExit) as stopped:
        comparison.main(cli(path, other / "ledger" / "report.json"))
    assert stopped.value.code == 2
    assert "outside the repository" in capsys.readouterr().err
    assert not (other / "ledger" / "report.json").exists()


def test_cli_refuses_a_missing_output_folder_before_reading_the_input(tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        comparison.main(cli(tmp_path / "absent.csv", tmp_path / "nodir" / "report.json"))
    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert "nodir" in error and "not an existing folder" in error
    assert "absent.csv" not in error
