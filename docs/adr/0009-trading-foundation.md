---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Trading foundation: NautilusTrader engine, alpaca-py venue SDK, adapter built to upstream's guide

## Context and Problem Statement

The owner named the Alpaca org repos and NautilusTrader as the foundations. What exactly is pinned, and how do
the two connect?

## Decision Drivers

* One strategy codebase for backtest and live.
* Official, maintained sources only as foundations.
* No live capital exposed to unreviewed or pre-release code.

## Considered Options

1. NautilusTrader for backtests, direct alpaca-py calls for live trading.
2. NautilusTrader for both, using a community Alpaca adapter.
3. NautilusTrader for both, with an out-of-tree Alpaca adapter written to upstream's adapter guide on top of
   alpaca-py.
4. NautilusTrader with a venue it supports officially (for example Interactive Brokers) instead of Alpaca.

## Decision Outcome

Chosen: **option 3**, with option 4 recorded as the fallback that needs no custom adapter. Choosing a broker
is the owner's call, not this ADR's.

**The fact that shapes this decision: NautilusTrader has no official Alpaca adapter.** Alpaca is absent from
upstream's integrations list, the request is an open RFC, and the maintainer wrote that an official adapter is
"not in the near term" and suggested a community listing.

Pins (see `canon/upstreams.lock.json`):

* **NautilusTrader `v2.0.0rc8`** for development. v2 is the line upstream's current docs describe, and v1 now
  gets critical security backports only. It is still a release candidate, so: **paper trading only until
  v2.0.0 stable**. `v1.231.0` is pinned as a reference fallback. Install v2 with `--pre`; a plain install
  silently gives v1, which does not match the docs.
* **alpaca-py `v0.45.0`.** The older `alpaca-trade-api-python` is archived.
* **alpaca-mcp-server `v2.4.1`**, **alpaca-skills** and **alpacahq/agentic** are pinned for reference and
  **not enabled**. The four trading skills were read on 2026-11-09 and rejected for now (ADR 0004).
* **Docs for agents:** `https://nautilustrader.io/docs/llms.txt` and `https://docs.alpaca.markets/llms.txt`.

How the adapter is built is ADR 0030, split from this record on 2026-11-10: its package, its paper tests
before anything depends on it, the community adapters read for reference only, and the known upstream
limits (cache backing, whole shares).

Safety rules (also in `.claude/rules/trading-safety.md`):

* Paper credentials only in any environment an agent can read. Live keys never enter the repo, a settings
  file, or an agent's context.
* Agents do not place, modify or cancel live orders. Going live is a human action.

Enforced by `.claude/hooks/guard.sh`, which runs before every tool call in every permission mode: it
refuses commands, fetches and Python edits that name Alpaca's live trading or production broker hosts, pass
`paper=False` or `url_override`, or set the variables that switch Alpaca's CLI or MCP server to live. The
host names come from the pinned `alpaca-py` (`alpaca.common.enums.BaseURL`) and the variable names from the
pinned `alpaca-skills`. `scripts/test-hooks` covers it. It checks text an agent sends, so it cannot stop
code that builds a live URL at run time; keeping live credentials off this machine remains the real control.

The paragraph above is superseded by ADR 0020 (2026-11-10): the guard is removed. Both safety rules stand.

### Consequences

* Good: backtest and live parity on one engine; official sources at every foundation.
* Bad: we own an adapter upstream does not; we build on a release candidate and must re-pin at v2 stable.

### Confirmation

`scripts/canon check` reports when either upstream moves. This ADR is reviewed when NautilusTrader v2.0.0
ships or in 30 days, whichever is first.

The adapter's first slice (2026-11-09) and execution slice (2026-11-10), what their tests cover and the
limits found while building them, are in ADR 0030.

## Evidence

The issue 5462 line and the sources for the adapter's two slices moved to ADR 0030 when it was split from
this record on 2026-11-10, with no change of decision; the lines that support both are kept in each. Of the
2026-11-09 second reader's fixes, the Python range is in this record, and the "alpha" wording and the
fractional-share limit are in ADR 0030. A second reader of the split (2026-11-10, `evidence-verifier`) found
nothing lost and asked for this sentence.

Read 2026-11-09 (fetched; fetches are model-summarised. Pins resolved by `scripts/canon`).

* https://nautilustrader.io/docs/latest/integrations/ — 21 integrations; Alpaca is not among them
  (fetched twice: by the research lane and again by the orchestrator).
* https://github.com/nautechsystems/nautilus_trader/issues/3374 — open RFC for an Alpaca adapter; maintainer
  comments of 2026-01-06 ("not in the near term") and 2026-10-18 (community listing; keep the
  `nautilus-alpaca` crate name free) (fetched twice, as above).
* https://nautilustrader.io/docs/latest/developer_guide/python_adapters — the out-of-tree adapter path and
  its base classes; cache-backing limit.
* https://github.com/nautechsystems/nautilus_trader/releases — v2.0.0rc8 is the newest release, a
  pre-release (resolved to `d53849b216e2`); v1.231.0 resolved to `27a8e54e7ac3`. Branches `develop`
  (default), `master`, `nightly`, `develop_v1` confirmed by ref advertisement.
* https://github.com/nautechsystems/nautilus_trader — LGPL-3.0; `--pre` needed for v2; v1 gets critical
  security backports only. Python 3.13 and 3.14 in practice: the README lists 3.15 as a source build only,
  and PyPI's rc8 metadata says `>=3.13,<3.15`.
* https://nautilustrader.io/docs/latest/concepts/live/ — the same strategy code runs in backtest and live;
  venue, transport and timing behaviour may not be reproduced in simulation.
* https://github.com/alpacahq/alpaca-py — Apache-2.0, v0.45.0 (resolved to `c42add7c0a70`).
* https://github.com/alpacahq/alpaca-trade-api-python — archived 2026-06-17, points to alpaca-py.
* https://github.com/alpacahq/alpaca-mcp-server — MIT; v2 is a full rewrite (tag `v2.4.1` resolved to
  `63cc4278d87b`; the releases page showed none, the tag exists in the ref advertisement).
* https://github.com/alpacahq/alpaca-skills and https://github.com/alpacahq/agentic — official skills and
  plugin marketplace; contents not yet read.
* https://github.com/jssyxd/nautilus-alpaca and https://github.com/gmatrunich/nautilus-alpaca — community
  adapters; last commits 2026-10-18 and 2026-10-01.
* Second reader, 2026-11-09 (`evidence-verifier`): 17 of 19 claims supported. The two corrections (the
  "alpha" wording and the Python range) and the fractional-share limit are applied above.
