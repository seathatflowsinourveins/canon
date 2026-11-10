@AGENTS.md

## Claude Code

- **Skills:** `adopt-upstream` (add or move a pin), `new-adr`, `landscape-refresh` (re-verify the canon),
  `lane-report` (state of every session).
- **Subagents:** `upstream-scout` (Sonnet; sourced web research) and `evidence-verifier` (Opus; re-checks an
  ADR's claims against its sources). Give a scout the question, not your expected answer.
- **Hooks:** an edit under `canon/` or `docs/adr/` runs `scripts/canon lint`, and an edit to a Python file
  runs ruff on it. Fix what they report before moving on. `guard.sh` can refuse a tool call; do not route
  around a refusal. Session start prints the pin count, any ADR past review and the last upstream check.
- **Before saying a change is done:** run `scripts/ci`.
- **Models:** run orchestration on `fable`, build sessions on `opus` (table in `canon/models.json`). Pick the
  model when the session starts.
- **Planning:** use plan mode. No spec framework is adopted (ADR 0004).
