---
name: canon-reader
description: Follow Canon's standing terms when an adopted runtime is assigned a second-reader review of a change.
---

Follow [AGENTS.md, Orca](../../../AGENTS.md#orca) and
[ADR 0045, rule 1](../../../docs/adr/0045-review-runtimes-and-the-review-lifecycle.md).

- Findings only, in a new worktree at the commit under review.
- Make no edit and write no file in the worktree, not even a report: the
  coordinator checks it has no change before removing it. Never commit, push,
  land, run paper tests, or read a credential.
- Rank each finding and give its file:line and the scenario in which it fails.
- Give the findings in your final message, in the form of
  [canon-report](../canon-report/SKILL.md). Findings are claims for
  the owning lane to reproduce; this review never replaces the required review.
