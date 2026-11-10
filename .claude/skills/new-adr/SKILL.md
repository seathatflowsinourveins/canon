---
name: new-adr
description: Create an architecture decision record in this repo's format (MADR 4.0.0 with an Evidence section and a review-by date). Use when a decision about a dependency, practice, model, tool or architecture needs recording or an existing decision needs superseding.
---

# Write an ADR

1. Find the next number: list `docs/adr/` and add one to the highest `NNNN`.
2. Copy `docs/adr/template.md` to `docs/adr/NNNN-kebab-title.md`.
3. Fill it in. The title names the problem and the direction. Context is two or three sentences on what
   forces the decision now.
4. List the options actually considered, including the one the request assumed.
5. Evidence: one line per source with the URL, what it shows, and how it was checked (`fetched`, `resolved`,
   `bundled`, `UNVERIFIED`). Cite only what was read this session.
6. Where the evidence corrects the request or disagrees with itself, say so in the Decision Outcome.
7. Set `review-by`: 30 days for models, prices or pre-release upstreams; 90 days otherwise.
8. Set `status`: `accepted` when the evidence supports one option; `proposed` when upstream is silent or
   sources have not converged.
9. If this replaces an earlier ADR, set that one's status to `superseded by ADR-NNNN` and leave its text.
10. Add a row to the Decisions table in `AGENTS.md`.
11. `scripts/canon lint` must pass. For a decision other work will build on, run the `evidence-verifier`
    subagent on the new file.

The format rules are in `.claude/rules/adr.md`.
