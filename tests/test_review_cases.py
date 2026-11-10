"""Every reviewer benchmark case is well formed and points at commits and tests that exist.

The contract is the case format of ADR 0046, written out in `evals/review/README.md`: one TOML file
per case in `evals/review/cases/`, read with the standard library's `tomllib`. A ready case names the
commit to review and, for each confirmed defect, a later fix on this history and a test the fix commit
changes and defines; a clean case names none; a pending case says what would confirm its defects. Git is asked
through its own plumbing: `git cat-file -e <sha>^{commit}` for existence and
`git merge-base --is-ancestor` for ancestry (both in `git help`). No test here touches the network.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CASES = sorted((ROOT / "evals" / "review" / "cases").glob("*.toml"))
SHA = re.compile(r"[0-9a-f]{40}")
CASE_KEYS = {"id", "status", "commit", "title", "paths", "clean", "confirm", "defects"}
DEFECT_KEYS = {"id", "location", "summary", "match", "reported_by", "fix", "test"}


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def is_commit(sha: str) -> bool:
    return git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def is_ancestor(older: str, newer: str) -> bool:
    return git("merge-base", "--is-ancestor", older, newer).returncode == 0


def check_fix(fix: object, commit: str, where: str) -> list[str]:
    if not isinstance(fix, str) or not SHA.fullmatch(fix):
        return [f"{where}: fix is not a 40-character lower-case sha"]
    if not is_commit(fix):
        return [f"{where}: fix {fix} is not a commit in this repository"]
    problems = []
    if fix == commit or not is_ancestor(commit, fix):
        problems.append(f"{where}: fix {fix} does not come after the case's commit")
    if not is_ancestor(fix, "HEAD"):
        problems.append(f"{where}: fix {fix} is not an ancestor of HEAD")
    return problems


def check_test(test: object, fix: str, where: str) -> list[str]:
    if not isinstance(test, str) or test.count("::") != 1:
        return [f"{where}: test is not '<file>::<test name>'"]
    path, name = test.split("::")
    changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", f"{fix}^", fix).stdout
    if path not in changed.splitlines():
        return [f"{where}: the fix commit {fix[:12]} does not change {path}"]
    shown = git("show", f"{fix}:{path}")
    if shown.returncode != 0:
        return [f"{where}: {path} is not in the fix commit {fix[:12]}"]
    if not re.search(rf"^\s*(async\s+)?def {re.escape(name)}\(", shown.stdout, re.MULTILINE):
        return [f"{where}: {path} at {fix[:12]} defines no {name}"]
    return []


def problems(path: Path) -> list[str]:
    try:
        case = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        return [f"not valid TOML: {error}"]
    found = [f"unknown key {key!r}" for key in sorted(set(case) - CASE_KEYS)]
    if case.get("id") != path.stem:
        found.append("id is not the file name")
    for key in ("title", "id"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            found.append(f"{key} is missing or empty")
    paths = case.get("paths")
    if not (isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths)):
        found.append("paths is not a non-empty list of strings")
    commit = case.get("commit")
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        return [*found, "commit is not a 40-character lower-case sha"]
    if not is_commit(commit):
        return [*found, f"commit {commit} is not a commit in this repository"]
    if not is_ancestor(commit, "HEAD"):
        found.append(f"commit {commit} is not an ancestor of HEAD")

    status = case.get("status")
    defects = case.get("defects", [])
    clean = case.get("clean", False)
    if not isinstance(defects, list) or not isinstance(clean, bool):
        return [*found, "defects is not a list of tables, or clean is not a boolean"]
    if status == "pending":
        if not isinstance(case.get("confirm"), str) or not case["confirm"].strip():
            found.append("a pending case says in confirm what would confirm its defects")
        if defects or clean:
            found.append("a pending case lists no defects and is not clean")
    elif status == "ready":
        if "confirm" in case:
            found.append("confirm belongs to a pending case")
        if clean == bool(defects):
            found.append("a ready case either is clean or lists defects, not both or neither")
    else:
        found.append("status is neither 'ready' nor 'pending'")

    ids: set[str] = set()
    for number, defect in enumerate(defects, 1):
        where = f"defect {number}"
        if not isinstance(defect, dict):
            found.append(f"{where} is not a table")
            continue
        if set(defect) != DEFECT_KEYS:
            found.append(f"{where}: keys are {sorted(defect)}, not {sorted(DEFECT_KEYS)}")
            continue
        for key in ("id", "location", "summary", "match", "reported_by"):
            if not isinstance(defect[key], str) or not defect[key].strip():
                found.append(f"{where}: {key} is empty")
        if isinstance(defect["id"], str):
            if defect["id"] in ids:
                found.append(f"{where}: id {defect['id']!r} repeats")
            ids.add(defect["id"])
        fix_problems = check_fix(defect["fix"], commit, where)
        found += fix_problems
        if not fix_problems:
            found += check_test(defect["test"], defect["fix"], where)
    return found


def test_there_are_cases() -> None:
    assert CASES, "evals/review/cases/ holds no case"


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_case_is_well_formed(path: Path) -> None:
    assert problems(path) == []


def test_the_check_refuses_a_case_that_names_no_real_commit(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'id = "bad"\nstatus = "ready"\ncommit = "' + "0" * 40 + '"\ntitle = "t"\n'
        'paths = ["src/"]\nclean = true\n',
        encoding="utf-8",
    )
    assert problems(bad) == [f"commit {'0' * 40} is not a commit in this repository"]


def write_case(path: Path, commit: str, fix: str) -> Path:
    path.write_text(
        f'id = "{path.stem}"\nstatus = "ready"\ncommit = "{commit}"\ntitle = "t"\n'
        'paths = ["src/"]\n[[defects]]\nid = "d"\nlocation = "l"\nsummary = "s"\nmatch = "m"\nreported_by = "r"\n'
        f'fix = "{fix}"\ntest = "tests/nowhere.py::test_x"\n',
        encoding="utf-8",
    )
    return path


def test_the_check_refuses_a_fix_before_the_commit_and_a_test_not_in_the_fix(
    tmp_path: Path,
) -> None:
    # The two flaky-test fixes on main, 05899f8 before 2669c86.
    first = "05899f8468e859ed5023dd531d413fc68135d04a"
    second = "2669c8645852c3d4e46dda75ff02bf6c16b30602"
    backwards = write_case(tmp_path / "backwards.toml", second, first)
    assert problems(backwards) == [f"defect 1: fix {first} does not come after the case's commit"]
    forwards = write_case(tmp_path / "forwards.toml", first, second)
    assert problems(forwards) == [
        f"defect 1: the fix commit {second[:12]} does not change tests/nowhere.py"
    ]
