"""The handbook's pages keep the form each page states (`docs/handbook/`).

The contract is the pages' own: the rows table's columns and its two states, one row per file under
`rows/` with `rows.md` only the introduction and the two tables' headers, each practice file under
`practices/` holding one line that names a record that exists or says "not yet built" with a roadmap
step, with `practices.md` only the introduction and the index of move folders, the
journal's ten fields (`journal.md`, closed at `L0009`) and twelve in each later entry, one per file
under `journal/`, and the nightly study's six (`nights.md`), and both of those append-only, so the
entries already on `main` stay as they were, and the named limits' one table (`limits.md`), each line
a case, its reader, a reproducing test that exists or `none`, and a date. No upstream reads these
pages, so the parsing is the standard library's `re` and `ast`; what is on `main` is asked of git
through its own plumbing, `git rev-parse --verify`, `git merge-base`, `git ls-tree -r --name-only -z`
and `git show <rev>:<path>` (all in `git help`). No test here touches the network.
"""

from __future__ import annotations

import ast
import datetime
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = ROOT / "docs" / "handbook"
ROWS = HANDBOOK / "rows"
ROWS_TABLES = ("settings", "window-settings")
PRACTICES = HANDBOOK / "practices"
MARKER = "<!-- append-only: add new entries below this line; never edit or remove one above it -->"
JOURNAL_MARKER = "<!-- append-only: closed; new entries go in docs/handbook/journal/ -->"
ADR = re.compile(r"\bADR (\d{4})\b")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
FRAGMENT = re.compile(r"\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")

ROWS_HEADER = "| Row | Value | Status | Source or study | Date |"
ROW_NAME = re.compile(r"`[A-Z][A-Z0-9_]*`")
ROW_SEPARATOR = re.compile(r"\|(?:\s*:?-+:?\s*\|){5}")
ROW_STATES = {"UNCALIBRATED", "calibrated"}

JOURNAL_HEADING = re.compile(r"### L(\d{4}): \S.*")
JOURNAL_FRAGMENT_ID = r"\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*"
JOURNAL_FRAGMENT = re.compile(rf"{JOURNAL_FRAGMENT_ID}\.md")
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
TRACKED_FIELDS = ("owner", "closes")
"""Google SRE's terms for a postmortem's action items, an owner and "a verifiable end state"
(https://sre.google/workbook/postmortem-culture/, "Concrete Action Items"), asked of every fragment
after `source`: who closes it, and the test or unit that does. An open entry names both; a closed one
says `none` in both, since the entry that closes it is the record of its closure."""
UNNAMED = {"none", "n/a", "na", "nobody", "tbd", "todo", "later", "pending", "?", "-", "unknown"}
"""Values that name no one and nothing, refused on an open entry's two tracked fields."""
LAST_LEGACY = 9
"""`journal.md`'s entries end at `L0009`: the page is closed and every later entry is a fragment."""
AREAS = {"ranking", "entries", "exits", "sizing", "execution", "data", "risk", "strategies"}
STATUSES = {"open", "confirmed", "refuted", "adopted", "superseded"}
SOURCES = {"reflection", "nightly", "review", "lead", "owner"}

NIGHT_HEADING = re.compile(r"### (\d{4}-\d{2}-\d{2})")
NIGHT_FIELDS = ("sessions", "data", "ran", "result", "proposals", "journal")
# An absolute or home path: evidence points inside the repository or names a record kept outside it.
PRIVATE_PATH = re.compile(r"(^|[\s(`'\"])(/|~/)[A-Za-z]")
JOURNAL_ID = rf"(?:L\d{{4}}|{JOURNAL_FRAGMENT_ID})"
JOURNAL_REFS = re.compile(rf"none|{JOURNAL_ID}(, {JOURNAL_ID})*")


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


def entries(text: str, markers: tuple[str, ...] = (MARKER,)) -> list[str]:
    """The entry blocks below the first of `markers` the text holds, each from its `### ` heading,
    trailing space cut. Anything else between the marker and the first heading is refused."""
    sep, tail = "", ""
    for marker in markers:
        _, sep, tail = text.partition(marker)
        if sep:
            break
    if not sep:
        raise ValueError("no append-only marker")
    blocks = re.split(r"(?m)^(?=### )", tail)
    if blocks[0].strip():
        raise ValueError(f"text between the marker and the first entry: {blocks[0].strip()[:60]!r}")
    return [block.rstrip() for block in blocks[1:]]


def journal_entries(text: str) -> list[str]:
    """`journal.md`'s entries, below its closed marker or, at a revision before it closed, the open one."""
    return entries(text, (JOURNAL_MARKER, MARKER))


def unnamed(value: str) -> bool:
    """A tracked field's value that names no one and nothing, read the same way on both sides."""
    value = value.strip().rstrip(".").strip().lower()
    return value in UNNAMED or value.startswith("none ")


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


def check_journal_entry(
    block: str, name: str, targets: set[str], tracked: bool = False
) -> list[str]:
    """One check for a legacy entry (ten fields) and a fragment (twelve, `tracked`); a `supersedes`
    must name one of `targets`."""
    found, problems = fields(block, JOURNAL_FIELDS + (TRACKED_FIELDS if tracked else ()))
    errors = [f"{name}: {p}" for p in problems]
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
    if supersedes != "none" and supersedes not in targets:
        errors.append(f"{name}: supersedes {supersedes!r} is not none or another existing entry")
    if tracked:
        is_open = found.get("status") == "open"
        for field in TRACKED_FIELDS:
            value = found.get(field, "none")
            if is_open and unnamed(value):
                errors.append(f"{name}: an open entry names its {field}")
            elif not is_open and value.strip().rstrip(".").strip().lower() != "none":
                errors.append(f"{name}: a closed entry says none for its {field}")
    return errors


def check_journal(text: str, fragments: Mapping[str, str] | None = None) -> list[str]:
    """`journal.md`'s closed legacy entries, `L0001` to `LAST_LEGACY` in order, then each fragment by
    filename: one entry headed by its own id, with the twelve fields."""
    errors: list[str] = []
    ids: set[str] = set()
    for block in journal_entries(text):
        heading = block.splitlines()[0]
        match = JOURNAL_HEADING.fullmatch(heading)
        if not match:
            errors.append(f"heading {heading!r} is not `### L<4 digits>: <subject>`")
            continue
        number = int(match.group(1))
        name = f"L{number:04d}"
        if number != len(ids) + 1:
            errors.append(
                f"{name}: expected L{len(ids) + 1:04d}; add new entries under docs/handbook/journal/"
            )
        if number > LAST_LEGACY:
            errors.append(f"journal.md: {name}: closed; add a file under docs/handbook/journal/")
        errors += check_journal_entry(block, name, ids)
        ids.add(name)
    fragments = fragments or {}
    targets = ids | {Path(filename).stem for filename in fragments}
    superseded: dict[str, str] = {}
    for filename, content in sorted(fragments.items()):
        name = Path(filename).stem
        try:
            blocks = entries(MARKER + "\n" + content)
        except ValueError as error:
            errors.append(f"{filename}: {error}")
            continue
        if len(blocks) != 1:
            errors.append(f"{filename}: {len(blocks)} entries, expected exactly one")
            continue
        block = blocks[0]
        if not re.fullmatch(rf"### {re.escape(name)}: \S.*", block.splitlines()[0]):
            errors.append(f"{filename}: heading must be `### {name}: <subject>`")
        errors += [
            f"{filename}: {error}"
            for error in check_journal_entry(block, name, targets - {name}, tracked=True)
        ]
        found, _ = fields(block, JOURNAL_FIELDS + TRACKED_FIELDS)
        if found.get("session") != name[:10]:
            errors.append(f"{filename}: session {found.get('session')!r} is not its name's date")
        target = found.get("supersedes", "none")
        if target != "none":
            superseded[name] = target
            if iso_date(target[:10]) and target[:10] > name[:10]:
                errors.append(f"{filename}: supersedes {target!r}, an entry of a later date")
    for start in sorted(superseded):
        seen, current = {start}, superseded[start]
        while current in superseded:
            if current in seen:
                errors.append(f"{start}.md: a supersedes cycle through {current!r}")
                break
            seen.add(current)
            current = superseded[current]
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


def check_rows(texts: Mapping[str, str]) -> list[str]:
    """Each file under `rows/`, by label, holds one table line: five cells, a name in backticks, a value,
    one of the two states, a source and an ISO date. A name may not repeat across any two files."""
    errors: list[str] = []
    names: set[str] = set()
    rows = 0
    for label, text in texts.items():
        lines = [line for line in text.splitlines() if line.strip()]
        prefix = f"{label}: " if label else ""
        if len(lines) != 1:
            errors.append(f"{prefix}{len(lines)} non-blank lines, expected one table line")
            continue
        line = lines[0]
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5:
            errors.append(f"{prefix}{len(cells)} cells, expected 5: {line!r}")
            continue
        rows += 1
        name, value, state, source, date = cells
        if not ROW_NAME.fullmatch(name):
            errors.append(f"{prefix}name {name!r} is not an upper-case name in backticks")
        if name in names:
            errors.append(f"{prefix}{name}: a second row of that name")
        names.add(name)
        if not value:
            errors.append(f"{prefix}{name}: no value")
        if state not in ROW_STATES:
            errors.append(f"{prefix}{name}: state {state!r}, expected one of {sorted(ROW_STATES)}")
        if not source:
            errors.append(f"{prefix}{name}: no source")
        if state == "calibrated" and not ADR.search(source):
            errors.append(f"{prefix}{name}: calibrated, but its source names no record")
        if not iso_date(date):
            errors.append(f"{prefix}{name}: date {date!r} is not a date")
    if not rows:
        errors.append("no row")
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


def row_fragments() -> list[Path]:
    """Every file under `rows/`, at any depth: `test_one_row_one_file` holds each to its shape."""
    return sorted(
        path for path in ROWS.rglob("*") if path.is_file() and not path.name.startswith(".")
    )


def row_cells(name: str) -> list[str]:
    """The cells of one row, by its name in backticks; exactly one file under `rows/` must hold it."""
    found = [
        line
        for path in row_fragments()
        for line in path.read_text().splitlines()
        if line.startswith(f"| `{name}` |")
    ]
    assert len(found) == 1, f"{name}: {len(found)} row lines under docs/handbook/rows/"
    return [cell.strip() for cell in found[0].strip().strip("|").split("|")]


def test_rows_table_form() -> None:
    fragments = row_fragments()
    assert fragments, "no row files under docs/handbook/rows/"
    texts = {path.relative_to(ROOT).as_posix(): path.read_text() for path in fragments}
    assert check_rows(texts) == []


def test_adoption_threshold_row_is_adr_0010s() -> None:
    """The gate's threshold is ADR 0010's; the row cannot drift from it without a record."""
    _, value, _, source, _ = row_cells("DSR_THRESHOLD")
    assert value.startswith("0.95,")
    assert "ADR 0010" in source


def test_the_trail_cap_row_states_the_spread_floor_and_where_a_refusal_is_written() -> None:
    """The lead's O1-O3 and record rule 1 of 2026-11-12: the cap's row says every entry outside the
    fixed arm trails at least the spread floor at its step, a remade sale keeps it in price terms, and
    an entry the cap forbids is one row of the run's `refusals` by its cause."""
    _, value, state, source, _ = row_cells("TRAIL_CAP")
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
    _, value, state, source, date = row_cells(name)
    assert state == "UNCALIBRATED" and iso_date(date)
    assert "never profit and loss" in value and "ranks nothing" in value
    assert "Lead's D" in source


def test_one_row_one_file() -> None:
    """A row is one file directly under its table's folder, named by a place and the row's name, holding
    one line: its table line. A row's table is its folder and its name is its file's slug."""
    fragments = row_fragments()
    assert fragments, "no row files under docs/handbook/rows/"
    tables: set[str] = set()
    for path in fragments:
        assert path.parent.parent == ROWS, f"{path.relative_to(ROOT)} is not under one table folder"
        table = path.parent.name
        assert table in ROWS_TABLES, f"{table!r} is not one of {ROWS_TABLES}"
        assert FRAGMENT.fullmatch(path.name), f"{path.name!r} is not a place, a slug and `.md`"
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 1, f"{path.relative_to(ROOT)}: {len(lines)} non-blank lines"
        cells = [cell.strip() for cell in lines[0].strip().strip("|").split("|")]
        assert len(cells) == 5, f"{path.relative_to(ROOT)}: {len(cells)} cells, expected 5"
        name = cells[0].strip("`")
        assert path.name == f"{path.name[:3]}-{name.lower().replace('_', '-')}.md", (
            f"{path.relative_to(ROOT)} does not name its row {name}"
        )
        tables.add(table)
    # A place orders a table and need not be unique: two units that take one free place still write
    # two files, read in the order of their slugs, so neither has to renumber.
    assert sorted(tables) == sorted(ROWS_TABLES), f"tables {sorted(tables)}, expected {ROWS_TABLES}"


def test_rows_page_is_an_index_and_lists_no_row() -> None:
    """`rows.md` keeps the introduction and the two tables' headers, points at each table's folder, and
    lists no row: adding a row is one new file in its table's folder, never an edit here."""
    text = (HANDBOOK / "rows.md").read_text()
    assert text.startswith("# The rows table\n")
    assert text.count(ROWS_HEADER) == 2, "expected one header for each table"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line == ROWS_HEADER:
            after = lines[index + 1] if index + 1 < len(lines) else ""
            assert ROW_SEPARATOR.fullmatch(after), (
                f"line {index + 1}: the header has no separator row"
            )
    tables = [index for index, line in enumerate(lines) if line == ROWS_HEADER]
    kept = {index for start in tables for index in (start, start + 1)}
    stray = [line for index, line in enumerate(lines) if "|" in line[:2] and index not in kept]
    assert stray == [], f"rows.md holds a table line that is not a header or its separator: {stray}"
    for table in ROWS_TABLES:
        assert f"(rows/{table}/)" in text, f"rows.md points at nothing for {table}"


def test_two_row_adds_under_the_same_table_merge_clean(tmp_path: Path) -> None:
    """Two branches that each add one row file under the same table merge without a conflict, and
    neither touches `rows.md`."""
    assert row_fragments(), "no row files under docs/handbook/rows/"
    repo = tmp_path / "handbook"
    rows_md = repo / "docs" / "handbook" / "rows.md"
    rows_md.parent.mkdir(parents=True)
    shutil.copyfile(HANDBOOK / "rows.md", rows_md)
    shutil.copytree(ROWS, repo / "docs" / "handbook" / "rows")
    assert git_in(repo, "init", "--quiet").returncode == 0
    assert git_in(repo, "add", "-A").returncode == 0
    assert git_in(repo, "commit", "--quiet", "-m", "the handbook").returncode == 0
    base = git_in(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert base
    adds: dict[str, Path] = {}
    for branch, name, row in (
        ("first-add", "480-first-add.md", "`FIRST_ADD`"),
        ("second-add", "480-second-add.md", "`SECOND_ADD`"),
    ):
        assert git_in(repo, "checkout", "--quiet", base).returncode == 0
        assert git_in(repo, "checkout", "--quiet", "-b", branch).returncode == 0
        adds[branch] = repo / "docs" / "handbook" / "rows" / "settings" / name
        adds[branch].write_text(f"| {row} | 1 | UNCALIBRATED | the {branch} row | 2026-11-13 |\n")
        assert git_in(repo, "add", "-A").returncode == 0
        assert git_in(repo, "commit", "--quiet", "-m", branch).returncode == 0
    assert git_in(repo, "checkout", "--quiet", base).returncode == 0
    for branch, added in adds.items():
        merged = git_in(repo, "merge", "--quiet", "--no-edit", branch)
        assert merged.returncode == 0, merged.stdout + merged.stderr
        assert added.exists(), added
    assert git_in(repo, "diff", "--quiet", base, "--", "docs/handbook/rows.md").returncode == 0
    conflicts = [path for path in repo.rglob("*.md") if "<<<<<<<" in path.read_text()]
    assert conflicts == []


def practice_fragments() -> list[Path]:
    """Every file under `practices/`, at any depth: `test_one_practice_one_file` holds each to its shape."""
    return sorted(
        path for path in PRACTICES.rglob("*") if path.is_file() and not path.name.startswith(".")
    )


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
    """A practice is one file directly under its move's folder, named by its place and a slug, holding
    one line; what the line must say is `test_practices_lines_name_a_record_or_a_step`'s to check."""
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    for path in fragments:
        assert path.parent.parent == PRACTICES, (
            f"{path.relative_to(ROOT)} is not under one move folder"
        )
        assert SLUG.fullmatch(path.parent.name), f"{path.parent.name!r} is not a move slug"
        assert FRAGMENT.fullmatch(path.name), f"{path.name!r} is not a place, a slug and `.md`"
        fragment_line(path)


def test_practices_page_is_an_index_and_lists_no_practice() -> None:
    """`practices.md` keeps the introduction and points at each move's folder, never at a practice."""
    text = (HANDBOOK / "practices.md").read_text()
    assert [line for line in text.splitlines() if line.startswith("* ")] == []
    folders = sorted(path.name for path in PRACTICES.iterdir() if path.is_dir())
    assert folders, "no move folders under docs/handbook/practices/"
    for folder in folders:
        assert f"(practices/{folder}/)" in text, f"practices.md points at nothing for {folder}"


@pytest.mark.parametrize(
    "page", sorted(path.relative_to(HANDBOOK).as_posix() for path in HANDBOOK.rglob("*.md"))
)
def test_every_cited_record_exists(page: str) -> None:
    cited = set(ADR.findall((HANDBOOK / page).read_text()))
    assert sorted(n for n in cited if not record_exists(n)) == []


def git_in(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=handbook",
            "-c",
            "user.email=handbook@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
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
    """Every file git tracks in this checkout, from `git ls-files`; a git that cannot answer fails."""
    listed = git("ls-files", "-z")
    assert listed, "git ls-files gave nothing, so no tracked file could be checked"
    return [ROOT / name for name in listed.split("\0") if name]


def test_no_tracked_file_lists_a_practice_line() -> None:
    """A practice lives in its own file: no tracked file repeats one, under any bullet, so no shared
    list returns. A tracked path absent from the working tree, or not a file, has nothing to read."""
    fragments = practice_fragments()
    assert fragments, "no practice files under docs/handbook/practices/"
    texts = [fragment_line(path).removeprefix("* ").encode() for path in fragments]
    copies: list[str] = []
    for path in tracked_files():
        if PRACTICES in path.parents or not path.is_file():
            continue
        data = path.read_bytes()
        for text in texts:
            if text in data:
                copies.append(f"{path.relative_to(ROOT)}: {text[:60]!r}")
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
    folder = HANDBOOK / "journal"
    fragments: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if any(part.startswith(".") for part in relative.parts) or not path.is_file():
            continue
        assert path.parent == folder, (
            f"journal/{relative}: must be directly under docs/handbook/journal/"
        )
        assert JOURNAL_FRAGMENT.fullmatch(path.name) and iso_date(path.name[:10]), (
            f"journal/{relative}: expected <YYYY-MM-DD>-<slug>.md with a valid date and lowercase slug"
        )
        try:
            fragments[path.name] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise AssertionError(f"journal/{relative}: not UTF-8 ({error.reason})") from error
    assert check_journal((HANDBOOK / "journal.md").read_text(), fragments) == []


def test_nights_form() -> None:
    assert check_nights((HANDBOOK / "nights.md").read_text()) == []


def git(*args: str) -> str | None:
    # Decode without text mode's newline conversion, so a fragment's bytes stay comparable.
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    return result.stdout.decode() if result.returncode == 0 else None


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
    read = journal_entries if page == "journal.md" else entries
    now = read((HANDBOOK / page).read_text())
    for label, sha in revisions.items():
        before_text = git("show", f"{sha}:docs/handbook/{page}")
        if before_text is not None:  # the page may not have existed at that revision
            before = read(before_text)
            assert now[: len(before)] == before, (
                f"{page}: an entry at {label} was edited or removed"
            )
        if page == "journal.md":
            listed = git("ls-tree", "-r", "--name-only", "-z", sha, "--", "docs/handbook/journal/")
            assert listed is not None, f"journal/: cannot list the fragments at {label}"
            for filename in listed.split("\0"):
                if not filename or any(part.startswith(".") for part in Path(filename).parts):
                    continue
                before_fragment = git("show", f"{sha}:{filename}")
                assert before_fragment is not None, (
                    f"{filename}: cannot read the fragment at {label}"
                )
                path = ROOT / filename
                assert path.is_file(), f"{filename}: an entry at {label} was removed"
                assert path.read_bytes() == before_fragment.encode(), (
                    f"{filename}: an entry at {label} was edited"
                )


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


TRACKED = "* owner: the lane that builds the stop\n* closes: a replay test\n"


# Contract: towncrier's news-fragment convention (https://towncrier.readthedocs.io/en/stable/),
# which ADR 0145 already follows for the practice and row files: one file per entry, read in
# filename order, with dotfiles skipped; the journal names a file by a date and a slug, not a place.
@pytest.fixture
def journal_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """All journal writes stay in pytest's temporary tree, including the historical snapshots."""
    handbook = tmp_path / "docs" / "handbook"
    folder = handbook / "journal"
    folder.mkdir(parents=True)
    (handbook / "journal.md").write_text(
        journal_page(
            *(JOURNAL_ENTRY.replace("L0001", f"L{n:04d}") for n in range(1, LAST_LEGACY + 1))
        )
    )
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "HANDBOOK", handbook)
    return folder


def fragment_entry(name: str, supersedes: str = "none") -> str:
    """A well-formed fragment: the ten fields, then `owner` and `closes` for an open entry."""
    entry = (JOURNAL_ENTRY + TRACKED).replace("L0001", name)
    if iso_date(name[:10]):
        entry = entry.replace("session: 2026-11-12", f"session: {name[:10]}")
    return entry.replace("supersedes: none", f"supersedes: {supersedes}")


def test_journal_fragments_match_their_filenames(journal_tree: Path) -> None:
    # Two independently chosen slugs on the same date require no shared counter or page edit.
    first = journal_tree / "2026-11-13-first-lesson.md"
    second = journal_tree / "2026-11-13-second-lesson.md"
    first.write_text(fragment_entry(first.stem))
    second.write_text(fragment_entry(second.stem))
    test_journal_form()
    second.write_text(fragment_entry(first.stem))
    with pytest.raises(AssertionError, match=re.escape(second.name)):
        test_journal_form()


@pytest.mark.parametrize(
    "name",
    [
        "2026-13-01-lesson.md",
        "2026-02-30-lesson.md",
        "2026-11-13-.md",
        "2026-11-13-Bad_slug.md",
        "2026-11-13 lesson.md",
        "L0010.md",
        "nested/2026-11-13-lesson.md",
    ],
)
def test_journal_fragment_names_are_dates_and_slugs(journal_tree: Path, name: str) -> None:
    path = journal_tree / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(fragment_entry(path.stem))
    with pytest.raises(AssertionError, match=re.escape(name)):
        test_journal_form()


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("area: exits", "area: luck", "area"),
        ("session: 2026-11-13", "session: 2026-02-30", "session"),
        ("status: open", "status: maybe", "status"),
        ("source: reflection", "source: a hunch", "source"),
        ("* next: a replay\n", "", "fields"),
        ("* next: a replay", "* next: a replay\n* next: twice", "fields"),
        ("* lesson: a lesson\n* change: none", "* change: none\n* lesson: a lesson", "fields"),
        ("* lesson: a lesson", "* lesson:", "not a field line"),
        ("evidence: docs/adr/", "evidence: /tmp/notes/", "outside the repository"),
        ("evidence: docs/adr/", "evidence: see ~/notes/", "outside the repository"),
    ],
)
def test_journal_fragment_reuses_the_field_check(
    journal_tree: Path, old: str, new: str, complaint: str
) -> None:
    path = journal_tree / "2026-11-13-field-check.md"
    path.write_text(fragment_entry(path.stem).replace(old, new))
    with pytest.raises(AssertionError, match=re.escape(path.name)) as error:
        test_journal_form()
    assert complaint in str(error.value)


@pytest.mark.parametrize("content", ["empty", "two", "preamble", "tail"])
def test_journal_fragment_holds_exactly_one_entry(journal_tree: Path, content: str) -> None:
    path = journal_tree / "2026-11-13-one-lesson.md"
    block = fragment_entry(path.stem)
    texts = {
        "empty": "",
        "two": block + "\n" + block,
        "preamble": "stray\n" + block,
        "tail": block + "\nstray\n",
    }
    path.write_text(texts[content])
    with pytest.raises(AssertionError, match=re.escape(path.name)):
        test_journal_form()


@pytest.mark.parametrize("number", [1, LAST_LEGACY, LAST_LEGACY + 1])
def test_journal_legacy_append_point_is_closed(number: int) -> None:
    page = journal_page(
        *(JOURNAL_ENTRY.replace("L0001", f"L{n:04d}") for n in range(1, LAST_LEGACY + 1)),
        JOURNAL_ENTRY.replace("L0001", f"L{number:04d}"),
    )
    errors = check_journal(page)
    assert any(
        f"L{number:04d}" in error and "docs/handbook/journal/" in error for error in errors
    ), errors


@pytest.mark.parametrize("supersedes", [f"L{LAST_LEGACY:04d}", "2026-11-13-z-original"])
def test_journal_fragments_resolve_both_kinds_of_supersedes(
    journal_tree: Path, supersedes: str
) -> None:
    # An existing target can sort after its correction on the same date: slugs are not a counter.
    original = journal_tree / "2026-11-13-z-original.md"
    correction = journal_tree / "2026-11-13-a-correction.md"
    original.write_text(fragment_entry(original.stem))
    correction.write_text(fragment_entry(correction.stem, supersedes))
    test_journal_form()
    correction.write_text(fragment_entry(correction.stem, "2026-11-13-missing"))
    with pytest.raises(AssertionError, match="supersedes") as error:
        test_journal_form()
    assert correction.name in str(error.value)
    assert "2026-11-13-missing" in str(error.value)


@pytest.mark.parametrize("supersedes", ["L0000", f"L{LAST_LEGACY + 1:04d}", "2026-11-13-self"])
def test_journal_fragments_refuse_a_missing_or_self_reference(
    journal_tree: Path, supersedes: str
) -> None:
    path = journal_tree / "2026-11-13-self.md"
    path.write_text(fragment_entry(path.stem, supersedes))
    with pytest.raises(AssertionError, match="supersedes"):
        test_journal_form()


def test_an_open_fragment_names_its_owner_and_end_state_and_a_closed_one_says_none(
    journal_tree: Path,
) -> None:
    path = journal_tree / "2026-11-13-tracked.md"
    path.write_text(fragment_entry(path.stem))
    test_journal_form()
    closed = (
        fragment_entry(path.stem)
        .replace("status: open", "status: adopted")
        .replace("owner: the lane that builds the stop", "owner: none")
        .replace("closes: a replay test", "closes: none")
    )
    path.write_text(closed)
    test_journal_form()


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("owner: the lane that builds the stop", "owner: None", "names its owner"),
        ("owner: the lane that builds the stop", "owner: TBD.", "names its owner"),
        ("closes: a replay test", "closes: n/a", "names its closes"),
        ("status: open", "status: adopted", "a closed entry says none"),
        ("* owner: the lane that builds the stop\n", "", "fields"),
        ("* closes: a replay test\n", "", "fields"),
        ("owner: the lane that builds the stop", "owner: none", "names its owner"),
        ("closes: a replay test", "closes: none", "names its closes"),
        ("owner: the lane that builds the stop", "owner: none yet", "names its owner"),
        ("closes: a replay test", "closes: later", "names its closes"),
        ("owner: the lane that builds the stop", "owner: ?", "names its owner"),
    ],
)
def test_a_fragment_without_its_owner_or_end_state_is_refused(
    journal_tree: Path, old: str, new: str, complaint: str
) -> None:
    path = journal_tree / "2026-11-13-untracked.md"
    path.write_text(fragment_entry(path.stem).replace(old, new))
    with pytest.raises(AssertionError, match=re.escape(path.name)) as error:
        test_journal_form()
    assert complaint in str(error.value)


def test_a_legacy_entry_keeps_ten_fields() -> None:
    assert check_journal(journal_page(JOURNAL_ENTRY)) == []
    assert any("fields" in e for e in check_journal(journal_page(JOURNAL_ENTRY + TRACKED)))


@pytest.mark.parametrize("value", ["None", "none.", " none "])
def test_a_closed_fragment_may_write_none_as_an_open_one_reads_it(
    journal_tree: Path, value: str
) -> None:
    # The two sides read a value the same way: what an open entry refuses as naming no one, a
    # closed one accepts as its none (code review, 2026-11-13).
    path = journal_tree / "2026-11-13-closed.md"
    path.write_text(
        fragment_entry(path.stem)
        .replace("status: open", "status: adopted")
        .replace("owner: the lane that builds the stop", f"owner: {value.strip()}")
        .replace("closes: a replay test", "closes: none")
    )
    test_journal_form()


def test_a_fragments_name_carries_its_session_date(journal_tree: Path) -> None:
    # Filename order is the journal's reading order, so the name's date is the session's.
    path = journal_tree / "2026-11-13-dated.md"
    path.write_text(fragment_entry(path.stem).replace("session: 2026-11-13", "session: 2026-11-12"))
    with pytest.raises(AssertionError, match=re.escape(path.name)) as error:
        test_journal_form()
    assert "session" in str(error.value)


def test_a_fragment_supersedes_no_later_date(journal_tree: Path) -> None:
    earlier = journal_tree / "2026-11-12-earlier.md"
    later = journal_tree / "2026-11-13-later.md"
    later.write_text(fragment_entry(later.stem))
    earlier.write_text(fragment_entry(earlier.stem, later.stem))
    with pytest.raises(AssertionError, match=re.escape(earlier.name)) as error:
        test_journal_form()
    assert "later date" in str(error.value)


def test_fragments_that_supersede_each_other_are_refused(journal_tree: Path) -> None:
    first = journal_tree / "2026-11-13-first.md"
    second = journal_tree / "2026-11-13-second.md"
    first.write_text(fragment_entry(first.stem, second.stem))
    second.write_text(fragment_entry(second.stem, first.stem))
    with pytest.raises(AssertionError, match="cycle"):
        test_journal_form()


def test_a_fragment_that_is_not_utf8_is_named(journal_tree: Path) -> None:
    path = journal_tree / "2026-11-13-latin.md"
    path.write_bytes(fragment_entry(path.stem).replace("a lesson", "caf\u00e9").encode("latin-1"))
    with pytest.raises(AssertionError, match=re.escape(path.name)):
        test_journal_form()


def test_the_closed_journal_marker_is_the_journals_alone() -> None:
    # `nights.md` keeps the open marker: the journal's closed one there is refused.
    with pytest.raises(ValueError, match="marker"):
        check_nights(JOURNAL_MARKER + "\n")


@pytest.mark.parametrize("label", ["merge-base with main", "HEAD~1"])
@pytest.mark.parametrize("change", ["edit", "delete", "rename", "trailing-space", "line-endings"])
def test_journal_fragments_already_landed_cannot_change(
    journal_tree: Path, monkeypatch: pytest.MonkeyPatch, label: str, change: str
) -> None:
    path = journal_tree / "2026-11-13-kept.md"
    before = fragment_entry(path.stem)
    path.write_text(before)
    legacy = (journal_tree.parent / "journal.md").read_text()
    relative = "docs/handbook/journal/2026-11-13-kept.md"

    def read_revision(*args: str) -> str:
        replies = {
            ("show", "base:docs/handbook/journal.md"): legacy,
            ("ls-tree", "-r", "--name-only", "-z", "base", "--", "docs/handbook/journal/"): relative
            + "\0",
            ("show", f"base:{relative}"): before,
        }
        return replies[args]

    monkeypatch.setitem(globals(), "bases", lambda: {label: "base"})
    monkeypatch.setitem(globals(), "git", read_revision)
    test_entries_are_only_appended("journal.md")
    # Adding another fragment leaves the landed one intact.
    added = journal_tree / "2026-11-13-added.md"
    added.write_text(fragment_entry(added.stem))
    test_entries_are_only_appended("journal.md")
    if change == "edit":
        path.write_text(before.replace("a lesson", "a different lesson"))
    elif change == "trailing-space":
        path.write_text(before + "\n")
    elif change == "line-endings":
        path.write_bytes(before.replace("\n", "\r\n").encode())
    elif change == "rename":
        path.rename(journal_tree / "2026-11-13-renamed.md")
    else:
        path.unlink()
    with pytest.raises(AssertionError, match=re.escape(path.name)) as error:
        test_entries_are_only_appended("journal.md")
    assert label in str(error.value)


def test_journal_fragment_dotfiles_are_skipped(journal_tree: Path) -> None:
    path = journal_tree / "2026-11-13-unfinished.md"
    path.write_text("an unfinished entry\n")
    with pytest.raises(AssertionError, match=re.escape(path.name)):
        test_journal_form()
    path.rename(journal_tree / ".unfinished.md")
    (journal_tree / ".DS_Store").write_bytes(b"\xff")
    test_journal_form()


@pytest.mark.parametrize("name", ["notes.txt", "2026-11-13-lesson.MD", "2026-11-13-lesson"])
def test_journal_fragments_refuse_non_markdown_files(journal_tree: Path, name: str) -> None:
    (journal_tree / name).write_bytes(b"\xff")  # Reject the name before trying to decode it.
    with pytest.raises(AssertionError, match=re.escape(name)):
        test_journal_form()


def journal_section(section: str) -> str:
    text = (HANDBOOK / "journal.md").read_text()
    return text.split(f"## {section}\n", 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize(
    "section", ["The learn step", "The session-end reflection", "An entry's form"]
)
def test_journal_instructions_name_the_fragment_destination(section: str) -> None:
    text = journal_section(section)
    assert "docs/handbook/journal/" in text
    assert "<YYYY-MM-DD>-<slug>.md" in text


def test_journal_form_gives_the_two_tracked_fields_and_their_source() -> None:
    text = journal_section("An entry's form")
    for field in TRACKED_FIELDS:
        assert f"\n* {field}: <" in text, field
    assert "https://sre.google/workbook/postmortem-culture/" in text
    assert f"`L{LAST_LEGACY:04d}`" in text


def test_journal_marker_is_closed_and_keeps_the_legacy_entries() -> None:
    text = (HANDBOOK / "journal.md").read_text()
    assert JOURNAL_MARKER in text
    assert MARKER not in text
    assert f"Closed: `L0001` through `L{LAST_LEGACY:04d}`" in text
    assert [block.split(":", 1)[0] for block in journal_entries(text)] == [
        f"### L{n:04d}" for n in range(1, LAST_LEGACY + 1)
    ]


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


def test_nights_check_accepts_fragment_ids() -> None:
    page = journal_page(NIGHT_ENTRY.replace("L0001", "L0001, 2026-11-13-a-lesson"))
    assert check_nights(page) == []


def test_rows_check_refuses_a_malformed_row() -> None:
    good = "| `MIN_MOVE` | 3 per cent | UNCALIBRATED | The owner's requirement | 2026-11-11 |"
    assert check_rows({"row": good}) == []
    assert any("second row" in e for e in check_rows({"one": good, "two": good}))
    assert any("state" in e for e in check_rows({"row": good.replace("UNCALIBRATED", "guessed")}))
    assert any(
        "names no record" in e
        for e in check_rows({"row": good.replace("UNCALIBRATED", "calibrated")})
    )
    assert any("date" in e for e in check_rows({"row": good.replace("2026-11-11", "today")}))
    assert any("2 non-blank lines" in e for e in check_rows({"row": good + "\n" + good}))


def test_practices_check_refuses_an_unsourced_line() -> None:
    steps = {"6", "7"}
    page = "# Practices\n\n## Rank\n\n"
    assert check_practices(page + "* A practice (ADR 0010).\n", steps) == []
    assert check_practices(page + "* A practice; not yet built (roadmap step 7).\n", steps) == []
    assert check_practices(page + "* A practice.\n", steps) != []
    assert check_practices(page + "* A practice; not yet built (roadmap step 9).\n", steps) != []


LIMITS_HEADER = "| Case | Reader | Reproducing test | Received |"
LIMIT_SEPARATOR = re.compile(r"\|(?:\s*:?-+:?\s*\|){4}")
LIMIT_TEST = re.compile(r"`([\w./-]+\.py)::(\w+)(?:::(\w+))?(?:\[[^\]`]*\])?`")
CELL_BAR = re.compile(r"(?<!\\)\|")


def defined_tests(path: Path) -> set[str]:
    """The test functions a file defines, by pytest's node id: `name` at module level and
    `Class::name` in a class, read with the standard library's `ast`, so a name only quoted is none."""
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names |= {
                f"{node.name}::{item.name}"
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            }
    return names


def check_limits(text: str, root: Path = ROOT, today: datetime.date | None = None) -> list[str]:
    """The named limits table: one header, its separator, then rows of a case named once, the reader
    that found it, the test that reproduces it as a pytest node id in this repository (a file and a
    function, a class's method, or one parametrized case; the function must be defined there) or
    `none`, and the ISO date it was received, never after today. An empty table is a list with nothing
    on it; a `|` line outside it, indented or not, is refused. A `\\|` in a cell is its text."""
    today = today or datetime.date.today()
    lines = text.splitlines()
    if lines.count(LIMITS_HEADER) != 1:
        return ["the page needs exactly one limits table header"]
    start = lines.index(LIMITS_HEADER)
    if start + 1 >= len(lines) or not LIMIT_SEPARATOR.fullmatch(lines[start + 1].strip()):
        return ["the limits header has no separator line"]
    end = start + 2
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    errors = [
        f"a table line outside the limits table: {line[:60]!r}"
        for index, line in enumerate(lines)
        if line.lstrip().startswith("|") and not start <= index < end
    ]
    cases: set[str] = set()
    for line in lines[start + 2 : end]:
        cells = [cell.strip() for cell in CELL_BAR.split(line.strip().strip("|"))]
        if len(cells) != 4:
            errors.append(f"{line[:60]!r}: {len(cells)} cells, not 4")
            continue
        case, reader, test, received = cells
        if not case:
            errors.append(f"{line[:60]!r}: no case")
        elif case in cases:
            errors.append(f"{case[:40]!r}: the case is listed twice")
        cases.add(case)
        if not reader:
            errors.append(f"{case[:40]!r}: no reader")
        if test != "none":
            named = LIMIT_TEST.fullmatch(test)
            if named is None:
                errors.append(
                    f"{case[:40]!r}: test {test!r} is not a `<file>.py::<test>` node id or none"
                )
            else:
                path = root / named.group(1)
                function = "::".join(part for part in named.group(2, 3) if part)
                if not path.is_file() or function not in defined_tests(path):
                    errors.append(f"{case[:40]!r}: test {test!r} does not exist")
        if not iso_date(received):
            errors.append(f"{case[:40]!r}: received {received!r} is not an ISO date")
        elif datetime.date.fromisoformat(received) > today:
            errors.append(f"{case[:40]!r}: received {received!r} is after today")
    return errors


def test_limits_page_form() -> None:
    assert check_limits((HANDBOOK / "limits.md").read_text()) == []


LIMITS_PAGE = f"# Limits\n\n{LIMITS_HEADER}\n|---|---|---|---|\n"
A_LIMIT = "| A case | a reader | `tests/test_handbook.py::test_limits_page_form` | 2026-11-13 |"


def test_limits_check_takes_an_empty_list_and_a_well_formed_line() -> None:
    assert check_limits(LIMITS_PAGE) == []
    assert (
        check_limits(LIMITS_PAGE + A_LIMIT + "\n| Another | a reader | none | 2026-11-13 |\n") == []
    )


@pytest.mark.parametrize(
    ("old", "new", "complaint"),
    [
        ("A case", "", "no case"),
        ("a reader", "", "no reader"),
        ("`tests/test_handbook.py::test_limits_page_form`", "a test", "is not a `"),
        ("test_limits_page_form`", "test_no_such_function`", "does not exist"),
        ("2026-11-13", "today", "not an ISO date"),
        (" 2026-11-13 |", " 2026-11-13 | extra |", "5 cells"),
    ],
)
def test_limits_check_refuses_a_malformed_line(old: str, new: str, complaint: str) -> None:
    errors = check_limits(LIMITS_PAGE + A_LIMIT.replace(old, new, 1) + "\n")
    assert any(complaint in error for error in errors), errors


def test_limits_check_refuses_a_page_without_its_table_or_a_stray_line() -> None:
    assert check_limits("# Limits\n") == ["the page needs exactly one limits table header"]
    assert check_limits(f"# Limits\n\n{LIMITS_HEADER}\n\n") == [
        "the limits header has no separator line"
    ]
    stray = LIMITS_PAGE + A_LIMIT + "\n\nText.\n\n| A stray | line | none | 2026-11-13 |\n"
    assert any("outside the limits table" in error for error in check_limits(stray))


# Code review of the limits check (fbad5890): what a reproducing test may be, and what a line may hold.
ADAPTER_TEST = (
    "`packages/canon-alpaca/tests/test_data_client.py::"
    "test_bars_quotes_and_trades_reach_strategy_and_cache`"
)


@pytest.mark.parametrize(
    "test",
    [
        ADAPTER_TEST,
        "`tests/test_handbook.py::test_limits_check_refuses_a_malformed_line[no case]`",
    ],
)
def test_limits_check_takes_any_test_of_the_repository_by_its_node_id(test: str) -> None:
    line = f"| A case | a reader | {test} | 2026-11-13 |\n"
    assert check_limits(LIMITS_PAGE + line) == []


def test_limits_check_takes_a_class_method_and_refuses_a_name_only_quoted(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        'class TestX:\n    def test_a(self):\n        pass\n\n\nNOTE = "def test_quoted("\n'
    )
    page = LIMITS_PAGE + "| A case | a reader | `tests/test_x.py::TestX::test_a` | 2026-11-13 |\n"
    assert check_limits(page, root=tmp_path) == []
    quoted = LIMITS_PAGE + "| A case | a reader | `tests/test_x.py::test_quoted` | 2026-11-13 |\n"
    assert any("does not exist" in error for error in check_limits(quoted, root=tmp_path))


@pytest.mark.parametrize(
    ("lines", "complaint"),
    [
        (
            A_LIMIT
            + "\n"
            + A_LIMIT.replace("`tests/test_handbook.py::test_limits_page_form`", "none"),
            "twice",
        ),
        ("| A case | a reader | none | 2999-01-01 |", "after today"),
        (
            A_LIMIT + "\n\nText.\n\n  | Indented | a reader | none | someday |",
            "outside the limits table",
        ),
    ],
)
def test_limits_check_refuses_a_repeated_case_a_future_date_and_an_indented_row(
    lines: str, complaint: str
) -> None:
    errors = check_limits(LIMITS_PAGE + lines + "\n")
    assert any(complaint in error for error in errors), errors


def test_limits_check_reads_an_escaped_bar_as_text() -> None:
    assert (
        check_limits(LIMITS_PAGE + "| bid \\| ask crossed | a reader | none | 2026-11-13 |\n") == []
    )
