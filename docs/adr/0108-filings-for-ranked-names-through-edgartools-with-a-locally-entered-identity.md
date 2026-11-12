---
status: proposed
date: 2026-11-12
decision-makers: the lead, under the owner's delegation (ADR 0074), for the adoption and for the project kept apart; master orchestrator (Claude) for the terms below
review-by: 2026-12-12
---

# Filings for ranked names: edgartools 6.0.0 adopted as a tool in a project of its own, with its own lock and environment, for a GET-only recorder that sets a locally entered SEC identity through `set_identity()` before any request; the recorder is not built yet, and the record is proposed until its verifier pass

## Context and Problem Statement

A name that starts moving carries no label for why. Alpaca's assets hold no filings, and the practice has
read none. The SEC's EDGAR is public and needs no account, but it asks an automated client to declare
itself in its `User-Agent` and to keep to a request rate. Which code reads filings for the names the
ranking lists, how does it identify itself from a public repository, and where is it installed?

## Decision Drivers

* Rule 3: a filings reader comes from a maintained upstream, not from parsers written here.
* The repository is public. The identity the SEC asks for is a person's contact detail, so it never enters
  a tracked file, a log or a record.
* Rule 5: with no identity the recorder stops and says so. It does not invent one, and it never waits at
  a prompt.
* A tool off the trading path must not change the environment the gate runs in (ADR 0074). This one
  would: see the second outcome below.

## Considered Options

1. Call the SEC's endpoints from code here and parse the answers here.
2. A downloader with no parsing (`sec-edgar-downloader`, `sec-edgar`).
3. `edgartools` 5.62.1, the last 5.x release.
4. `edgartools` 6.0.0, as a dependency of the root package, in the shared environment.
5. `edgartools` 6.0.0, in a project of its own with its own lock and environment.

## Decision Outcome

Chosen: **option 5**. Option 1 is invention. Option 2 leaves the parsing to be written here. Landscape:
of the three EDGAR readers the data-foundation scout read, `edgartools` alone parses filings (2.8k
stars, a release this week); the two downloaders fetch files and stop (as the scout read them;
UNVERIFIED). Option 3 is ruled out by two lines of the 6.0.0 notes: until 6.0 `set_identity()`
wrote the full identity to the INFO log, and 6.0 removes the 5.x names a new consumer should not start on.
The cost is a release three days old.

**Why not the shared environment (option 4).** It was tried first, and one test of this repository
failed. `edgartools` requires pyarrow, which the root's `uv.lock` did not hold. With pyarrow installed
pandas backs its text columns with Arrow (its 3.0 notes: "backed by PyArrow under the hood, if
installed"), in every module that imports pandas: the ledger, the ranking behind the gate, the backtest,
the returns matrix and the studies. The failure was an outer merge of two empty frames over two text keys
and a timestamp, which then raises `ArrowInvalid`; the other 2,839 tests passed. A reader of filings is
not a reason to change what the gate runs on, so the lead ruled for a project apart.

* **The project** is `filings/`: its own `pyproject.toml`, `uv.lock` and environment, outside the
  workspace, in the form uv documents for a package that wants a separate environment (an independent
  project, with commands run from its folder). The root's `pyproject.toml` and `uv.lock` are unchanged.
  `scripts/ci` checks every tracked `uv.lock` below the root (as `git ls-files` lists them, staged ones
  included) as it checks the root's: `uv lock --check`, then `uv run --locked` for that folder's ty and
  pytest. The root's ty leaves the folder out through a `ty.toml` at the root, and the folder has an
  empty one of its own, since ty would otherwise go up to the root's.
* **The consumer** is a filings recorder for ranked names, built test-first in its own change, in that
  project. Its records go outside the repository. Until it lands only the project's test of the pin
  imports `edgar`.
* **Identity.** The owner enters the identity on the machine that records, in a setting git does not
  track. A session never enters, reads or prints it. There is no default and none is derived: not the git
  user, not an address from a commit, not a placeholder. If the setting is absent or empty the recorder
  refuses by name before it calls `edgartools` at all. Otherwise its first call is `set_identity()`, which
  sets `EDGAR_IDENTITY` for the process, and every request path runs after it. Passing the `identity`
  argument alone is not enough: at the pin the shared HTTP client takes `get_identity` as its user-agent
  factory, and with `EDGAR_IDENTITY` unset that function prompts on the terminal for 60 seconds and then
  raises `TimeoutError`. Three request paths (`xmlfiling.py`, `attachments.py`, `sgml_common.py`) build
  that client without the check that raises `IdentityNotSetError`. The recorder's refusal test shows that
  with the setting absent no prompt is reachable. The value is written nowhere.
* **GET only.** The package's `post` helpers have no caller under `edgar/` at the pin. The recorder's
  change names each call it makes and the endpoint behind it, and a test refuses any other method.
* **Rate.** The SEC states a maximum of 10 requests a second. `edgartools` waits at 9 by default
  (`EDGAR_RATE_LIMIT_PER_SEC`), and the recorder does not raise it. The limiter lives in one process, so
  one recorder process runs at a time; two would each be allowed 9.
* **Storage.** By default the package writes under `.edgar` and `.edgar_cache` in the home folder. The
  recorder sets `EDGAR_LOCAL_DATA_DIR` and `EDGAR_CACHE_DIR` to its own folder outside the repository.
* **Not enabled, never run.** `edgar.ai` is never imported: `install_skill()` there writes a skill into
  the agent's skills folder, and the package ships an MCP server. The install puts two console scripts in
  the project's environment, `edgartools` and `edgartools-mcp`; neither is run. No skill or MCP server
  from this package is an adopted extension (ADR 0004). The `ai`, `data`, `cloud` and other extras are
  not installed.
* **A label, not a signal.** A filing changes no order. A factor that uses one is registered in a study
  record first, as ADR 0100 does for the bar factors.

Open: how long after acceptance a filing appears in what the recorder reads. Upstream says only "every
few minutes" of the current filings feed, so the recorder's change measures it before a study relies on it.

### Consequences

* Good: filings arrive through a maintained reader that already keeps to the SEC's rate, and nothing the
  gate, the ledger or the studies import changes.
* Bad: a second lock and a second environment to keep. The dependency bot reads the root's `uv.lock`
  only (ADR 0016), so this project's indirect packages move when the pin moves, not weekly.
* Bad: the lint's version comparison reads the root's `uv.lock` only, so the registry entry carries no
  `pypi` field; a test in the project holds the registry, the manifest, this record's title, the
  architecture page's row and the installed version to one, and pytest and ty there to the root's pins.
* Bad: the new checks have not run on GitHub's runner before this lands; there the project's environment
  is installed on every run.
* Bad: one value must be entered by hand on each machine that records, and the recorder is idle until it is.
* A study that joins filings to bars reads the recorder's records as files; it does not import `edgar`.

### Confirmation

* `scripts/ci` runs `uv lock --check` and, in the project's own environment, ty and pytest for
  `filings/`, after the supply-chain checks have started; `scripts/test-ci` holds that for a tracked
  `uv.lock` one folder below the root, and that a stale one fails its run and skips its checks.
* `filings/tests/test_upstream_contract.py`: the installed version is the one every place that names it
  gives; the project's pytest and ty are the root's pins; `set_identity()` sets the variable and neither
  logs nor prints any part of the value; a request with no identity raises before a client is built,
  while an empty one passes that check and reaches the prompt; the shared client asks `get_identity()`,
  which prompts when none is set and does not once `set_identity()` has run. No request is made, and the
  identity is a made-up one.
* `git diff` of the root's `uv.lock` and `pyproject.toml` against `main` is empty for this change.
* The recorder's change adds the refusal test (no identity or an empty one, no request and no prompt) and
  the method test, and brings the row in `docs/architecture.md` up to date.

## Evidence

Read 2026-11-12. "Pin" is `dgunning/edgartools` at `1ce77f9903e06ec58609a40d01797331e4ead460` (tag
`v6.0.0`), resolved by `scripts/canon resolve edgartools` and read through `scripts/canon src edgartools`.

* https://github.com/dgunning/edgartools/releases/tag/v6.0.0 — the release, published 2026-11-09T19:12:07Z, not a pre-release; v5.62.1 was published 2026-10-23. The repository has 2,848 stars. Read through GitHub's API. Fetched.
* Pin, `CHANGELOG.md:10-250` — 6.0.0 requires Python 3.11 and pandas 2.2.2, moves to `httpx2`, removes the deprecated 5.x names, and (`:244`) "`set_identity()` no longer writes your name and email to the INFO log". Resolved, read.
* Pin, `edgar/settings.py:95-110` — `set_identity()` sets `EDGAR_IDENTITY`, logs only that an identity was set and closes the open clients. `:138-170` — `get_identity()` calls `ask_for_identity()` when the variable is unset: a prompt with a 60 second timer, then `TimeoutError`. Resolved, read; the project's tests run both.
* Pin, `edgar/httpclient.py:320` — the client manager is built with `user_agent_factory=get_identity`. `httpxthrottlecache` 0.7.0 (the version the project's lock holds), `httpxclientmanager.py:98-108`, `:195`, `:219` — it calls that factory when it builds a client. Read in the installed package.
* Pin, `edgar/httprequests.py:783-800` — the decorated request functions take an explicit identity, then a callable, then `EDGAR_IDENTITY`, and raise `IdentityNotSetError` with none. `edgar/xmlfiling.py:281`, `edgar/attachments.py:899` and `edgar/sgml/sgml_common.py:32` open the client outside those functions. Resolved, read.
* Pin, `edgar/httprequests.py:1032-1105` — the `post` helpers; a search of `edgar/` finds no caller but themselves. Resolved, searched.
* Pin, `edgar/httpclient.py:266-276` — "Defaults to 9 requests/sec (SEC's rate limit)", from `EDGAR_RATE_LIMIT_PER_SEC`; `:297-309` — the limiter is an in-memory bucket, so it counts one process. Resolved, read.
* https://www.sec.gov/os/accessing-edgar-data — "Current max request rate: 10 requests/second."; "Please declare your user agent in request headers", with a sample of a company name and a contact address; "Last Reviewed or Updated: June 26, 2024". Fetched through a summarising reader, which quoted these lines.
* Pin, `edgar/paths.py:51-85` — `EDGAR_LOCAL_DATA_DIR` (default `.edgar` in the home folder), `EDGAR_CACHE_DIR` (default `.edgar_cache`), `CLAUDE_SKILLS_DIR` (default `.claude/skills`). `edgar/ai/__init__.py:81-110` — `install_skill()`. Resolved, read.
* Pin, `pyproject.toml:140-142` — the console scripts `edgartools` (`edgar._cli:main`) and `edgartools-mcp` (`edgar.ai.mcp.server:main`). Resolved, read.
* Pin, `edgar/current_filings.py:109` and `:462-465` — `get_current_filings()` reads `browse-edgar?action=getcurrent`, "updated every few minutes". Resolved, read.
* https://pypi.org/pypi/edgartools/json — 6.0.0 is the latest, `license_expression` MIT, `requires_python >=3.11`, a pure-Python wheel uploaded 2026-11-09, eighteen requirements outside the extras, pyarrow 23.0.1 or later among them. Fetched.
* Pin, `LICENSE.txt` — MIT. Resolved, read.
* https://docs.astral.sh/uv/concepts/projects/workspaces/ and the same page at uv's pin `316c827e058c` (0.13.7), `docs/concepts/projects/workspaces.md:160-200`, "When (not) to use workspaces" — "the workspace shares a single lockfile"; workspaces "are _not_ suited for cases in which members have conflicting requirements, or desire a separate virtual environment for each member"; "you can always define each package as its own independent project"; then "commands must be run from the relevant package directory". Fetched 2026-11-12 (summarised) and read at the pin.
* uv at the pin, `crates/uv-workspace/src/workspace.rs:1966-1979` — a project below a workspace root whose `members` do not include it is given no workspace. `uv --help` — `--directory`, "Change to the given directory prior to running the command". Resolved, read; and run, below.
* `uv add edgartools==6.0.0` run from `filings/` — wrote `filings/uv.lock` (45 packages) and an environment in that folder; the root's `uv.lock` and `pyproject.toml` did not change. `uv --directory filings lock --check`, and `uv --directory filings run --locked` for ty and pytest (7 passed) — the commands `scripts/ci` runs. Run.
* The shared-environment trial, 2026-11-12: `uv add edgartools==6.0.0` at the root added pyarrow 26.0.1 and twenty-three other packages; the suite then gave 1 failed, 2,839 passed, the failure `tests/test_fill_gap.py::test_no_orders_are_an_empty_frame_of_the_same_columns` with `ArrowInvalid: cannot construct ChunkedArray from empty vector and omitted type` from pandas 3.1.1's merge. With one key, or an inner merge, it passes; without pyarrow the test passes. Run, then discarded.
* The evidence-verifier's pass on this record's first draft, 2026-11-12, which had option 4: the adoption held. It found the first identity bullet wrong (it said only `diagnose_ssl` could reach the prompt), read the SEC's page that an earlier fetch had been refused, and added the console scripts and the per-process limiter. Applied, and each corrected line was re-read at the pin. The project kept apart came after that pass.
* https://pandas.pydata.org/docs/whatsnew/v3.0.0.html — "Starting with pandas 3.0, a dedicated string data type is enabled by default (backed by PyArrow under the hood, if installed, otherwise falling back to being backed by NumPy `object`-dtype)". Fetched through a summarising reader, which quoted it; the first 100,000 characters of the page were read.
* ty at its pin `ac02f737a976` (0.0.101), `docs/configuration.md:5-30` — ty reads a `ty.toml` or a `[tool.ty]` table, and with neither in a folder goes on up the folders; `docs/reference/configuration.md`, `src.exclude` — anchored at the project root. Run: from `filings/` without a `ty.toml` there, ty took the root's and checked the root's tree; with the empty one it checks that folder alone, and a file importing an absent module fails it.
* `/code-review` (high) of this change, 2026-11-12, ten findings. Fixed: `scripts/test-ci` would have failed once this project was in its copy of the repository; the root's plain `ty check` failed on the folder; a failed file listing was not a failure for these checks, and a path could be listed twice during a merge; the project's checks delayed the supply-chain checks; an empty identity was not covered; the log assertion had nothing to show it was capturing; the project's pytest and ty were held to nothing; two sentences said a test held more than it did; the architecture row sat above the gate's. Declined: one shared function for the root's and a project's checks, since the root's set differs (ruff over the whole tree, its own skip labels).
* The evidence-verifier's pass on this record with the project apart, 2026-11-12: every checked claim held, and it reproduced the `ArrowInvalid` in the project's environment and not in the root's. It asked for pandas' own page, for "tracked" where the record said every `uv.lock`, for the merge's keys as they are, and for the runner limit under Consequences. Applied.
* What `sec-edgar-downloader` and `sec-edgar` do — a scout's summary of their pages. UNVERIFIED here.
* Any request made through `edgartools` from this repository — not run. UNVERIFIED.
* The new checks on GitHub's runner — not run before landing; they are the same commands. UNVERIFIED.
