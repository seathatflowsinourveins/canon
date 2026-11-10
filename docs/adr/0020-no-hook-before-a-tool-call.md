---
status: accepted
date: 2026-11-10
decision-makers: repo owner (instruction, 2026-11-10); master orchestrator (Claude)
review-by: 2027-02-08
---

# Rule enforcement: no hook runs before a tool call; the merge gate and permission rules hold the rules

## Context and Problem Statement

A `PreToolUse` hook, `guard.sh`, read the text of every tool call and refused four kinds of thing: a
commit that skipped the commit gate, an install that went around `uv.lock`, anything that named live
trading, and any write in a session marked unattended (ADR 0009, ADR 0011). On 2026-11-10 it was set to
block the call when the hook itself failed. A session that had started in another folder then lost every
tool, the edit that would have repaired it included, until the owner changed the settings by hand. Before
that setting the same session had worked with the guard not running. The owner asked for the hook to be
removed: the work should not have a script in front of every call. What holds the rules instead?

## Decision Drivers

* Work must not stop because a check on the work could not start.
* A rule is held where it cannot be skipped, not where it is easiest to write (ADR 0013, ADR 0017).
* The mechanism the vendor documents for a job comes before a script of this repo's own (ADR 0001).
* Workflow and quality come before added layers of defence (owner, 2026-11-10).

## Considered Options

1. Keep the guard, blocking when it fails.
2. Keep the guard, not blocking when it fails.
3. Remove the guard and hold each rule with what already exists.
4. Replace the guard with permission `deny` rules and the sandbox's network allow-list.

## Decision Outcome

Chosen: **option 3**. `guard.sh`, `unattended-mark.sh` and their two registrations are removed.

| Rule the guard held | Held now by |
|---|---|
| A commit passes the checks | The ruleset on `main` requires a pull request and the statuses `all required checks passed` from hosted CI and `signoff` from `scripts/land` (ADR 0014, ADR 0017). A commit made with the local gate skipped must still pass the same script on GitHub before it can merge |
| Installs follow `uv.lock` | `scripts/ci` runs `uv lock --check` and hosted CI installs with `uv sync --locked` (ADR 0013). The settings pre-approve `uv run` only with `--locked`; `uv sync` and `uv add` ask the owner |
| Paper trading only | Live credentials are kept off this machine, which ADR 0009 called "the real control", so a call to a live host fails at Alpaca. The execution client, written later that day, holds the paper-only check in its configuration: `AlpacaExecutionClientConfig` refuses `paper` other than `True` and any `url_override`, and the client builds alpaca-py's clients with `paper=True` whatever the config holds |
| An unattended run only reports | The task's prompt. `ask` rules apply in every permission mode, so a push, a pin move and an install still wait for the owner; an edit or a commit in a bypass-mode run is held by the prompt alone |

The guard also refused these, and nothing refuses them now: `pip install` and `uv pip install`; `uv run`
without `--locked` in a bypass-mode session; a Python edit that passes `paper=False` or `url_override`;
the variables that switch Alpaca's CLI or MCP server to live; a fetch of a live host; an edit to
`.git/config`. The trading ones fail at Alpaca for want of live credentials. The rest are left to review
and the checks.

1. **No hook runs before a tool call.** `scripts/test-hooks` fails if a `PreToolUse` hook is registered.
2. **No hook stops a session by failing.** None sets `onFailure` to `block`. The hooks that remain give
   feedback after an edit or print at session start; they never decide whether work goes on.
3. **Every registered hook file exists, and every hook file is registered.** `scripts/test-hooks` checks
   both: a path that named no file is how the guard failed.
4. **`ask` rules catch the usual form of a push, a pin move and an install**, in every permission mode.
   The docs say such a rule "isn't a security boundary around the program": another spelling of the
   command gets past it. A prompt is the owner's to answer; an agent does not route around one.
5. **Watch: the sandbox.** Claude Code's docs name it for enforcement "that doesn't depend on the command
   text". It covers shell commands only. It is not adopted: with no live credentials there is nothing
   for it to protect yet. Revisit when the execution client is written. It was, on 2026-11-10 (pull
   request 24), so the revisit is due and is the owner's to close. Its reason, no live credential on
   this machine, was not re-checked for this note (UNVERIFIED). Paper credentials come next, for the
   paper tests.

Option 1 is what failed: `${CLAUDE_PROJECT_DIR}` is "the project root where the session started", so a
session that starts in another folder has no hook file to run. Option 2 is how the guard ran before, and
in that session it did not run at all; the docs add that a timed-out hook "doesn't block the tool call".
Option 4 is two things. Its sandbox half is rule 5. Its `deny` half would be a second text matcher: the
docs call Bash patterns that constrain arguments "fragile" and pair them with the sandbox "when the
restriction must hold".

Supersedes ADR 0009's enforcement paragraph, and in ADR 0011 the `Unattended guard` row, the enforcement
in rule 2 and the guard's consequence. The rest of both stands.

### Consequences

* Good: no cost per call and no lock-out. 749 lines of shell are gone, 260 test cases became 21, and a
  session is held the same way whichever app, host or runtime starts it (ADR 0018).
* Bad: a mistaken command is no longer refused before it runs. An `ask` rule, the checks, or Alpaca
  rejecting paper keys on a live host catch what they cover; the list under the table is covered by none.
* Bad: "Unattended runs report; they do not decide" (ADR 0011 rule 2) is enforced only for a push, a pin
  move and an install. The weekly task's prompt asks for the rest.
* Bad: rule 1 is tested on this repository's settings only. A plugin's hooks module can run before a
  tool call and, where Claude Code's built-in guard is not loaded, as on this machine, approve what a
  `deny` or `ask` rule would stop. The one Orca loads handles no tool call. Sources: ADR 0023.

### Confirmation

`scripts/test-hooks`: 21 cases, rules 1 to 3 among them. `scripts/ci` passes with the two files gone. The
cause of the lock-out was read from that session, which cannot be re-run: the folder it started in holds
three hook files and no `guard.sh`; every call returned `hook error ... blocking because onFailure is
"block"`; the same hook, run by hand with the project directory set to this repository, exited 0. ADR 0011
had already recorded "a guard added mid-session did not act there; the cause is unknown". The weekly
task's prompt no longer names the guard, and the manual guard self-test task is deleted. Not tested: an
unattended run since.

## Evidence

Read 2026-11-10. "Second reader": the `evidence-verifier` fetched each page whole the same day and found
the quoted sentences verbatim.

* https://code.claude.com/docs/en/hooks.md — the `${CLAUDE_PROJECT_DIR}` sentence, and that Claude Code
  "keeps `${CLAUDE_PROJECT_DIR}` where it was" when a session enters a worktree; a hook that "fails in some
  other way, such as not starting" is a non-blocking error "except for hooks whose failure blocks the
  action, for example a hook with `onFailure` set to `"block"`"; the timed-out sentence, followed by "don't
  count on a stalled hook to act as a gate" (fetched; second reader).
* https://code.claude.com/docs/en/permissions.md — "For filesystem and network enforcement that doesn't
  depend on the command text, use sandboxing. To inspect the full command text with your own logic before
  it runs, use a PreToolUse hook."; the "fragile" sentence and the pairing with the sandbox; the sentence
  in rule 4, with `git -C . push origin main` as a form `Bash(git push *)` does not stop (fetched; second
  reader).
* https://code.claude.com/docs/en/permission-modes.md — "Deny rules block in every mode, including
  `bypassPermissions`"; "Tools matched by an explicit ask rule" under "Actions no mode auto-approves"
  (fetched; second reader).
* https://code.claude.com/docs/en/sandboxing.md — "The operating system enforces the sandbox boundary on
  the running process"; "The sandbox covers shell commands only"; "`allowedDomains` doesn't limit
  WebFetch" (fetched; second reader).
* GitHub REST API, this repository's rulesets — one active ruleset on `main`, no bypass actors:
  `deletion`, `non_fast_forward`, `pull_request`, `required_linear_history`, and `required_status_checks`
  naming `signoff` and `all required checks passed` (fetched; second reader).
* `scripts/ci` — `uv lock --check`; `.github/workflows/ci.yml` — `uv sync --locked`;
  `.claude/settings.json` — the `allow` and `ask` lists (read; second reader).
* `packages/canon-alpaca/README.md` — "There is no execution client"; `execution.py`, "Not started" (read).
  After pull request 24 (`12f19a0`), the same day: `AlpacaExecutionClientConfig.__init__` raises on
  `paper` other than `True` and on any `url_override`; the client's header says it is "always built with
  `paper=True` and never with `url_override`" (read; 175 tests passed and 2 paper tests were skipped).
* `git show 3e280e6:.claude/hooks/guard.sh` — 749 lines, and its header for the list under the table;
  `scripts/test-hooks`, 260 cases there and 21 here (read, run; second reader).
* The session of 2026-11-10 that lost its tools, and the owner's instruction to remove the hook — this
  repository's own history that day; not re-observable.
* What the desktop app's scheduled tasks do with a call that would prompt: not fetched, UNVERIFIED.
* Second reader, 2026-11-10: every quote held. It corrected what a bypass-mode session still asks, the
  wording of rule 4 and of the first table row, the reason for leaving option 4, and the scope of ADR
  0009's phrase, and it supplied the list of refusals that nothing holds now. All are fixed above.
