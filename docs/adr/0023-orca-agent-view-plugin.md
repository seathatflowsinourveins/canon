---
status: accepted
date: 2026-11-10
decision-makers: repo owner (by delegation, 2026-11-10); coordinator (Claude)
review-by: 2026-12-10
---

# Orca's agent-view plugin: left on as the cockpit's own, with the risk stated

## Context and Problem Statement

Orca starts each terminal Claude Code session with a plugin of its own, `orca-agent-view`, through
`--plugin-dir`. No record mentioned it. ADR 0004 asks that a third-party plugin be pinned, read and
registered before it is enabled, and ADR 0021 leaves "plugins" out of what a session uses. This one holds
a hooks module, which Claude Code calls a mod: "code that runs with your permissions". Does it stay on?

## Decision Drivers

* The owner's word decides a tool that is cheap to leave (ADR 0018).
* A third-party plugin is read before it runs with the agent's permissions (ADR 0004).
* A push, a full re-resolve, `uv sync` and a loose `uv add` wait for the owner behind `ask` rules (ADR 0028).

## Considered Options

1. Leave it on and unrecorded, as it was.
2. Leave it on, read and recorded, as an exception to ADR 0004.
3. Switch it off with Orca's "Claude subagent navigation" setting.
4. Bring it under ADR 0004 with a pinned copy this repo holds.
5. Keep it and add a managed settings file, so that Claude Code's built-in guard loads.

## Decision Outcome

Chosen: **option 2**. The plugin stays on. The owner was told the risk below (UNVERIFIED; see Evidence) and
then left that day's open choices to the coordinator; the coordinator chose to keep it, because this version
handles no tool call and leaving it is one setting: the first driver, not sources converging. Option 3 is the
way out if a later version handles a tool call. Option 5 stays open to the owner: it needs an administrator's
file on the machine.

What the module does, read in full at Orca 1.4.255:

* It handles session start, session end, its own command `/orca-agent-view` (registered to run at once,
  with no model turn) and the render of one prompt component. It handles no `tool.*` event, so today it
  neither sees nor decides a tool call.
* It posts to the Orca app on `127.0.0.1`, with the app's token: `register` every five seconds, with the
  pane, the session id, an id of its own, the open view and the whole of the user's `keybindings.json`;
  and `observe`, `take`, `ack` and `end` around a change of view, the `ack` carrying Claude Code's
  refusal text if any. The app's handler keeps a shortcut read from the keybindings, not the file.
* The app asks for a switch by typing that shortcut into the pane; the command then asks the app which
  agent, and shows that agent's transcript.
* It reads `keybindings.json`, an endpoint file Orca names and six environment variables. It writes
  nothing, starts no process and posts no subagent event. Orca itself writes a `ctrl+alt+g` binding and a
  marker file into every Claude config folder it knows.

The risk is not what this version does. Claude Code's docs say a setup with no managed settings and no Team or
Enterprise login has no built-in guard, and there "the mod can approve a call that a deny rule refuses or a
`PreToolUse` hook blocked" and "you aren't prompted when the mod approves a call that an ask rule ... would
prompt for". This machine is that setup. A mod's own file, process and network calls are outside permission
rules in every setup. Orca rewrites the plugin with the app, about once a day, and starts sessions with
permissions bypassed (ADR 0018). So a later version of this module could approve a push that ADR 0020 leaves
to an `ask` rule, or a read this repo denies. Orca's settings hooks cannot: an ask rule "still prompts even
when the hook returned `"allow"`".

1. **It is the cockpit's, not a session's.** No session installs, loads or enables a plugin (ADR 0021
   limit 6, read as the plugins Orca's guides offer a session).
2. **None of ADR 0004's four conditions is met.** It was read after Orca had loaded it, cannot be pinned,
   is named under Orca's `tool` entry and not as a `source`, and is loaded by a flag for the session.
3. **A new folder name means it is unread.** Orca names the folder by a hash of the plugin's three files.
   `claude plugin validate` lists a mod's hooks and calls without running it, and the module's 113 lines
   are then read, since a new address to post to does not show there. Nothing flags a new name.

Option 1 is what ADR 0004 exists to stop. Option 3 removes the risk for this plugin, at the cost of
opening a subagent's transcript from the sidebar: Orca builds the plugin only while that setting and its
status hooks are on. Option 4 cannot be done: Orca passes the folder it wrote. Option 5 is the docs' own
answer, the guard loading on "a machine with managed settings"; what else such a file changes was not
read, and it is the owner's to create.

### Consequences

* Good: a record says what runs in every Orca session, and what stands between a mod and an `ask` rule.
* Bad: the `ask` rules of ADR 0020 hold in Orca sessions only for as long as the loaded module handles no tool
  call. ADR 0018 and ADR 0021 call those rules what still prompts.

### Confirmation

2026-11-10, Orca 1.4.255 and Claude Code 2.1.321. `claude plugin validate` on the folder printed hooks
`ui.render{component=AbovePrompt}`, `session.start`, `command.run{command=orca-agent-view}` and
`session.end`; calls `$.clock.every`, `$.clock.now`, `$.command.register`, `$.env.get`, `$.fs.read`,
`$.http.fetch`, `$.session.id` and `$.ui.view`; "env writes: nothing"; "Validation passed". `claude
--plugin-dir <folder> plugin list` printed `orca-agent-view@inline`, 0.2.0, loaded. The folder is
`28f6355857715d67ffa2`. UNVERIFIED: that the setting removes the plugin on this machine; the guard's
absence as seen in `/plugin`, which a session cannot open for itself; what the app does with a post
beyond the handler; and whether the first run in ADR 0018 had the plugin.

**Exception, 2026-11-11: Claude HUD.** The owner decided on 2026-11-11 that installs are run for the
sessions, and the lead chose Claude HUD (`jarrodwatts/claude-hud`, registry entry `claude-hud`, pinned at
tag v0.15.0) as the status-line renderer. It is the one plugin a session may have installed besides Orca's.
It is installed by the lead or the owner, never by a session, from the shell:
`claude plugin marketplace add jarrodwatts/claude-hud#v0.15.0`, then
`claude plugin install claude-hud@claude-hud --scope user` (the marketplace's name is its `name` field,
`claude-hud`). `~/.claude/canon/statusline.sh` stays the configured status line. It runs the HUD, but
not through the plugin's launcher, which loads the newest copy from any marketplace's cache: it runs the
newest version's `hud.mjs` in the marketplace the user settings enable. The `/hud` skill's `hud install`
would point `statusLine` at the plugin instead, so it is not run;
`hud set` writes the plugin's own `config.json`, which canon's `~/.claude/claude-hud.json` overrides. Read
at the pin: `.claude-plugin/plugin.json` (no hooks, no MCP servers), `skills/hud/SKILL.md` (one skill that
runs `hud.mjs doctor` when invoked and pre-approves only `hud.mjs` doctor, schema, preview and report),
`scripts/statusline.mjs` (picks the newest cached version and enters `hud.mjs`), `hud.mjs` and `fence.mjs`
(the logic runs under Node's permission model with read grants by name, and an import fence that refuses
network modules and removes `fetch` and `WebSocket`; its own comment says the fence is "never proven
complete", so that it sends nothing is UNVERIFIED beyond that reading), `src/config.ts` and `src/auth.ts`
(the account display reads `~/.claude.json`'s `oauthAccount` and is off by default and in canon's file),
and `collector.mjs` (git's status read runs with `protocol.allow=never`, every git call with
`GIT_NO_LAZY_FETCH`; each render writes `~/.claude/plugins/claude-hud/heartbeat.json`, mode 600, holding the
status-line input, so the session's paths and model, on this machine only).
That the CLI commands above install this tag as written is UNVERIFIED until the first run.

**Exception, 2026-11-12: Claude Code's "You should know" mod.** The owner decided on 2026-11-12 to turn
on `cc-plugin-you-should-know@builtin`, a mod built into the pinned Claude Code CLI and off by default
(since v2.1.287). As Claude works, a side agent rereads the conversation and shows a `✦ Heads up · …`
note above the prompt. The docs say the side agent has no tools and cannot change what Claude does.
Each check is a background request to the session's model, billed for the mod's instructions and its
reply. Notes and dismissed topics are kept in `~/.claude/plugins/store/cc-plugin-you-should-know*`.
It loads only in a direct first-party session that fetches feature flags, and Orca is launched with
`DO_NOT_TRACK=1`, which every agent it starts inherits and which turns that fetch off ("Features that need
feature-flag fetching", env-vars.md). So canon's user settings set `DO_NOT_TRACK` to `"0"` in `env`, which
overrides the inherited value for Claude Code alone (a settings `env` value replaces one exported by the
shell, env-vars.md). Claude Code then also sends its own analytics to Anthropic again, which the owner
accepted on 2026-11-12. Orca and the other runtimes keep `DO_NOT_TRACK=1`. Fetching flags
also resumes the sync of the skills enabled for the owner's claude.ai account into
`~/.claude/skills/synced/` (skills.md, "Where synced skills load"), which would load skills canon has not
adopted (rule 2). So canon's user settings also set `syncClaudeAiSkills` to `false`, which only user, local or
managed settings can set (settings-reference.md): Claude Code stops downloading them and moves those already
synced to `~/.claude/skills/.trash/`. The owner also decided on 2026-11-13 to turn off claude.ai
connectors in every session, not only in canon's checkouts, whose project settings already set
`disableClaudeAiConnectors`. So canon's user settings set it to `true` too: a `true` in any settings file
applies, and a project-level `false` cannot override a user-level `true` (settings-reference.md). It
reaches only the connectors Claude Code fetches itself, in terminal, IDE and Agent SDK sessions, not the
ones a cloud host or the desktop app delivers (mcp.md, "How connectors reach Claude Code"). ADR 0103
already rules out a connector as a path to orders. `scripts/bootstrap --user-config` adds all three
settings to an existing `settings.json` that lacks them. Checked 2026-11-13: in a session Orca started after the change,
`/plugin enable cc-plugin-you-should-know@builtin` answered `Enabled cc-plugin-you-should-know`. That
session's `/plugin` also listed two built-in mods, on by default in Claude Code 2.1.323, that the docs read
here do not describe, `cc-plugin-easel` and `cc-plugin-privacy`; what they do is UNVERIFIED. The owner turns it on, never a
session, with `/plugin enable cc-plugin-you-should-know@builtin`, and turns it off with
`/plugin disable cc-plugin-you-should-know@builtin`. Read 2026-11-12 in
https://code.claude.com/docs/en/interactive-mode.md ("You should know suggestions") and
https://code.claude.com/docs/en/plugins/mods/overview.md ("Mods built into Claude Code"). The mod's
source is not among those the docs publish, so what it sends beyond the documented check is UNVERIFIED.

## Evidence

Read 2026-11-10. `fetched`: downloaded whole with `curl` and read, not summarised.

* The plugin folder named by this session's `--plugin-dir` argument — `plugin.json` (0.2.0, `stablyai`,
  MIT), `hooks/hooks.json` (one module), `hooks/register.js` (113 lines, sha256 `e14526dff993`): the
  handlers, the five posts, one `fetch`, to `'http://127.0.0.1:' + port + "/claude/agent-view"`
  (`bundled` with the app; read in full, and run through `claude plugin validate`).
* `~/.claude/keybindings.json`, `~/.claude/orca-agent-view.json` — the binding and the marker, owner
  `stablyai/orca/claude-agent-view` (read on this machine).
* https://github.com/stablyai/orca/tree/v1.4.255/src/main/claude/agent-view — `agent-view-plugin.ts`: the
  three files, and the folder name from their sha256. `agent-view-service.ts`: built only when
  `claudeSubagentNavigationEnabled !== false` and the status hooks are on; every config folder
  reconciled. `agent-view-bridge.ts`: `receive` stores the pane, the ids, a chord and the view; `view`
  sends the shortcut. `agent-view-config.ts`: the writes (`fetched`). Not read: `agent-view-install.ts`,
  `agent-view-binding-plan.ts`, `agent-view-keybindings.ts`, the route's server, the code that adds the
  flag.
* https://github.com/stablyai/orca/blob/v1.4.255/docs/site/content/docs/agents/claude-code.mdx — names
  neither the plugin nor the setting (`fetched`). https://www.onorca.dev/docs/agents/claude-code — the
  setting, "on by default when Claude status hooks are available" (`fetched` by the second reader).
* https://code.claude.com/docs/en/plugins/mods/overview.md — "A mod is code that runs with your
  permissions"; `claude plugin validate` (`fetched`).
* https://code.claude.com/docs/en/permissions.md — the two sentences on a setup without the guard, and
  the one on a hook's `"allow"` (`fetched`).
* https://code.claude.com/docs/en/plugins/mods/admin.md — the guard loads with managed settings, a
  `managed-mcp.json` file or "`/login` credentials for a Team or Enterprise plan"; permission rules do
  not apply to "a mod's `$.fs` and `$.process` calls" (`fetched`).
* https://code.claude.com/docs/en/managed-settings.md — macOS: `/Library/Application Support/ClaudeCode/`
  and the domain `com.anthropic.claudecode` (`fetched`). Neither of the guard's two conditions holds on
  the machine this was read on (run there).
* `~/.orca/agent-hooks/claude-hook.sh` — passes back an answer for `PreToolUse` only (read).
* Second reader, 2026-11-10 (`evidence-verifier`): it found the guard's conditions decided for this machine,
  the `deny` half of the risk, that Orca's hooks cannot answer an `ask` rule, the posts and writes the first
  draft left out, and that no condition of ADR 0004 is met; it asked for `proposed`. The status changed to
  `accepted` on the owner's delegation below; a second pass, the same day, had the sentence on the risk being
  told labelled and the driver the choice rests on named.
* The owner's delegation, 2026-11-10: the coordinator proceeds automatically on state-of-the-art convergence,
  leaving the open choices to it. It was given
  after this record's risk was reported to the owner (not re-observable; UNVERIFIED).
* Claude HUD exception, 2026-11-11: https://github.com/jarrodwatts/claude-hud at tag v0.15.0 (`b86e1c3e5255`,
  `resolved`), the files named above read from a clone at that tag;
  https://code.claude.com/docs/en/discover-plugins.md — `claude plugin install`, `enable` and `marketplace
  add` "as ordinary terminal commands", and `owner/repo#ref` "to pin a branch or tag" (`fetched`, raw).
