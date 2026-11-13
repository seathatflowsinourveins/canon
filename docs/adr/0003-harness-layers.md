---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Harness layers: each kind of instruction goes in the layer Claude Code built for it

## Context and Problem Statement

Claude Code offers several places to put guidance: memory files, scoped rules, skills, subagents, hooks,
permissions. Putting everything in `CLAUDE.md` costs tokens every turn and still cannot guarantee behaviour.
What goes where?

## Decision Drivers

* Fixed context is paid on every request; on-demand context is paid only when used.
* An instruction is advice; only the harness can guarantee something happens.
* Other agent tools should be able to read the same shared instructions.
* Scaffolding added for one model generation can become dead weight in the next.

## Considered Options

1. One large `CLAUDE.md`.
2. Layered: shared `AGENTS.md`, thin `CLAUDE.md`, scoped rules, skills, subagents, hooks, permissions.
3. Adopt a community harness framework wholesale.

## Decision Outcome

Chosen: **option 2**.

| Layer | File | Holds | Cost |
|---|---|---|---|
| Shared instructions | `AGENTS.md` | Facts and rules any agent needs | Every session |
| Claude entry point | `CLAUDE.md` | `@AGENTS.md` import plus Claude-only pointers | Every session |
| Scoped rules | `.claude/rules/*.md` with `paths:` | Rules for one part of the tree | When matching files are touched |
| Procedures | `.claude/skills/<name>/SKILL.md` | Multi-step workflows | Description always, body on use |
| Workers | `.claude/agents/*.md` | Role, tools, `model`, `effort` | On delegation |
| Enforcement | `hooks` in `.claude/settings.json` | Checks that must always run | None (harness runs them) |
| Permissions | `permissions` in `.claude/settings.json` | allow / ask / deny | None |
| Machine-level | `~/.claude/` (from `home/`) | Status line, event ledger, telemetry | None |

Rules that follow:

* `AGENTS.md` plus `CLAUDE.md` stay under 200 lines combined. A new rule that applies to part of the tree
  goes in a scoped rule; a new procedure becomes a skill.
* Anything that must hold every time is a hook or a permission rule, not a sentence.
* Subagents declare `model` and `effort` in frontmatter (ADR 0005).
* Scaffolding is re-tested at each model release and deleted when the model no longer needs it.
* No community harness framework is adopted wholesale (ADR 0004).

### Consequences

* Good: small fixed context; deterministic checks; one shared instruction file across tools.
* Bad: guidance is spread over several files, so `AGENTS.md` must index them.

### Confirmation

`/context` shows what the memory files cost; `/doctor prompt-audit` checks the instruction files;
`.claude/hooks/canon-lint.sh` proves the enforcement layer runs.

## Evidence

Read 2026-11-09 (fetched; fetches are model-summarised).

* https://code.claude.com/docs/en/memory.md — load order; `@path` imports; `.claude/rules/**/*.md` with
  `paths:`; target under 200 lines per file; `AGENTS.md` is read natively only when no `CLAUDE.md` is present
  (v2.1.277+), and a `CLAUDE.md` that imports it pulls it in.
* https://agents.md — `AGENTS.md` is stewarded by the Agentic AI Foundation under the Linux Foundation;
  nearest file wins (repo pinned as `agents-md`).
* https://code.claude.com/docs/en/skills.md — `SKILL.md` locations and frontmatter; listing text truncated at
  1,536 characters; keep `SKILL.md` under 500 lines with detail in supporting files.
* https://code.claude.com/docs/en/sub-agents.md — frontmatter includes `model`, `effort`, `tools`; frontmatter
  `model` takes precedence over `CLAUDE_CODE_SUBAGENT_MODEL` since v2.1.251; a model named on the individual
  call outranks both.
* Second reader, 2026-11-09 (`evidence-verifier`): every claim supported except one post title, now
  corrected. It also found the scout subagent declared no `effort`, against this record's own rule; fixed.
* https://code.claude.com/docs/en/hooks.md — event list, exit-code semantics, `${CLAUDE_PROJECT_DIR}`.
* https://code.claude.com/docs/en/settings.md — precedence; from project files `defaultMode` `auto` and
  `bypassPermissions` are ignored, and telemetry export variables in `env` are ignored.
* https://code.claude.com/docs/en/costs.md — move workflow content from `CLAUDE.md` into skills.
* https://www.anthropic.com/engineering — index of the posts this layering follows: "Effective context
  engineering for AI agents" (2025-09-29), "Equipping agents for the real world with Agent Skills"
  (2025-10-16), "Effective
  harnesses for long-running agents" (2025-11-26), "Harness design for long-running application development"
  (2026-03-24). The last reports a planner/generator/evaluator harness costing about $200 against $9 for a
  solo run, and the author simplifying it after a model upgrade.

## Amendment, 2026-11-13: what the work learns lives in checked-in, tested files; recall memory and lane notes are not the learning

**What this adds.** A lesson counts as learned only when it is in a checked-in place that a check reads: a
test that failed before the fix and passes after it (the trading path needs one first, ADR 0074; a fix
commit names it in a `Fails-before` trailer, ADR 0073); the standing case set, past commits with confirmed
defects that reviewers are scored on (ADR 0046); a hook, a permission rule or a `scripts/ci` or
`scripts/canon lint` check, where the layering above puts what must hold every time; and the handbook, the
lessons journal (`docs/handbook/journal.md`) and the practices and rows pages, whose form
`tests/test_handbook.py` checks. A journal entry records a lesson while it is `open`; it is learned once
it is `adopted` or `confirmed` and its change is in one of the others. The journal takes trading lessons
only (its areas), so a lesson about the harness or the process is learned as a test, a case or a check. A
recall memory (Claude Code's auto memory, the ChatGPT and Codex memories) and a lane's notes are context:
they may hold what a session found, but nothing relies on a lesson until it has moved into one of these.
No tool is added and nothing is enabled.

**Why.** The vendors of both recall memories say so. Claude Code's memory page says CLAUDE.md files and
auto memory alike are "context, not enforced configuration", and that auto memory "is machine-local" and "Files are not
shared across machines or cloud environments". The memories page for ChatGPT and Codex says: "Keep required
team guidance in AGENTS.md or checked-in documentation. Treat memories as a helpful recall layer, not as
the only source for rules that must always apply." Anthropic's evaluation guide builds its cases from
failures: "Converting user-reported failures into test cases ensures your suite reflects actual usage;
prioritizing by user impact helps you invest effort where it counts."

**Evidence for this amendment,** read 2026-11-13 through a summarising fetch:

* https://code.claude.com/docs/en/memory.md — "CLAUDE.md vs auto memory": "Claude treats them as context,
  not enforced configuration"; "Storage location": "Auto memory is machine-local", "Files are not shared
  across machines or cloud environments" — `fetched`, checked by the evidence-verifier.
* https://learn.chatgpt.com/docs/customization/memories?surface=app — the opening paragraph under
  "Memories", quoted above — `fetched` raw by the evidence-verifier.
* https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents (2026-01-09) — "Going from zero to
  one: a roadmap to great evals for agents", "Collect tasks for the initial eval dataset", "Step 1. Start
  with what you already test manually": the sentence quoted above, which is about an eval set built from bug
  reports, applied here by analogy — `fetched` raw by the evidence-verifier.
* ADR 0046 (and `tests/test_review_cases.py`), ADR 0073, ADR 0074, `docs/handbook/journal.md` ("The
  learn step") and `tests/test_handbook.py` — read in the tree.
