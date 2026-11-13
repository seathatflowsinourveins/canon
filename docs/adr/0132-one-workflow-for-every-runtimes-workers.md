---
status: proposed
date: 2026-11-12
decision-makers: lead orchestrator (Claude), on the owner's order of 2026-11-12 that every runtime's workers get one workflow (harness, skills, clean landing, each part as its upstream documents it); repo owner ratifies
review-by: 2026-12-12
---

# One workflow for every runtime's workers: standing terms as repository skills, the read-back before a worker counts, a standing release, and the waits shown; the one-shot dispatch stays open until a probe shows Orca can host it

## Context and Problem Statement

On 2026-11-12 the Codex, Muse, Antigravity and DeepSeek Harness workers lost time to the workflow, not to the work.
- A mail ended a builder's turn (log 16:44:12). Eight Codex builders took the rate-limit prompt's offer of `gpt-6-luna` on that turn (log 17:02:24).
- Workers stood unseen at a prompt: the ADR 0057 eval workers waited 13 to 20 minutes at mise's trust prompt (log 21:44).
- A stopped worker took its run with it (log 19:54:59).
- Cleanup was by hand: 67 of 426 worktrees removed (log 21:19:49), and 10 idle Codex sessions stopped (log 21:11:58).

Each brief retyped the same terms. The owner ordered one workflow, each part from what its upstream documents.

## Decision Drivers

* Every runtime documents the means it gives a worker. The scout tables (Evidence) show they mostly agree.
* ADR 0079 rule 3, as amended on the evening of 2026-11-11: workers of the other runtimes build the open gaps, and Claude keeps the high review, the paper firing, landing and credential-handling code. Nothing here changes who builds what.
* Rule 10 of `AGENTS.md`: this is off the trading path, so it gets the smallest documented form.
* No pre-tool hook (ADR 0020). No guardian key: the guardian question stays open.

## Considered Options

1. **Keep briefs and hand cleanup as they are.** Tonight's frictions recur.
2. **Amend ADR 0058 and ADR 0059 only.** Those records hold the worker's terms and its defaults. Release, read-back and waits are not in either record, and their scope would blur.
3. **One record that maps each property to its upstream means, landed as small units.** The open parts stay open by name.

## Decision Outcome

Chosen: **option 3**. This record is `proposed`. Units 1 to 6 are documented means and may land one at a time, each with its own review. Unit 7 is open until its probe runs.

1. **Standing terms as repository skills.**
   - Contents: the builder's terms, the reader's terms and the report form (base commit; what changed or was found; tests with counts; what was declined). A builder writes it to `REPORT-<unit>.md` at its worktree's root, untracked; a reader, which writes no file, gives it in its final message.
   - Location: skills in `.agents/skills/<name>/SKILL.md`, in the pinned `agentskills-spec` format.
   - Who reads that folder: Codex (pin `ext/skills/src/host_roots.rs:27-28`, read this session), DeepSeek Harness (pin), Antigravity (pin `CHANGELOG.md:463`, read by the verifier this session) and Muse (its docs, summarised, UNVERIFIED verbatim). All four also read `AGENTS.md`. Muse and Codex do so only for a trusted workspace (Muse's docs, summarised; Codex pin `core/src/agents_md.rs:64-66`).
   - `AGENTS.md` keeps one pointer to the skills.
   - Briefs name the skill instead of retyping its terms.
   - Check: `scripts/canon lint` on each `SKILL.md` and on the AGENTS.md budget, with cases in `scripts/test-canon` ("agent skills") and `scripts/test-agent-skills-blind`.
   - Who builds it: under ADR 0079 rule 3 as amended on 2026-11-11, a worker of another runtime may. The amendment keeps with Claude only the high review, the paper firing, landing and credential-handling code, which the lead reads as lifting rule 4's exclusions for building (ADR 0079 says only that rule 3 is reversed for building, :65-67, and :58 still names dot-directories, `AGENTS.md` and `CLAUDE.md`). Unit 1 edits all three, so this reading is the lead's to keep or reverse. These files configure every runtime that opens the repository, which was the reason for that exclusion (ADR 0058 rule 4). So the checking lane reviews them at high effort.
2. **What a worker of another runtime loads beyond those skills is checked, not assumed.**
   - Muse also reads `.claude/skills` and `.codex/skills`, and the user's `~/.claude/skills`; DeepSeek Harness reads `CLAUDE.md` (scout; dsh pin `packages/context/agent-instructions/README.md:67`). Both hold Claude-lane text. The probe covers the user-level folders too.
   - The unit's lane runs one reader worker of each and records what it lists. Only then is anything moved.
   - Which of Canon's files reach each runtime is UNVERIFIED until this probe runs, the three skills unit 1 adds included.
3. **The read-back before a worker counts.**
   - Model, effort and tier are read from the worker's own first record before the worker is counted. A mismatch stops it.
   - Codex: `thread_settings_applied` in its rollout, which a new thread writes after its first turn's context (pin `core/src/session/mod.rs:5270-5285`; seen in a real rollout this session, after `session_meta` and `turn_context`). A snapshot copied from another thread keeps its original owner's `thread_id` (`protocol/src/protocol.rs:2442-2444`), so the read-back compares that id with its own thread's and skips a foreign one. The tier read there is the configured one: Codex omits from requests a tier the model does not list (`core/src/session/mod.rs:1230-1246`). The pin emits the request's `service_tier` and `ttft_ms` on `codex.sse_event` (`otel/src/events/session_telemetry.rs:1142-1157`); the local collector drops both today, and unit 3 has it keep them. No script reads those two yet. Account id and email stay dropped.
   - Antigravity: UNVERIFIED. Its changelog says `-p "/model"` and `/effort` return structured JSON and adds a typed stream-json `init` event (pin `CHANGELOG.md:869,923`); whether those name what a worker actually ran is for the unit 7 probe.
   - DeepSeek Harness: over ACP, where effort is set. Muse: its `--json` events have no documented fields, so Muse is a recorded limit.
   - A worker that has not produced its first record has not started. That includes one held at a shell prompt before its agent runs, as the eval workers were at mise's trust prompt. Orca reported those as failed at `agent_readiness`, not as waiting.
4. **Codex sub-agents.**
   - `home/.codex/defaults.toml` is to set `agents.default_subagent_model` and `agents.default_subagent_reasoning_effort` to the table's row (`canon/models.json`). A spawn that names no model then takes the table's, but the pin applies the default only "when the spawn call does not select one" (line 778), so an explicit spawn can still choose another model. The documentation is summarised, UNVERIFIED verbatim; the keys are at pin `config/src/config_toml.rs:778,780`, read this session.
   - Each key gets a `--check` row.
5. **The waits and the model shown.**
   - `scripts/lanes` reads `orca worktree ps --agent-wait --json` (pin `cli/specs/core.ts:114`) and shows each worker parked on a prompt, and since when. Its evidence is a hook, the prompt text or the title. Whether it covers a prompt shown before the agent starts, such as mise's, is UNVERIFIED; unit 3 covers that case.
   - It shows the model and tier for each Codex worker. That comes from the read-back, and from OFF-MODEL once the tier unit lands.
6. **A standing release under the stop rule.**
   - Orca keeps open every worker started in a new worktree (pin `orchestration-worker-specs.ts:45`), and every terminal dispatch is told "Do not exit the shell" (preamble `:136`, `worker-dispatch-preamble.ts:173`).
   - So after the report is saved, one step does three things: list `worker-list --terminal-state reclaimable`, release, then `orca worktree rm` for a clean tree.
   - That step reads the worker's process tree and output files in the same step, so a detached run is never closed with its terminal.
   - `scripts/lanes prune` stays for landed branches.
   - The release runs as one batch, just after GitHub's hourly reset, never in drips. On 2026-11-12, ten `orca worktree rm` calls at 22:02-22:03 were followed within 90 s by about 600 REST calls on the landing account's core budget, about two per remaining worktree, and none came through gh (coordinator's measurement; which process made them is UNVERIFIED, likely Orca).
7. **Open: a one-shot dispatch.**
   - Each runtime documents a run to completion with machine-readable output:
     - `codex exec` with `-o` and `--output-schema` (pin `exec/src/cli.rs:53-54,79`);
     - `muse exec`;
     - `agy -p`, which exits 3 on a timeout (pin `CHANGELOG.md:100`);
     - `dsh --profile headless`.
   - Such a worker has no screen, so no mail can steer it and no stray Enter can accept an offer.
   - Unknown: whether Orca's `worker-start` can host one, and with which account home (the scout found no flag for either).
   - What would close it: one probe per runtime, reading a trivial task and reporting.
   - Until then, the rule of no mail to a running worker stands. It is grounded in pin `terminal-wait-settlements.ts:118-120`: "agents queue or steer text sent mid-turn".

Recorded limits, where an upstream documents nothing:
- tokens per second for Muse and DeepSeek Harness. Antigravity's headless page (https://antigravity.google/docs/cli/headless, read by the verifier this session) lists `usage.output_tokens` and `duration_seconds`, so a wall-clock rate can be derived there;
- Antigravity has no prompt-file or message-file flag; it takes the prompt on stdin (`--input-format`) and writes to stdout (its headless page, summarised);
- DeepSeek Harness headless has no model or effort flag (it has one through `dst` or ACP);
- Codex's backend-banner model switch has no config key (pin `tui/src/chatwidget/backend_banners.rs:82`, applied at `tui/src/app/backend_banner_fallback.rs:98`). The rate-limit prompt that moved the eight builders does have one, `notice.hide_rate_limit_model_nudge` (pin `config/src/types.rs:1077`), and the Codex tier unit sets it;
- the cockpit's polling shares the landing account's GitHub quota, and what Orca's sidebar polls is the owner's setting, not a session's;
- one dispatch cannot set an account, since `orca account select` is per host (pin `account.ts:44,48`).

No extension from Codex's own source is adopted for this. `github.com/openai/skills` reads "This repository is deprecated" and points to OpenAI's plugins repository (fetched by the evidence-verifier this session). Nothing upstream supplies unit 1's Canon-specific terms. `skill-creator` ships with the pinned `codex-cli` and needs no adoption. The question is reopened once unit 2 shows what each runtime loads.

### Consequences

* Good:
  - A brief shrinks to the task, the paths and the skill's name.
  - A model switch, a prompt or a leftover shows within the step that should catch it.
* Bad:
  - Seven units of glue.
  - Skills read by four runtimes must stay in the common subset of the spec. Antigravity treats `name` as optional, while DeepSeek Harness requires it.
* Leaving: delete `.agents/skills/` and the pointer, the rows and the lanes column; briefs carry the terms again.

### Confirmation

* `scripts/canon lint` checks each skill's front matter in a subset of YAML it states: the spec's field table, the name rule, the description and compatibility limits, that `metadata` is a mapping and the other fields are scalars, and no field twice. It also checks the 32768-byte budget of the AGENTS.md files on each path Codex reads. The lint hook runs it on an edit to either; it is a Claude Code hook, so another runtime's edit is caught by the lint in `scripts/ci --staged` at commit (ADR 0138); the self-tests of that lint run at landing. The pinned `skills-ref` validator is not adopted: running it would be an adoption of its own. The subset is a known gap against real YAML: it refuses some valid forms (a multi-line quoted scalar, a flow sequence) rather than guess, and it holds an unquoted null or number to be no string, as `skills-ref` does. Adopting `skills-ref` through `adopt-upstream` would close that gap.
* A reader writes no file in its worktree; its findings go in its final message (`canon-reader`), so the clean-worktree check of ADR 0045 holds.
* For each unit: its check row or `scripts/test-lanes` case, named in its list entry.
* The coordinator's digest counts workers stopped at the read-back, prompts shown and worktrees released.

## Evidence

Read 2026-11-12. Scout reports are saved in the coordinator's private review folder and are named here by file. Their WEB quotes came through a summariser.

* `scout-orca-worker-lifecycle.md`, Orca pin d1466f6017c4 — start, mail, finish, release, agent-wait, account per host — PIN lines; `orchestration-worker-specs.ts:45,118`, `core.ts:89,114`, preamble `:136-137` and `terminal-wait-settlements.ts:118-120` were re-read at the pin this session.
* `scout-codex-worker-workflow.md`, codex-cli pin 0a94398fa60c — `exec`, AGENTS.md (`core/src/agents_md.rs:43,45`; `project_doc_max_bytes = 32768` at `config/defaults.toml:9`), hooks (`hooks/src/lib.rs:29-44`), telemetry — resolved; those lines and `exec/src/cli.rs:53-54,79` were re-read this session. The scout did not find the skill root discovery; this session found it at `ext/skills/src/host_roots.rs:27-28` (`.agents`, `skills`).
* https://learn.chatgpt.com/docs/agent-configuration/subagents.md and https://learn.chatgpt.com/docs/build-skills.md — the sub-agent model keys and `.agents/skills` — UNVERIFIED (summarised by the scout).
* `scout-other-runtimes-harness.md` — Antigravity pin 3e9fbcc9773e (`CHANGELOG.md:100` timeout exit 3, `:226` AGENTS.md and GEMINI.md, re-read this session); deepseek-harness pin 1f81c047fc48 (skill-filesystem and agent-instructions READMEs, resolved by the scout, not re-read); Muse has no pin.
* https://antigravity.google/docs/skills, https://dev.meta.ai/docs/muse-code (`/extending`, `/configuration`) — skills folders, `.claude/skills`, AGENTS.md after workspace trust, `muse exec` — UNVERIFIED (summarised by the scout).
* `scout-codex-usage-metering.md` — the limit message at pin `protocol/src/error.rs:744-757` — resolved by the scout.
* Coordinator's log, 2026-11-12 16:44:12, 17:02:24, 19:54:59, 21:11:58, 21:19:49, 21:44 and the 21:53 note — the frictions and counts — read this session.
* `canon/upstreams.json` ids `agentskills-spec`, `orca`, `codex-cli`, `muse-code`, `antigravity-cli`, `deepseek-harness`, `dsh-tui`, `otelcol` — read this session.
* evidence-verifier pass, 2026-11-12 about 22:35: 2 must-fix (ADR 0079 rule 3 as amended; the unsupported premise correction) and 7 should-fix, all applied in this text; the log citations and the 13-20 minute wait corrected.
