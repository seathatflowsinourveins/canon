---
status: accepted
date: 2026-11-11
decision-makers: repo owner (scope, 2026-11-11); coordinator (Claude), by the owner's delegation
review-by: 2026-12-11
---

# Each runtime starts in its own documented sandbox with nothing asking, where its vendor documents one; the others stay as they are

## Context and Problem Statement

The cockpit pre-fills each runtime's bypass flag, so Codex, Muse Code and Antigravity start with no
sandbox and no approvals (ADR 0043 rule 2, ADR 0045 rule 2). Three of their vendors say that posture
belongs inside a container, a VM or a sandbox of their own (Evidence). On 2026-11-10 one reader wrote a
`mise.toml` into `/private/tmp`, which every mise run below it then read (`docs/architecture.md`), and
another unset `core.hooksPath` in the main checkout's shared `.git/config`. Which launch form per runtime
keeps a worker in a sandbox its vendor documents, with nothing asking?

## Decision Drivers

* No lane stops at a prompt: the owner decided on 2026-11-10 that every lane is seamless.
* Only settings a runtime's own documentation or pinned source gives (rule 3, ADR 0059 driver 3).
* The owner decided on 2026-11-11 that security is not a goal of this project: one proportionate pass,
  landed on converged vendor documentation, with no end-to-end proof of every item.

## Considered Options

1. Keep every bypass flag.
2. Per runtime, the vendor's own sandbox with its never-ask approval setting, passed as launch arguments.
3. The outer boundary the vendors name: a container, a VM, or Claude Code's sandbox runtime.

## Decision Outcome

Chosen: **option 2 where the vendor documents a sandbox with nothing asking for the launch the cockpit
makes; option 1 for the rest.** Option 3 is `watch`: no container or VM runtime and no `srt` is
installed here, each would be the owner's install, and the sandbox runtime "is a beta research preview".

| Runtime | Launch arguments | Two incidents stopped | Nothing asks |
|---|---|---|---|
| Codex 0.167.0 | `-c approval_policy="never" -c 'permissions.canon-worker={extends=":workspace", filesystem={":slash_tmp"="read", "~/.codex/auth.json"="deny"}}' -c default_permissions="canon-worker"` | both, by probe | by two runs |
| Muse Code 1.4.15 | `--trust-workspace --disable-approval --sandbox-network restricted` | neither, by probe | documented; the flag untried |
| Antigravity 1.5.5, DeepSeek Harness 0.2.1-rc.3, Claude Code | unchanged | — | — |

1. **Codex.** `:workspace` "allows writes inside the active workspace roots and system temp directories"
   and keeps "the workspace root's `.git`, `.agents`, `.codex`, and `.aws` paths read-only". The form
   makes `/tmp` read-only and denies the sign-in file to sandboxed commands; Codex itself still signs in.
   Under `approval_policy = "never"` a refused command fails and the model carries on. Profiles "do not
   compose with the older sandbox settings", so no `--sandbox`. The cockpit holds one argument list per
   agent, so this form serves a reader too; `extends=":read-only"` with `":tmpdir" = "write"` is the
   tighter reader form for a run started by hand. Three costs. Two offline tests open a localhost
   listener, which the sandbox refuses. A zsh here-document fails, since its temp file goes to `/tmp`.
   And a builder cannot commit: **supersedes ADR 0058 rule 2's "commits on its branch" for a Codex build
   worker.** It leaves the change in the working tree, and the lane that checks it commits it.
2. **Muse Code** moves from `--yolo` to `--disable-approval`, "keep the sandbox, but skip approval
   prompts". `restricted` takes the network off; the default `proxy-only` stops at each new host "for
   review". This sandbox stops neither incident. Its docs say "the `.git`, `.muse`, and `.agents`
   directories stay read-only". Yet the probe wrote the worktree's `.git` pointer file and the main
   clone's `.git/config`, and the root of `/private/tmp`. Muse's own policy text says "The resolved
   active-repository Git storage is also writable" and "system temp roots remain full read/write", and
   its docs name no setting that narrows them. What it holds: no writes to `$HOME` or `~/.cache`, and no
   network. Reads are not bounded. Its `:read-only` profile stalled in its one trial.
3. **Antigravity** stays on its bypass flag. Its CLI sandbox is prompt-free only in print mode, where an
   action needing approval "is soft-denied". In the interactive session the cockpit starts, a request to
   leave the sandbox "always require[s] your approval unless the command matches a `command` allow rule"
   outside `auto`. In `auto`, "irreversible commands still prompt for confirmation". The print-mode form
   is in Confirmation.
4. **DeepSeek Harness** stays as it is. Its presets pair each sandbox with `approval: ask`. Its guide says
   "`approval: never` rejects approval requests; it does not auto-approve" and "Without an answerer,
   official approval fails closed". For the cockpit's `dsh-tui .` that would be a profile patch row,
   untried; its one sandboxed trial, `read-only`, ran out its deadline with no output. (Amended 2026-11-13
   by ADR 0136: the dsh-tui profile refuses every approval request and offers no preset without the sandbox,
   through an `approval` row and a `permission` row in its user patch.)
5. **Claude Code** lanes stay under ADR 0025. Its vendor asks for a container, a VM or the sandbox
   runtime around a bypass session (option 3).

**Threat model, today.** A worker started with a bypass flag can do whatever this user can. It can read
the keychain pair through `security` (ADR 0029), use the `gh` sign-in, write any checkout's `.git`, and
reach any host; only its brief and the check afterwards stop it. A plugin or hook inside a runtime has
that runtime's rights, because these sandboxes bound only the commands the model runs. Any process of
this user can read the keychain pair. A repository file that configures a runtime (`AGENTS.md`, a project
settings or profile file) steers whatever loads it. Under rule 1's form, a Codex command can write only
its workspace, minus the workspace's `.git`, and the system temp directories.

### Consequences

* Good: Codex, the first reader of the riskiest paths, runs in a sandbox that stops both incidents with
  nothing asking, once one setting is made. Muse loses network and `$HOME` writes.
* Bad: Antigravity, DeepSeek Harness and Claude Code lanes run unbounded, and Muse's sandbox leaves both
  incidents open. A Codex worker reports two localhost tests as failing, and its build needs a commit by
  the lane.

### Owner step

Orca 1.4.255, Settings → Agents → per-agent launch defaults, the agent's Arguments. A "non-empty custom
value" is "an explicit override", left out of permission-mode changes. Set Codex's and Muse's Arguments
to the table's. Set Muse to Manual under Agent Permissions → Other agents, so that no `--yolo` is
pre-filled. That Orca then starts each without its bypass flag was read in its bundle and is UNVERIFIED
by a launch: the first worker's process line shows it.

### Confirmation

The trial is in ADR 0025's Confirmation. UNVERIFIED: a cockpit start with these arguments; Muse's
`--disable-approval` in any run; whether a sandboxed command reaches the login keychain's items (a
throwaway keychain file's item was refused inside Codex's sandbox); DeepSeek Harness under
`approval: never`.

**Codex's profile in its config file, 2026-11-11.** `home/.codex/defaults.toml` now carries rule 1's three
settings (`approval_policy = "never"`, `default_permissions = "canon-worker"` and the `canon-worker` profile),
and `scripts/bootstrap --user-config` writes whichever of them `${CODEX_HOME:-~/.codex}/config.toml` lacks at
its top, keeping a dated `.replaced-*` copy and leaving every other key, and a value the owner set otherwise,
as it was. `scripts/bootstrap --check` has a `codex permissions` row: ok when the profile is present and the
default, NOT SET naming what is missing, OWNER when a value differs or the file sets `sandbox_mode`, which
Codex uses in place of `default_permissions`. Two runs of Codex 0.167.0 from an empty folder, `codex exec`
with the three settings as `-c` overrides:

* without a bypass flag: no prompt; `touch /tmp/<probe>` failed with "Operation not permitted" and the file
  did not exist afterwards; a read of `~/.codex/auth.json` failed with "Operation not permitted" (the
  command discarded its output, so nothing of the file was shown);
* with `--dangerously-bypass-approvals-and-sandbox` added, the flag Orca pre-fills for Codex: the same
  `/tmp` write succeeded. The flag wins over the profile, as Codex's precedence puts CLI flags first and a
  `--sandbox` over `default_permissions`.

So the profile holds for a Codex started without the bypass flag. An Orca-started Codex worker gets it
once Orca stops pre-filling that flag for Codex (Settings → Agents → Agent Permissions, Codex from Yolo
to Manual, the owner's switch); with approvals `never` from the config, Manual should add no prompt.
Until then `AGENTS.md`'s sentence about the bypass flag stands.

Three limits. Codex keeps its sign-in file in `CODEX_HOME`, so when that is set the profile written there
denies `$CODEX_HOME/auth.json` in place of `~/.codex/auth.json`. Orca starts Codex with a `CODEX_HOME` of its
own, a managed folder with its own `config.toml` and sign-in file. A run of `--user-config` without that
variable writes `~/.codex/config.toml`, which an Orca-started Codex does not read unless Orca carries it
over. The settings are user-level, so they also hold for a Codex the owner starts by hand. `--check`
reads the user config only, and a project `.codex/config.toml` that sets `sandbox_mode` would still win.
UNVERIFIED: whether Orca copies `~/.codex/config.toml` into its managed home; an Orca launch in Manual
mode, and that it adds no prompt; the probes on Codex 0.167.1, which the machine moved to the same day.

Antigravity's print form, for a run started by hand, uses absolute paths: `agy -p --sandbox --settings`.
The settings set `enableTerminalSandbox` and `"toolPermission": "proceed-in-sandbox"`. They allow
`read_file` of mise's installs, binary and config and of the checkout's common `.git`, which a worktree
needs (the trial's builder had a standalone tree and did without it). They allow `write_file` of the
run's temp folder and, for a builder, the workspace. They deny `write_file` of `/private/tmp`, `/tmp`,
`~/.cache` and the workspace's `.git`, and for a reader the workspace.

## Evidence

Read 2026-11-11. `fetched`: downloaded whole with `curl` and searched; `run`: a command run here.

* https://learn.chatgpt.com/docs/config-file/config-basic.md — the resolution order, "CLI flags and
  `--config` overrides" first, user `config.toml` fourth; https://learn.chatgpt.com/docs/permissions.md —
  "If `sandbox_mode` appears in any loaded config file, you pass `--sandbox`, ... Codex uses those older
  sandbox settings instead of `default_permissions`"; the old developers.openai.com/codex address
  redirects to learn.chatgpt.com (fetched 2026-11-11).
* https://learn.chatgpt.com/docs/permissions.md — the built-in profiles, `:workspace`'s two sentences,
  `extends`, `:tmpdir` and `:slash_tmp`, "do not compose with the older sandbox settings"; on macOS
  Codex "refuses to run the command instead of silently running it unsandboxed" (fetched).
* https://learn.chatgpt.com/docs/agent-approvals-security.md — the bypass flag, "No sandbox; no
  approvals _(not recommended)_"; a container as the boundary for `danger-full-access` (fetched).
* https://learn.chatgpt.com/docs/non-interactive-mode.md — "Use `danger-full-access` only in a
  controlled environment (for example, an isolated CI runner or container)" (fetched).
* `codex exec --help`, `codex sandbox --help` at 0.167.0 — `-c`, `--ignore-user-config`, `-P` (bundled).
* https://dev.meta.ai/docs/muse-code/permissions.md — the two layers, `--disable-approval`, `--yolo`
  "only where you already trust both the environment and the code", the network modes, the `.git`
  sentence (fetched); /docs/muse-code/extending.md — Headless and CI (fetched). The Muse 1.4.15
  binary — the two policy sentences and the `:read-only` built-in (`strings`).
* https://antigravity.google/docs/sandbox.md (its Antigravity CLI section), /docs/cli/modes.md,
  /docs/cli/headless.md — the CLI sandbox settings and flags, the two quotes in rule 3, "soft-denied"
  (fetched). `agy --help` at 1.5.5 — `--sandbox`, `--settings` "never saved" (run).
* dsh-TUI 0.17.0 `guide/dsh-tui-guide/cli.en.md` in the installed profile — the permission modes and
  the two quotes in rule 4 (read). `dsh --profile headless --dump-config` — the presets (run).
* https://code.claude.com/docs/en/sandbox-environments.md — "Always run `--dangerously-skip-permissions`
  sessions inside a container, a VM, or the sandbox runtime"; "beta research preview" (fetched).
* https://www.onorca.dev/docs/agents/supported, /docs/settings — the pre-filled flags, Agent Permissions,
  "Other agents", the override sentence, per-agent "Command and Arguments (and environment)" (fetched).
  Orca 1.4.255's CLI bundle — modes `own`, `ask`, `accept-edits`, `auto`, `full-access`; Muse mapped to
  `--yolo`; the keys that mark custom arguments as carrying a mode (read, `grep`).
* https://raw.githubusercontent.com/apple/container/main/README.md — Linux containers "as lightweight
  virtual machines", macOS 26, an administrator password; release 1.7.1, 2026-11-06 (fetched; `gh api`).
  `command -v` for docker, podman, colima, lima, container and srt: none (run).
* ADR 0058 rule 2 and `AGENTS.md`'s Orca section, "commits on its branch" (read). The trial (run).
* Second reader, 2026-11-11 (`evidence-verifier`): the quotes held. It found a refused command in the
  Codex reader's run, sandbox notices in Antigravity's, Muse's prompt-free claim untried, DeepSeek
  Harness's `approval: never`, the CLI rather than App quotes, the narrower `.git` sentence, the
  supersede line, the docs-and-trial disagreements and the length; all above and in ADR 0025.
