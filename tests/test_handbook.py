"""The handbook's pages keep the form each page states (`docs/handbook/`).

The contract is the pages' own: the rows table's columns and its two states (`rows.md`), each practice
file under `practices/` holding one line that names a record that exists or says "not yet built" with a
roadmap step, with `practices.md` only the introduction and the index of move folders, the
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
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = ROOT / "docs" / "handbook"
PRACTICES = HANDBOOK / "practices"
MARKER = "<!-- append-only: add new entries below this line; never edit or remove one above it -->"
ADR = re.compile(r"\bADR (\d{4})\b")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRAGMENT = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

ROWS_HEADER = "| Row | Value | Status | Source or study | Date |"
ROW_NAME = re.compile(r"`[A-Z][A-Z0-9_]*`")
ROW_SEPARATOR = re.compile(r"\|(?:\s*:?-+:?\s*\|){5}")
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
    """Check every rows table with shared names, refusing pipe lines outside those tables."""
    lines = text.splitlines()
    if ROWS_HEADER not in lines:
        return [f"no header {ROWS_HEADER!r}"]
    errors: list[str] = []
    rows = []
    table_lines: set[int] = set()
    for index, line in enumerate(lines):
        if line != ROWS_HEADER:
            continue
        table_lines.update((index, index + 1))  # the header and separator
        separator = lines[index + 1] if index + 1 < len(lines) else ""
        if not ROW_SEPARATOR.fullmatch(separator):
            errors.append(f"line {index + 1}: the header has no separator row: {separator!r}")
        before = len(rows)
        for row_index in range(index + 2, len(lines)):
            row = lines[row_index]
            if not row.startswith("|") or row == ROWS_HEADER:
                break
            rows.append(row)
            table_lines.add(row_index)
        if len(rows) == before:
            errors.append("the table has no row")
    for index, line in enumerate(lines):
        if line.startswith("|") and index not in table_lines:
            errors.append(f"row outside a headered table: {line!r}")
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
    lines = [line for line in text.splitlines() if line.startswith("* ")]
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


def window_row(name: str) -> list[str]:
    """The cells of one row of the rows page, by its name in backticks."""
    text = (HANDBOOK / "rows.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith(f"| `{name}` |"))
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def test_the_trail_cap_row_states_the_spread_floor_and_where_a_refusal_is_written() -> None:
    """The lead's O1-O3 and record rule 1 of 2026-11-12: the cap's row says every entry outside the
    fixed arm trails at least the spread floor at its step, a remade sale keeps it in price terms, and
    an entry the cap forbids is one row of the run's `refusals` by its cause."""
    _, value, state, source, _ = window_row("TRAIL_CAP")
    assert value.startswith("0.02 of the entry") and state == "UNCALIBRATED"
    for words in (
        "`SPREAD_FLOOR_M` at its step",
        "cold-ATR",
        "in price terms",
        "`refusals`",
        "`floor_over_cap`",
        "`distance_over_cap`",
    ):
        assert words in value, words
    assert "O1-O3" in source


@pytest.mark.parametrize("name", ["TIGHTEN_CRITERION", "REENTRY_CRITERION"])
def test_a_counted_criterion_row_is_uncalibrated_and_counts_no_profit(name: str) -> None:
    """The lead's D4(i) and D5 of 2026-11-12: a tightening arm and a re-entry arm are read by a counted
    criterion from the run record, registered UNCALIBRATED before a window selects them; it ranks
    nothing and never reads profit and loss."""
    _, value, state, source, date = window_row(name)
    assert state == "UNCALIBRATED" and iso_date(date)
    assert "never profit and loss" in value and "ranks nothing" in value
    assert "Lead's D" in source


def practice_fragments() -> list[Path]:
    """Every practice file: one Markdown file, under one folder per move, under `practices/`."""
    return sorted(PRACTICES.glob("*/*.md"))


def fragment_line(path: Path) -> str:
    """The one practice line a fragment holds; any other non-blank line is refused."""
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, f"{path}: {len(lines)} non-blank lines, expected one practice line"
    assert lines[0].startswith("* "), f"{path}: {lines[0][:60]!r} is not a `* ` practice line"
    return lines[0]


def test_practices_lines_name_a_record_or_a_step() -> None:
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    steps = roadmap_steps()
    errors: list[str] = []
    for path in fragments:
        line = fragment_line(path)
        errors += [f"{path.relative_to(ROOT)}: {error}" for error in check_practices(line, steps)]
    assert errors == []


def test_one_practice_one_file() -> None:
    """A practice is one file under its move's folder, named by a slug, and the file holds one line."""
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    steps = roadmap_steps()
    for path in fragments:
        assert path.parent.parent == PRACTICES, path
        assert SLUG.fullmatch(path.parent.name), f"{path.parent.name!r} is not a move slug"
        assert FRAGMENT.fullmatch(path.name), f"{path.name!r} is not a practice slug and `.md`"
        line = fragment_line(path)
        assert check_practices(line, steps) == [], f"{path.relative_to(ROOT)}: {line[:80]!r}"
        missing = [number for number in ADR.findall(line) if not record_exists(number)]
        assert missing == [], f"{path.relative_to(ROOT)} cites records that do not exist: {missing}"


def test_practices_page_is_an_index_and_lists_no_practice() -> None:
    """`practices.md` keeps the introduction and points at each move's folder, never at a practice."""
    text = (HANDBOOK / "practices.md").read_text()
    assert [line for line in text.splitlines() if line.startswith("* ")] == []
    folders = sorted(path.name for path in PRACTICES.iterdir() if path.is_dir())
    assert folders, "no move folders under docs/handbook/practices/"
    for folder in folders:
        assert f"(practices/{folder}/)" in text, f"practices.md points at nothing for {folder}"


def test_no_handbook_page_copies_a_practice_line() -> None:
    """A practice line lives in its own file and nowhere else: no page may list the practices again."""
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    lines = {fragment_line(path) for path in fragments}
    copies: list[str] = []
    for page in sorted(HANDBOOK.rglob("*.md")):
        if PRACTICES in page.parents:
            continue
        text = page.read_text()
        for line in lines:
            if line in text:
                copies.append(f"{page.relative_to(ROOT)} repeats a practice: {line[:60]!r}")
    assert copies == []


@pytest.mark.parametrize(
    "page", sorted(path.relative_to(HANDBOOK).as_posix() for path in HANDBOOK.rglob("*.md"))
)
def test_every_cited_record_exists(page: str) -> None:
    cited = set(ADR.findall((HANDBOOK / page).read_text()))
    assert sorted(n for n in cited if not record_exists(n)) == []


def git_in(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=handbook", "-c", "user.email=handbook@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_two_adds_under_the_same_move_merge_clean(tmp_path: Path) -> None:
    """Two branches that each add one practice file under the same move merge without a conflict."""
    repo = tmp_path / "handbook"
    repo.mkdir()
    rank = repo / "docs" / "handbook" / "practices" / "rank"
    rank.mkdir(parents=True)
    (repo / "docs" / "handbook" / "practices.md").write_text(
        "# Practices\n\n- [Rank](practices/rank/)\n"
    )
    (rank / "one-seed.md").write_text("* A seed practice (ADR 0010).\n")
    assert git_in(repo, "init", "--quiet").returncode == 0
    assert git_in(repo, "add", "-A").returncode == 0
    assert git_in(repo, "commit", "--quiet", "-m", "the handbook").returncode == 0
    base = git_in(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert base
    adds: dict[str, Path] = {}
    for branch, name in (("first-add", "two-first.md"), ("second-add", "three-second.md")):
        assert git_in(repo, "checkout", "--quiet", base).returncode == 0
        assert git_in(repo, "checkout", "--quiet", "-b", branch).returncode == 0
        adds[branch] = rank / name
        adds[branch].write_text(f"* The {branch} practice (ADR 0010).\n")
        assert git_in(repo, "add", "-A").returncode == 0
        assert git_in(repo, "commit", "--quiet", "-m", branch).returncode == 0
    assert git_in(repo, "checkout", "--quiet", base).returncode == 0
    for branch, added in adds.items():
        merged = git_in(repo, "merge", "--quiet", "--no-edit", branch)
        assert merged.returncode == 0, merged.stdout + merged.stderr
        assert added.exists(), added
    conflicts = [path for path in repo.rglob("*.md") if "<<<<<<<" in path.read_text()]
    assert conflicts == []


def tracked_files() -> list[Path]:
    """Every file git tracks in this checkout, from `git ls-files`."""
    listed = git("ls-files", "-z") or ""
    return [ROOT / name for name in listed.split("\0") if name]


def test_no_tracked_file_lists_a_practice_line() -> None:
    """A practice line lives in its own file: no tracked file repeats one, so no shared list returns."""
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    lines = [fragment_line(path).encode() for path in fragments]
    copies: list[str] = []
    for path in tracked_files():
        if PRACTICES in path.parents:
            continue
        data = path.read_bytes()
        for line in lines:
            if line in data:
                copies.append(f"{path.relative_to(ROOT)}: {line[:60]!r}")
    assert copies == []


def test_every_relative_link_in_the_handbook_resolves() -> None:
    """Every relative Markdown link under `docs/handbook/` names a path that exists."""
    missing: list[str] = []
    for page in sorted(HANDBOOK.rglob("*.md")):
        for target in LINK.findall(page.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = unquote(target.split("#", 1)[0])
            if path and not (page.parent / path).exists():
                missing.append(f"{page.relative_to(ROOT)}: {target}")
    assert missing == []


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


def test_check_rows_validates_every_rows_table_in_rows_md() -> None:
    bad = (
        HANDBOOK / "rows.md"
    ).read_text() + "\n| `TRAIL_CAP` | duplicate | guessed | | bad-date |\n"
    assert check_rows(bad) != []


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("`TRAIL_CAP`", "`MIN_MOVE`", "second row"),
        ("`TRAIL_CAP`", "trail_cap", "upper-case name"),
        ("0.02", "", "no value"),
        ("UNCALIBRATED", "guessed", "state"),
        ("The owner's requirement", "", "no source"),
        ("UNCALIBRATED", "calibrated", "names no record"),
        ("2026-11-11", "bad-date", "not a date"),
        ("2026-11-11", "2026-02-30", "not a date"),
        (" | 2026-11-11", "", "expected 5"),
    ],
)
def test_rows_check_validates_later_tables(old: str, new: str, complaint: str) -> None:
    table = f"{ROWS_HEADER}\n|---|---|---|---|---|\n"
    first = "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | The owner's requirement | 2026-11-11 |"
    second = "| `TRAIL_CAP` | 0.02 | UNCALIBRATED | The owner's requirement | 2026-11-11 |"
    page = table + first + "\n\nWindow settings.\n\n" + table
    assert check_rows(page + second) == []
    errors = check_rows(page + second.replace(old, new))
    assert any(complaint in error for error in errors), errors


@pytest.mark.parametrize("position", ["before", "between", "after"])
def test_rows_check_refuses_rows_outside_a_headered_table(position: str) -> None:
    table = f"{ROWS_HEADER}\n|---|---|---|---|---|\n"
    first = table + "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | Requirement | 2026-11-11 |"
    second = table + "| `TRAIL_CAP` | 0.02 | UNCALIBRATED | Requirement | 2026-11-11 |"
    stray = "| `ATR_K` | 1 | UNCALIBRATED | Requirement | 2026-11-11 |"
    pages = {
        "before": f"{stray}\n\n{first}\n\n{second}",
        "between": f"{first}\n\n{stray}\n\n{second}",
        "after": f"{first}\n\n{second}\n\n{stray}",
    }
    errors = check_rows(pages[position])
    assert any("outside a headered table" in error for error in errors), errors


def test_rows_check_refuses_an_empty_later_table() -> None:
    table = f"{ROWS_HEADER}\n|---|---|---|---|---|\n"
    first = "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | Requirement | 2026-11-11 |"
    errors = check_rows(table + first + "\n\n" + table)
    assert "the table has no row" in errors


@pytest.mark.parametrize(
    ("row", "complaint"),
    [
        ("| `TRAIL_CAP` | 0.02 | UNCALIBRATED | Requirement | 2026-11-12 |", "second row"),
        ("| `LATE_ROW` | 1 | UNCALIBRATED | Requirement | bad-date |", "not a date"),
    ],
)
def test_rows_md_second_table_row_is_checked_by_name(row: str, complaint: str) -> None:
    # Code review of the rows check: the row is appended inside the last table, not after a blank
    # line, so it is caught by its own fault rather than only as a stray row.
    text = (HANDBOOK / "rows.md").read_text().rstrip("\n") + "\n" + row + "\n"
    errors = check_rows(text)
    assert any(complaint in error for error in errors), errors
    assert not any("outside a headered table" in error for error in errors), errors


@pytest.mark.parametrize(
    "after_header",
    [
        "| `TRAIL_CAP` | | guessed | | bad-date |",
        "| Row | Value | Status | Source or study | Date |",
    ],
)
def test_rows_check_refuses_a_header_with_no_separator(after_header: str) -> None:
    # Code review of the rows check: the line after a header was taken as its separator unread.
    page = f"{ROWS_HEADER}\n{after_header}\n|---|---|---|---|---|\n"
    page += "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | Requirement | 2026-11-11 |\n"
    errors = check_rows(page)
    assert any("no separator" in error for error in errors), errors
