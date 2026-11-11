---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Repository layout: a uv workspace with the adapter as its own package

## Context and Problem Statement

The repo held only the harness. The owner asked for the folder structure of the code foundation. Where does
each kind of thing live, in the repo and on this Mac?

## Decision Drivers

* Take the structure from the pinned tools and upstream guides, not from habit.
* The adapter must be separable: upstream keeps strategies and node configuration out of adapter packages.
* No folder before its first file.
* Market data and credentials never enter git.

## Considered Options

1. One flat package holding adapter and strategies.
2. A uv workspace: a root package plus members under `packages/`, each in the `src` layout.
3. Separate repositories for the adapter and the strategies.

## Decision Outcome

Chosen: **option 2**. It is the shape uv's workspace guide documents, and uv generated it.

| Path | Holds | Convention from |
|---|---|---|
| `pyproject.toml`, `uv.lock` | Workspace root; one lockfile for every member | uv |
| `src/canon_trading/` | Strategies and node configuration | uv `src` layout; NautilusTrader template README |
| `packages/canon-alpaca/` | The Alpaca adapter: `src/canon_alpaca/`, `tests/`, `README.md` | uv workspace member |
| `tests/`, `packages/*/tests/` | Tests beside each package's `src/` | pytest good practices |
| `pytest.toml` | `importlib` import mode | pytest good practices |
| `catalog/` | Local `ParquetDataCatalog`; ignored by git | NautilusTrader catalog guide |
| `ledger/` | The trial ledger, an Optuna SQLite study; the main checkout's, shared by every worktree; ignored by git | ADR 0035 |
| `evals/review/` | The reviewer benchmark: cases, the brief, runs and score sheets | ADR 0046 |
| `evals/build/` | The build eval: cases, the brief, the harness, runs and score sheets | ADR 0057 |
| `canon/`, `docs/adr/`, `scripts/`, `home/`, `.claude/`, `.githooks/` | The harness | ADR 0003, ADR 0011 |

On this Mac:

| Path | Holds |
|---|---|
| `~/canon` | This repository |
| `~/.claude/` | User-level Claude Code config; `~/.claude/canon/` has the event ledger, watchdog and alerts |
| `~/.local/` | The toolchain (mise, the tools it installs, the Claude Code CLI) |
| `~/Library/LaunchAgents/local.canon.lanes-watch.plist` | The lane watchdog's schedule |

Rules:

1. **The tool makes the structure.** A new package is created with `uv init --lib packages/<name>`.
2. **No folder before its first file.** Backtest run configs, notebooks and result folders are added when
   first needed, by amending this ADR.
3. **Direct dependencies are pinned exactly** in `pyproject.toml` and listed in `canon/upstreams.json`.
   Transitive ones are pinned by `uv.lock`.
4. **One source for the Python version:** `mise.toml`. `requires-python` is `>=3.14,<3.15`, the range the
   pinned NautilusTrader wheel supports. There is no `.python-version` file.
5. **The adapter follows upstream's five modules** (`constants`, `providers`, `data`, `execution`,
   `factories`). They are created when the work starts.

Not decided, or not verified:

* Lint, format and type-check tools are not adopted yet; each goes through `adopt-upstream`.
* Where Claude Code puts git worktrees was not read, so no path is ignored for them.
* No convention was found for where repositories live on macOS. `~/canon` was chosen because the owner
  picked the home folder.

### Consequences

* Good: the adapter can be built, tested and published on its own; one lockfile; structure a newcomer to
  uv will recognise.
* Bad: a workspace shares one `requires-python` and one dependency resolution, so the adapter cannot pin
  differently from the strategies.

### Confirmation

`uv lock --check` shows the lock matches the manifests. `scripts/canon list` shows every direct dependency.
`uv run --locked pytest` runs the suite, including `packages/canon-alpaca/tests/test_upstream_contract.py`, which
fails if a pin moves and upstream has renamed anything this repo's docs quote.

At first install on 2026-11-09: 24 third-party packages installed from wheels with `--no-build`; the two
workspace packages built with uv's own backend; `.venv` was 239 MB; 19 tests passed. Both have grown since. Against the installed
`nautilus-trader` 2.0.0rc8, all 13 quoted import paths exist and both factory signatures match the adapter
guide. `alpaca-py` 0.45.0's `TradingClient` defaults to `paper=True`.

## Evidence

Read 2026-11-09.

* https://docs.astral.sh/uv/concepts/projects/workspaces/ — `[tool.uv.workspace] members = ["packages/*"]`;
  `{ workspace = true }` sources; a single lockfile; one `requires-python`; meant for "multiple
  interconnected packages within a single repository" (fetched).
* https://docs.astral.sh/uv/concepts/projects/init/ and https://docs.astral.sh/uv/concepts/projects/layout/
  — uv prefers `src/<project_name>/` with a build system; commit `uv.lock`, not `.venv` (fetched).
* uv 0.13.7 on this Mac generated the root and the member with `uv init --package` and `uv init --lib`
  (run by the orchestrator).
* https://docs.pytest.org/en/stable/explanation/goodpractices.html — tests outside application code is one
  of two layouts it offers; the `src` layout is "strongly suggested" chiefly for the default `prepend`
  import mode, which this repo does not use; `importlib` mode is recommended for new projects (fetched).
* Second reader, 2026-11-09 (`evidence-verifier`): the uv, pytest, PyPA and NautilusTrader claims are
  supported and the files on disk match. The pytest wording above was stronger than the page and the
  package and size figures were stale; both corrected. The maintainer's request in issue #3374 concerns the
  crate name `nautilus-alpaca`.
* https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/ — the `src` layout stops the
  in-development copy being imported by accident. The page makes no explicit recommendation (fetched).
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/examples/live/_template/README.md
  — the adapter template's five modules, and "Keep node configuration and strategies outside the adapter
  package" (read by the orchestrator at the pinned commit).
* https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md (base classes, factory
  signature, limits), `…/concepts/data/catalog.md` (`Path.cwd() / "catalog"`) and
  `…/developer_guide/testing.md` (pytest free functions and fixtures) (all fetched).
* https://pypi.org/pypi/nautilus-trader/2.0.0rc8/json — `requires_python` `>=3.13,<3.15`; a `cp314` macOS
  arm64 wheel exists (read by the orchestrator).
* `uv add --no-sync --no-build` resolved 27 packages with both pins together on Python 3.14 (run by the
  orchestrator).
* https://github.com/nautechsystems/nautilus_trader/issues/3374 — the maintainer asked that the name
  `nautilus-alpaca` stay free, hence `canon-alpaca` (fetched twice, ADR 0009).
* 2026-11-10: `ledger/` added to the table under rule 2, for the trial ledger of ADR 0035 (an Optuna
  SQLite study written by `canon_trading.ledger`); ignored by git in `.gitignore`, as `catalog/` is.
* 2026-11-10, after the full review found each worktree counting its own trials: the ledger is the main
  checkout's `ledger/` only, which every worktree of the clone opens (ADR 0035 term 1).
* 2026-11-10: `evals/review/` added to the table under rule 2, for the reviewer benchmark of ADR 0046
  (case files checked by `tests/test_review_cases.py`).
