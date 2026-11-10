---
paths:
  - "docs/adr/**"
---

# Writing ADRs

* Start from `docs/adr/template.md` (MADR 4.0.0). File name: `NNNN-kebab-title.md`, next free number.
* Front matter must have `status`, `date`, `decision-makers`, `review-by`.
* `review-by`: 30 days for anything about models, prices or a pre-release upstream; 90 days otherwise.
* `## Evidence` is required. One line per source: the URL, what it shows, and how it was checked: `fetched`,
  `resolved` (to a SHA by `scripts/canon`), `bundled` (shipped with a pinned tool), or `UNVERIFIED`.
* Cite only what was read this session. If a fetch was truncated or summarised, say so.
* State where evidence disagrees with the request or with other evidence. Do not smooth it over.
* Use `proposed` when upstream is silent or sources have not converged. A `proposed` ADR names the gap and
  what would close it.
* Do not rewrite an accepted ADR to change its decision. Write a new one and mark the old
  `superseded by ADR-NNNN`. Fixing a typo or refreshing evidence is fine.
* Keep the decision (everything above Confirmation) under about 80 lines. Confirmation and Evidence add to
  that, and they grow as the decision is tested and re-read. An ADR past about 130 lines should be split.
* When a second reader corrects an ADR, fix the text and add one Evidence line saying so with the date.
