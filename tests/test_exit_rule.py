"""`python -m canon_trading.exit_rule`: each clause of ADR 0150 printed from the case set's own marks.

A case of the set is `@pytest.mark.no_position_without_its_exit(clause=N)`. A clause is built when every
one of its cases passes, not built when any is a strict expected failure (with that failure's reason),
and has no case when none names it. A case that fails, or an expected failure that passes, is broken, and
the command exits 1. Fixture test files only; nothing here reaches the network.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from canon_trading import exit_rule

PASSING = """
    import pytest

    @pytest.mark.no_position_without_its_exit(clause=1)
    def test_armed():
        assert True

    @pytest.mark.no_position_without_its_exit(clause=2)
    def test_found_holding_covered():
        assert True

    @pytest.mark.no_position_without_its_exit(clause=2)
    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="gap: over the ceiling")
    def test_found_holding_over_the_ceiling():
        assert False

    def test_unmarked():
        assert True
"""


def _write(folder: Path, body: str) -> Path:
    path = folder / "test_cases.py"
    path.write_text(textwrap.dedent(body))
    return path


ROOT = Path(__file__).resolve().parents[1]


def _run(*paths: Path | str, cwd: Path = ROOT) -> tuple[int, str]:
    """The command as a reader runs it, in its own interpreter: pytest is not re-entered in this one."""
    done = subprocess.run(
        [sys.executable, "-m", "canon_trading.exit_rule", *map(str, paths)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
        cwd=cwd,
    )
    return done.returncode, done.stdout + done.stderr


def test_each_clause_is_printed_from_its_cases(tmp_path) -> None:
    code, said = _run(_write(tmp_path, PASSING))
    assert code == 0, said
    out = said.splitlines()
    assert out[0].startswith("No position without its exit (ADR 0150): ")
    row = {line.split(":", 1)[0]: line for line in out if line.startswith("clause ")}
    assert set(row) == {f"clause {n}" for n in range(1, 8)}
    assert row["clause 1"] == "clause 1: built as far as its 1 case reaches"
    assert row["clause 2"] == "clause 2: not built (1 case passing, 1 expected failure)"
    assert row["clause 4"] == "clause 4: no case"
    assert "  expected failure test_found_holding_over_the_ceiling: gap: over the ceiling" in out
    assert not [line for line in out if "test_unmarked" in line]


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=1)
            def test_failing():
                assert False
            """,
            "broken: test_failing failed",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=3)
            @pytest.mark.xfail(strict=True, raises=AssertionError, reason="gap")
            def test_fixed_but_still_marked():
                assert True
            """,
            "broken: test_fixed_but_still_marked passed while marked an expected failure",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit
            def test_no_clause():
                assert True
            """,
            "broken: test_no_clause names no clause from 1 to 7",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=1)
            @pytest.mark.xfail(reason="gap")
            def test_not_strict():
                assert False
            """,
            "broken: test_not_strict is an expected failure that is not strict",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=1)
            def test_imperative():
                pytest.xfail("gap")
            """,
            "broken: test_imperative is an expected failure with no strict mark",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=True)
            def test_bool_clause():
                assert True
            """,
            "broken: test_bool_clause names no clause from 1 to 7",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(2)
            def test_positional():
                assert True
            """,
            "broken: test_positional names its clause by position; write clause=2",
        ),
        (
            """
            import pytest

            @pytest.mark.no_position_without_its_exit(clause=1)
            @pytest.mark.skip(reason="later")
            def test_skipped():
                assert True
            """,
            "broken: test_skipped skipped",
        ),
    ],
    ids=["fails", "xpass", "no-clause", "not-strict", "imperative", "bool", "positional", "skip"],
)
def test_a_broken_case_is_named_and_exits_1(tmp_path, body, said) -> None:
    code, out = _run(_write(tmp_path, body))
    assert code == 1 and said in out, out


def test_a_broken_case_is_named_by_its_node_id(tmp_path) -> None:
    body = "import pytest\n\n@pytest.mark.no_position_without_its_exit(clause=1)\ndef test_armed():\n    assert False\n"
    code, out = _run(_write(tmp_path, body))
    assert code == 1 and "test_cases.py::test_armed" in out, out


def test_a_set_with_no_case_is_broken(tmp_path) -> None:
    code, out = _run(_write(tmp_path, "def test_unmarked():\n    assert True\n"))
    assert code == 1 and "broken: the set has no case" in out, out


def _read_the_set(out: str) -> None:
    lines = out.splitlines()
    assert lines[0].startswith("No position without its exit (ADR 0150): ")
    assert int(lines[0].split(": ", 1)[1].split(" ", 1)[0]) > 0
    rows = [line for line in lines if line.startswith("clause ")]
    assert [row.split(":", 1)[0] for row in rows] == [f"clause {n}" for n in range(1, 8)]


def test_the_repositorys_own_set_names_a_clause_for_every_case_and_runs_clean() -> None:
    """The real set, as the record points at it: every case names a clause and none is broken."""
    code, out = _run()
    assert code == 0, out
    _read_the_set(out)


def test_the_set_is_the_repositorys_own_from_any_folder(tmp_path) -> None:
    code, out = _run(cwd=tmp_path)
    assert code == 0, out
    _read_the_set(out)


def test_the_record_holds_no_hand_kept_status_and_points_at_the_command() -> None:
    root = Path(__file__).resolve().parents[1]
    (record,) = (root / "docs" / "adr").glob("0150-*.md")
    text = record.read_text()
    assert "python -m canon_trading.exit_rule" in text
    assert "| As of 2026-11-13 |" not in text


def test_a_case_names_no_clause_out_of_range() -> None:
    case = exit_rule.Case("test_x", None, outcome="passed")
    assert exit_rule.broken(case) == "test_x names no clause from 1 to 7"
