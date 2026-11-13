"""Print each clause of ADR 0150, "no position without its exit", from the case set's own marks.

The case set is every test marked `no_position_without_its_exit(clause=N)`, N from 1 to 7 as ADR 0150
numbers its clauses. This command runs that set in process and prints one line per clause:

* `built`: it has cases and every one passes;
* `not built`: at least one of its cases is a strict expected failure; that failure's reason is printed
  under it, and is the clause's open gap;
* `no case`: no case names it, so the set does not hold it yet.

A case that fails, an expected failure that passes (its fix landed but its mark stayed), a strict mark
missing (an imperative `pytest.xfail` has none), a skipped case, a case naming no clause, or a set with no
case at all is `broken`. It is named by its node ID, and the command exits 1. The record holds no status
of its own; a landing that closes a gap removes its case's expected-failure mark in the same pull request,
and this command's next run prints the change.

The run is the repository's own: it runs from the repository root, so pytest reads `pytest.toml` and its
`testpaths` from whatever folder it is started in, and a path given is read from the caller's folder.
Output the run writes, the Rust logs of NautilusTrader's test kit among it, goes to file descriptor 1
directly; it is held at that descriptor and printed only when the set is broken.

Contracts, pytest 9.2.0 at the pin: `pytest.main(args, plugins=[...])` runs in process with plugin
objects (`_pytest/config/__init__.py:214-223`); `pytest_collection_modifyitems` sees each item's marks;
`pytest_runtest_logreport` sees each phase's report, and an expected failure is a report with
`wasxfail`, `skipped` when it failed as expected, while a strict one that passed is `failed` with
`[XPASS(strict)]` (`_pytest/skipping.py:280-319`). It sends nothing and reads no credential; it imports
pytest only when it runs.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Sequence

MARK = "no_position_without_its_exit"
CLAUSES = range(1, 8)
"""ADR 0150's seven clauses."""
RECORD = "ADR 0150"
ROOT = Path(__file__).resolve().parents[2]
"""The repository root, where `pytest.toml` names the test paths."""


@dataclass
class Case:
    name: str
    clause: int | None
    where: str = ""
    """The case's pytest node ID."""
    strict: bool | None = None
    """None when the case is no expected failure; else whether its mark is strict."""
    reason: str = ""
    outcome: str = "not run"
    positional: object = None
    """A clause given by position rather than as `clause=N`."""


@dataclass(eq=False)  # a plugin is registered by identity, so it stays hashable
class _Collector:
    """A pytest plugin object: reads each marked case's clause and expected-failure mark, then its result."""

    cases: dict[str, Case] = field(default_factory=dict)

    def pytest_collection_modifyitems(self, config: Any, items: list[Any]) -> None:
        for item in items:
            mark = item.get_closest_marker(MARK)
            if mark is None:
                continue
            clause = mark.kwargs.get("clause")
            named = type(clause) is int and clause in CLAUSES  # True is an int, and no clause
            case = Case(item.name, clause if named else None, where=item.nodeid)
            if mark.args:
                case.positional = mark.args[0]
            xfail = item.get_closest_marker("xfail")
            if xfail is not None:
                strict = xfail.kwargs.get("strict")  # pytest's own order: the mark, then the ini
                if strict is None:
                    strict = config.getini("strict_xfail")
                if strict is None:
                    strict = config.getini("strict")
                case.strict = bool(strict)
                case.reason = str(xfail.kwargs.get("reason", ""))
            self.cases[item.nodeid] = case

    def pytest_runtest_logreport(self, report: Any) -> None:
        case = self.cases.get(report.nodeid)
        if case is None:
            return
        if report.failed:
            if "[XPASS(strict)]" in str(report.longrepr):
                case.outcome = "xpassed"
            else:
                case.outcome = "failed"
        elif report.when == "call":
            if hasattr(report, "wasxfail"):
                case.outcome = "xfailed" if report.skipped else "xpassed"
                if case.strict is None:  # pytest.xfail() in the body: no mark, so never strict
                    case.outcome = "imperative"
            elif report.passed:
                case.outcome = "passed"
        elif report.skipped and case.outcome == "not run":
            case.outcome = "skipped"


def broken(case: Case) -> str | None:
    """Why `case` breaks the set, or None; the reason ends with the case's node ID."""
    why = _why(case)
    return f"{why} ({case.where})" if why and case.where else why


def _why(case: Case) -> str | None:
    if case.positional is not None:
        return f"{case.name} names its clause by position; write clause={case.positional!r}"
    if case.clause is None:
        return f"{case.name} names no clause from 1 to 7"
    if case.strict is False:
        return f"{case.name} is an expected failure that is not strict"
    if case.outcome == "imperative":
        return f"{case.name} is an expected failure with no strict mark"
    if case.outcome == "failed":
        return f"{case.name} failed"
    if case.outcome == "xpassed":
        return f"{case.name} passed while marked an expected failure"
    if case.outcome not in ("passed", "xfailed"):
        return f"{case.name} {case.outcome}"
    return None


def _count(n: int, word: str) -> str:
    return (
        f"{n} {word}"
        if n == 1
        else f"{n} {word.replace('case', 'cases').replace('failure', 'failures')}"
    )


def report(cases: Sequence[Case]) -> tuple[list[str], bool]:
    """The printed lines, and whether the set is clean."""
    faults = [why for case in cases if (why := broken(case))]
    if not cases:
        faults.append("the set has no case")
    lines = [
        f"No position without its exit ({RECORD}): {len(cases)} cases, "
        + ("clean" if not faults else f"{len(faults)} broken")
        + "; a clause is built as far as its cases reach, and a gap with no case is not seen"
    ]
    for clause in CLAUSES:
        mine = [c for c in cases if c.clause == clause]
        passing = [c for c in mine if c.outcome == "passed"]
        gaps = [c for c in mine if c.outcome == "xfailed"]
        if not mine:
            lines.append(f"clause {clause}: no case")
            continue
        if not gaps and len(passing) == len(mine):
            lines.append(
                f"clause {clause}: built as far as its {_count(len(mine), 'case')} "
                + ("reaches" if len(mine) == 1 else "reach")
            )
            continue
        parts = [_count(len(passing), "case passing")]
        if gaps:
            parts.append(_count(len(gaps), "expected failure"))
        state = "not built" if gaps else "broken"
        lines.append(f"clause {clause}: {state} ({', '.join(parts)})")
        lines += [f"  expected failure {c.name}: {c.reason}" for c in gaps]
    lines += [f"broken: {why}" for why in faults]
    return lines, not faults


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canon_trading.exit_rule", description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="test files or folders, from this folder (default: the repository's testpaths)",
    )
    args = parser.parse_args(argv)
    import pytest  # a dev dependency, needed only when the set runs

    paths = [str(Path(path).resolve()) for path in args.paths]
    collector = _Collector()
    with tempfile.TemporaryFile("w+") as held, contextlib.chdir(ROOT):
        sys.stdout.flush()
        saved = os.dup(1)
        os.dup2(
            held.fileno(), 1
        )  # the clauses come first; the run's own output, native code's too, after
        try:
            with contextlib.redirect_stdout(held):
                code = pytest.main(
                    ["-q", "-p", "no:cacheprovider", "--no-header", "-m", MARK, *paths],
                    plugins=[collector],
                )
            held.flush()
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        held.seek(0)
        pytest_said = held.read()
    if code not in (
        pytest.ExitCode.OK,
        pytest.ExitCode.TESTS_FAILED,
        pytest.ExitCode.NO_TESTS_COLLECTED,
    ):
        print(f"broken: pytest stopped with {code!r} before the set ran")
        print(pytest_said)
        return 1
    lines, clean = report(list(collector.cases.values()))
    print("\n".join(lines))
    if not clean:
        print("\npytest's own report of the run:\n" + pytest_said)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
