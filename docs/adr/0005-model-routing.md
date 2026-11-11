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
| Default build session: implementing against a clear plan | `opus` (`claude-opus-5-5`) | medium; high for hard changes; the orchestrator's model for design-heavy work (rule 7) | Vendor "start here" model at its default effort |
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
7. **A build session may be raised to the orchestrator lane's model** (the owner's instruction,
   2026-11-10: Claude workers may also run on that model, its use kept about level with the other
   models' overall). The build row of the table has two raises. `raise_to` names an effort on the same
   model, for a hard change. `raise_model_to` names a lane, `orchestrator`, whose model and effort the
   session takes instead; it names the lane, not a model, so the two cannot drift apart. Limiting the
   raise to design-heavy work is this record's narrowing of the owner's words: a decision record, or a
   state machine on which review rounds keep finding the same family of defect. That is the case in
   hand. On Opus 5.5, modify by replace took two changes (#55, #69) and two more that fixed replace
   defects among others (#99, #107); a session window's account check took four (#97, #105, #110, #114)
   after two on its stop and close (#83, #101). Each landed after its review, and each but the last of
   its group was followed by another fixing defects of the same kind.
   The evidence supports less than the instruction. The vendor names the step, to Fable 5.2 "for
   demanding reasoning and long-horizon agentic work, or when your evals on Claude Opus 5.5 at higher
   effort still fall short", but after an effort sweep: on its SWE-bench Pro subset Opus 5.5 at `xhigh`
   was level with Fable 5.2 at `high` for a little over half the cost. No eval of ours shows a
   shortfall, the effort those ten changes were built at was not recorded, and whether the raise ends
   such rounds is not measured. So rule 2 still comes first, a raise in effort before a raise in model;
   the model is picked when the session starts (rule 1); and the raise stays occasional, as the owner's
   condition asks.

Not adopted:

* **Advisor pairings.** For the current-generation pairs measured (Opus 5.5 with a Fable 5.1 advisor; Haiku
  or Sonnet 5.5 with an Opus 5.5 advisor) the gain was at the edge of noise or negative, at about twice the
  cost. A weak executor can gain a lot (a low-effort Sonnet 5 gained 23 points), but no lane here is one.
* **A second model provider for runtime workers.** Artificial Analysis shows GPT-6.1 Sol (xhigh) at index 52
  for $0.76 per task and Gemini 4 Carbon (high) at index 60. A second provider adds a second SDK, cache
  namespace and set of failure modes; it is adopted only after an eval on our own tasks shows it wins.
  A coding runtime is another matter: ADR 0058 lets an adopted one build on the owner's word, and ADR
  0057's eval, which has not run, keeps or retires it.

Where this corrects the request: a cheaper model is often not cheaper per task. Opus 5.5 at `low` cost about
7% more per solved task than Sonnet 5.5 at its default, and replacing Opus 5.5 with Fable 5.2 as orchestrator
cost 19% more for about the same score. Artificial Analysis points the other way on its own index: Fable 5.2
at `low` scores 54 for $3.10 per task against Opus 5.5 at `high` scoring 52 for $3.46. The vendor's own
defect-search runs also show Fable 5.2 alone scoring 1.6 to 5.9 points above Opus 5.5 at `high`, for 1.5 to
2.4 times the cost. The sources disagree because the workloads differ, which is why the table is a starting
point.

ADR 0059 sets the default for a session started with no model named to the build lane's `opus` (ADR 0022
had set `fable`), pins no alias, so each follows the vendor and a lane's model is what its alias resolved to
on the table's `read_at`, and holds the picker to the four families with Fable at the orchestrator lane's ID.
The table's `claude_code` section holds those settings and its `runtimes` section the other runtimes'
defaults. The lanes above are unchanged.

### Consequences

* Good: caches survive; lanes are cheap where output is checkable; the table has an expiry date.
* Bad: until this repo has its own eval, every row rests on external measurements.

### Confirmation

Subagent frontmatter pins `model` and `effort`. A raised build session (rule 7) is started with the
orchestrator lane's model by its full ID, and `scripts/lanes` shows the model of each session that drew
a status line; nothing yet measures the owner's condition on use. The status line records a model change inside a session to
the event ledger, and `scripts/lanes` flags that session. The machine settings pin no alias (ADR 0059).
`scripts/bootstrap --check` labels an `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` in the installed settings PINNED, or
MISMATCH when it names another model than `canon/models.json`, and an unreadable table UNKNOWN; it labels
MISMATCH a top-level `model` that `canon/models.json` does not name, and checks the allowlist and picker row
against the template. `scripts/test-lanes` fails when the template's model keys differ from the table's
`claude_code` section, so the two move together. The picker's "Fable 5.1" row was an extra option whose
value was `claude-fable-5-1[1m]`; ADR 0059 shows the allowlist hiding it.

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
  Fable 5 and Fable 5.1 (fetched). Read again 2026-11-10: "Unless you set
  `ANTHROPIC_DEFAULT_FABLE_MODEL`, the `fable` alias resolves to Fable 5 in Claude apps gateway sessions and
  to Fable 5.2 everywhere else"; "To pin to a specific version, use the full model name ... or set the
  corresponding environment variable"; the page's own example sets one in a settings file's `env` (fetched).
* https://code.claude.com/docs/en/costs.md — `model: haiku` for simple subagents (fetched). The same page
  says to use Sonnet by default, which conflicts with the platform docs' Opus 5.5 starting point; this ADR
  follows the platform docs because they carry the measurements.
* https://artificialanalysis.ai/leaderboards/models — Intelligence Index snapshot; every figure quoted here
  and in `canon/models.json` was re-checked against the page by the second reader. The version label
  (v5.0.1) comes from Artificial Analysis's methodology pages, not the leaderboard page.
* Rule 7, 2026-11-10. The owner's instruction of that day, as rule 7 gives it. The cost guide above, read
  again that day: "Use Claude Fable 5.2 for demanding reasoning and long-horizon agentic work, or when
  your evals on Claude Opus 5.5 at higher effort still fall short"; Opus 5.5 "at `xhigh` 96.5% for $0.75
  USD, level with Fable 5.2 at `high` for a little over half the cost" (fetched, searched in full). This
  repository's history: the subjects and `Co-Authored-By` trailers of the ten commits (read, `git log`).
* Second reader of rule 7, 2026-11-10 (`evidence-verifier`): the quotation and the trailers held, and
  nothing that reads the table breaks. It regrouped the ten changes by what each fixed, and had the
  guide's effort sweep, the unrecorded effort and the narrowing of the owner's words stated; all above.
* 2026-11-10, with ADR 0059: the machine default, the alias pins and the picker settings above, and the
  table's `claude_code` and `runtimes` sections; the evidence is in that record.
* Cost-optimisation guide bundled with Claude Code 2.1.321 (`claude-api` skill, `shared/cost-optimization.md`)
  — model selection comes after every lever that does not lower the intelligence ceiling (bundled).
