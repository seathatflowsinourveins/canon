---
status: accepted
date: 2026-11-12
decision-makers: the lead, under the owner's delegation (ADR 0074), for the adoption; master orchestrator (Claude) for the terms below
review-by: 2026-12-12
---

# Replay tearsheets: NautilusTrader's `create_tearsheet` writes the HTML, plotly is declared directly at the locked 7.1.0, and Kaleido is not adopted; the tearsheet call is not built yet

## Context and Problem Statement

A replay ends in a `BacktestResult` and numbers, with nothing to look at. NautilusTrader ships a tearsheet
that draws one, and it needs plotly. `uv.lock` holds plotly 7.1.0, but only because skfolio depends on it;
this workspace declares no plotly of its own. What draws a replay's tearsheet, and on what dependency?

## Decision Drivers

* Rule 3: call the upstream's documented API; draw nothing here.
* A package imported on behalf of this repository's code is declared by this repository.
* Proportion (ADR 0074): a view of a replay moves no roadmap step by itself, so this is the smallest
  form: one declared pin, no new package, documentation at the pin and one verifier pass.

## Considered Options

1. Draw the charts here.
2. Declare `nautilus-trader[visualization]`, the extra the upstream documents.
3. Declare `plotly==7.1.0` directly and call `create_tearsheet` for HTML only.

## Decision Outcome

Chosen: **option 3**. Option 1 is invention. Option 2 also installs Kaleido and simplejson: Kaleido is
needed only for a static image, and simplejson is not imported anywhere under `analysis/` at the pin.
Landscape: the viewers scout found NautilusTrader's own visualization to be these post-run Plotly
tearsheets and no live dashboard; the other grids and dashboards it read are for a live view, a
different need (the landscape record, numbered 0102 and not yet on `main`).

* **The call.** `nautilus_trader.analysis.tearsheet.create_tearsheet(engine_or_result, output_path=...)`
  takes a `BacktestEngine` or a `BacktestResult` and writes one self-contained HTML file; with
  `output_path=None` it returns the HTML as a string. Without plotly it raises `ImportError`.
* **HTML only.** A path ending `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg` or `.pdf` goes through Kaleido,
  which is not installed, so the consumer passes an `.html` path and nothing else.
* **The version.** `plotly==7.1.0`: the version the lock already held, inside the `>=7.0.0,<8.0.0` of
  NautilusTrader's `visualization` extra and the `>=6.0.0` skfolio asks for. `uv.lock` gains no package
  and changes no version; it gains two lines naming plotly among this workspace's own requirements.
* **The consumer** is the replay tearsheet: one HTML file per symbol replay, written to a folder git
  ignores, as `catalog/` and `ledger/` are; its change names the folder. Until it lands nothing here
  imports plotly or calls `create_tearsheet`.
* **Not covered.** A paper window has no `BacktestEngine` or `BacktestResult`. A tearsheet for one would
  go through `create_tearsheet_from_stats` with a returns series computed here, which is a calculation of
  this repository's own and needs a record of its own first.

Accepted on the evidence-verifier's pass of 2026-11-12, as a tool that is cheap to leave (ADR 0018).
Not settled here: no tearsheet has been written from one of this repository's replays; that the default
charts render from its `BacktestResult` is UNVERIFIED until the consumer's test does it.

### Consequences

* Good: a replay can be read as charts with no new package in the lock.
* Bad: a plotly move now has three ranges to satisfy: this pin, NautilusTrader's extra and skfolio's.

### Confirmation

* `scripts/canon lint` compares the registry's `plotly` version with `uv.lock`, and the `uv lock --check`
  of `scripts/ci` holds `uv.lock` to `pyproject.toml`.
* The tearsheet's change adds its test and cites this record.

## Evidence

Read 2026-11-12. "Pin" is NautilusTrader at `d53849b216e20f2c122ca01b4dd0da377638b186` (tag `v2.0.0rc8`),
read through `scripts/canon src nautilus-trader`.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/python/nautilus_trader/analysis/tearsheet.py — `:80-87` the guarded plotly import; `:100` and `:151-160` static suffixes go to `write_image`, anything else to `write_html`; `:366-392` `create_tearsheet`'s signature and its `output_path` forms; `:423-427` the `ImportError`; `:975` `create_tearsheet_from_stats`. Resolved, read.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/docs/concepts/visualization.md — "self-contained HTML files"; a static image extension "exports a static image through Kaleido". Resolved, read.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/d53849b216e20f2c122ca01b4dd0da377638b186/python/pyproject.toml — `:28-33` the `visualization` extra: pandas, `plotly>=7.0.0,<8.0.0`, kaleido, simplejson. Resolved, read. A search of `python/nautilus_trader/analysis/` finds neither `simplejson` nor an import of `kaleido`.
* https://pypi.org/pypi/plotly/json — 7.1.0 is the current release, MIT, uploaded 2026-09-15, requiring narwhals and packaging. Fetched.
* https://github.com/plotly/plotly.py/releases/tag/v7.1.0 — the release, published 2026-09-15; the tag resolves to `243bfd0fa5c0a5c29a185e4034a1ab5b111695a5`, and `LICENSE.txt` there is MIT by GitHub's reading. Fetched, resolved.
* skfolio at its pin `7721090537634a549008a546c8968cdec0eaf69a`, `pyproject.toml:63` — `plotly>=6.0.0`. Resolved, read.
* `uv add --no-sync plotly==7.1.0` — resolved 57 packages; `uv.lock` changed by two lines, both in this workspace's own requirement lists. Run.
* A tearsheet written from this repository's `BacktestResult` — not run. UNVERIFIED.
* The evidence-verifier's pass, 2026-11-12: the decision held. It corrected the Confirmation line (the lint compares the registry with `uv.lock`; `uv lock --check` holds `uv.lock` to `pyproject.toml`) and the tearsheet folder's description, and noted that at v2.0.0rc9 `create_tearsheet` is at `:805` and `create_tearsheet_from_stats` at `:1417`, so the lines cited here hold for the rc8 pin only. Applied.
