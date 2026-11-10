---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Canon-first: build only on pinned, evidenced upstreams

## Context and Problem Statement

Agents can produce plausible APIs, patterns and "best practices" faster than anyone can check them, and the
landscape (models, harness features, trading libraries) moves monthly. Both the owner's knowledge and the model's
training data (cutoff June 2026) go stale. What is the source of truth for decisions in this repo?

The first research pass on 2026-11-09 already contradicted several common assumptions (see Evidence), which is the
practical case for not trusting recollection.

## Decision Drivers

* Every claim must be checkable by someone who was not in the conversation.
* A checkout next month must build on the same upstream code as today.
* Staleness must be detectable, not discovered by accident.
* Third-party code and instructions run with the agent's permissions.

## Considered Options

1. Trust owner and model knowledge; cite when convenient.
2. Track upstream `latest` everywhere.
3. A pinned registry ("the canon") plus evidence-carrying ADRs.

## Decision Outcome

Chosen: **option 3**. The rules:

1. **Traceability.** Every dependency, pattern or practice adopted here traces to an entry in
   `canon/upstreams.json` (pinned to a 40-hex commit in `canon/upstreams.lock.json`) or to a dated URL in an
   ADR's Evidence section.
2. **Precedence when sources disagree:** pinned upstream code and its own docs → vendor primary docs →
   results measured in this repo → community convergence → anyone's recollection, the owner's and the agent's
   included. The owner sets goals and risk limits; evidence decides the means.
3. **Glue yes, invention no.** Code here either calls an upstream API as documented or implements a contract an
   upstream documents (a hook, an adapter base class), and its header says which. Where no upstream covers a
   need, write an ADR with status `proposed` that states the gap. Do not quietly fill it.
4. **Pins move deliberately.** `scripts/canon resolve` plus a read of the upstream changelog. Nothing we execute
   floats on a branch.
5. **Decisions expire.** Every ADR carries `review-by`; past that date it is re-verified before being relied on.
6. **Label uncertainty.** Anything not confirmed on a fetched page or a resolved ref is marked `UNVERIFIED`.
7. **No silent fallback.** A check that cannot run is a failure, not a pass.

### Consequences

* Good: decisions are reproducible and auditable; drift shows up in `scripts/canon check`.
* Bad: adoption is slower; the registry and ADRs need upkeep; pins go stale without the refresh loop
  (`landscape-refresh` skill).

### Confirmation

`scripts/canon lint` (structure, evidence, review dates) runs from a hook on every edit under `canon/` or
`docs/adr/`. `scripts/canon check` (upstream drift) runs in the landscape refresh.

## Evidence

Read 2026-11-09. "fetched" means a research lane fetched the page; fetches are model-summarised, not raw text.

* https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
  — pin third-party actions to a full commit SHA. The same page now ranks a workflow dependency lockfile
  above SHA pinning; GitHub documents it as a preview. It is not adopted here: the one workflow has two
  SHA pins, and they are cross-checked by `scripts/canon lint` (fetched; re-read by the second reader).
* https://github.com/ossf/scorecard — `Pinned-Dependencies` and `Dependency-Update-Tool` are scored checks
  (fetched; pinned in canon as `ossf-scorecard`).
* https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference
  — a three-day default cooldown before version updates, though not before security updates (fetched).
* Second reader, 2026-11-09 (`evidence-verifier`): all eight claims supported; the lockfile note was added.
* https://slsa.dev/spec/v1.1/ — page states v1.1 is retired in favour of v1.2 (fetched); SLSA v1.2 itself
  not read: UNVERIFIED.
* Assumptions contradicted on first contact, all resolved by `scripts/canon resolve` or fetched:
  `alpacahq/alpaca-trade-api-python` is archived; NautilusTrader's default branch is `develop` and its only
  v2 releases are release candidates; NautilusTrader has no official Alpaca adapter
  (https://github.com/nautechsystems/nautilus_trader/issues/3374); Claude Code's npm install is deprecated
  (https://github.com/anthropics/claude-code).
