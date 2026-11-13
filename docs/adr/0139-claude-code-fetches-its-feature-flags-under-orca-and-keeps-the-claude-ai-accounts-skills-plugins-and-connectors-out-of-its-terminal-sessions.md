---
status: accepted
date: 2026-11-13
decision-makers: repo owner (DO_NOT_TRACK "0", 2026-11-12; connectors off, 2026-11-13); master orchestrator (Claude) for skill and plugin sync, under rule 2 and the owner's delegation (ADR 0074), and for the record
review-by: 2027-02-11
---

# Claude Code fetches its feature flags under Orca, and keeps the claude.ai account's skills, plugins and connectors out of its terminal sessions

## Context and Problem Statement

Orca starts every agent with `DO_NOT_TRACK=1`, which turns off Claude Code's feature-flag fetch and with it
the "You should know" mod the owner chose on 2026-11-12 (ADR 0023), along with the other features the docs
list as needing the fetch. The same fetch turns on the sync of the skills and plugins enabled for the owner's
claude.ai account, which canon has not adopted (rule 2). Separately, the owner decided on 2026-11-13 that
claude.ai connectors stay out of every session, not only canon's checkouts.

## Decision Drivers

* The owner's decisions: You should know on (ADR 0023); `DO_NOT_TRACK` `"0"` for Claude Code, with its
  usage metrics accepted (2026-11-12); connectors off in every session (2026-11-13).
* Rule 2: nothing loads that the canon has not adopted. A synced plugin loads its skills, agents, hooks,
  MCP and LSP servers "with the same trust as a marketplace plugin you installed".
* ADR 0103: no connector is a path to orders.
* One place: the user settings that `scripts/bootstrap --user-config` installs and the maintenance agent
  keeps in step with main (ADR 0065), so every session on the machine reads them, in any folder.

## Considered Options

1. In canon's user settings: `env.DO_NOT_TRACK` `"0"`, `syncClaudeAiSkills` and `syncClaudeAiPlugins`
   `false`, `disableClaudeAiConnectors` `true`, added by `scripts/bootstrap --user-config`.
2. Leave Orca's `DO_NOT_TRACK=1` in place: nothing syncs, but You should know never loads, against the
   owner's decision.
3. Change the launch environment Orca builds: no Orca setting for it is known here (UNVERIFIED), and it
   would reach every runtime Orca starts, which the owner kept at `1`.
4. Per session or per item: `ENABLE_CLAUDEAI_MCP_SERVERS=false` in each session's environment and
   `claude plugin disable <name>@synced` for each synced plugin. Nothing holds it for the next item the
   account turns on.

## Decision Outcome

Chosen: **option 1**, because a settings `env` value replaces the one a session inherits from its shell in
most sessions, Orca's among them, so one file turns the fetch on for Claude Code and keeps what the account
would sync and connect out of its sessions. `syncClaudeAiSkills` and `syncClaudeAiPlugins` can be set only
in user, local or managed settings or a `--settings` file, and Claude Code honors only `false`; a `true` for
`disableClaudeAiConnectors` in any settings file applies, and a project-level `false` cannot override it.

Where the evidence corrects the request:

* The owner asked for `"0"` for Claude Code alone. A settings `env` value also reaches every subprocess
  Claude Code starts. So the commands a Claude Code session runs (scripts, hooks, MCP servers, other CLIs)
  see `"0"` too. Orca, and the runtimes Orca starts directly, keep `1`.
* The owner asked for connectors off in every session. The setting reaches only the connectors Claude Code
  fetches itself, in terminal, IDE and Agent SDK sessions. Cloud sessions get theirs from the cloud host,
  and the desktop app delivers them to its local and SSH sessions in-process; this setting reaches neither.
  For the desktop app the docs point to disconnecting a connector on claude.ai's connectors page, and for
  cloud sessions to the claude.ai organization's settings: both are the owner's to change, not a session's.

### Consequences

* Good: You should know loads (checked 2026-11-13, ADR 0023), as do the other features that need the
  fetch, such as `/skill-doctor` and the advisor tool.
* Good: once `scripts/bootstrap --user-config` has added all four settings, no skill, plugin or connector
  of the claude.ai account loads in a terminal session on this machine. Until then the plugin sync runs: by
  this record's date it had recorded only Anthropic's default directory and downloaded no plugin. The skill
  sync has downloaded nothing since it was set to `false`. Whether `false` also stops a plugin an
  organization marks as required on claude.ai is UNVERIFIED: such a plugin loads even when disabled one at
  a time.
* Bad: Claude Code sends its usage metrics to Anthropic and third-party logging infrastructure. The docs
  say these never include code, prompts or file paths. Whether `DO_NOT_TRACK=1` had also stopped its error
  reports or the session quality survey, and so whether they return, is UNVERIFIED.
* Bad: each command a Claude Code session runs inherits `"0"`; which of canon's tools read
  `DO_NOT_TRACK` is UNVERIFIED.
* Neutral: a skill or plugin turned on in claude.ai does not reach Claude Code. To use one, adopt it through
  the canon.

### Confirmation

* `scripts/bootstrap --user-config` adds each of the four to an existing settings file that lacks it, with a
  `NOTE` line saying what it changes, and keeps and names a value the owner set otherwise. A new settings
  file gets the same notes (`scripts/test-lanes`).
* `scripts/bootstrap --check` has rows `claude.ai skill sync`, `claude.ai plugin sync` and
  `claude.ai connectors`, and its telemetry row names `DO_NOT_TRACK "0"` while the template sets it. A key
  the file lacks is `NOT SET` and fails the check. A skill or plugin sync set to anything but `false` is a
  `MISMATCH` and fails, since it loads what the canon has not adopted. Connectors set otherwise are `OWNER`,
  named without failing, since canon's project settings keep them off in its checkouts. A template that
  cannot be read is `UNKNOWN` and fails (`scripts/test-lanes`).
* `scripts/test-hooks` checks that canon's project settings turn connectors off as well.

## Evidence

Read 2026-11-13. `fetched`: downloaded whole with `curl` at 13:18 UTC and read in the parts named.

* https://code.claude.com/docs/en/env-vars.md: "Features that need feature-flag fetching", where
  `DO_NOT_TRACK` is among the variables that skip the fetch, and the list names the You should know mod and
  the sync of the account's skills and plugins. The `DO_NOT_TRACK` row: "a standard boolean, so `0` leaves
  telemetry on". A settings file's `env` value replaces the shell's "in most sessions" (`fetched`).
* https://code.claude.com/docs/en/settings-reference.md: "How `env` values interact with your shell".
  A settings value overwrites the shell's, except when the desktop app or a self-hosted runner builds the
  launch environment, and values "reach every subprocess Claude Code starts". Also the sections
  `syncClaudeAiSkills`, `syncClaudeAiPlugins` and `disableClaudeAiConnectors` (`fetched`).
* https://code.claude.com/docs/en/skills.md: "Where synced skills load", which syncs only with a claude.ai
  sign-in and the fetch, and moves the synced skills to `~/.claude/skills/.trash/` under `false`
  (`fetched`).
* https://code.claude.com/docs/en/plugins/loading.md: "Plugins synced from claude.ai", which load as
  `<name>@synced` with every component, not in a session that skips the fetch, and, when the organization
  requires one, "even if you disabled it earlier" (`fetched`).
* https://code.claude.com/docs/en/mcp.md: "How connectors reach Claude Code", its table of the three
  routes, and that the setting acts "only on the first row, the connectors Claude Code fetches itself"
  (`fetched`).
* https://code.claude.com/docs/en/data-usage.md: "Telemetry services", covering usage metrics and error
  reports, which "you can turn each off individually" (`fetched`).
* https://code.claude.com/docs/en/interactive-mode.md: "You should know suggestions". It is not listed in a
  session that skips the fetch, and its checks count toward usage limits or API costs (`fetched`).
* This machine, 2026-11-13, by `ps -E` filtered to the one variable. `orca-terminal-host` and a Claude Code
  process Orca 1.4.255 started were launched with `DO_NOT_TRACK=1`, as were the Codex and Antigravity
  processes running. A shell that session's Bash tool started printed `DO_NOT_TRACK=0` (run on this machine).
* This machine, 2026-11-13: `~/.claude/plugins/synced/` recorded one marketplace, Anthropic's default
  directory, and no plugin; `~/.claude/skills/synced/` held no file newer than the change (read on this
  machine; no identifier copied here).
* Second reader, 2026-11-13 (`evidence-verifier`): it re-fetched the seven pages and found that "every
  session" claimed more than the connector docs allow, that plugin sync runs until the installer adds its
  setting, that `/restart` needs the fetch only on some plans, that a count of the synced folders said
  something about the account, and that ADR 0023's trimmed text had lost the "direct" condition. Each is
  corrected above or in ADR 0023.
