---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-09
---

# Model routing: one model per conversation, routed by delegation, re-measured every release

## Context and Problem Statement

The owner asked for "suitable models for suitable tasks", from Claude Code sessions down to runtime workers,
using the latest models. Which model and effort does each lane use, and how is that kept current?

## Decision Drivers

* Cost per completed task, not price per token.
* Prompt caches are per model: switching models inside a conversation discards the cache.
* The measured results available today are on other people's workloads, not ours.

## Considered Options

1. One top model everywhere.
2. Cheapest model that seems adequate per task, switching freely.
3. A routing table by lane, applied through subagents and sessions, treated as a starting point to measure.

## Decision Outcome

Chosen: **option 3**. The table lives in `canon/models.json`.

| Lane | Model | Effort | Basis |
|---|---|---|---|
| Orchestrator: cross-lane planning, ADR decisions, long-horizon work | `fable` (`claude-fable-5-2`) | high | Vendor: step up to Fable 5.2 for demanding reasoning and long-horizon work, or when Opus 5.5 at higher effort still falls short |
| Default build session: implementing against a clear plan | `opus` (`claude-opus-5-5`) | medium; high for hard changes | Vendor "start here" model at its default effort |
| Bulk, well-specified sub-tasks run in parallel | `sonnet` (`claude-sonnet-5-5`) | medium | Opus orchestrator with Sonnet 5.5 subagents matched Opus alone at `medium` for 31% less (defect search); 1.9 points below Opus alone at `high` |
| Read-heavy scouting and web research | `sonnet` | default | Judgement needed to weigh sources |
| Mechanical, checkable sub-tasks; high-volume runtime work | `haiku` (`claude-haiku-5-5`) | default | Vendor: Haiku 5.5 first for high-volume work with checkable outputs; Batch API (50% off) when nobody is waiting |
| Evidence verification and review | `opus` | high | Judgement call; no primary source says a reviewer must match the author's tier |

Rules:

1. **One model per conversation.** Route by delegating to a subagent or a separate session.
2. **Model choice is the last lever** (ADR 0006). Sweep effort first and compare cost per completed task.
3. **For checkable work, run low and re-run failures high.** Opus 5.5 on SWE-bench Pro: `low` with failures
   re-run at `high` solved about 97% at about $0.17, against 95.3% at $0.29 for everything at `high`.
4. **Haiku is not for coding loops.** Haiku subagents under Opus cut cost per attempt 32% and pass rate about
   4 points on coding; on defect search they scored 0.677 against 0.827.
5. **Re-check at every model release**; this ADR expires in 30 days regardless.
6. **The aliases are for the Anthropic API.** `opus`, `sonnet`, `haiku` and `fable` resolve to the models
   above there. On Bedrock, Foundry and other providers they resolve to older models; use full IDs there.

Not adopted:

* **Advisor pairings.** For the current-generation pairs measured (Opus 5.5 with a Fable 5.1 advisor; Haiku
  or Sonnet 5.5 with an Opus 5.5 advisor) the gain was at the edge of noise or negative, at about twice the
  cost. A weak executor can gain a lot (a low-effort Sonnet 5 gained 23 points), but no lane here is one.
* **A second model provider for runtime workers.** Artificial Analysis shows GPT-6.1 Sol (xhigh) at index 52
  for $0.76 per task and Gemini 4 Carbon (high) at index 60. A second provider adds a second SDK, cache
  namespace and set of failure modes; it is adopted only after an eval on our own tasks shows it wins.

Where this corrects the request: a cheaper model is often not cheaper per task. Opus 5.5 at `low` cost about
7% more per solved task than Sonnet 5.5 at its default, and replacing Opus 5.5 with Fable 5.2 as orchestrator
cost 19% more for about the same score. Artificial Analysis points the other way on its own index: Fable 5.2
at `low` scores 54 for $3.10 per task against Opus 5.5 at `high` scoring 52 for $3.46. The vendor's own
defect-search runs also show Fable 5.2 alone scoring 1.6 to 5.9 points above Opus 5.5 at `high`, for 1.5 to
2.4 times the cost. The sources disagree because the workloads differ, which is why the table is a starting
point.

ADR 0022 adds a machine default, `fable`, for a session started with no model named. The lanes above are
unchanged.

### Consequences

* Good: caches survive; lanes are cheap where output is checkable; the table has an expiry date.
* Bad: until this repo has its own eval, every row rests on external measurements.

### Confirmation

Subagent frontmatter pins `model` and `effort`. The status line records a model change inside a session to
the event ledger, and `scripts/lanes` flags that session.

## Evidence

Read 2026-11-09.

* https://platform.claude.com/docs/en/about-claude/models/overview.md — current lineup, IDs, prices
  ($8/$40 Fable 5.2, $4/$20 Opus 5.5, $2/$10 Sonnet 5.5, from $0.10/$0.50 Haiku 5.5 per MTok), "start with
  Claude Opus 5.5 for most workloads" (fetched by the orchestrator).
* https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md — starting
  point, step-down and step-up guidance, orchestrator and advisor pairings, effort curve, re-run-failures
  result (fetched by the orchestrator, first 100k characters; then read in full by the second reader).
* https://platform.claude.com/docs/en/about-claude/pricing — Haiku 5.5 costs more above 100K-token prompts.
* Second reader, 2026-11-09 (`evidence-verifier`): 17 of 18 claims supported. The advisor reason was wrong
  as first written and is restated above; rows 1 and 3 of the table and rules 4 and 6 gained their limits.
* https://code.claude.com/docs/en/model-config.md — aliases `opus`, `sonnet`, `haiku`, `fable` and what they
  resolve to; Claude Code's effort default is `medium` on Fable 5.2 and the 5.5 models, and `high` on Opus 5,
  Fable 5 and Fable 5.1 (fetched).
* https://code.claude.com/docs/en/costs.md — `model: haiku` for simple subagents (fetched). The same page
  says to use Sonnet by default, which conflicts with the platform docs' Opus 5.5 starting point; this ADR
  follows the platform docs because they carry the measurements.
* https://artificialanalysis.ai/leaderboards/models — Intelligence Index snapshot; every figure quoted here
  and in `canon/models.json` was re-checked against the page by the second reader. The version label
  (v5.0.1) comes from Artificial Analysis's methodology pages, not the leaderboard page.
* Cost-optimisation guide bundled with Claude Code 2.1.321 (`claude-api` skill, `shared/cost-optimization.md`)
  — model selection comes after every lever that does not lower the intelligence ceiling (bundled).
