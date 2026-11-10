@AGENTS.md

## Claude Code

- **Skills:** `adopt-upstream` (add or move a pin), `new-adr`, `landscape-refresh` (re-verify the canon),
  `lane-report` (state of every session).
- **Subagents:** `upstream-scout` (Sonnet; sourced web research) and `evidence-verifier` (Opus; re-checks an
  ADR's claims against its sources). Give a scout the question, not your expected answer.
- **Hooks:** an edit under `canon/` or `docs/adr/` runs `scripts/canon lint`, and an edit to a Python file
  runs ruff on it. Fix what they report before moving on. No hook runs before a tool call (ADR 0020); a
  permission prompt is the owner's to answer, so do not route around one. Session start prints the pin
  count, any ADR past review and the last upstream check.
- **Before saying a change is done:** run `scripts/ci`.
- **Models:** run orchestration on `fable`, build sessions on `opus` (table in `canon/models.json`). Pick the
  model when the session starts.
- **Orchestrate without being asked.** Start the lane a task needs yourself, on the model the table gives
  that lane: `upstream-scout` for research, `evidence-verifier` for a decision record, `/code-review` for
  code, each before landing (ADR 0017), and report what it found. Do not wait for the owner to launch a
  review. The one exception is `/code-review ultra`: it is billed and only the owner can start it, so say
  when a change is large enough to deserve it and give the exact command.
- **Planning:** use plan mode. No spec framework is adopted (ADR 0004).
