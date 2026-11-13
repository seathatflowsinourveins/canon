---
name: canon-report
description: Write the report for a Canon builder or reader worker when completing its assigned unit.
---

Use the report form from ADR 0132, unit 1 (proposed), alongside
[AGENTS.md, Orca](../../../AGENTS.md#orca), [Reporting](../../../AGENTS.md#reporting) and
[ADR 0079, rule 5](../../../docs/adr/0079-build-workers-trading-paths-levels-and-commits.md).

A builder writes `REPORT-<unit>.md` at the workspace root, using the task's
unit name, and leaves it untracked. A reader writes no file and gives the same
form in its final message. Include, in order:

1. The base commit.
2. What changed, file by file, or what was found.
3. Each test command with counts passed, failed and skipped; name the tests
   that failed on the base before the change. State when no tests ran.
4. What was declined or left UNVERIFIED, why, and what remains.
