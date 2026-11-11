"""Make a build case's tree, show by running that the case is sound, and grade a worker's change.

The contract is SWE-bench's, as its paper states it (https://arxiv.org/abs/2310.06770): a task is a
repository at the commit before a reference change, with tests that fail there and pass once the change
is made (FAIL_TO_PASS) and tests that pass before and after (PASS_TO_PASS); a change resolves the task
when both sets pass. Here the reference is a fix on `main`, the tests are the ones that fix added or
changed, and they are in the tree the worker gets (ADR 0057 says why). The procedure and the case format
are in `evals/build/README.md`.

Three things are done the way the maintained benchmarks do them. The tree is one commit with no history
(SWE-bench Pro's audit rebuilt each repository that way, after agents read the fix from `git log`). A
case is admitted only when its tests fail before and pass after on repeated runs (SWE-bench-Live keeps
"only instances with consistent results across all runs"). And the grader trusts nothing the worker
ran: it makes the tree again, takes only the worker's files, puts every test file of the starting tree
back, and runs the suite itself, before the change and after it: a test that passed before must pass
after, not merely not fail.

Git is used through documented commands (`git help archive`, `init`, `add`, `diff`, `apply`,
`checkout`), with a temporary index (`GIT_INDEX_FILE`, `git help git`). pytest's results are read from
its JUnit XML report (`--junitxml` with `junit_family=xunit1`, whose test cases carry `file`). The code
checks are the three `scripts/ci` runs on Python.

    uv run --locked python evals/build/harness.py tree  <case> <new dir>
    uv run --locked python evals/build/harness.py admit <case> [--runs 3]
    uv run --locked python evals/build/harness.py grade <case> <worker's dir> <new result dir>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from pathlib import PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals" / "build" / "cases"

# Left out of every tree: the benchmarks' own files, which describe the defects and name the fixes, and
# the two tests that read this repository's history, which a one-commit tree does not have.
WITHHELD = ("evals", "tests/test_review_cases.py", "tests/test_build_cases.py")
# Besides every test file of the starting tree, what the grader puts back before it runs anything.
PROTECTED = ("pytest.toml",)
# A change to one of these, at any depth, is listed for the reader, unless the case's own `paths` name
# the file: each can change what a test run or a code check means, or what a later session of some
# runtime does. So is any change in one of the directories, or in a dot-directory.
FLAGGED = (
    "pyproject.toml", "uv.lock", "mise.toml", "mise.lock", "AGENTS.md", "CLAUDE.md", "conftest.py",
    "pytest.ini", "pytest.toml", "tox.ini", "setup.cfg", "ruff.toml", ".ruff.toml", "ty.toml",
    ".gitignore", "orca.yaml",
)  # fmt: skip
FLAGGED_DIRS = ("scripts", "canon", "home")
CHECKS = {
    "ruff lint": ["ruff", "check", "."],
    "ruff format": ["ruff", "format", "--check", "."],
    "ty type check": ["ty", "check"],
}
SUITE_SECONDS = 1800
IGNORED = ".venv/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n"

Outcomes = dict[str, str]
Suite = Callable[[Path], Outcomes]
Checks = Callable[[Path], dict[str, bool]]


class HarnessError(Exception):
    """The harness could not do its work; never a verdict on a worker."""


def clean_env() -> dict[str, str]:
    """The environment without git's and uv's per-process variables.

    A commit hook sets GIT_DIR and GIT_INDEX_FILE, which would point every git call below at this
    repository; `uv run` sets VIRTUAL_ENV, which is this checkout's and not the tree's.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "VIRTUAL_ENV"}


def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> bytes:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env or clean_env(), capture_output=True, check=False
    )
    if done.returncode != 0:
        raise HarnessError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def load(case_id: str) -> dict:
    path = CASES / f"{case_id}.toml"
    if not path.is_file():
        raise HarnessError(f"no case {case_id} in {CASES}")
    case = tomllib.loads(path.read_text(encoding="utf-8"))
    if case.get("status") != "ready":
        raise HarnessError(f"case {case_id} is not ready: {case.get('reason', 'no reason given')}")
    return case


def is_test_path(path: str) -> bool:
    return "tests" in PurePosixPath(path).parts[:-1]


def extract(commit: str, paths: list[str], dest: Path) -> None:
    """Write `paths` (everything, when empty) as they are at `commit` into `dest`."""
    archive = git("archive", "--format=tar", commit, "--", *paths, cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def exists_at(commit: str, path: str) -> bool:
    done = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        env=clean_env(),
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def make_tree(case: dict, dest: Path, *, reference: bool = False) -> Path:
    """The tree a worker starts in, as a repository of one commit; with `reference`, the fix itself.

    The starting tree is the fix's parent with the case's test files as the fix has them: a test file
    the fix deletes is deleted. Nothing of this repository's history goes with it.
    """
    if dest.exists() and any(dest.iterdir()):
        raise HarnessError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    fix = case["fix"]
    extract(fix if reference else f"{fix}^", [], dest)
    if not reference:
        kept = [path for path in case["tests"] if exists_at(fix, path)]
        for path in set(case["tests"]) - set(kept):
            (dest / path).unlink(missing_ok=True)
        if kept:
            extract(fix, kept, dest)
    for path in WITHHELD:
        target = dest / path
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    when = git("log", "-1", "--format=%cI", f"{fix}^", cwd=ROOT).decode().strip()
    env = clean_env() | {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    git("init", "--quiet", "--initial-branch=main", cwd=dest)
    git("add", "--all", cwd=dest)
    git(
        "-c", "user.name=build eval", "-c", "user.email=build-eval@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
        "commit", "--quiet", "--message", "The tree a build case starts from",
        cwd=dest, env=env,
    )  # fmt: skip
    return dest


def node_id(case: ET.Element) -> str:
    """`<file>::<name>`, with a test class between them when there is one."""
    name = case.get("name", "")
    file = case.get("file")
    if not file:  # a collection error has no file: pytest names the module instead
        return f"{case.get('classname', '')}::{name}".strip(":") or "(unnamed)"
    module = file.removesuffix(".py").replace("/", ".")
    inner = case.get("classname", "").removeprefix(module).strip(".")
    return "::".join([file, *([inner.replace(".", "::")] if inner else []), name])


def read_report(xml: str) -> Outcomes:
    """Each test's outcome from a JUnit report: passed, failed (a failure or an error) or skipped."""
    outcomes: Outcomes = {}
    for case in ET.fromstring(xml).iter("testcase"):
        kinds = {child.tag for child in case}
        outcome = (
            "failed"
            if kinds & {"failure", "error"}
            else "skipped"
            if "skipped" in kinds
            else "passed"
        )
        key = node_id(case)
        if outcomes.get(key) != "failed":  # a test and its teardown error share one id
            outcomes[key] = outcome
    return outcomes


def run_suite(tree: Path) -> Outcomes:
    """Run the offline suite in `tree` from its own lockfile and return every test's outcome."""
    report = tree / ".build-eval-report.xml"
    command = [
        "uv", "run", "--locked", "pytest", "-q", "-p", "no:cacheprovider",
        "-o", "junit_family=xunit1", f"--junitxml={report}",
    ]  # fmt: skip
    try:
        # A session of its own, so that a timeout can end pytest and what it started, not only uv.
        process = subprocess.Popen(
            command, cwd=tree, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )  # fmt: skip
    except FileNotFoundError as error:
        raise HarnessError("uv is not on PATH: run this through `mise exec --`") from error
    try:
        output, _ = process.communicate(timeout=SUITE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        report.unlink(missing_ok=True)
        return {"(the suite)": "failed"}  # a hang is a failed run, as a failing test is
    if not report.is_file():
        raise HarnessError(f"pytest wrote no report in {tree}:\n{output[-2000:]}")
    outcomes = read_report(report.read_text(encoding="utf-8"))
    report.unlink()
    if not outcomes:
        raise HarnessError(f"pytest ran no test in {tree}:\n{output[-2000:]}")
    return outcomes


def run_checks(tree: Path) -> dict[str, bool]:
    """The three checks `scripts/ci` runs on Python, in `tree`."""
    results = {}
    for label, command in CHECKS.items():
        done = subprocess.run(
            ["uv", "run", "--locked", *command], cwd=tree, env=clean_env(), capture_output=True,
            check=False,
        )  # fmt: skip
        results[label] = done.returncode == 0
    return results


def function(test: str) -> str:
    """A test's id without its parameters: every parametrisation of a function counts under it."""
    return re.sub(r"\[.*\]$", "", test)


def failing(outcomes: Outcomes) -> set[str]:
    return {function(test) for test, outcome in outcomes.items() if outcome == "failed"}


def passing(outcomes: Outcomes) -> set[str]:
    return {test for test, outcome in outcomes.items() if outcome == "passed"}


def judge(
    fail_to_pass: list[str], before: Outcomes, after: Outcomes, checks: dict[str, bool]
) -> dict:
    """The mechanical verdict on one tree: what SWE-bench calls resolved, plus the code checks.

    `before` is the starting tree's run and `after` the changed tree's. A test that passed before and
    is skipped, deselected or gone after is `lost`, and counts as a failure does.
    """
    by_function: dict[str, set[str]] = {}
    for test, outcome in after.items():
        by_function.setdefault(function(test), set()).add(outcome)
    named = {test: sorted(by_function.get(test, {"not run"})) for test in fail_to_pass}
    failed = sorted(failing(after))
    lost = sorted(passing(before) - passing(after) - {t for t in after if after[t] == "failed"})
    return {
        "resolved": all(seen == ["passed"] for seen in named.values())
        and not failed
        and not lost
        and all(checks.values()),
        "fail_to_pass": named,
        "failed": failed,
        "lost": lost,
        "checks": checks,
        "tests": {
            kind: list(after.values()).count(kind) for kind in ("passed", "failed", "skipped")
        },
    }


def admit(
    case: dict, runs: int, work: Path, suite: Suite = run_suite, checks: Checks = run_checks
) -> list[str]:
    """Why the case is not sound, by running it; an empty list when it is.

    Sound means: on every run the starting tree fails exactly the functions the case lists and nothing
    else, and the reference resolves the case as a worker's tree would have to: the listed functions
    pass, nothing fails, nothing that passed before is lost, and the code checks pass.
    """
    if runs < 1:
        raise HarnessError("a case is admitted on at least one run")
    start = make_tree(case, work / "start")
    reference = make_tree(case, work / "reference", reference=True)
    wanted = set(case["fail_to_pass"])
    checked = checks(reference)
    problems = [f"the reference fails {label}" for label, ok in checked.items() if not ok]
    for number in range(1, runs + 1):
        before = suite(start)
        if failing(before) != wanted:
            problems.append(
                f"run {number}: the starting tree fails {sorted(failing(before) - wanted)} beyond "
                f"fail_to_pass and passes {sorted(wanted - failing(before))} of it"
            )
        verdict = judge(case["fail_to_pass"], before, suite(reference), checked)
        if verdict["failed"]:
            problems.append(f"run {number}: the reference fails {verdict['failed']}")
        if verdict["lost"]:
            problems.append(f"run {number}: the reference loses {verdict['lost']}")
        unmet = sorted(t for t, seen in verdict["fail_to_pass"].items() if seen != ["passed"])
        if unmet and not verdict["failed"]:
            problems.append(f"run {number}: the reference does not pass {unmet}")
    return problems


def change_of(start: Path, worker: Path, scratch: Path) -> tuple[bytes, list[tuple[str, str]]]:
    """The worker's files against the starting tree: a patch, and each changed path with its status.

    The worker's directory is read as a work tree of the grader's own copy of the start, through a
    temporary index, so nothing in the worker's `.git` is read and nothing of the worker's is written.
    What a run leaves behind is ignored through that copy's `info/exclude`, beside the machine's own
    ignore file, which still applies.
    """
    exclude = start / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(IGNORED, encoding="utf-8")
    env = clean_env() | {"GIT_INDEX_FILE": str(scratch / "index")}
    base = ["--git-dir", str(start / ".git"), "--work-tree", str(worker)]
    git(*base, "add", "--all", cwd=worker, env=env)
    patch = git(*base, "diff", "--cached", "--binary", "HEAD", cwd=worker, env=env)
    listing = git(
        *base,
        "diff",
        "--cached",
        "--name-status",
        "--no-renames",
        "-z",
        "HEAD",
        cwd=worker,
        env=env,
    )
    fields = listing.decode().split("\0")
    return patch, list(zip(fields[0::2], fields[1::2], strict=False))


def flags(changed: list[tuple[str, str]], expected: list[str]) -> list[str]:
    """Changed paths the reader must look at before anything else; `expected` are the case's `paths`."""
    found = []
    for _, path in changed:
        parts = PurePosixPath(path).parts
        if path in expected:
            continue
        if parts[-1] in FLAGGED:
            found.append(f"{path}: it can change what a test run or a code check means")
        elif parts[0] in FLAGGED_DIRS or (len(parts) > 1 and parts[0].startswith(".")):
            found.append(f"{path}: a file the harness or a runtime reads")
    return found


def grade(
    case: dict, worker: Path, out: Path, suite: Suite = run_suite, checks: Checks = run_checks
) -> dict:
    """Grade the files in `worker` and write `change.patch` and `grade.json` into `out`.

    `out` is written only once there is a verdict, so a run the harness could not grade can be graded
    again into the same folder.
    """
    if not worker.is_dir():
        raise HarnessError(f"{worker} is not a directory")
    if out.exists() and any(out.iterdir()):
        raise HarnessError(f"{out} exists and is not empty")
    with tempfile.TemporaryDirectory() as scratch:
        tree = make_tree(case, Path(scratch) / "tree")
        patch, changed = change_of(tree, worker, Path(scratch))
        patch_file = Path(scratch) / "change.patch"
        patch_file.write_bytes(patch)
        result: dict = {
            "case": case["id"],
            "changed": [path for _, path in changed],
            "flags": flags(changed, case.get("paths", [])),
            "restored": [],
        }
        if not changed:
            result |= {"resolved": False, "why": "the worker changed no file"}
        else:
            before = suite(
                tree
            )  # a failure here is the harness's or the case's, never the worker's
            held = [
                path
                for status, path in changed
                if status != "A" and (is_test_path(path) or path in PROTECTED)
            ]
            result["restored"] = held
            try:
                git("apply", "--index", "--binary", str(patch_file), cwd=tree)
                if held:  # a worker's edit to a test of the starting tree is not graded
                    git("checkout", "HEAD", "--", *held, cwd=tree)
                result |= judge(case["fail_to_pass"], before, suite(tree), checks(tree))
            except HarnessError as error:
                # The same tree ran a moment ago, so what stops it now is the worker's change.
                result |= {"resolved": False, "why": f"the changed tree could not be run: {error}"}
    out.mkdir(parents=True, exist_ok=True)
    (out / "change.patch").write_bytes(patch)
    (out / "grade.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    tree = commands.add_parser("tree", help="write the tree a worker starts in")
    tree.add_argument("case")
    tree.add_argument("dest", type=Path)
    admitting = commands.add_parser("admit", help="show by running that a case is sound")
    admitting.add_argument("case")
    admitting.add_argument("--runs", type=int, default=3)
    grading = commands.add_parser("grade", help="grade the files in a worker's directory")
    grading.add_argument("case")
    grading.add_argument("worker", type=Path)
    grading.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        case = load(args.case)
        if args.command == "tree":
            print(make_tree(case, args.dest.resolve()))
            return 0
        if args.command == "admit":
            with tempfile.TemporaryDirectory() as work:
                problems = admit(case, args.runs, Path(work))
            for problem in problems:
                print(f"{args.case}: {problem}")
            if not problems:
                count = len(case["fail_to_pass"])
                print(f"{args.case}: sound on {args.runs} runs; fail_to_pass {count}")
            return 1 if problems else 0
        result = grade(case, args.worker.resolve(), args.out.resolve())
        print(json.dumps(result, indent=2))
        return 0
    except HarnessError as error:
        print(f"harness: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
