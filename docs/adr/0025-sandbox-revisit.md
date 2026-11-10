---
status: accepted
date: 2026-11-10
decision-makers: repo owner (by delegation, 2026-11-10); coordinator (Claude)
review-by: 2026-12-10
---

# Claude Code's sandbox, revisited: part of it is adopted, subject to a trial that costs no lane a stop

## Context and Problem Statement

ADR 0020 rule 5 left Claude Code's sandbox unadopted, "with no live credentials there is nothing for it to
protect yet", to be revisited once the execution client was written. It was, on 2026-11-10 (pull request
24). What would the sandbox hold in this repository's sessions as they run today, what would it cost the
workflow, and is it adopted?

## Decision Drivers

* A rule is held where it cannot be skipped; work must not stop because a check could not start (ADR 0020).
* Every lane is seamless: one that follows a skill as written does not stop for a prompt (owner, 2026-11-10).
* Paper trading only, and no live credential within an agent's reach (ADR 0009).
* The mechanism the vendor documents comes before a script of this repo's own (ADR 0001).

## Considered Options

1. Leave the sandbox unadopted.
2. Adopt it for shell commands with a network allow-list (ADR 0020's option 4).
3. Adopt part of it: the filesystem boundary, no retry outside it, and a deny-list of Alpaca's live hosts,
   which alpaca-py 0.45.0 names as `api.alpaca.markets` and `broker-api.alpaca.markets`.
4. Put a boundary around the whole Claude Code process: the sandbox runtime, a container or a VM.
5. Leave bypass mode: Orca's Manual launch, with the sandbox approving shell commands.

## Decision Outcome

Chosen: **option 3**, by the coordinator under the owner's delegation. It is the one option that holds
"paper only" for shell commands by the operating system and not by command text, which is what rule 5 kept
the sandbox for, and in the trial it added no prompt to a bypass-mode session.

**This record turns nothing on.** Turning it on is a separate change, after every worker of this run has
settled, tried in one workspace first. It lands only if `scripts/ci` and `scripts/land` run to the end
with it on, `scripts/land` as an excluded command, with no new prompt and no failure, each allowance and
exclusion written down with its reason. If a lane stops or fails there, the change is dropped,
Confirmation says so, and the state is option 1.

What has changed since ADR 0020, each checked. The adapter places and cancels orders, on paper only: its
config refuses anything else and the client is built with `paper=True` (ADR 0009). Paper keys come next;
none is in this session's environment, and that no live key is on the machine is still UNVERIFIED. Every
Orca session runs with `--dangerously-skip-permissions` (ADR 0018). With no built-in guard, as here, a mod
can approve what an `ask` rule would prompt for (ADR 0023). In such a session, from the docs and a trial:

| What | Held? | Seen in the trial |
|---|---|---|
| A shell write outside the worktree, to the settings files and `hooks` under `.claude/`, or to `config` and `hooks/` of the shared `.git` | Yes | `Operation not permitted` each time; `git fetch` still wrote the shared `.git` |
| A shell connection to a host in `deniedDomains` | Yes: "refused in every permission mode" | A stand-in host: 403 from the proxy |
| A shell connection to a host that is only missing from the allow-list | No: "Allowed without a prompt". `strictAllowlist` refuses it, and "A repository can't turn it on or off" | 200, no prompt |
| A retry outside the sandbox | No: "the retry runs without a prompt". Yes with `allowUnsandboxedCommands: false` | Reached the denied host; with the key set, refused |
| The file and web tools, hooks, MCP servers, mods | Outside it | A file tool rewrote the trial's settings file; a sandboxed `rm` could not touch it |
| Environment variables, a paper key among them | No by default: "Inherited from Claude Code, including any secrets in its environment". A `credentials.envVars` entry can unset or mask one | Not tried |

So option 3 holds a mistaken shell command, not a session that sets out to leave it: "Mods aren't
sandboxed" (ADR 0023), a file tool can rewrite the settings that carry it, a mod the session writes "can
run when the turn ends", and for an excluded script in the worktree "Claude can write that file and then
run it outside the sandbox". The vendor says the Bash sandbox "on its own constrains only shell commands,
so it is not sufficient for fully unattended runs in either mode".

Of what ADR 0020 says nothing refuses, the deny-list would refuse a shell fetch of a live host, a process
that builds a live URL at run time, which the guard never could (ADR 0009), and Alpaca's CLI switched to
live; the boundary would refuse a shell edit of `.git/config`. `pip install`, `uv run` without `--locked`,
a Python edit and an MCP server switched to live stay as they are.

Prompts. Under bypass the sandbox adds none; an `ask` rule still prompts, as now, being among the "Actions
no mode auto-approves". With the retry off a command the allowances missed fails, hence the trial. ADR
0028, written in the same run and not landed or read here (UNVERIFIED), would take the prompt off a single
pin move, an exactly pinned `uv add` and `uv sync --locked`; package code would then run unprompted, and
the sandbox would keep its writes to the worktree, uv's cache and the temp folder.

* **Option 1** is the state until the trial passes, and after it if it fails.
* **Option 2**: not chosen. Under bypass it needs `strictAllowlist`, which a repository's file cannot
  set: user or managed settings, machine-wide, or a `--settings` argument in Orca's launch command.
* **Option 4**: `watch`. The vendor asks it of bypass mode, "the isolation boundary you choose is what
  protects your system", but the runtime "is a beta research preview". Adoptable once it leaves beta.
* **Option 5**: not chosen. Without the bypass flag this CLI starts a session in auto mode, where a
  classifier that blocks repeatedly "resumes prompting", or in Manual mode where auto is unavailable:
  a prompt or a model's verdict in front of a lane. Not tried.

A managed settings file, open under ADR 0023 to load the guard, would not turn the sandbox on. But
"Nothing in your own settings files or `--settings` overrides a managed key", so its keys there would be
out of a session's reach if the session cannot write that file (UNVERIFIED); `disableSideloadFlags` there
"keeps mods Claude writes during a session from loading"; and the retry switched off there makes the
sandbox admin-required: a repository's allowances are then ignored.

### Consequences

* Good: the direction is set, and what the sandbox would and would not hold is written down.
* Bad: until the trial passes nothing holds the list under ADR 0020's table; after it, the rows marked No
  and Outside stay as they are.

### Confirmation

Trial, 2026-11-10: Claude Code 2.1.321, macOS, bypass mode, through an ignored file in the worker's own
worktree, `.claude/settings.local.json`, created for it and deleted afterwards. It took effect in the
running session within seconds and no prompt appeared. `scripts/ci` failed 7 of 20 checks and skipped 5:
bare `mktemp -d` used the macOS temp folder, not the sandbox's `$TMPDIR`, and uv could not write its cache.
With both writable, `test-ci` still failed until `com.apple.sysmond` could be looked up; then all 20
passed. `gh api` failed with `tls: failed to verify certificate: x509: OSStatus -26276`, as the docs warn
it may, `gh auth status` failed too, and so `scripts/land --dry-run` stopped at "gh is not signed in or
cannot reach GitHub"; a `gh` call listed in `excludedCommands` ran. The three logs are under
`/private/tmp/claude-501/-Users-noeron-canon/ci-logs/` as `adr-0025-sandbox-trial-ci-*`; the second also
holds one pytest failure that the third, with no code changed, does not.

The change that turns it on must still try what this trial did not: a commit, `scripts/land` to the end as
an excluded command, `uv sync`, a pin move, the `orca` command a worker reports with, and a paper test's
stream through the proxy (all UNVERIFIED). It waits because a settings file reloads into running sessions.

## Evidence

Read 2026-11-10. `fetched`: downloaded whole with `curl` and read, not summarised.

* https://code.claude.com/docs/en/sandboxing.md — the quotations in the table but one, and the one
  on an excluded script; "Writes to `hooks/` and `config` inside that directory remain denied";
  Go-based tools "may fail TLS verification"; the admin-required sandbox; "Sandboxing reduces risk but is
  not a complete isolation boundary". https://code.claude.com/docs/en/sandbox-environments.md — the "not
  sufficient" sentence; option 4's; "The runtime is a beta research preview" (both `fetched`, read whole).
* https://code.claude.com/docs/en/settings-reference.md — `strictAllowlist`: scope "User or managed", and
  its sentence in the table; `allowUnsandboxedCommands`: when `false`, "Claude Code ignores that parameter";
  `deniedDomains`: scope "Any file" (`fetched`; the sandbox entries read, not the whole page).
* https://code.claude.com/docs/en/settings.md — the managed-key sentence; in a worktree, "it uses the file
  at the main checkout's root", shared by every worktree, and a file "an earlier version left" in the
  starting folder is still read (one created in the trial was too); Claude Code "watches your settings
  files and reloads them when they change". https://code.claude.com/docs/en/permissions.md — "you aren't
  prompted when the mod approves a call that an ask rule" would prompt for (both `fetched`, read whole).
* https://code.claude.com/docs/en/plugins/mods/overview.md — "Mods aren't sandboxed."; `mods/create.md` and
  `mods/admin.md` — the sentences on a mod the session writes; https://code.claude.com/docs/en/changelog.md,
  2.1.321 — such mods "turn on without asking in auto and bypass modes" (`fetched`; found by search).
* https://code.claude.com/docs/en/permission-modes.md — an explicit `ask` rule under "Actions no mode
  auto-approves"; auto mode is the built-in starting mode from v2.1.283; after 3 blocks in a row or 20,
  "auto mode pauses and Claude Code resumes prompting"; where auto is unavailable, "Claude Code starts the
  session in Manual instead" (`fetched`; three sections read).
* alpaca-py 0.45.0 as installed from `uv.lock`, `alpaca/common/enums.py`, `BaseURL` — the two live hosts,
  and `paper-api.alpaca.markets` for paper (read; the pin is `c42add7c0a70` in `canon/upstreams.lock.json`).
* The trial — commands and exit codes in the worker's session, and the three logs. That session's process
  arguments, its environment (no `ALPACA` variable), `claude auth status` (plan `max`), and no
  `/Library/Application Support/ClaudeCode/` (run). ADR 0009, 0018, 0020, 0023, the adapter's README, both
  settings files and `scripts/land` (read). ADR 0028 was not read: it had not landed.
* The owner's words, 2026-11-10, as the coordinator relayed them to the worker: "auto proceed with sota
  convergence you as master orchestrator"; "high quality is the directive, research convergence
  convergenced is the evidance, proceed with sota evidances automatically as long as they are form sota
  sources, per our very harness rules"; and the driver, "make sure every lanes are seamless without
  consistant premission frictions". The coordinator chose option 3 and its terms the same day.
* Second reader, 2026-11-10 (`evidence-verifier`, two passes): every quotation and the hosts held. It
  corrected the second run's account and the cause of the `gh` failure, narrowed two table rows and option
  2, added auto mode, `credentials.envVars` and `--settings`, marked ADR 0028 and the managed file's reach
  UNVERIFIED, and supplied the missing item of ADR 0020's list and what lets a session leave option 3.
