---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Quality gates: one script, pinned tools, the same checks on this Mac and in CI

## Context and Problem Statement

The repo checked its registry and ran pytest, and nothing else. Most of its own code is shell that runs
machine-wide; there was no linter, type checker, secret scan or CI. Which tools, and how are they run?

## Decision Drivers

* A change is judged the same way at the edit, at the commit and in CI.
* Every tool is pinned and verified like any other upstream (ADR 0001).
* Where a field has not converged, say so and hold the choice to an exact pin.

## Considered Options

1. The pre-commit config format run by `prek`, which is what NautilusTrader and ruff both use.
2. One plain script, `scripts/ci`, calling tools pinned by `mise.lock` and `uv.lock`.
3. A Makefile or `just` file.

## Decision Outcome

Chosen: **option 2**. `mise.lock` and `uv.lock` already pin and checksum tools; a hook framework would
add a second pinning mechanism. Option 1 is where the upstreams converge and is the one to revisit.

| Check | Tool, pinned | Held by |
|---|---|---|
| Registry, lock, ADR evidence; release rule | `scripts/canon lint`, `selftest` | this repo |
| Shell syntax and static analysis | shellcheck 0.11.0 | `mise.lock` |
| Lane report; practice survey | `scripts/lanes --selftest`, `scripts/survey-github selftest` | this repo |
| Each tracked executable `scripts/test-*`: hook registration (21 cases), the checks themselves (56), landing and the address check (66), watchdog, automation and bootstrap (62), the registry script and its hooks (157) | `test-hooks`, `test-ci`, `test-land`, `test-lanes`, `test-canon` | this repo |
| Lockfile current | `uv lock --check` | uv 0.13.7 |
| Python lint and format | ruff 0.17.4 | `uv.lock` |
| Python types | ty 0.0.101 | `uv.lock` |
| Tests | pytest 9.2.0 | `uv.lock` |
| Workflow files | actionlint 1.7.12 | `mise.lock` |
| Secrets, in history and working tree | gitleaks 8.30.1 | `mise.lock` |

Rules:

1. **One entry point.** `.githooks/pre-commit` runs `scripts/ci --staged`; the workflow runs `scripts/ci`
   and `scripts/land` runs `scripts/ci --landing`, which takes the last of several CI slots first, so a
   landing starts ahead of waiting hooks (2026-11-11). Since 2026-11-13 `--staged` is a commit's scope:
   the checks that take seconds, each other check a skip line that the landing and the hosted run take
   up (ADR 0138, proposed).
2. **A missing tool is a failure**, never a skipped check.
3. **Ruff:** its default rules plus import sorting. Line length 100 and single-line imports follow
   NautilusTrader's own config, so adapter code reads like upstream. (NautilusTrader itself selects every
   rule, with preview rules on; this repo does not go that far.)
4. **ty is beta upstream** (0.0.x, "does not yet have a stable API") and type checking is contested: mypy
   and pyright are the alternatives and typing.python.org recommends none. ty is chosen because it is the
   one type checker NautilusTrader runs at all, though only on its examples, not on its package. Moving
   its pin can change diagnostics; treat it as a pin move.
5. **Edit-time feedback:** a `PostToolUse` hook runs ruff on each edited Python file.
6. **CI** (`.github/workflows/ci.yml`): both actions referenced by the commit SHA in the canon lock; token
   `contents: read`; tools installed from `mise.lock`. Checked with actionlint. It runs on every pull
   request and on `main` (ADR 0014).
7. **Fast checks first** (2026-11-13). `scripts/ci` runs the lock check, the environment, ruff, its format
   check and ty first, and waits for them (under a second on this Mac, warm). If one fails, nothing slower
   starts. Each later check is a skip line saying a fast check failed, except those that fail by name at once
   (a missing tool, a test script not executable or missing, a failed file list), so rule 2 holds. This is
   NautilusTrader's order: its test and build jobs need its `pre-commit` job (`.github/workflows/build.yml`
   at `d53849b216e2`, `:201-205` and `:295-300`). A run with a lint error and a failing test shows the lint
   error first, and the test failure on the next run.

Not adopted, with the reason:

* **pre-commit format with prek:** see option 1.
* **zizmor, typos, codespell** (NautilusTrader runs them): one workflow and little prose so far. `watch`.
* **Further security scanning.** The owner's direction (2026-11-09) is that security scanning is not a
  priority here and must not add friction; the focus is workflow and quality. The secret scan stays because
  it is already in and costs a fraction of a second. No scanner is added without a concrete need, and one
  that starts blocking work on false positives is demoted or removed.
* **uv's `exclude-newer` cooldown** (NautilusTrader sets 7 days, 3 for ruff): three pins are younger than
  that (`alpaca-py` and ruff four days, ty hours), so the lock would not resolve. Enable once they age.

### Consequences

* Good: about seven to nine seconds, offline, when written. By 2026-11-13 the hosted run took about 19
  minutes; ADR 0138 takes the whole run out of the commit hook, and the landing and hosted runs keep it; shellcheck's first run found 20 style findings in this repo's
  scripts, all fixed or marked with a reason.
* Bad: actionlint does not check an action's inputs when the action is referenced by SHA, so those four inputs were checked by hand against each `action.yml`.

### Confirmation

`scripts/ci` prints one line per check in a fixed order, with any skip lines under it, and exits non-zero
on any failure; a missing tool, or a tracked `scripts/test-*` that is not executable, fails by name.
`scripts/test-ci` holds that behaviour on a copy of the tree with a fake toolchain.

## Evidence

Read 2026-11-09 (fetched by a research lane unless noted; fetches are model-summarised).

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/python/pyproject.toml — ruff with
  line-length 100 and `force-single-line`; dev group `ruff>=0.17.3`, `ty>=0.0.92`; `exclude-newer = "7 days"`.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/.pre-commit-config.yaml — ruff,
  a ty hook, shellcheck, gitleaks, actionlint, zizmor, typos, codespell; its Makefile runs them with prek.
* https://github.com/astral-sh/ruff/releases — 0.17.4, 2026-11-05 (resolved to `05cdc36a45a2`).
* https://github.com/astral-sh/ty — "currently in beta", 0.0.x, no stable API (0.0.100 resolved to
  `6c90fe0aff16`; the installed binary reports the same commit).
* https://typing.python.org/en/latest/ — lists mypy, pyrefly, pyright, ty and Zuban; recommends none.
* https://docs.astral.sh/uv/guides/integration/github/ — pins `actions/checkout` v7.0.1 at
  `3d3c42e5aac5…`, the same SHA `scripts/canon resolve` returned.
* https://mise.jdx.dev/continuous-integration.html and https://github.com/jdx/mise-action — the action runs
  `mise install --locked` when `mise.lock` is present; `version` and `cache` inputs (fetched by the
  orchestrator's fetch lane; v5.2.7 resolved to `b44d12e7d0d8`).
* https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
  — set the default token permission to read-only and grant the minimum per job; pin actions to a full
  commit SHA (a workflow lockfile now ranks above that; see ADR 0001).
* Second reader, 2026-11-09 (`evidence-verifier`): the tool versions, SHAs and NautilusTrader's config are
  supported. The reason for ty overstated what NautilusTrader type-checks, the token sentence misquoted
  GitHub's guide, and the cooldown reason named one pin of three; all corrected above. The count of 20
  shellcheck findings was observed once by the orchestrator and cannot be re-checked.
* `action.yml` of both actions at the pinned SHAs — declares `fetch-depth`, `persist-credentials`, `version`
  and `cache` (read by the orchestrator).
* Restated 2026-11-10 against `scripts/ci`, a run of it and of each `scripts/test-*`: the check table,
  rule 1, the run time (three runs of `scripts/ci` on macOS) and Confirmation.
* Licences read from each repo's licence file at the pinned tag (by the orchestrator): MIT for ruff, ty,
  gitleaks, actionlint, checkout and mise-action; GPL for shellcheck, which is run as a tool, not linked.
* ty pin moved 2026-11-11 to 0.0.101 (`tag:0.0.101` → `ac02f737a976`, resolved; `uv add --dev
  ty==0.0.101`). https://github.com/astral-sh/ty/releases/tag/0.0.101 — released 2026-11-11: bug fixes
  (recursive named tuple unions, nested partials, Pydantic strictness through PEP 695 aliases, tuple
  callbacks, exception checkpoints), an LSP watcher fallback, and `Field[..]` narrowing; no new rule and
  nothing breaking (`gh`). `scripts/ci` passed with it, ty check included (run 2026-11-11).
