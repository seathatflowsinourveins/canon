---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Runtimes update themselves where the vendor provides it: Claude Code on latest above the pin, Antigravity's updater on, except in the sessions the cockpit starts with a prompt

## Context and Problem Statement

ADR 0008 and ADR 0022 held Claude Code on the `stable` channel, about a week behind, and ADR 0045 turned
Antigravity's self-updater off so it stayed at its pin. The owner's instruction of 2026-11-10: "it is
important for our practice be auto default into sota as llm native, such as env set, cli update etc".
Which runtimes update themselves, through which setting, and what does a registry pin then mean?

## Decision Drivers

* The owner's instruction above.
* A claim that a tool "follows the newest by itself" must be true of the sessions that actually run here.
* The vendor's documented update setting before a script of this repository's own.

## Considered Options

1. Keep the holds: `stable` for Claude Code, Antigravity's updater off.
2. Turn on each runtime's own documented updater, follow the newest-release channel, keep the pin as a floor.
3. Option 2, with `scripts/bootstrap` running each runtime's update command itself.

## Decision Outcome

Chosen: **option 2**.

1. **Claude Code follows `latest`**, "receive new features as soon as they're released", named in the
   settings template as `autoUpdatesChannel`. With no key the two pages disagree: the settings reference
   says "Default: unset, so Claude Code follows "latest"", and the setup page "The channel you choose at
   install time becomes your default for auto-updates"; naming it holds under both. `stable` gives "a version that is typically about
   one week old, skipping releases with major regressions". It served 2.1.311, published 2026-10-26, below
   the 2.1.321 floor, so on `stable` the CLI stays where it is until stable passes the floor. Given up: a
   release is no longer read here before it runs, and a regression stable would skip can arrive.
2. **The registry's `claude-code` pin is a floor**, the template's `minimumVersion`: "Background
   auto-updates and `claude update` refuse to install any version below this value". `scripts/bootstrap
   --install` still installs it on a machine below it, and `--check` calls a CLI ahead of it ok, naming the
   version. The pin moves by `adopt-upstream` as before; what runs can be newer than what was read.
3. **Antigravity's self-updater is on.** Its docs give `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the shell
   profile to "Opt out or disable auto-updates"; `scripts/bootstrap --install` now takes canon's own line
   out of `~/.zshrc` and leaves one of the owner's alone. Its pin is then the version last read.
4. **Codex, Muse Code, DeepSeek Harness and `dsh-tui` have no updater turned on here.** Codex's
   `check_for_update_on_startup` is to "Check for Codex updates on startup"; installed through mise, each
   moves with `mise upgrade <tool>` (or `dsh-tui`'s `/update`), the owner's step, and its pin follows
   through `adopt-upstream`.
5. **What this means in practice.** Orca starts every lane here. Its 1.4.255 code passes an option,
   `suppressStartupUpdatePrompt`, on each launch that submits a prompt, which is every orchestration
   worker, and with it sets `DISABLE_AUTOUPDATER=1` for Claude Code, `AGY_CLI_DISABLE_AUTO_UPDATE=true`
   for Antigravity and `-c check_for_update_on_startup=false` for Codex; this session carries the first.
   So a worker or coordinator Orca starts with a prompt never updates its runtime in the background. Claude
   Code "checks for updates on startup and periodically while running", and an update takes "effect the
   next time you start Claude Code": the background update runs in the sessions with no such switch, a
   tab the owner opens in Orca without a prompt or a terminal outside it. `claude update` installs at once
   from any session, since "Manual `claude update` still works" under `DISABLE_AUTOUPDATER`; `agy update`
   does the same for Antigravity. "Updates itself" is true in that sense: it needs a session without the
   switch, or one command. The desktop app bundles its own copy and does not move the terminal CLI.
6. **`scripts/bootstrap` runs no update** (option 3 not taken): an update command is the vendor's and
   needs no wrapper, and `--install` keeps its one job, reaching the floor. `--check` reports the channel
   in effect from the settings and this environment; an off switch in the environment alone, as Orca sets
   it, is named without failing the check.

This supersedes, as dated pointers in each: ADR 0022 rule 2's `stable` channel; ADR 0008's Claude Code row
and its week of hold; and ADR 0045 rule 2's Antigravity switch.

### Consequences

* Good: the CLI no longer sits below the vendor's release for a week or more, and every pin's meaning is
  stated: a floor for Claude Code, the version last read for the rest.
* Bad: a release runs before it is read. Orca sessions never update, so the newest arrives only when the
  owner starts a session outside Orca or runs `claude update`.

### Confirmation

On this machine, 2026-11-10: `claude doctor` run from an empty environment gave `Auto-updates: enabled`,
`Auto-update channel: stable` (the owner's settings before this change) and `Last update attempt: failed
(install_failed)`; from this Orca session, `Auto-updates: disabled (set by env: DISABLE_AUTOUPDATER)`.
`scripts/test-lanes` covers the channel row (latest ok, none NOT SET, stable MISMATCH, an off switch in the
settings OFF and failing, one in the environment OFF and not failing), the CLI ahead of the pin, and
`--install` removing canon's Antigravity line and keeping an owner's.

UNVERIFIED: whether Antigravity's background updater, installed through mise, replaces the binary mise
runs; whether Codex's startup check installs or only reports (Orca's code says its "Update now" choice
"installs and exits"; OpenAI's docs do not say); whether `DISABLE_AUTOUPDATER=0` counts as off (the check
counts any non-empty value).

Applied on 2026-11-11, at the owner's request, from the main checkout at commit 229110a, together with ADR
0059's settings: `scripts/bootstrap --user-config` set the `latest` channel and `scripts/bootstrap
--install` removed the Antigravity no-update line; `claude update` moved the CLI from 2.1.321 to 2.1.322;
`scripts/bootstrap --check` ended "All present".

## Evidence

Read 2026-11-10. `fetched`: downloaded and searched here; `run`: a command run here.

* https://code.claude.com/docs/en/setup.md — the channel sentences in rule 1; "The channel you choose at
  install time becomes your default for auto-updates"; the `minimumVersion` sentence; the
  startup-and-periodic check (fetched).
* https://code.claude.com/docs/en/settings-reference.md — `autoUpdatesChannel` takes `"stable"` or
  `"latest"`, "Default: unset, so Claude Code follows "latest"" (fetched by the second reader).
* https://code.claude.com/docs/en/env-vars.md — `DISABLE_AUTOUPDATER` "Manual `claude update` still
  works", `DISABLE_UPDATES`, and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` also stops auto-updates
  (fetched).
* `https://downloads.claude.ai/claude-code-releases/stable` and `/latest`, the pointers the vendor's
  install script reads — 2.1.311 (published 2026-10-26) and 2.1.322 (fetched by the second reader and by
  the landscape refresh; not a documented page).
* https://antigravity.google/docs/cli/troubleshooting/ — "Opt out or disable auto-updates : Set the
  AGY_CLI_DISABLE_AUTO_UPDATE environment variable to true inside your shell profile" (fetched).
* `agy --help` — `update` "Update CLI" (run).
* https://learn.chatgpt.com/docs/config-file/config-reference — `check_for_update_on_startup` (fetched).
* Orca 1.4.255's code, `out/shared/agent-launch/startup/tui-agent-launch-command.js` in the app and its
  terminal host's bundle — the three switches, applied with `suppressStartupUpdatePrompt`, which the
  launch path sets from whether the prompt is submitted (read, `grep`; second reader).
* This session's process environment — `DISABLE_AUTOUPDATER` set by Orca; its
  `AGY_CLI_DISABLE_AUTO_UPDATE` comes from canon's earlier line in `~/.zshrc` (names only, `ps`).
* Second reader, 2026-11-10 (`evidence-verifier`): the quotes held. It corrected rule 5 (the switch is
  set per launch that submits a prompt, `claude update` works under it, the desktop app moves nothing),
  the two pages' disagreement on the default channel, and where this session's Antigravity switch came
  from; all above.
* `claude doctor`, from an empty environment and from this session (run).
