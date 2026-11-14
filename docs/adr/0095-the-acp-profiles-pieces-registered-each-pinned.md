---
status: accepted
date: 2026-11-11
decision-makers: the owner for the direction (2026-11-11); master orchestrator (Claude) for what is registered and how, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# What the ACP-served DeepSeek Harness profiles stand on is registered, each piece pinned: the Agent Client Protocol as a standard, the plugin list as a reference, and every component of the two profiles

## Context and Problem Statement

A probe of 2026-11-11 (an amendment to ADR 0086 on a branch not yet landed, commit `62ccb4c`) lists the
direct packages of two DeepSeek Harness profiles, `review-acp` and `build-acp`, each started as `dsh
--profile <name>` and carrying `@deepseek-ai/dsh-acp-app`, which serves the Agent Client Protocol over
stdio through `@deepseek-ai/dsh-acp`; running either is `UNVERIFIED` there. The protocol, the
community list the plugins came from, and those packages are not in `canon/upstreams.json`, so nothing
pins them and a later reader cannot say what a result labelled with either profile ran on. The pieces
land one at a time; this record gains one numbered item per landing.

## Decision Drivers

* `AGENTS.md` rule 2: what is installed or relied on has an entry and a pin.
* ADR 0072 rule 3: a list is not a source of trust.
* A component is pinned where its source is published: a release tag where one exists.

## Considered Options

1. One entry per piece, each pinned and each landed on its own.
2. One entry for the profiles as a whole.
3. Leave them recorded only in ADR 0086.

## Decision Outcome

Chosen: **option 1**. Option 2 hides which piece moved; option 3 pins nothing.

1. **The Agent Client Protocol, v1** (`agentclientprotocol/agent-client-protocol`, Apache-2.0) is a
   `standard`, tracked by its schema release tag `schema-v1.31.0` and held to the `schema-v1.` line,
   so a `schema-v2` release does not move it (the Rust crate's `v1.x` tags never match the prefix). The
   protocol version on the wire is the integer `1`, raised only for a breaking change; the page counts
   new capabilities as not breaking. DeepSeek Harness's ACP server, `@deepseek-ai/dsh-acp` 0.2.1-rc.3,
   says it "exposes only the standard ACP v1 surface", leaves out `session/load`, and builds on
   `@agentclientprotocol/sdk` 1.4.0, which generates its types from `schema-v1.21.0`, ten minors behind
   the pin. Whether the server conforms is `UNVERIFIED`: no conformance run exists.
2. **The plugin list** (`awesome-dsh-plugin/awesome-dsh-plugin`, CC0-1.0) is a `reference`, tracking its
   default branch because it publishes no tags. It lists every plugin ADR 0094 names (10,581 entries at the pin), and is
   nothing more: its own notice says "Being on this list is not a security review", so no plugin is
   installed on its strength (ADR 0072 rule 3).
3. **`dsh-acp-app`** (DeepSeek Harness's `packages/bundle/acp-app`, MIT), `tool`, pinned with the engine
   at `dsh-v0.2.1-rc.3`: the bundle both profiles carry, which starts `dsh-acp` on stdio with a default
   model route. Its own source makes no network, file or subprocess call.
4. **`dsh-experimental-terminal-bundle`** (`packages/experimental/terminal-bundle`, MIT), `tool`, at
   `dsh-v0.2.1-rc.3`: persistent shells as six tools, in both profiles, and the one addition of ADR
   0086's review configuration on the `dsh-tui` profile (`review-acp` is not that configuration). The
   bundle's own code is empty; its dependency `dsh-terminal-bash` spawns the shells, confined by the
   host sandbox unless its mode is `danger-full-access`. The profiles' sandbox mode is `UNVERIFIED`.
   2026-11-13: at `dsh-v0.2.2-rc.2` the bundle leaves `review-acp` and `build-acp` as well. Its backend is now
   the terminal panel, which needs the `terminalController` service, and only the Web app's bundle composes
   that (`packages/bundle/web-app/cordis.patch.yml` L126-127); the rc.2 dumps hold the panel and no
   controller (Evidence).
5. **`dsh-subagent-codex`** (`packages/subagent/subagent-codex`, MIT), `tool`, at `dsh-v0.2.1-rc.3`,
   build arm only: delegates one task to a fresh Codex thread in the parent workspace through its own
   `@openai/codex` 0.153.4, outside the `codex-cli` pin. Key-like variables are scrubbed from the child's
   environment, so it runs under whatever login `CODEX_HOME` and `HOME` point to where `dsh` started.
   `permissionMode` defaults to `never`, which declines approvals and leaves the sandbox to Codex's
   settings. Which login, mode and sandbox `build-acp` gets is `UNVERIFIED`. It sends a task, not the
   transcript, so ADR 0072 rule 6 places it by extension: another family answers, and it edits the
   workspace.
6. **`dsh-subagent-claude-code`** (`packages/subagent/subagent-claude-code`, MIT), `tool`, at
   `dsh-v0.2.1-rc.3`, build arm only: delegates one task to a fresh Claude Code session in the parent
   workspace through its own Agent SDK (0.3.263, which bundles Claude Code 2.1.263, behind the
   `claude-code` pin) and never falls back to the host `claude`. It reads the host's user, project and
   local Claude settings, hooks and allow rules included. The child's environment is the profile's `env`
   over the parent's with key-like and `DSH_` names scrubbed, so its login is the Claude configuration
   directory (`CLAUDE_CONFIG_DIR`, else `~/.claude`) and its Keychain entry, unless a surviving provider
   variable or a settings `apiKeyHelper` outranks it. `permissionMode` defaults to `dontAsk`: what the
   host's allow rules and read-only defaults permit runs, the rest is denied. Which login and mode
   `build-acp` gets is `UNVERIFIED`. It sends a task, not the transcript, so ADR 0072 rule 6 places it by
   extension: another family answers, and it edits the workspace. It may also read and write the host's
   auto memory, which would be state across sessions under rule 6 directly (`UNVERIFIED`).

### Consequences

* Good: a profile's result can be read against pinned pieces.
* Bad: entries that share the `deepseek-ai/deepseek-harness` pin move with it, not on their own.

### Confirmation

Each piece has its own entry naming this record; `scripts/canon check` and `outdated` watch each pin.

## Evidence

Read 2026-11-11.

* https://agentclientprotocol.com/llms.txt — the v1 specification's pages (fetched).
* https://agentclientprotocol.com/protocol/v1/overview.md — JSON-RPC 2.0; initialize, session, prompt
  turn (fetched).
* https://agentclientprotocol.com/protocol/v1/initialization.md — "a single integer that identifies a
  **MAJOR** protocol version. This version is only incremented when breaking changes are introduced"
  (fetched).
* https://agentclientprotocol.com/protocol/v1/transports.md — stdio is the one defined transport;
  Streamable HTTP is still an RFD (fetched).
* https://api.github.com/repos/agentclientprotocol/agent-client-protocol/releases — `schema-v1.31.0`,
  2026-11-10, beside `v1.17.0` (Rust crate) and `schema-v2.0.0-alpha.15`; licence Apache-2.0 (fetched).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/acp/acp/README.md` and `package.json` —
  "exposes only the standard ACP v1 surface"; `@agentclientprotocol/sdk` `1.4.0` (resolved).
* https://registry.npmjs.org/@agentclientprotocol/sdk/1.4.0 — Apache-2.0, `gitHead` `e6463f444093`,
  the `typescript-sdk` repository's `v1.4.0` tag (fetched); npm's `latest` is 2.6.0, so dsh is a major
  version behind on the SDK (a second reader's fetch, 2026-11-11).
* https://raw.githubusercontent.com/agentclientprotocol/typescript-sdk/e6463f444093ed7c5f1cc937c3f32afb5853e906/scripts/generate.js
  — `CURRENT_V1_SCHEMA_RELEASE = "schema-v1.21.0"` (fetched). A second reader also found the SDK's
  `schema/schema.json` identical to the schema at ACP's `schema-v1.21.0` and different at v1.20.0 and
  v1.22.0.
* Commit `62ccb4c`, ADR 0086 "Amendment — 2026-11-11: ACP profile composition", from each profile's
  `package.json` and `pnpm-lock.yaml` (read; not on `main` or any remote, and its source files are in
  one machine's profile folders, so a stranger cannot check it).
* https://github.com/awesome-dsh-plugin/awesome-dsh-plugin and
  https://raw.githubusercontent.com/awesome-dsh-plugin/awesome-dsh-plugin/f613760b0ecc1f1dc61da874d3fd10d3584385f7/README.md
  — 10,581 plugin entries between its `BEGIN PLUGINS` and `END PLUGINS` markers (10,584 lines start
  with `- [`; three are contents links); "Being on this list is not a security review"; its disclaimer: listing "is not
  an endorsement" (fetched). All ten repositories ADR 0094 names appear in it once. The worker report
  ADR 0094 cites read it at `1ff709f72741` (that report, read; `UNVERIFIED` here); that commit has the
  same count, three commits behind the pin (a second reader's fetch, 2026-11-11). ADR 0072 rule 3's
  10,426 is an earlier count.
* https://api.github.com/repos/awesome-dsh-plugin/awesome-dsh-plugin — licence CC0-1.0; no tags (fetched).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/bundle/acp-app` — `package.json` (MIT; depends
  on `dsh-acp`, `dsh-cmdline`, `commander`), README Summary and `cordis.patch.yml` (persona, provider
  `deepseek-official`, model `deepseek-v4-flash`, `session-title-llm` disabled); a search of its `src`
  for `fetch(`, `node:http`, `node:fs`, `writeFile`, `child_process` and `spawn(` found none (resolved).
  https://registry.npmjs.org/@deepseek-ai/dsh-acp-app/0.2.1-rc.3 — MIT, that directory, no `gitHead`
  (fetched); a second reader found its tarball's patch and READMEs byte-identical to the pinned source,
  a match by content, not provenance. Both profiles carry 0.2.1-rc.3 (commit `62ccb4c`, read).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/experimental/terminal-bundle` — `package.json`
  (MIT; "Keep shell processes alive across tool calls"), README Summary (six tools, isolated registry,
  off by default in Web and Desktop) and `cordis.patch.yml`; a search of its `src` for network, file and
  subprocess calls found none (resolved). https://registry.npmjs.org/@deepseek-ai/dsh-experimental-terminal-bundle/0.2.1-rc.3
  (fetched; no `gitHead`, so its match to
  the tag is by content only).
* A second reader of item 4 (2026-11-11) found no model call in the bundle or its three dependencies,
  terminal sessions process-local and owner-scoped, and the shell spawned in
  `packages/terminal/terminal-bash/src/index.ts` under `ctx.sandbox.confine` except in
  `danger-full-access` (resolved): a channel to another model or to earlier sessions exists only
  through what a shell can reach.
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/subagent/subagent-codex` — `package.json` (MIT;
  `@openai/codex` `0.153.4`), README Summary and options table (`permissionMode` default `never`;
  native configuration and authentication authoritative) (resolved).
  https://registry.npmjs.org/@deepseek-ai/dsh-subagent-codex/0.2.1-rc.3 (fetched; no `gitHead`, so its match to
  the tag is by content only).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/subprocess/subprocess/src/index.ts`,
  `scrubbedParentEnv()` — drops variables whose names contain `KEY`, `PASSWORD`, `SECRET` or `TOKEN`;
  `HOME` and `CODEX_HOME` pass (a second reader, 2026-11-11, resolved). It also found that an
  Orca-started session's `CODEX_HOME` is an Orca-managed home, not `~/.codex` (path names only).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/subagent/subagent-claude-code` — `package.json`
  (MIT; `@anthropic-ai/claude-agent-sdk` `0.3.263`), README Summary ("never falls back to the host
  `claude` executable"), options (`permissionMode` default `dontAsk`) and "Native settings are
  authoritative" (resolved). https://registry.npmjs.org/@deepseek-ai/dsh-subagent-claude-code/0.2.1-rc.3
  (fetched; no `gitHead`, so its match to
  the tag is by content only).
* `packages/subagent/subagent-claude-code/src/process.ts` and `src/index.ts` at the same pin — the
  child environment is the configured `env` over `scrubbedParentEnv()`, which the SDK composes from;
  `inheritsParentContext = false` (resolved). https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/0.3.263
  (fetched; `claudeCodeVersion` 2.1.263). https://code.claude.com/docs/en/authentication.md,
  https://code.claude.com/docs/en/permission-modes.md and https://code.claude.com/docs/en/memory.md
  (fetched 2026-11-11; current docs, not checked against 2.1.263).
* A second reader of item 6, 2026-11-11, named the bundled Claude Code version, replaced `HOME` with the
  configuration directory and Keychain, widened what `dontAsk` permits, and raised auto memory as a
  possible direct rule 6 ground; all are in. The SDK's own licence is not MIT; MIT covers the package.
* A second reader checked this record on 2026-11-11: it settled the SDK's schema minor, corrected how
  the profiles are started and what the `line` holds off, and asked for the scope note; all are in. A
  second reader of item 2 corrected the entry count from 10,584 to 10,581 and asked that the list's
  provenance be labelled; both are in.
* 2026-11-13, the four DeepSeek Harness pieces move with the engine pin to `dsh-v0.2.2-rc.2` → `ddac96760b6e`
  (resolved; ADR 0045's Evidence); `acp-spec` stays at `schema-v1.31.0`. At the new pin `@deepseek-ai/dsh-acp` is
  0.2.2-rc.2 on `@agentclientprotocol/sdk` 1.4.0, and its README still leaves out `session/load`;
  `dsh-acp-app`'s `cordis.patch.yml` is unchanged; `dsh-subagent-codex` keeps `@openai/codex` 0.153.4 and
  `permissionMode` `never`, `dsh-subagent-claude-code` keeps `@anthropic-ai/claude-agent-sdk` 0.3.263 and
  `dontAsk`, and `scrubbedParentEnv()` drops the same names (resolved). Item 4 changed: the terminal bundle's
  backend is now `dsh-terminal-panel` in place of `dsh-terminal-bash`, a Web sidebar terminal that needs the
  `terminalController` service, which only the `web-app` bundle composes and neither ACP profile does, so read in
  source its tools would find no backend there (the registry entry; `UNVERIFIED`, no tool was called). The
  bundle therefore leaves both ACP profiles (item 4's 2026-11-13 pointer; `home/dsh/profiles.json`), as it
  leaves `dsh-tui` (ADR 0086 rule 2's 2026-11-13 pointer), and its entry stays at `dsh-v0.2.2-rc.2`, the
  version last read. A rehearsal (run 2026-11-13) installed `review-acp` and `build-acp` with their first-party
  packages at 0.2.2-rc.2, the bundle among them, each in a throwaway `DSH_HOME` with the pinned pnpm 10.34.9,
  and composed both (exit 0): each dump holds `dsh-terminal-panel` and no terminal controller. A scripted
  rehearsal (run 2026-11-13, each step's exit code logged) then installed and composed both exactly as
  `home/dsh/profiles.json` records them, without the bundle: install and `--dump-config` exit 0, no admission
  refusal, no duplicate id, no `terminal_*` row, and `tool-bash` enabled in `dsh-base`'s row. `acp-probe` was
  not run on them, since it starts sessions.
