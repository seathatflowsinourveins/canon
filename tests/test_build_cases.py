"""Every build case is well formed and points at a fix and tests that exist, and the harness does
what `evals/build/README.md` says.

The contract is the case format of ADR 0057, written out in `evals/build/README.md`: one TOML file per
case in `evals/build/cases/`, read with the standard library's `tomllib`. A ready case names a fix on
this history, the test files that fix changes, and the test functions that fail before it; a left-out
case says why no case is made of its fix. Every fix that confirms a defect in `evals/review/cases/` has
one or the other. That the listed tests fail before the fix and pass after it is shown by running them,
with `harness.py admit`, not here: this file runs no suite and touches no network. Git is asked through
its own plumbing, as `tests/test_review_cases.py` does.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "evals" / "build"
CASES = sorted((BUILD / "cases").glob("*.toml"))
REVIEW_CASES = sorted((ROOT / "evals" / "review" / "cases").glob("*.toml"))
SHA = re.compile(r"[0-9a-f]{40}")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
READY_KEYS = {
    "id", "status", "fix", "review_case", "defects", "paths", "tests", "fail_to_pass", "statement",
    "admitted",
}  # fmt: skip
LEFT_OUT_KEYS = {"id", "status", "fix", "review_case", "defects", "reason"}

spec = importlib.util.spec_from_file_location("build_harness", BUILD / "harness.py")
assert spec is not None and spec.loader is not None
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=harness.clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def strings(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)


def review_fixes() -> dict[str, dict[str, set[str]]]:
    """Each fix a review case's confirmed defects name, with those defects by review case."""
    fixes: dict[str, dict[str, set[str]]] = {}
    for path in REVIEW_CASES:
        case = tomllib.loads(path.read_text(encoding="utf-8"))
        for defect in case.get("defects", []):
            fixes.setdefault(defect["fix"], {}).setdefault(case["id"], set()).add(defect["id"])
    return fixes


def problems(path: Path) -> list[str]:
    try:
        case = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [f"not valid TOML: {error}"]
    found = []
    if case.get("id") != path.stem:
        found.append("id is not the file name")
    status = case.get("status")
    if status not in ("ready", "left-out"):
        return [*found, "status is neither 'ready' nor 'left-out'"]
    keys = READY_KEYS if status == "ready" else LEFT_OUT_KEYS
    if set(case) != keys:
        return [*found, f"keys are {sorted(case)}, not {sorted(keys)}"]
    fix = case["fix"]
    if not isinstance(fix, str) or not SHA.fullmatch(fix):
        return [*found, "fix is not a 40-character lower-case sha"]
    if git("cat-file", "-e", f"{fix}^{{commit}}").returncode != 0:
        return [*found, f"fix {fix} is not a commit in this repository"]
    if git("merge-base", "--is-ancestor", fix, "HEAD").returncode != 0:
        found.append(f"fix {fix} is not an ancestor of HEAD")

    confirmed = review_fixes().get(fix, {})
    if len(confirmed) > 1:
        found.append(
            f"the fix confirms defects in {sorted(confirmed)}; a case names one review case"
        )
    if not isinstance(case["review_case"], str) or not strings(case["defects"]):
        found.append("review_case is not a string, or defects is not a list of names")
    elif set(case["defects"]) != confirmed.get(case["review_case"], set()):
        found.append(
            f"defects are not the ones evals/review/cases/{case['review_case']}.toml confirms by this fix"
        )

    if status == "left-out":
        if not isinstance(case["reason"], str) or not case["reason"].strip():
            found.append("a left-out case says in reason why no case is made of its fix")
        return found

    changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", f"{fix}^", fix).stdout.split(
        "\n"
    )
    supplied = sorted(p for p in changed if harness.is_test_path(p) and p not in harness.WITHHELD)
    if case["tests"] != supplied:
        found.append(f"tests are not the test files the fix changes: {supplied}")
    if not strings(case["paths"]):
        found.append("paths is not a non-empty list of strings")
    if not isinstance(case["admitted"], str) or not DAY.fullmatch(case["admitted"]):
        found.append("admitted is not the day (YYYY-MM-DD) `harness.py admit` last showed it sound")
    statement = case["statement"]
    if not isinstance(statement, str) or not statement.strip():
        found.append("statement is empty")
    elif fix[:7] in statement:
        found.append("statement names the fix")
    if not strings(case["fail_to_pass"]):
        return [*found, "fail_to_pass is not a non-empty list of '<file>::<function>'"]
    for test in case["fail_to_pass"]:
        file, _, name = test.partition("::")
        if file not in supplied:
            found.append(f"{test}: {file} is not one of the case's tests")
            continue
        shown = git("show", f"{fix}:{file}")
        if not re.search(rf"^\s*(async\s+)?def {re.escape(name)}\(", shown.stdout, re.MULTILINE):
            found.append(f"{test}: {file} at the fix defines no {name}")
    return found


def test_there_are_cases() -> None:
    assert any(tomllib.loads(p.read_text(encoding="utf-8")).get("status") == "ready" for p in CASES)


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_is_well_formed(path: Path) -> None:
    assert problems(path) == []


def test_every_fix_of_a_confirmed_defect_has_one_case() -> None:
    cased = [tomllib.loads(p.read_text(encoding="utf-8"))["fix"] for p in CASES]
    assert sorted(cased) == sorted(review_fixes())


def test_the_check_refuses_a_case_whose_tests_are_not_the_fix_s(tmp_path: Path) -> None:
    good = next(
        p for p in CASES if tomllib.loads(p.read_text(encoding="utf-8"))["status"] == "ready"
    )
    text = good.read_text(encoding="utf-8")
    listed = tomllib.loads(text)["tests"][0]
    bad = tmp_path / good.name
    bad.write_text(text.replace(f'"{listed}"', '"tests/test_nowhere.py"', 1), encoding="utf-8")
    assert any("tests are not the test files the fix changes" in p for p in problems(bad))


# The harness, on a repository of two commits made here: a parent, and a fix of `src/mod.py` that also
# changes one test file and deletes another.


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "evals").mkdir()
    (root / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text("def test_old(): pass\n", encoding="utf-8")
    (root / "tests" / "test_kept.py").write_text("def test_kept(): pass\n", encoding="utf-8")
    (root / "tests" / "test_gone.py").write_text("def test_gone(): pass\n", encoding="utf-8")
    (root / "evals" / "answers.md").write_text("the fix\n", encoding="utf-8")
    (root / "pytest.toml").write_text("[pytest]\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    identity = [
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    assert git("init", "--quiet", cwd=root).returncode == 0
    git("add", "--all", cwd=root)
    assert (
        git(*identity, "commit", "--quiet", "--no-verify", "-m", "parent", cwd=root).returncode == 0
    )
    (root / "src" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text("def test_new(): pass\n", encoding="utf-8")
    (root / "tests" / "test_gone.py").unlink()
    git("add", "--all", cwd=root)
    assert git(*identity, "commit", "--quiet", "--no-verify", "-m", "fix", cwd=root).returncode == 0
    monkeypatch.setattr(harness, "ROOT", root)
    return {
        "id": "scratch",
        "fix": git("rev-parse", "HEAD", cwd=root).stdout.strip(),
        "tests": ["tests/test_gone.py", "tests/test_mod.py"],
        "fail_to_pass": ["tests/test_mod.py::test_new"],
    }


def test_the_starting_tree_is_the_parent_with_the_fix_s_tests_and_no_history(
    repo: dict, tmp_path: Path
) -> None:
    tree = harness.make_tree(repo, tmp_path / "start")
    assert (tree / "src" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (tree / "tests" / "test_mod.py").read_text(encoding="utf-8") == "def test_new(): pass\n"
    assert not (tree / "tests" / "test_gone.py").exists()
    assert (tree / "tests" / "test_kept.py").exists()
    assert not (tree / "evals").exists()
    assert git("rev-list", "--count", "--all", cwd=tree).stdout.strip() == "1"
    assert git("status", "--porcelain", cwd=tree).stdout == ""
    assert git("remote", cwd=tree).stdout == ""


def test_the_reference_tree_is_the_fix(repo: dict, tmp_path: Path) -> None:
    tree = harness.make_tree(repo, tmp_path / "reference", reference=True)
    assert (tree / "src" / "mod.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert not (tree / "evals").exists()


def test_a_tree_is_not_written_over_files(repo: dict, tmp_path: Path) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "file").write_text("x", encoding="utf-8")
    with pytest.raises(harness.HarnessError, match="not empty"):
        harness.make_tree(repo, tmp_path / "taken")


PASSING = {"tests/test_mod.py::test_new": "passed", "tests/test_kept.py::test_kept": "passed"}
CHECKS_OK = dict.fromkeys(harness.CHECKS, True)


def test_grading_takes_the_worker_s_files_and_puts_the_starting_tests_back(
    repo: dict, tmp_path: Path
) -> None:
    worker = harness.make_tree(repo, tmp_path / "worker")
    (worker / "src" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    (worker / "src" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    (worker / "tests" / "test_mod.py").write_text("def test_new(): assert True\n", encoding="utf-8")
    (worker / "tests" / "test_added.py").write_text("def test_added(): pass\n", encoding="utf-8")
    (worker / "tests" / "conftest.py").write_text("# hooks\n", encoding="utf-8")
    (worker / "pytest.toml").write_text("[pytest]\naddopts = ['-k', 'nothing']\n", encoding="utf-8")
    (worker / ".venv").mkdir()
    (worker / ".venv" / "big").write_text("ignored\n", encoding="utf-8")
    shutil.rmtree(worker / ".git")  # whatever the worker did to its repository is not read
    seen = {}

    def suite(tree: Path) -> dict[str, str]:
        seen["mod"] = (tree / "src" / "mod.py").read_text(encoding="utf-8")
        seen["test"] = (tree / "tests" / "test_mod.py").read_text(encoding="utf-8")
        seen["config"] = (tree / "pytest.toml").read_text(encoding="utf-8")
        seen["added"] = (tree / "tests" / "test_added.py").exists()
        return PASSING

    result = harness.grade(repo, worker, tmp_path / "out", suite=suite, checks=lambda _: CHECKS_OK)
    assert seen == {
        "mod": "VALUE = 2\n",
        "test": "def test_new(): pass\n",
        "config": "[pytest]\n",
        "added": True,
    }
    assert result["resolved"] is True
    assert result["restored"] == ["pytest.toml", "tests/test_mod.py"]
    assert result["changed"] == [
        "pytest.toml", "src/mod.py", "src/new.py", "tests/conftest.py", "tests/test_added.py",
        "tests/test_mod.py",
    ]  # fmt: skip
    assert result["flags"] == [
        "pytest.toml: it can change what a test run or a code check means",
        "tests/conftest.py: it can change what a test run or a code check means",
    ]
    patch = (tmp_path / "out" / "change.patch").read_text(encoding="utf-8")
    assert "+VALUE = 2" in patch and ".venv" not in patch
    assert (tmp_path / "out" / "grade.json").is_file()


def test_grading_a_tree_nobody_changed_resolves_nothing(repo: dict, tmp_path: Path) -> None:
    worker = harness.make_tree(repo, tmp_path / "worker")

    def suite(_: Path) -> dict[str, str]:
        raise AssertionError("no suite runs when nothing changed")

    result = harness.grade(repo, worker, tmp_path / "out", suite=suite, checks=lambda _: CHECKS_OK)
    assert result["resolved"] is False
    assert result["why"] == "the worker changed no file"


def test_a_change_to_a_file_the_harness_reads_is_flagged(repo: dict, tmp_path: Path) -> None:
    worker = harness.make_tree(repo, tmp_path / "worker")
    (worker / "pkg").mkdir()
    (worker / "pkg" / "ruff.toml").write_text("exclude = ['src']\n", encoding="utf-8")
    (worker / ".gitignore").write_text(".venv/\n/ledger/\n", encoding="utf-8")
    (worker / "src" / ".hidden.py").write_text("X = 1\n", encoding="utf-8")
    (worker / ".omp").mkdir()
    (worker / ".omp" / "config.yml").write_text("approval: yolo\n", encoding="utf-8")
    result = harness.grade(
        repo | {"paths": ["src/", ".gitignore"]},
        worker,
        tmp_path / "out",
        suite=lambda _: PASSING,
        checks=lambda _: CHECKS_OK,
    )
    assert result["changed"] == [".gitignore", ".omp/config.yml", "pkg/ruff.toml", "src/.hidden.py"]
    assert result["flags"] == [
        ".omp/config.yml: a file the harness or a runtime reads",
        "pkg/ruff.toml: it can change what a test run or a code check means",
    ]


def test_a_changed_tree_that_cannot_be_run_is_a_verdict_and_can_be_graded_again(
    repo: dict, tmp_path: Path
) -> None:
    worker = harness.make_tree(repo, tmp_path / "worker")
    (worker / "src" / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    calls = []

    def suite(_: Path) -> dict[str, str]:
        calls.append(1)
        if len(calls) == 2:
            raise harness.HarnessError("pytest wrote no report")
        return PASSING

    result = harness.grade(repo, worker, tmp_path / "out", suite=suite, checks=lambda _: CHECKS_OK)
    assert result["resolved"] is False
    assert result["why"] == "the changed tree could not be run: pytest wrote no report"
    assert (tmp_path / "out" / "grade.json").is_file()

    def broken(_: Path) -> dict[str, str]:
        raise harness.HarnessError("uv is not on PATH")

    with pytest.raises(harness.HarnessError, match="uv is not on PATH"):
        harness.grade(repo, worker, tmp_path / "again", suite=broken, checks=lambda _: CHECKS_OK)
    assert not (tmp_path / "again").exists()


BEFORE = PASSING | {"tests/test_mod.py::test_new": "failed"}


@pytest.mark.parametrize(
    ("after", "checks", "resolved"),
    [
        (PASSING, CHECKS_OK, True),
        ({"tests/test_kept.py::test_kept": "passed"}, CHECKS_OK, False),  # the named test never ran
        (PASSING | {"tests/test_mod.py::test_new": "skipped"}, CHECKS_OK, False),
        (PASSING | {"tests/test_mod.py::test_new[a]": "failed"}, CHECKS_OK, False),
        (PASSING | {"tests/test_kept.py::test_kept": "failed"}, CHECKS_OK, False),
        (PASSING | {"tests/test_kept.py::test_kept": "skipped"}, CHECKS_OK, False),  # passed before
        ({"tests/test_mod.py::test_new": "passed"}, CHECKS_OK, False),  # one that passed is gone
        (PASSING, CHECKS_OK | {"ruff lint": False}, False),
    ],
)
def test_resolved_means_the_named_tests_pass_nothing_fails_or_is_lost_and_the_checks_pass(
    after: dict[str, str], checks: dict[str, bool], resolved: bool
) -> None:
    verdict = harness.judge(["tests/test_mod.py::test_new"], BEFORE, after, checks)
    assert verdict["resolved"] is resolved


def test_a_test_that_passed_before_and_is_skipped_or_gone_after_is_lost() -> None:
    after = {"tests/test_mod.py::test_new": "passed", "tests/test_kept.py::test_kept": "skipped"}
    assert harness.judge([], BEFORE, after, CHECKS_OK)["lost"] == ["tests/test_kept.py::test_kept"]
    assert harness.judge([], BEFORE, {}, CHECKS_OK)["lost"] == ["tests/test_kept.py::test_kept"]
    failed = {"tests/test_kept.py::test_kept": "failed"}
    assert harness.judge([], BEFORE, failed, CHECKS_OK)["lost"] == []


def test_a_suite_that_hangs_is_stopped_with_what_it_started_and_counts_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    pid_file = tmp_path / "child.pid"
    (fake / "uv").write_text(
        f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\nwait\n", encoding="utf-8"
    )
    (fake / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setattr(harness, "SUITE_SECONDS", 1)
    tree = tmp_path / "tree"
    tree.mkdir()
    assert harness.run_suite(tree) == {"(the suite)": "failed"}
    child = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):  # the kill is sent to the whole group; give the kernel a moment to reap
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the suite's child outlived the timeout")


def test_a_report_is_read_by_file_class_and_name() -> None:
    report = """<testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>
      <testcase classname="tests.test_a.TestThing" name="test_in_class[x-1]" file="tests/test_a.py">
        <failure message="boom"/></testcase>
      <testcase classname="tests.test_a" name="test_setup" file="tests/test_a.py"><error/></testcase>
      <testcase classname="tests.test_a" name="test_later" file="tests/test_a.py"><skipped/></testcase>
      <testcase classname="" name="tests.test_broken"><error message="collection failure"/></testcase>
    </testsuite></testsuites>"""
    outcomes = harness.read_report(report)
    assert outcomes == {
        "tests/test_a.py::test_ok": "passed",
        "tests/test_a.py::TestThing::test_in_class[x-1]": "failed",
        "tests/test_a.py::test_setup": "failed",
        "tests/test_a.py::test_later": "skipped",
        "tests.test_broken": "failed",
    }
    assert harness.failing(outcomes) == {
        "tests/test_a.py::TestThing::test_in_class",
        "tests/test_a.py::test_setup",
        "tests.test_broken",
    }


def test_admitting_names_a_start_that_fails_too_much_and_a_reference_that_fails(
    repo: dict, tmp_path: Path
) -> None:
    def suite(tree: Path) -> dict[str, str]:
        if (tree / "src" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n":
            return {
                "tests/test_mod.py::test_new": "failed",
                "tests/test_kept.py::test_kept": "failed",
            }
        return PASSING | {"tests/test_kept.py::test_kept": "failed"}

    found = harness.admit(repo, 1, tmp_path / "work", suite=suite, checks=lambda _: CHECKS_OK)
    assert found == [
        (
            "run 1: the starting tree fails ['tests/test_kept.py::test_kept'] beyond fail_to_pass "
            "and passes [] of it"
        ),
        "run 1: the reference fails ['tests/test_kept.py::test_kept']",
    ]


def test_admitting_names_a_reference_that_skips_what_it_should_pass(
    repo: dict, tmp_path: Path
) -> None:
    def suite(tree: Path) -> dict[str, str]:
        if (tree / "src" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n":
            return BEFORE
        return PASSING | {"tests/test_mod.py::test_new": "skipped"}

    found = harness.admit(repo, 1, tmp_path / "work", suite=suite, checks=lambda _: CHECKS_OK)
    assert found == ["run 1: the reference does not pass ['tests/test_mod.py::test_new']"]
    with pytest.raises(harness.HarnessError, match="at least one run"):
        harness.admit(repo, 0, tmp_path / "none", suite=suite, checks=lambda _: CHECKS_OK)


def test_admitting_a_sound_case_finds_nothing(repo: dict, tmp_path: Path) -> None:
    def suite(tree: Path) -> dict[str, str]:
        if (tree / "src" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n":
            return PASSING | {"tests/test_mod.py::test_new": "failed"}
        return PASSING

    assert harness.admit(repo, 2, tmp_path / "work", suite=suite, checks=lambda _: CHECKS_OK) == []
