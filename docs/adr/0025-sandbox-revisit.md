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

**This record turns nothing on.** Turning it on is a separate change, made when no other session is
running, tried in one workspace first. It lands only if `scripts/ci` and `scripts/land` run to the end
with it on, `scripts/land` as an excluded command, with no new prompt and no failure, each allowance and
exclusion written down with its reason. If a lane stops or fails there, the change is dropped,
Confirmation says so, and the state is option 1.

What has changed since ADR 0020, each checked. The adapter places and cancels orders, on paper only: its
config refuses anything else and the client is built with `paper=True` (ADR 0030). Paper keys come next;
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
0028 takes the prompt off a single pin move and an exactly pinned `uv add`. `uv sync --locked` still
asks: an `ask` rule wins over any `allow` rule, so no rule can ask for `uv sync` and spare `--locked`.
Package code an exactly pinned `uv add` installs then runs unprompted, and the sandbox would keep its
writes to the worktree, uv's cache and the temp folder.

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

Trial, 2026-11-10: Claude Code 2.1.321, macOS, bypass mode, through an ignored file in one worktree,
`.claude/settings.local.json`, created for it and deleted afterwards. It took effect in the
running session within seconds and no prompt appeared. `scripts/ci` failed 7 of 20 checks and skipped 5:
bare `mktemp -d` used the macOS temp folder, not the sandbox's `$TMPDIR`, and uv could not write its cache.
With both writable, `test-ci` still failed until `com.apple.sysmond` could be looked up; then all 20
passed. `gh api` failed with `tls: failed to verify certificate: x509: OSStatus -26276`, as the docs warn
it may, `gh auth status` failed too, and so `scripts/land --dry-run` stopped at "gh is not signed in or
cannot reach GitHub"; a `gh` call listed in `excludedCommands` ran. The three logs were kept outside the
repository, in the machine's temp folder; the second also holds one pytest failure that the third, with
no code changed, does not.

The change that turns it on must still try what this trial did not: a commit, `scripts/land` to the end as
an excluded command, `uv sync`, a pin move, the `orca` command a session reports with, and a paper test's
stream through the proxy (all UNVERIFIED). It waits because a settings file reloads into running sessions.

From 2026-11-11, ADR 0034 decides the sandbox for the other runtimes, from the trial below; this record's
decision for Claude Code is unchanged.

Trial of the other runtimes, 2026-11-11, macOS 27.2: Codex 0.167.0, Muse Code 1.4.15, Antigravity 1.5.5,
DeepSeek Harness 0.2.1-rc.3 with dsh-TUI 0.17.0. Each CLI ran non-interactively, with no credential, in a
throwaway clone at `5df7c9f` under the home folder, outside every temp folder, with `core.hooksPath` set
and one worktree per runtime, as the cockpit makes them. Each got its own `TMPDIR` and a `.venv` built
beforehand. A throwaway home was not used: each runtime keeps its sign-in in its own home. The model ran
a probe script that tries a write to the root of `/private/tmp` (a harmless name standing in for
`mise.toml`), `git config --unset core.hooksPath` from the worktree, which writes the main clone's
`.git/config`, and writes to the workspace, `$HOME` and `~/.cache/uv`, the network, and `uv run
--locked`. Codex's probes ran through `codex sandbox -P`; the others' through the model.

| Runtime and mode | `/private/tmp` | shared `.git/config` | workspace | `$HOME`, `~/.cache/uv` | network | `uv run` |
|---|---|---|---|---|---|---|
| Codex `:read-only` | refused | refused | refused | refused | refused | refused (uv's cache) |
| Codex reader: `:read-only`, own `:tmpdir` write, `auth.json` denied | refused | refused | refused | refused | refused | ran, `UV_CACHE_DIR` in `TMPDIR` |
| Codex builder: `:workspace`, `:slash_tmp` read | refused | refused | written; its `.git` refused | refused | refused | ran |
| Muse `--approval-mode never`, network `restricted` | written | written | written, `.git` too | refused | refused | ran |
| Muse, the same in a plain clone | written | own `.git/config` written; another clone's refused | written | refused | refused | — |
| Antigravity `--sandbox`, `proceed-in-sandbox` | written | refused (not mounted) | refused | `$HOME` refused, `~/.cache/uv` written | refused | refused (mise not mounted) |
| Antigravity, with the grants and denials in ADR 0034's Confirmation | refused | refused | written with a `write_file` grant | refused | refused | ran |

The Muse `:read-only` profile (`--permission-profile :read-only`) and the DeepSeek Harness `read-only`
mode (`dsh-tui run --profile dsh-tui --permission-mode read-only` with `home/dsh/review.patch.yml`)
each ran the one-command probe for ten minutes with no output and were stopped by their deadlines. The
`headless` profile could not sign in: the key is the `dsh-tui` profile's, and was not copied. A dummy
item in a throwaway keychain file was read outside Codex's sandbox and not inside it, though `securityd`
answered there too. Two results disagree with the vendor's docs: Muse's say `.git` stays read-only in the
workspace, and Antigravity's that sandboxed commands "can write to your workspace"; the table is what
ran.

Whole tasks under the chosen forms, with no prompt possible. **Codex reader**
(`codex exec --ignore-user-config --disable hooks --ephemeral`, the reader profile, `approval_policy =
"never"`, `gpt-6.1-sol` at `max`) on the replace-chain review brief. The first run ended after a minute
with no prompt. Codex's hosted web tool returned `Internal Error` for the adapter guide the brief cites,
though the page answers 200 here, and the reader stopped under rule 5, so the second run read the guide
from a saved copy. In 14 minutes it ran 65 commands, one refused (a zsh here-document's temp file under `/tmp`, which it
wrote with its patch tool instead), and wrote nothing in the checkout. It
wrote six findings, each reproduced in its temp folder, before it was stopped unfinished when the trial
was cut short. **Codex builder** on the build-eval case
`cancel-again-and-unholdable-id`: 9 minutes, no prompt, no refusal it could not work around. The four
named tests (five cases) passed, `ruff`, `ruff format` and `ty` passed, and the change was left uncommitted. Of the
offline suite, 494 passed and 3 failed. Two failed because the sandbox refused `socket.create_server(("127.0.0.1",
0))` (`Errno 1`). One failed because the worker put its own `TMPDIR` inside the checkout, so a test that
expects a temp folder outside git did not get one. The worker named all three. **Antigravity** reader
and builder, print mode, the forms above: the reader ran 15 commands and wrote only in its temp folder;
the builder ran 28 and edited the tree. Neither asked to leave the sandbox or met a prompt; 5 and 9 of
their commands carried a "sandbox errors that may or may not be related" notice, and both were stopped
after about 15 minutes, before writing a report, when the trial was cut short. Muse and DeepSeek Harness ran no whole task, since
neither has a form ADR 0034 adopts with both properties shown. Afterwards the main clone's
`core.hooksPath` was `.githooks`, every change Muse made was put back, and the clone was removed; this
checkout and the real main checkout showed no change.

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
* The trial — commands and exit codes in the session that ran it, and the three logs. That session's
  process arguments, its environment (no `ALPACA` variable), and no
  `/Library/Application Support/ClaudeCode/` (run). ADR 0009, 0018, 0020, 0023, the adapter's README, both
  settings files and `scripts/land` (read). ADR 0028 was not read: it had not landed.
* The owner's instructions, 2026-11-10 (reported to the author, not read first-hand): "auto proceed with sota
  convergence you as master orchestrator"; "high quality is the directive, research convergence
  convergenced is the evidance, proceed with sota evidances automatically as long as they are form sota
  sources, per our very harness rules"; and the driver, "make sure every lanes are seamless without
  consistant premission frictions". The coordinator chose option 3 and its terms the same day.
* Second reader, 2026-11-10 (`evidence-verifier`, two passes): every quotation and the hosts held. It
  corrected the second run's account and the cause of the `gh` failure, narrowed two table rows and option
  2, added auto mode, `credentials.envVars` and `--settings`, marked ADR 0028 and the managed file's reach
  UNVERIFIED, and supplied the missing item of ADR 0020's list and what lets a session leave option 3.
* The 2026-11-11 trial — the Muse (worktree), Antigravity and DeepSeek Harness probe outputs, each
  whole task's event stream and the Codex reports, copied to a session folder outside the repository
  before the clone was removed. The three Codex rows, the Muse plain-clone row and the Muse
  `:read-only` stall were printed in the session that ran them and not kept (run).
* Second reader of the 2026-11-11 trial (`evidence-verifier`, 2026-11-11): it found the Codex reader's
  refused command, Antigravity's sandbox notices, the four tests and which probe outputs were kept;
  corrected above.
* ADR 0028, read 2026-11-10 after it landed: it leaves `uv sync --locked` asking. The sentence on prompts
  above is corrected to say so (read).
