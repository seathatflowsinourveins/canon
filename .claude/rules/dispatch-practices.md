# Dispatch practices

A coordinator's brief to a builder or reader follows these eight. The first six are read from superpowers at
`22f0b463` (registry `reference`, read for patterns; `scripts/canon src superpowers`; `SDD` below is
`skills/subagent-driven-development/`).

1. The brief names the base commit, which superpowers records before dispatch for the review and
   fix-round diffs (`SDD/SKILL.md:200-201`), and passes artifacts as paths and commits, never as copied
   history (`SDD/SKILL.md:183-185`).
2. Every dispatch names its model (`SDD/SKILL.md:160-162`) and, as Canon adds, its effort (models and
   efforts from `canon/models.json`, ADR 0005).
3. A builder or reader starts no agent of its own; the coordinator holds every review seat
   (`SDD/SKILL.md:225-230` for the implementer, `SDD/task-reviewer-prompt.md:55-62` for the reviewer).
4. The builder writes its evidence in a report file, and its message is a short summary with that path
   (`SDD/SKILL.md:216-219`).
5. A reviewer ends with what it declined to judge, one line each with the reason, or says it set
   nothing aside (`skills/requesting-code-review/code-reviewer.md:43-48`).
6. A fix round carries a covering test, named with its command and output
   (`SDD/references/fix-loop.md:36-42`).
7. A subagent that only searches or looks something up is given `model: sonnet` or `model: haiku`, not
   the session's model. Claude Code's docs advise a smaller model for simple subagent tasks
   (https://code.claude.com/docs/en/costs.md, "Choose the right model" and "Delegate verbose
   operations to subagents"; read 2026-11-11). The built-in Explore agent is not cheaper by itself: it
   runs on the session's model, or on Opus when the session runs Fable
   (https://code.claude.com/docs/en/sub-agents.md, "Built-in subagents"), so a dispatch to Explore
   names the smaller model too.
8. A change on `scripts/land`'s `TRADING_PATHS` is landed by a Claude lane started for the landing, in
   the change's workspace, on the build lane's model; the builder, Claude or another runtime, stops at
   its report. Before that lane starts, the coordinator merges `advisorModel`, set to `canon/models.json`'s
   `claude_code.advisor.final_landing`, into the `.claude/settings.local.json` at the workspace's own root,
   never the main checkout's, which every canon session reads and which wins where both set a key; that
   Claude Code reads the worktree's own file beside it rests on one probe (ADR 0141). Once the lane runs,
   the coordinator reads the advisor model its first request carried, from the transcript's request-shape
   entry, that field alone, and names in its report a lane whose request carried another or none. The
   lane reproduces the builder's tests, runs the review ADR 0017 requires and lands; its subagents
   inherit the advisor. When it has landed or stopped, the coordinator removes the key before any other
   lane starts in that workspace. No session runs `/advisor`, which saves its pick to the user settings
   every session reads (ADR 0141; https://code.claude.com/docs/en/advisor.md, settings.md and
   settings-reference.md, read 2026-11-13).

Where Canon differs, its own limit holds: one fix round, then the task returns to its builder (ADR 0045
and the lead's order), not superpowers' five (`SDD/references/fix-loop.md:21`). No skill is enabled:
superpowers stays `reference` in `canon/upstreams.json`, and mattpocock's `wayfinder` and `implement-spec`
are not adopted (they work through an issue tracker and write handoffs outside the project).
