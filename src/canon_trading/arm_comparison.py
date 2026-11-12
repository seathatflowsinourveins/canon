"""ADR 0104's daily arm diagnostic through arch 9.0.0's public SPA and StepM API.

Contract: arch at ef63ff467d329320f5e03fcf426df95214648539,
`arch/bootstrap/multiple_comparison.py:621-1020,1063-1088,1206-1218`: T finite
benchmark losses, T by k model losses, T >= 3, explicit stationary block length,
replications and seed, studentize=True. Losses are the negatives of returns.
`arch/bootstrap/base.py:1915-1929` defines the average stationary block length.
SPA and StepM compare mean per-session-day loss differentials, studentized as
`multiple_comparison.py:864-870,1143-1145` describes. DSR remains ADR 0010's measure.

Input: the file form of ADR 0104's matrix, one row per session day and one column per
arm, which this module defines: UTF-8 CSV with header
`session_day,<benchmark arm>,<other arm>,...`, in any arm order. The first column
contains unique, strictly increasing YYYY-MM-DD session dates; every remaining
column has a distinct, nonempty arm name and one finite numeric return per day.
There must be at least three days and two arms. Returns are decimal portfolio
returns on a common capital basis, already reduced to session days by the caller.
A flat no-entry day has an explicit 0.0; a missing cell is refused, never filled. A
cell is a plain decimal (digits, an optional point, sign and exponent), so the underscores
and padding `float()` would take are refused, as is a byte order mark. This
reader neither verifies the session calendar or registered arm set nor invents
capital weights for symbol-day dollar P&L. No producer of this matrix is built yet.

Arch warns about constant loss differentials and retains those columns. This
module removes and names them before calling arch, following its advice at
`multiple_comparison.py:898-901`, and names the constant so that an arm ahead by a fixed
amount does not read as an equal one. Arch's rounding criterion at :974-992 measures the
spread against the differential itself; this module measures it against the two loss
columns as well, because the rounding error of a subtraction scales with what is
subtracted, so an arm that differs from the benchmark by a small fixed offset is constant
here whatever its size. With no testable candidates the p-values and the superior set are
null, not a claim to have tested equality. Arch bounds the standard error of a mean
differential at machine epsilon (`_std_err`, :1132-1135) and the statistic is then no
longer studentized; a differential whose sample standard error is at or below that bound,
and any warning arch raises while computing, is a refusal that names the input. Arch's own
variance is the kernel estimate that matches the bootstrap (:1161-1188), which the sample
variance stands in for here, since the estimate is not part of arch's public surface.
The bootstrap runs twice with one seed, once for SPA and once inside StepM: StepM's
documented surface is `compute`, `reset`, `bootstrap` and `superior_models`, and the
SPA it holds is not part of it, so its p-values are not read from there.
No statistic is implemented here. No pick is ranked or promoted: ADR 0010's
ledger-based DSR gate is unchanged. `ranking.rank(study, ...)` exposes no DSR for
each arm of a raw matrix, so each DSR is null with that reason, rather than using
the column count as the ledger's N. Reports must be written outside this checkout and
any other git checkout.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import warnings
from dataclasses import asdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from arch import __version__ as ARCH_VERSION
from arch.bootstrap import SPA
from arch.bootstrap import StepM

if TYPE_CHECKING:
    from collections.abc import Sequence

CHECKOUT = Path(__file__).resolve().parents[2]
DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
DSR_NOTE = (
    "DSR unavailable: ranking.rank accepts a ledger study and reports only its pick's DSR; "
    "it exposes no per-arm DSR for a raw daily matrix, which supplies neither the ledger's "
    "N nor its validation. Use the separate ADR 0010 ledger gate."
)


@dataclass(frozen=True)
class ComparisonReport:
    """Bootstrap results and their provenance, without a promotion decision."""

    input_file: str
    input_sha256: str
    benchmark: str
    arms: tuple[str, ...]
    tested_arms: tuple[str, ...]
    excluded_arms: dict[str, str]
    observations: int
    first_day: str
    last_day: str
    settings: dict[str, str | int | float | bool]
    spa_pvalues: dict[str, float | None]
    stepm_superior: tuple[str, ...] | None
    dsr: dict[str, None]
    comparison_measure: str = (
        "mean per-session-day loss differential (benchmark loss minus candidate loss)"
    )
    dsr_note: str = DSR_NOTE
    diagnostic_only: bool = True
    input_note: str = (
        "Minimal daily CSV contract; session calendar, capital basis and completeness of "
        "the registered arm set are supplied by the caller, not verified by this reader."
    )

    def as_dict(self) -> dict[str, object]:
        """JSON-compatible report; tuples become arrays when JSON serializes it."""
        return asdict(self)

    def summary(self) -> str:
        """Plain text counterpart to the JSON, including unavailable tests and the gate limit."""
        settings = self.settings
        pvalues = ", ".join(
            f"{name}={value:.6g}" if value is not None else f"{name}=not computed"
            for name, value in self.spa_pvalues.items()
        )
        lines = [
            (
                f"Arm comparison: benchmark {self.benchmark!r}, T={self.observations} session days "
                f"({self.first_day} to {self.last_day}), {len(self.tested_arms)} tested candidates."
            ),
            f"Input: {self.input_file}; sha256={self.input_sha256}.",
            (
                "SPA and StepM compare mean per-session-day loss differentials "
                "(benchmark loss minus candidate loss), using studentization."
            ),
            (
                f"arch {settings['arch_version']}: bootstrap=stationary, "
                f"block_size={settings['block_size']} session days (average), "
                f"reps={settings['reps']}, seed={settings['seed']}, studentize=True, nested=False, "
                f"size={settings['size']}; losses=-returns."
            ),
            f"SPA: {pvalues}.",
            "StepM superior set: "
            + (
                "not computed"
                if self.stepm_superior is None
                else ", ".join(self.stepm_superior) or "none"
            )
            + ".",
        ]
        if not self.tested_arms:
            lines.append("SPA and StepM not computed: no nonconstant loss differentials remain.")
        lines.extend(f"Excluded {arm!r}: {reason}" for arm, reason in self.excluded_arms.items())
        lines.extend(
            [
                self.dsr_note,
                self.input_note,
                "Diagnostic only (ADR 0104); ADR 0010's DSR gate at 0.95 is unchanged.",
            ]
        )
        return "\n".join(lines)


def _read_daily_returns(path: Path) -> tuple[pd.DataFrame, str]:
    """Validate the minimal CSV contract and hash the exact bytes that were parsed."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path}: starts with a byte order mark; write UTF-8 without one")
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    header = next(reader, [])
    if not header or header[0] != "session_day":
        raise ValueError(f"{path}: expected CSV header session_day,<arm>,<arm>,...")
    arms = header[1:]
    if len(arms) < 2:
        raise ValueError(f"{path}: expected a benchmark and at least one candidate arm")
    if any(not arm or arm != arm.strip() or arm == "session_day" for arm in arms):
        raise ValueError(f"{path}: each arm name must be nonempty, trimmed and not session_day")
    if len(set(arms)) != len(arms):
        raise ValueError(f"{path}: arm names must be distinct")
    days: list[str] = []
    values: list[list[float]] = []
    for row in reader:
        if len(row) != len(header):
            raise ValueError(f"{path}: row {reader.line_num} needs {len(header)} columns")
        day = row[0]
        try:
            canonical = date.fromisoformat(day).isoformat()
        except ValueError:
            canonical = ""
        if not day or canonical != day:
            raise ValueError(f"{path}: row {reader.line_num} session_day must be YYYY-MM-DD")
        if days and day <= days[-1]:
            raise ValueError(f"{path}: session days must be strictly increasing; found {day}")
        daily = []
        for arm, text in zip(arms, row[1:], strict=True):
            # float() also takes "1_0", padding and other digits; the contract is a plain decimal.
            if not DECIMAL.fullmatch(text):
                raise ValueError(f"{arm}: return on {day} is not a plain decimal number: {text!r}")
            value = float(text)
            if not math.isfinite(value):
                raise ValueError(f"{arm}: return on {day} is not finite: {text!r}")
            daily.append(value)
        days.append(day)
        values.append(daily)
    if len(days) < 3:
        raise ValueError(f"{path}: T={len(days)} session days; SPA and StepM require at least 3")
    return pd.DataFrame(values, index=days, columns=arms, dtype=float), hashlib.sha256(
        raw
    ).hexdigest()


def _validate_settings(t: int, block_size: int, reps: int, seed: int, size: float) -> None:
    """arch's documented bounds, including when all candidates must be excluded."""
    for name, value in (("block_size", block_size), ("reps", reps), ("seed", seed)):
        if type(value) is not int:
            raise ValueError(f"{name} must be an explicit integer")
    if not 1 <= block_size <= t:
        raise ValueError(f"block_size must be between 1 and T={t} session days")
    if reps < 1:
        raise ValueError("reps must be at least 1")
    if seed < 0:
        raise ValueError("seed must be nonnegative")
    if type(size) not in (int, float) or not math.isfinite(size) or not 0.0 < size < 1.0:
        raise ValueError("size (family-wise error rate) must be finite and in (0, 1)")


def compare_arms(
    input_path: Path,
    benchmark: str,
    *,
    block_size: int,
    reps: int,
    seed: int,
    size: float = 0.05,
) -> ComparisonReport:
    """Run SPA and StepM on one daily CSV; no ranking, calendar lookup or ledger read.

    `block_size` is an explicit average number of session days, in [1, T], not
    an estimated block length. `size` is StepM's FWER, default 0.05 as in arch.
    The same integer seed initializes each independent upstream test.
    """
    if ARCH_VERSION != "9.0.0":
        raise RuntimeError(f"arm comparison requires arch 9.0.0; found {ARCH_VERSION}")
    path = Path(input_path).resolve()
    try:
        returns, digest = _read_daily_returns(path)
    except (csv.Error, UnicodeError) as error:
        raise ValueError(f"{path}: expected UTF-8 session_day CSV: {error}") from error
    if benchmark not in returns.columns:
        raise ValueError(f"{benchmark!r}: benchmark arm is absent from {path}")
    _validate_settings(len(returns), block_size, reps, seed, size)
    losses = -returns
    tested = []
    excluded = {}
    for arm in returns.columns:
        if arm == benchmark:
            continue
        # arch's constant-differential criterion (:974-992 in the header), with the rounding
        # error measured against the two columns subtracted as well as against their difference.
        # Refuse overflow rather than passing a nonfinite derived loss to arch.
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            ours = losses[benchmark].to_numpy()
            theirs = losses[arm].to_numpy()
            difference = ours - theirs
            spread = np.ptp(difference)
            standard_error = math.sqrt(float(np.var(difference)) / len(difference))
        if not np.isfinite(difference).all() or not math.isfinite(float(spread)):
            raise ValueError(f"{arm}: loss differential or its range is not finite")
        largest = max(np.abs(difference).max(), np.abs(ours).max(), np.abs(theirs).max())
        if spread <= 8 * np.finfo(float).eps * largest:
            excluded[arm] = (
                f"constant loss differential of {float(np.median(difference)):+.6g} per session "
                "day (benchmark loss minus candidate loss); excluded by arm_comparison before "
                "calling arch (arch only warns)"
            )
        elif standard_error <= np.finfo(float).eps:
            # arch would floor the standard error there and report an unstudentized statistic.
            raise ValueError(
                f"{arm}: the variance of its loss differential is too small for arch to "
                f"studentize it: the standard error of its mean, {standard_error:.3g}, is at or "
                f"below arch's floor of machine epsilon ({path})"
            )
        else:
            tested.append(arm)

    pvalues: dict[str, float | None] = dict.fromkeys(("lower", "consistent", "upper"))
    superior: tuple[str, ...] | None = None
    if tested:
        # A warning about an unusable statistic is a refusal, never a substitute result.
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                spa = SPA(
                    losses[benchmark],
                    losses[tested],
                    block_size=block_size,
                    reps=reps,
                    bootstrap="stationary",
                    studentize=True,
                    nested=False,
                    seed=seed,
                )
                spa.compute()
                stepm = StepM(
                    losses[benchmark],
                    losses[tested],
                    size=size,
                    block_size=block_size,
                    reps=reps,
                    bootstrap="stationary",
                    studentize=True,
                    nested=False,
                    seed=seed,
                )
                stepm.compute()
        except RuntimeWarning as error:
            raise ValueError(
                f"{path}: arch warned while computing SPA or StepM, so no result is reported: "
                f"{error}"
            ) from error
        computed = {str(name): float(value) for name, value in spa.pvalues.items()}
        if not all(math.isfinite(value) for value in computed.values()):
            raise ValueError("SPA returned nonfinite p-values")
        pvalues.update(computed)
        superior = tuple(str(arm) for arm in stepm.superior_models)

    return ComparisonReport(
        input_file=str(path),
        input_sha256=digest,
        benchmark=benchmark,
        arms=tuple(returns.columns),
        tested_arms=tuple(tested),
        excluded_arms=excluded,
        observations=len(returns),
        first_day=str(returns.index[0]),
        last_day=str(returns.index[-1]),
        settings={
            "arch_version": ARCH_VERSION,
            "bootstrap": "stationary",
            "block_size": block_size,
            "block_size_unit": "session_days",
            "reps": reps,
            "seed": seed,
            "studentize": True,
            "nested": False,
            "size": size,
            "loss_transform": "negative_returns",
        },
        spa_pvalues=pvalues,
        stepm_superior=superior,
        dsr=dict.fromkeys(returns.columns),
    )


def _inside_checkout(path: Path) -> bool:
    """Whether `path` is in this checkout or in any other git checkout, however it is spelled.

    `os.path.samefile` compares the device and inode numbers `os.stat` gives, so a name in
    another case on a case-insensitive volume, or one through a link, is still found. A folder
    that holds a `.git` entry is a checkout too: the main one holds a folder of that name and a
    linked worktree a file (gitrepository-layout(5)), so a report cannot land in a sibling
    worktree's ignored folders either. The path itself need not exist: its nearest existing
    ancestors are what is compared.
    """
    return any(
        candidate.exists()
        and (os.path.samefile(candidate, CHECKOUT) or (candidate / ".git").exists())
        for candidate in (path, *path.parents)
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Write the JSON outside the checkout and print its plain text summary."""
    parser = argparse.ArgumentParser(
        description="Compare arms' daily returns against a benchmark arm with arch's SPA and "
        "StepM, as a diagnostic beside ADR 0010's gate (ADR 0104). Writes a JSON report."
    )
    parser.add_argument("--input", type=Path, required=True, help="strict daily return CSV")
    parser.add_argument("--benchmark", required=True, help="benchmark arm column name")
    parser.add_argument(
        "--block-size", type=int, required=True, help="average block length in days"
    )
    parser.add_argument("--reps", type=int, required=True, help="bootstrap replications")
    parser.add_argument("--seed", type=int, required=True, help="nonnegative integer seed")
    parser.add_argument("--size", type=float, default=0.05, help="StepM family-wise error rate")
    parser.add_argument(
        "--output", type=Path, required=True, help="new JSON file outside the repository"
    )
    args = parser.parse_args(argv)
    try:
        output = args.output.resolve()
        if _inside_checkout(output):
            raise ValueError(
                "output must be outside the repository and any other git checkout, including "
                "ignored directories"
            )
        if output.exists():
            raise ValueError(f"output already exists and is never replaced: {output}")
        if not output.parent.is_dir():  # found here, not after the bootstrap has run
            raise ValueError(f"output's folder is not an existing folder: {output.parent}")
        report = compare_arms(
            args.input,
            args.benchmark,
            block_size=args.block_size,
            reps=args.reps,
            seed=args.seed,
            size=args.size,
        )
        payload = json.dumps(report.as_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n"
        stream = output.open("x", encoding="utf-8")  # an existing file is not ours to remove
        try:
            with stream:  # the payload is buffered, so a full disk is reported at close
                stream.write(payload)
        except OSError:
            output.unlink(missing_ok=True)  # a partial report would block the rerun
            raise
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
