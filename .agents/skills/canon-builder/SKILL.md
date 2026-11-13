---
name: canon-builder
description: Follow Canon's standing terms when an adopted runtime other than Claude Code is assigned a build task.
---

Follow [AGENTS.md, Orca](../../../AGENTS.md#orca),
[ADR 0058, rule 2](../../../docs/adr/0058-terms-for-a-build-worker-of-another-runtime.md),
as superseded by [ADR 0079, rule 5](../../../docs/adr/0079-build-workers-trading-paths-levels-and-commits.md).

- Work in your own workspace; change only the paths the task names.
- Never commit, push, land, open a pull request, move a pin, install anything,
  start another agent or a scheduled job, run paper tests, or read a credential.
- Write failing tests first where a test can show the property (required on the
  trading path, AGENTS.md rule 10 and ADR 0074), then build and run the offline
  checks the task names.
- Leave the work in the tree and report with [canon-report](../canon-report/SKILL.md).
  The checking Claude lane reproduces the tests, commits, reviews and lands.
