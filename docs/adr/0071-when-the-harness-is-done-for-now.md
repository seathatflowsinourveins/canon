---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-11
review-by: 2026-12-11
---

# When the harness is done for now: ten gaps in the order they block the roadmap, six checks to pass, then one maintenance lane once they hold

## Context and Problem Statement

On 2026-11-10 the owner asked which parts of the harness are still missing, so that the work can turn to
the roadmap's goal, and whether its layers are final. None is final: each sits in a record with a `review-by` date (ADR 0002), and the layers
as they stand are listed under Confirmation. But nothing says when the harness has had enough work, so
harness lanes compete with the roadmap's. This record lists what is missing, in the order it blocks
roadmap steps 6 to 8, and sets a stopping point after which harness work is capped. When a single path
has been reviewed enough is ADR 0073, and how a runtime is enhanced is ADR 0072.

## Decision Drivers

* The north star is roadmap steps 6 to 8 (`docs/roadmap.md`). Harness work that does not unblock them waits.
* Each item of the stopping point is a check that passes or a record accepted, not a judgment.
* Nothing here loosens a gate already held: review before landing, paper only, no credential in context
  (ADR 0009, ADR 0017, ADR 0029).

## Considered Options

1. No stopping point: harness work continues as findings arrive (the state).
2. A stopping point made of checks and accepted records, then a fixed cap on harness lanes.
3. Freeze the harness now and take every lane to the roadmap.

## Decision Outcome

Chosen: **option 2**. Option 1 has no end: every review round on 2026-11-10 found new defects (ADR
0073). Option 3 would run step 8's two accounts and unattended windows with the containment gap open. The
cap was accepted by the lead under the owner's delegation of 2026-11-11, and takes effect when the six
checks of the stopping point hold; until then, gaps 1 to 5 are worked as they stand.

**The gaps, in the order they block the roadmap.**

1. *One paper account and one data stream.* Steps 6 and 8 need a window that does not share the account
   with the tests, and parallel strategies on one stream (ADR 0069, ADR 0070). ADR 0069's tasks are on
   `main` (#154), and no task gives account B's pair to a window yet. Closes when
   `mise run paper:check windows` passes and the windows run on account B.
2. *No containment for runtimes started with no sandbox and no approvals.* ADR 0025 covers Claude Code only.
   Its first trial did run (Confirmation there); what has not run is the change that turns the sandbox on.
   Codex, Muse, Antigravity and DeepSeek Harness start with Orca's bypass flag (`AGENTS.md`, "Orca").
   Claude Code's docs say: "Always run `--dangerously-skip-permissions` sessions inside a container, a VM,
   or the sandbox runtime", and the Bash sandbox on its own "is not sufficient for fully unattended runs".
   Codex's and Muse Code's docs say the same of their bypass flags (UNVERIFIED here). Every reader here runs
   that way, and two readers on 2026-11-10 showed the cost. One left a `mise.toml` in the root of `/tmp`
   that changed the self-tests running there (#134). `scripts/test-lanes` now checks at its end that
   this clone's `core.hooksPath` and credential helper are as it found them (#129); which reader's run
   changed them is not in the commit text (UNVERIFIED). Closes with a record, per
   runtime, of the containment its own docs offer, and ADR 0025's turn-on change landed or dropped.
3. *Coordination that lives in sessions.* Claude Code's session-scoped tasks stop when the session exits,
   and recurring ones expire after seven days (scheduled-tasks docs). Handovers are written by hand. Closes
   when no standing duty depends on a session's timer: each one runs from launchd, the desktop scheduler or
   the hosted workflow, and shows in `scripts/automation status`, and a handover is a file in the
   repository, not a message. The nearest documented pattern keeps a progress file and a feature list in
   git, which each new session reads first (Anthropic, "Effective harnesses for long-running agents").
4. *Plugins and skills added to a runtime.* An addition is adopted as itself, runs as a named
   configuration, and is kept or dropped by the two evals (ADR 0072). It waits on gap 2.
5. *The open behaviour-changing findings of 2026-11-10.* Not yet fixed on `main` (5df7c9f): of the two
   reviews after #135, Codex's 1 to 6 and Muse's 1 to 3 (Codex's 4 is Muse's 2); of OmniRoute's reviews,
   Antigravity's 1 to 7 and 9 and Codex's 1 to 3 and 7; and these sweep entries:
   `execution.py`'s cash default (M8), `bootstrap`'s `check || true` (M31, and Antigravity's OmniRoute 11),
   `bootstrap`'s `paper_row` reading any `security` failure as absent (M37),
   `py-lint.sh`'s `cd || exit 0` (M53), the relative records path when `getconf` fails (M61) and the
   otel row with an unread binary (M2). Lower: bare `assert` guards (M12, M15, M25), `lanes`' dropped
   malformed line (M44), `ci`'s unchecked argument (M68), a constant test (M47), and the hooks' fingerprint
   with no pipefail (M51, M70). The sweep's other entries are either landed (#128, #129, #132, #133,
   #135, #140) or were not checked one by one.
6. *The lock and deadline tests only this Mac runs.* GitHub's Ubuntu runner has no `lockf`, `plutil`,
   `launchd` or `security`, so `scripts/test-lanes` skips those cases there and says so
   (`docs/architecture.md`, "Where code runs"). This is accepted as a known limit: the commit gate on this
   Mac runs them. A hosted macOS job for the lock and deadline cases is queued; it stays off the
   stopping list.
7. *Benchmark entries not drafted from fixes.* ADR 0064's open item: fix messages report a failing test in
   free prose, with no marker a script can read. Closes with a trailer that names the case and the failing
   test, read by `evals/review`.
8. *The week's usage against its limit.* `scripts/lanes` shows spend per session. One documented source
   gives the weekly window: the status line's `rate_limits.seven_day.used_percentage` and `resets_at`, for
   subscribers or behind a Claude apps gateway, after the first response. It gives a percentage, not the limit itself, which only a
   gateway's `spend_limit.limit_usd` states. Nothing here records it. Closes when the status line keeps it
   and `scripts/lanes` shows it.
9. *A spend bound per lane, and one command that stops every lane.* Neither exists here. Claude Code
   documents a per-run budget in print mode (`--max-budget-usd`, CLI reference), and two of eight
   harnesses compared have a stop command (that one UNVERIFIED here). Each is its own change.
10. *The environment manifest.* The table under "Where code runs" landed (#126). A manifest proposal that
   goes further is not on `main` and was not among the saved reports: UNVERIFIED. It is its own record.

**The stopping point.** The harness is done for now when all six hold:

1. `mise run paper:check windows` exits 0, and the session windows run on account B (gap 1, ADR 0069).
2. The containment record for the bypass-mode runtimes (ADR 0034) is `accepted`, and ADR 0025's Confirmation records
   its turn-on change as landed or dropped (gap 2).
3. `scripts/automation status` shows every standing duty healthy, and none depends on a session timer
   (gap 3).
4. Gap 5's behaviour-changing findings each have a fix on `main` with a test that failed before it, or a
   record saying why not.
5. ADR 0073 is `accepted`, and each high-effort path's last count under its rule 5 is in its fix's message.
6. `scripts/ci` passes here and on the hosted runner.

Gaps 6 to 10 are not on that list. Gap 6 is queued, and 7 to 10 are worth doing but do not
block steps 6 to 8.

After that, harness work is **one lane at a time**, and the other lanes go to roadmap steps 6 to 8 in the
order ADR 0068 proposes. Maintenance means: a pin move or a Dependabot pull
request through `adopt-upstream` or `scripts/land --pr`; the drift issue's items; re-verifying a record past
its `review-by`; a fix to harness code for a finding that changes behaviour; the weekly landscape refresh;
and a path's re-entry review under ADR 0073. Anything else is not maintenance and needs a record first: a new
automation, runtime, eval, check or layer.

**Where the vendors disagree with this harness.** Claude Code's best-practices page says: "If something has
to happen every time, make it a hook, a deny rule, or a managed setting." Codex documents a `PreToolUse`
hook that can deny a call. ADR 0020 runs no hook before a tool call, for the failure it records: a guard
set to block when it failed left a session with no tool, and the owner wants none. This record does not reopen ADR
0020. The risk those hooks answer is the one gap 2's containment answers.

*Amended 2026-11-11 by ADR 0074:* stopping check 2's containment record is taken from the vendors' own
documentation, with no end-to-end trial of each runtime; check 5 counts the trading paths only; gaps 7 to
10 stay optional.

*Amended 2026-11-11 by ADR 0078:* gap 3's handover is the coordinator's Orca Run and git, not a file in
the repository; recurring duties run from launchd, the desktop scheduler or the hosted workflow.

### Consequences

* Good: the harness has a finish line made of checks, and the lanes have a cap after it.
* Bad: until stopping item 2 holds, every bypass-mode reader runs uncontained, as it does today.
* Bad: gaps 7 to 10 wait behind the roadmap, so the week's usage and a stop command stay manual.

### Confirmation

The layers as they stand on `main`. None is final; each is held by its records and their `review-by`
dates, and the stopping point stops them growing, not changing.

| Layer | State | Records | Shown by |
|---|---|---|---|
| Registry and pins | 52 upstreams pinned; drift compared daily | ADR 0001, 0016 | `scripts/canon list`, `scripts/canon check` |
| Records with evidence and expiry | MADR with Evidence and `review-by`; the brief lists the `proposed` and the expired | ADR 0002 | `scripts/canon lint` |
| Merge gate | one check script, an independent review note, the `signoff` status | ADR 0013, 0014, 0017, 0019 | `scripts/ci`, `scripts/land --reviewed` |
| Cockpit and workers | Orca; Claude builds; Codex, Muse, Antigravity, DeepSeek Harness read; another runtime may build under terms | ADR 0018, 0021, 0043, 0045, 0058 | `scripts/bootstrap --check` |
| Reviewer benchmark, build eval | two benchmark runs scored; the build eval's first trial not run | ADR 0046, 0057 | `evals/review/runs/`, `evals/build/harness.py` |
| Telemetry and rates | local Collector; per-session spend and output rates | ADR 0007 | `scripts/lanes --rates` |
| Standing automations | commit and push hooks, canon watch, watchdog, weekly run, paper windows | ADR 0011, 0026, 0050 | `scripts/automation status` |
| Drift issue | one locked `canon-drift` issue kept by the hosted watch | ADR 0064 | `scripts/drift issue` (hosted), the session brief |
| Native defaults, updates | each runtime's default model and effort; Claude Code and Antigravity update themselves | ADR 0059, 0065 | `scripts/bootstrap --check` |
| Paper and simulation | one paper account, bounded windows; backtests through the ledger and gate | ADR 0029, 0044, 0050, 0010, 0035 | `mise run paper:check`, `mise run paper:session` |

The stopping point is reached when each of its six checks passes; the record that reports it moves this
one to `accepted` or supersedes it.

## Evidence

Read 2026-11-10 and 2026-11-11.

* https://code.claude.com/docs/en/sandbox-environments.md — "Always run `--dangerously-skip-permissions`
  sessions inside a container, a VM, or the sandbox runtime"; the sandboxed Bash tool "is not sufficient
  for fully unattended runs"; the sandbox runtime "is a beta research preview" (fetched in full).
* https://code.claude.com/docs/en/cli-reference.md — `--max-budget-usd`, print mode only (fetched by a
  second reader on 2026-11-11, a correction to the research report's citation).
* https://code.claude.com/docs/en/best-practices.md — "If something has to happen every time, make it a
  hook, a deny rule, or a managed setting" (fetched, searched in full).
* https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents — a
  `claude-progress.txt` log, a JSON feature list whose `passes` field is the only one agents change, and
  "Read the git logs and progress files to get up to speed" (fetched, summarised by the fetch tool).
* https://learn.chatgpt.com/docs/agent-approvals-security, https://learn.chatgpt.com/docs/hooks.md and
  https://dev.meta.ai/docs/muse-code/permissions — Codex's bypass flag "not recommended" and its
  `PreToolUse` hook; Muse Code's `--yolo` only in a disposable container; and Claude Code's
  `--max-budget-usd` (a research report of 2026-11-10 that
  fetched them; not fetched here, UNVERIFIED by this record). Codex's docs moved from
  `developers.openai.com/codex` (now a 308 redirect, run); a search of `docs/` and `canon/` finds no record
  citing the old host, and ADR 0046's `developers.openai.com/api` page still answers 200 (run).

* https://code.claude.com/docs/en/scheduled-tasks.md — "Tasks are session-scoped"; "Closing the terminal
  or letting the session exit stops them firing"; "Recurring tasks automatically expire 7 days after
  creation"; durable options are cloud, desktop scheduled tasks and GitHub Actions (fetched, summarised by
  the fetch tool, quotes checked in its output).
* https://code.claude.com/docs/en/statusline.md — `rate_limits.five_hour` and `seven_day` with
  `used_percentage` and `resets_at`; "appears only for claude.ai Pro, Max, and Ultra subscribers, or
  behind a Claude apps gateway"; `spend_limit.limit_usd` only behind a gateway (fetched, searched in full).
* ADR 0025, Confirmation — the first trial, 2026-11-10, and what the turn-on change must still try (read).
* ADR 0064, "A fix drafting its eval entry" — the open item (read).
* `docs/architecture.md`, "Where code runs, and what it has" — the hosted runner's missing macOS tools
  (read).
* Commits on `main` from ec6bd5e to 5df7c9f (`git log`) — #123's Summary ("twelve defects, all one
  question asked badly"); #128, #129, #132, #133, #134, #135, #140 and #141 for what landed of the sweep
  and the rounds (read).
* The merged sweep of four readers (71 entries) and the second readers' reports of 2026-11-10 (the ones
  ADR 0073 counts), kept outside the repository; the open sweep entries were checked by searching `main` at 5df7c9f for each
  entry's line (run).
