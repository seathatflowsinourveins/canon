---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Runtimes update themselves where the vendor provides it: Claude Code on latest above the pin, Antigravity's updater on, except in the sessions the cockpit starts with a prompt

## Context and Problem Statement

ADR 0008 and ADR 0022 held Claude Code on the `stable` channel, about a week behind, and ADR 0045 turned
Antigravity's self-updater off so it stayed at its pin. The owner's requirement of 2026-11-10: the practice automatically defaults to the state of the art
and is LLM-native, through environment settings, CLI updates and the like.
Which runtimes update themselves, through which setting, and what does a registry pin then mean?

## Decision Drivers

* The owner's requirement above.
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

### Addition of 2026-11-11: the machine layer follows main by itself

The owner's instruction of 2026-11-11: the machine layer is reinstalled by itself whenever main changes
it, so no one has to remember an installer. A fourth launchd user agent, `local.canon.maintenance`, is
installed, reported and removed by `scripts/automation` beside ADR 0011's watchdog and ADR 0050's paper
agent and ADR 0007's Collector. It runs `~/.claude/canon/maintenance.sh` every 30 minutes
(`StartInterval` 1800, which launchd counts from the end of a round, ADR 0050; not at load). Each round is logged, and each step is bounded by timeout(1):

1. **The paper lock first.** The round takes, without waiting (lockf(1) `-t 0`), the three file locks
   that mise.toml's paper tasks take (ADR 0069): account B's, which a window holds; account A's, which a
   paper test holds; and the market-data stream's, which `paper:data`'s sandbox node takes alone. "The
   paper lock" here means all three. When one is held, one of those runs is running, perhaps from the
   checkout, and the round does nothing at all. The status row names the lock. The round also holds a lock of its own, so two rounds never overlap.
2. **(c) Once a day, the vendors' update commands** (rule 5), with the paper lock let go: `claude update`
   and `agy update`. They touch nothing a window uses, and a scheduled window waits only 900 s for the
   lock (mise.toml), so they do not hold it. Codex, Muse Code, DeepSeek Harness and `dsh-tui` are not
   updated (rule 4). Codex, DeepSeek Harness and `dsh-tui` are exact mise pins, whose versions move through
   the drift issue and `adopt-upstream`. Then the round takes the paper lock again without waiting. If a
   window took it meanwhile, the round ends there.
3. **(a) Fast-forward only.** The main checkout moves to origin/main with `git merge --ff-only`, and only
   when it is on main with no tracked change and the fetch succeeded. Otherwise neither (a) nor (b) runs,
   and the status row says why: SKIPPED, or FAILED for a fetch. It never resets, stashes or switches a
   branch.
4. **(b) What differs, then the installer that manages it.** At origin/main, `scripts/automation differs`
   compares what is installed with the tree. It checks the installed scripts, the Collector's
   configuration, each plist as install would write it now, the loaded agents, and a checksum of the
   settings sources (and of which runtimes' folders exist, since the settings step writes only into
   those) against the last settings step. A settings difference runs `scripts/bootstrap
   --user-config`, inline. An agent or script difference runs `scripts/automation install`, last and
   detached: the job is put in a process group of its own (bash `set -m`), and the agent's plist sets
   `AbandonProcessGroup`. So when install reloads this agent's own job, the reinstall is not ended with
   it. Install loads the maintenance job after everything else, and reloads it only when its plist
   changed or it is not loaded. The paper agent, the Collector and the gateway are kept the same way when
   their files are unchanged. So a landing that leaves them alone does not restart the paper interval
   (ADR 0050). An install is not repeated within a day for the same differences at the same origin/main
   once an install for them has finished. Otherwise a difference no install can fix, such as a Collector
   mise has not installed, would restart the paper interval every round. The row says FAILED until the
   next day, a new landing, or an install by hand. An install that timed out or never reported is tried
   again at the next round.
   The paper lock is held for (a), since any of those runs may run from the checkout. For the install,
   which reloads the paper agent and touches nothing else they use, the round holds account B's lock
   alone, so no window starts while it runs and a paper test is not held up. The compare and the
   settings step touch none of them and run with the locks let go. The install takes account B's again
   without waiting, and if a window took it meanwhile, the install waits for the next round.

The agent reads no secret, installs no tool (`scripts/bootstrap --install` stays a deliberate step) and
changes neither the registry nor the pins. Rule 6 stands: `scripts/bootstrap` itself runs no update.
Once the agent is installed, rule 5's "one command" runs once a day without anyone typing it.

What follows from this: whatever lands on main under `home/` and in the installers reaches this machine
at the next round, within half an hour of landing, and no person reads it first. A held paper lock, a
checkout kept off main, or a Mac asleep at the firing (launchd.plist(5)) puts it off to a later round. Main's gate is the control: `scripts/ci`, the
independent review of ADR 0017, and the `signoff` status that `scripts/land` posts. The agent adds no
control and removes none. A checkout that is off main, has a tracked change, or has a commit origin lacks
keeps the agent from installing.

`scripts/automation status` gains a `maintenance` row. It shows the last round, the fast-forward, what
differed, what was installed and what was left for the owner to merge. A step that failed is shown as
FAILED by name, and so is a reinstall that never reported. The row reads STALE when no round has run
for an interval past its longest round, SKIPPED when the checkout kept the round from main, and OUTDATED
when the installed script is not the tree's. The same install keeps ADR 0056's gateway running as an
agent (ADR 0056, addition of 2026-11-11), and the maintenance agent keeps that agent current like the
others. Each reinstall restarts the paper agent's interval (ADR 0050).

Confirmation, 2026-11-11, offline: `scripts/test-maintenance` runs the agent from an empty environment:
`HOME` and `CLAUDE_CONFIG_DIR`, as its plist gives them, plus the test's own overrides of getconf and the
bounds. It runs on a clone of a local bare origin, with stand-ins for both installers, `launchctl`, mise,
`claude` and `agy`, and getconf names a temporary folder for the paper lock. The cases:

* a fast-forward is done;
* the round is skipped on a dirty tree, on another branch, and when main has a commit origin lacks;
* when nothing differs, no installer runs;
* the settings step runs, and when it fails it is FAILED and is not recorded as applied;
* when any of the three locks is held, the round does nothing and names it; a paper test that takes its
  lock during the compare does not hold up the install; when a window takes the lock during the updates,
  the round ends there; when the agent's own lock is held, the record is left unchanged;
* the same differences an install left are not installed again within a day at the same origin/main,
  but are at a new one, after a day, and after an install that timed out;
* a compare that failed keeps that record;
* a window that takes the lock during the compare puts the install off to the next round;
* a fetch that hangs, and an installer that hangs, are stopped at their bounds, and with the fetch
  failed nothing is compared or installed;
* the updates run once a day, Codex is left untouched, and an update that fails or hangs is named;
* the agent starts a reinstall itself. The stand-in launchctl kills the job's process group at its
  bootout, and the round's process group is killed once the round ends: the worst case of launchd's
  kill "when a job dies" (launchd.plist(5)) with `AbandonProcessGroup` unset. The reinstall still
  completes and leaves all five agents loaded, holding account B's lock while it installs.

The real `scripts/automation`, rehearsed there, shows:

* the plists' arguments and keys;
* the maintenance, paper, Collector and gateway jobs kept loaded when their files are unchanged, the
  paper agent reloaded once its runner changed, the maintenance job loaded last, and a gateway agent
  removed once OmniRoute is gone;
* `differs` clean right after install, and naming a changed script, a changed gateway task, a gateway
  agent left behind, a runtime folder made since, and a changed settings source, and saying unknown, with
  the agent kept, when mise does not say whether OmniRoute is installed;
* the row's labels ok, FAILED, SKIPPED, STALE, OUTDATED, UNKNOWN and OFF.

UNVERIFIED until the first rounds on this machine:

* that real launchd's bootout of the maintenance job leaves the reinstall running, since both safeguards
  were shown only against the stand-in;
* that `claude update` and `agy update` complete from a launchd job with no terminal, and that `agy
  update` replaces the copy mise runs (above);
* the first round itself.

**2026-11-11, later: a window defers only what it runs from.** The owner required on 2026-11-11 that the
machine follow main by itself. Once a session's run is in progress through every weekday, step 1 would
have stopped the agent from Sunday evening to Friday evening. So a held windows' lock now defers only
what a running window depends on: the paper section of `scripts/automation install`, which covers the
runner, its plist, its agent and the clone the runner makes anew at each firing. The rest of the round
runs at once: the fast-forward, the settings step, `install --defer-paper` for the other agents, the
updates and the pins below. A paper test's lock, or the stream's held with no window running, still ends
the round, since those runs may run from the main checkout. The deferred part waits for the session
boundary, the moment the window's lock goes free. Alpaca's clock and calendar end each run at its
session's end, but this agent reads no secret and cannot read them itself. A round that defers starts
one detached waiter, which holds its own lock so a second is never added. The waiter waits on the
windows' lock (lockf(1) `-t`) for at most nine hours: the overnight session's eight and one more. With
that lock held, it runs a round that installs the whole and lets the lock go when the install ends, so
the next session's run starts from the clone its runner makes then. The status row reads "deferred to
the session boundary: the paper checkout". It reads FAILED once the deferral is older than nine hours,
or when the waiter timed out or stopped reporting. **(d) The exact pins.** At origin/main, each round
also brings mise (`mise self-update --yes --no-plugins <version>`) and the tools in mise's global config
with an exact registry pin (codex, pnpm, DeepSeek Harness, dsh-tui) to the versions
`canon/upstreams.json` records (`mise use --global --yes <tool>@<version>`). A tool already at its pin
is left alone, and so is one the global config does not name. `scripts/bootstrap --check` gains a
`codex version` row. Limits: a window started by hand from the main checkout is not told apart from
the scheduled one. Confirmed offline by `scripts/test-maintenance` with the stand-ins:

* a held windows' lock still moves the main checkout and installs the rest;
* a second round adds no second waiter;
* the waiter's round installs the whole once the lock frees, and a waiter past its bound is named;
* a pin that moved is applied, one already met changes nothing, and a failed update is FAILED by name.

UNVERIFIED until it runs here: that `mise self-update` replaces the binary while a window's mise runs
from it without disturbing that window.

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
* Addition of 2026-11-11, read and run 2026-11-11:
  * launchd.plist(5) (`man launchd.plist`), read:
    * `StartInterval`: "If the job is running during an interval firing, that interval firing will
      likewise be missed";
    * `AbandonProcessGroup`: "When a job dies, launchd kills any remaining processes with the same
      process group ID as the job. Setting this key to true disables that behavior".
  * bash(1) `set -m`: "Background processes run in a separate process group" (read).
  * lockf(1) `-t`; git-merge(1) `--ff-only`; git-config(1) `remote.<name>.uploadpack`, which the test
    uses for a fetch that hangs (read).
  * `scripts/test-maintenance` (run).
* 2026-11-11, later addition, read and run 2026-11-11:
  * lockf(1) (`man lockf`), read: the form `lockf [-s] [-t seconds] fd`; "-t seconds Specifies a
    timeout for waiting for the lock"; a timed-out wait exited 75 (EX_TEMPFAIL), run here.
  * `mise help self-update` (mise 2026.11.5, bundled): "[VERSION] Update to a specific version";
    `--yes`, `--no-plugins`. mise's src/cli/self_update.rs strips a leading `v` (fetched, at main, not
    the tag).
  * `mise help use` (bundled): `-g, --global` "Write to the global config"; `mise help ls`: `--global`,
    `--json`; `mise ls -g --json codex` printed an array with `requested_version` and `installed` (run).
  * session_window.py `OVERNIGHT_STARTS` (20:00) and the overnight session's end at 04:00 New York,
    which the nine-hour bound follows (read).
  * `scripts/test-maintenance` (run).
