"""The handbook's pages keep the form each page states (`docs/handbook/`).

The contract is the pages' own: the rows table's columns and its two states (`rows.md`), each practices
line naming a record that exists or saying "not yet built" with a roadmap step (`practices.md`), the
journal's ten fields and the nightly study's six (`journal.md`, `nights.md`), and both of those
append-only, so the entries already on `main` stay as they were. No upstream reads these pages, so the
parsing is the standard library's `re`; what is on `main` is asked of git through its own plumbing,
`git rev-parse --verify`, `git merge-base` and `git show <rev>:<path>` (all in `git help`). No test here
touches the network.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = ROOT / "docs" / "handbook"
MARKER = "<!-- append-only: add new entries below this line; never edit or remove one above it -->"
ADR = re.compile(r"\bADR (\d{4})\b")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

ROWS_HEADER = "| Row | Value | Status | Source or study | Date |"
ROW_NAME = re.compile(r"`[A-Z][A-Z0-9_]*`")
ROW_STATES = {"UNCALIBRATED", "calibrated"}

JOURNAL_HEADING = re.compile(r"### L(\d{4}): \S.*")
JOURNAL_FIELDS = (
    "area",
    "session",
    "result",
    "evidence",
    "lesson",
    "change",
    "next",
    "status",
    "supersedes",
    "source",
)
AREAS = {"ranking", "entries", "exits", "sizing", "execution", "data", "risk", "strategies"}
STATUSES = {"open", "confirmed", "refuted", "adopted", "superseded"}
SOURCES = {"reflection", "nightly", "review", "lead", "owner"}

NIGHT_HEADING = re.compile(r"### (\d{4}-\d{2}-\d{2})")
NIGHT_FIELDS = ("sessions", "data", "ran", "result", "proposals", "journal")
# An absolute or home path: evidence points inside the repository or names a record kept outside it.
PRIVATE_PATH = re.compile(r"(^|[\s(`'\"])(/|~/)[A-Za-z]")
JOURNAL_REFS = re.compile(r"none|L\d{4}(, L\d{4})*")


def record_exists(number: str) -> bool:
    return any((ROOT / "docs" / "adr").glob(f"{number}-*.md"))


def roadmap_steps() -> set[str]:
    text = (ROOT / "docs" / "roadmap.md").read_text()
    return set(re.findall(r"^\| (\d+) \|", text, re.MULTILINE))


def iso_date(value: str) -> bool:
    if not DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def entries(text: str) -> list[str]:
    """The entry blocks below the append-only marker, each from its `### ` heading, trailing space cut.
    Anything else between the marker and the first heading is refused."""
    _, sep, tail = text.partition(MARKER)
    if not sep:
        raise ValueError("no append-only marker")
    blocks = re.split(r"(?m)^(?=### )", tail)
    if blocks[0].strip():
        raise ValueError(f"text between the marker and the first entry: {blocks[0].strip()[:60]!r}")
    return [block.rstrip() for block in blocks[1:]]


def fields(block: str, names: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    """The `* name: value` lines of one entry, which must be exactly `names`, in order, each with a value."""
    lines = [line for line in block.splitlines()[1:] if line.strip()]
    errors: list[str] = []
    found: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"\* ([a-z]+): (\S.*)", line)
        if not match:
            errors.append(f"not a field line: {line!r}")
            continue
        found[match.group(1)] = match.group(2).strip()
    seen = tuple(re.findall(r"^\* ([a-z]+):", "\n".join(lines), re.MULTILINE))
    if seen != names:
        errors.append(f"fields {seen}, expected {names}")
    return found, errors


def check_journal(text: str) -> list[str]:
    errors: list[str] = []
    ids: list[int] = []
    for block in entries(text):
        heading = block.splitlines()[0]
        match = JOURNAL_HEADING.fullmatch(heading)
        if not match:
            errors.append(f"heading {heading!r} is not `### L<4 digits>: <subject>`")
            continue
        number = int(match.group(1))
        name = f"L{number:04d}"
        if number != len(ids) + 1:
            errors.append(f"{name}: expected L{len(ids) + 1:04d}")
        ids.append(number)
        found, problems = fields(block, JOURNAL_FIELDS)
        errors += [f"{name}: {p}" for p in problems]
        if found.get("area", "") not in AREAS:
            errors.append(f"{name}: area {found.get('area')!r}")
        if not iso_date(found.get("session", "")):
            errors.append(f"{name}: session {found.get('session')!r} is not a date")
        if found.get("status", "") not in STATUSES:
            errors.append(f"{name}: status {found.get('status')!r}")
        if found.get("source", "") not in SOURCES:
            errors.append(f"{name}: source {found.get('source')!r}")
        if PRIVATE_PATH.search(found.get("evidence", "")):
            errors.append(f"{name}: evidence names a path outside the repository")
        supersedes = found.get("supersedes", "")
        earlier = re.fullmatch(r"L(\d{4})", supersedes)
        if supersedes != "none" and not (earlier and 0 < int(earlier.group(1)) < number):
            errors.append(f"{name}: supersedes {supersedes!r} is not none or an earlier entry")
    return errors


def check_nights(text: str) -> list[str]:
    errors: list[str] = []
    last = ""
    for block in entries(text):
        heading = block.splitlines()[0]
        match = NIGHT_HEADING.fullmatch(heading)
        if not match or not iso_date(match.group(1)):
            errors.append(f"heading {heading!r} is not `### YYYY-MM-DD`")
            continue
        night = match.group(1)
        if night <= last:
            errors.append(f"{night}: not after {last}")
        last = night
        found, problems = fields(block, NIGHT_FIELDS)
        errors += [f"{night}: {p}" for p in problems]
        if not JOURNAL_REFS.fullmatch(found.get("journal", "")):
            errors.append(f"{night}: journal {found.get('journal')!r} is not none or entry ids")
    return errors


def check_rows(text: str) -> list[str]:
    lines = text.splitlines()
    if ROWS_HEADER not in lines:
        return [f"no header {ROWS_HEADER!r}"]
    start = lines.index(ROWS_HEADER) + 2
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    errors: list[str] = [] if rows else ["the table has no row"]
    names: set[str] = set()
    for line in rows:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            errors.append(f"{len(cells)} cells, expected 5: {line!r}")
            continue
        name, value, state, source, date = cells
        if not ROW_NAME.fullmatch(name):
            errors.append(f"name {name!r} is not an upper-case name in backticks")
        if name in names:
            errors.append(f"{name}: a second row of that name")
        names.add(name)
        if not value:
            errors.append(f"{name}: no value")
        if state not in ROW_STATES:
            errors.append(f"{name}: state {state!r}, expected one of {sorted(ROW_STATES)}")
        if not source:
            errors.append(f"{name}: no source")
        if state == "calibrated" and not ADR.search(source):
            errors.append(f"{name}: calibrated, but its source names no record")
        if not iso_date(date):
            errors.append(f"{name}: date {date!r} is not a date")
    return errors


def check_practices(text: str, steps: set[str]) -> list[str]:
    errors: list[str] = []
    body = text.split("\n## ", 1)[1] if "\n## " in text else ""
    lines = [line for line in body.splitlines() if line.startswith("* ")]
    if not lines:
        return ["no practice lines"]
    for line in lines:
        records = ADR.findall(line)
        built_later = "not yet built" in line
        if not records and not built_later:
            errors.append(f"names no record and is not marked not yet built: {line[:80]!r}")
        if built_later:
            step = re.search(r"roadmap step (\d+)", line)
            if not step or step.group(1) not in steps:
                errors.append(f"not yet built, with no roadmap step that exists: {line[:80]!r}")
    return errors


def test_rows_table_form() -> None:
    assert check_rows((HANDBOOK / "rows.md").read_text()) == []


def test_adoption_threshold_row_is_adr_0010s() -> None:
    """The gate's threshold is ADR 0010's; the row cannot drift from it without a record."""
    text = (HANDBOOK / "rows.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `DSR_THRESHOLD` |"))
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    assert cells[1].startswith("0.95,")
    assert "ADR 0010" in cells[3]


def test_practices_lines_name_a_record_or_a_step() -> None:
    text = (HANDBOOK / "practices.md").read_text()
    assert check_practices(text, roadmap_steps()) == []


@pytest.mark.parametrize("page", sorted(p.name for p in HANDBOOK.glob("*.md")))
def test_every_cited_record_exists(page: str) -> None:
    cited = set(ADR.findall((HANDBOOK / page).read_text()))
    assert sorted(n for n in cited if not record_exists(n)) == []


def test_journal_form() -> None:
    assert check_journal((HANDBOOK / "journal.md").read_text()) == []


def test_nights_form() -> None:
    assert check_nights((HANDBOOK / "nights.md").read_text()) == []


def git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def bases() -> dict[str, str]:
    """The revisions whose entries the tree must keep: where this branch left `origin/main` or `main`,
    each where git resolves it. A branch's own commits are not `main`'s, so an entry not yet landed may
    still be corrected on its branch. On `main` itself the merge-base is HEAD, and HEAD's parent, the
    state before the last squash, is added."""
    found: dict[str, str] = {}
    head = (git("rev-parse", "--verify", "--quiet", "HEAD^{commit}") or "").strip()
    for branch in ("origin/main", "main"):
        if git("rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"):
            base = (git("merge-base", "HEAD", branch) or "").strip()
            if base:
                found[f"merge-base with {branch}"] = base
            if base and base == head:
                parent = git("rev-parse", "--verify", "--quiet", "HEAD~1^{commit}")
                if parent:
                    found["HEAD~1"] = parent.strip()
    return found


@pytest.mark.parametrize("page", ["journal.md", "nights.md"])
def test_entries_are_only_appended(page: str) -> None:
    revisions = bases()
    if not revisions:
        pytest.skip("no `main` or `origin/main` to compare the entries with")
    now = entries((HANDBOOK / page).read_text())
    for label, sha in revisions.items():
        before_text = git("show", f"{sha}:docs/handbook/{page}")
        if before_text is None:  # the page did not exist at that revision
            continue
        before = entries(before_text)
        assert now[: len(before)] == before, f"{page}: an entry at {label} was edited or removed"


JOURNAL_ENTRY = """\
### L0001: A subject

* area: exits
* session: 2026-11-12
* result: something, n = 3
* evidence: docs/adr/0083-strategy-focus-extreme-movers-from-upstream-parts.md
* lesson: a lesson
* change: none
* next: a replay
* status: open
* supersedes: none
* source: reflection
"""


def journal_page(*blocks: str) -> str:
    return "# The lessons journal\n\n" + MARKER + "\n\n" + "\n".join(blocks)


def test_journal_check_takes_a_well_formed_entry() -> None:
    second = JOURNAL_ENTRY.replace("L0001", "L0002").replace(
        "supersedes: none", "supersedes: L0001"
    )
    assert check_journal(journal_page(JOURNAL_ENTRY, second)) == []


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("L0001", "L0002", "expected L0001"),
        ("area: exits", "area: luck", "area"),
        ("session: 2026-11-12", "session: 2026-13-01", "session"),
        ("status: open", "status: maybe", "status"),
        ("source: reflection", "source: a hunch", "source"),
        ("supersedes: none", "supersedes: L0001", "supersedes"),
        ("* next: a replay\n", "", "fields"),
        ("* lesson: a lesson", "* lesson:", "not a field line"),
        ("evidence: docs/adr/", "evidence: /tmp/notes/", "outside the repository"),
        ("evidence: docs/adr/", "evidence: see ~/notes/", "outside the repository"),
    ],
)
def test_journal_check_refuses_a_malformed_entry(old: str, new: str, complaint: str) -> None:
    errors = check_journal(journal_page(JOURNAL_ENTRY.replace(old, new)))
    assert any(complaint in e for e in errors), errors


def test_journal_check_refuses_text_outside_an_entry() -> None:
    with pytest.raises(ValueError, match="between the marker"):
        check_journal(journal_page("a stray line\n", JOURNAL_ENTRY))


NIGHT_ENTRY = """\
### 2026-11-12

* sessions: 2026-11-12
* data: a catalog slice
* ran: a command, exit 0
* result: a result, n = 4
* proposals: none
* journal: L0001
"""


def test_nights_check_takes_and_refuses() -> None:
    later = NIGHT_ENTRY.replace("### 2026-11-12", "### 2026-11-13")
    assert check_nights(journal_page(NIGHT_ENTRY, later)) == []
    assert any("not after" in e for e in check_nights(journal_page(later, NIGHT_ENTRY)))
    assert any(
        "journal" in e for e in check_nights(journal_page(NIGHT_ENTRY.replace("L0001", "J1")))
    )


def test_rows_check_refuses_a_malformed_row() -> None:
    good = "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | The owner's requirement | 2026-11-11 |"
    table = f"{ROWS_HEADER}\n|---|---|---|---|---|\n"
    assert check_rows(table + good) == []
    assert any("second row" in e for e in check_rows(table + good + "\n" + good))
    assert any("state" in e for e in check_rows(table + good.replace("UNCALIBRATED", "guessed")))
    assert any(
        "names no record" in e
        for e in check_rows(table + good.replace("UNCALIBRATED", "calibrated"))
    )
    assert any("date" in e for e in check_rows(table + good.replace("2026-11-11", "today")))


def test_practices_check_refuses_an_unsourced_line() -> None:
    steps = {"6", "7"}
    page = "# Practices\n\n## Rank\n\n"
    assert check_practices(page + "* A practice (ADR 0010).\n", steps) == []
    assert check_practices(page + "* A practice; not yet built (roadmap step 7).\n", steps) == []
    assert check_practices(page + "* A practice.\n", steps) != []
    assert check_practices(page + "* A practice; not yet built (roadmap step 9).\n", steps) != []
