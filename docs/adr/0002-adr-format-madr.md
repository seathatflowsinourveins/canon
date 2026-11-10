---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-05-09
---

# Record decisions as MADR 4.0.0 with mandatory evidence and a review date

## Context and Problem Statement

ADR 0001 makes ADRs the place where evidence becomes a decision. Which format, and what must every record hold?

## Considered Options

1. MADR 4.0.0.
2. Nygard-style records via adr-tools.
3. A home-grown format.

## Decision Outcome

Chosen: **MADR 4.0.0**, with two additions that `scripts/canon lint` enforces:

* front-matter `review-by: YYYY-MM-DD`: the date after which the decision must be re-verified;
* a `## Evidence` section with at least one URL, each line saying how it was checked
  (`fetched`, `resolved` to a SHA, `bundled` with a pinned tool, or `UNVERIFIED`).

Files live in `docs/adr/NNNN-kebab-title.md`. Status values: `proposed`, `accepted`, `rejected`, `deprecated`,
`superseded by ADR-NNNN`. A superseded ADR is kept, never edited into agreement with its successor.
Copy `docs/adr/template.md` to start one.

### Consequences

* Good: a standard format other tools and people already read; lintable evidence.
* Bad: the two additions are ours, not MADR's. MADR's template says to adapt it and calls its own
  front-matter fields optional, but nothing in MADR says extra fields or sections are allowed. Whether
  these records are still "valid MADR" is UNVERIFIED.

## Evidence

Read 2026-11-09.

* https://github.com/adr/madr/releases and https://adr.github.io/madr/ — 4.0.0 (2024-09-17) is the latest
  release; ships full, minimal and bare templates (fetched; tag `4.0.0` resolved to commit `2475fe1973f6`).
* https://github.com/thomvaill/log4brains — the ADR site generator defaults to MADR (fetched); its recent
  activity is UNVERIFIED, so it is not adopted.
* https://github.com/npryce/adr-tools — older Nygard-style tool; licence and activity UNVERIFIED.
* Second reader, 2026-11-09 (`evidence-verifier`): the version and template claims are supported; the
  "valid MADR" claim had no source and is corrected above.
