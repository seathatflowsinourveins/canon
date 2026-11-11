---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); master orchestrator (Claude)
review-by: 2026-12-10
---

# Claude Code CLI: installed at the registry's pin and never below it

## Context and Problem Statement

ADR 0008 let the terminal Claude Code CLI follow the `stable` channel and noted that a newly launched
model can need a newer CLI than that channel serves. That happened. The bootstrap installed 2.1.311 while
the registry pinned 2.1.321, the version the desktop app runs. Orca starts the terminal CLI (ADR 0018), so
its sessions came up on the older one, which does not know `claude-fable-5-2`, the model the lane table
gives orchestration (ADR 0005). Nothing compared the installed CLI with the pin. How is the terminal CLI
held to the version this repo's records were read at?

## Decision Drivers

* One pin, declared once: the registry says which version is adopted (ADR 0001, ADR 0013).
* The model a lane names must exist on the CLI that runs it (ADR 0005).
* Claude Code stays the one tool allowed to update itself (ADR 0008).
* The vendor's documented settings come before a script of this repo's own.

## Considered Options

1. Keep the `stable` channel alone.
2. Follow the `latest` channel.
3. Install the registry's pin, keep the `stable` channel for later updates, and hold a floor at the pin.
4. Install the registry's pin and switch every update off.

## Decision Outcome

Chosen: **option 3**.

1. **`scripts/bootstrap --install` brings the CLI to the pin.** When the CLI is absent or below the pin it
   runs the vendor's installer with the pinned version, then stops with an error if the installed version
   is still below the pin or cannot be read. A CLI at or above the pin is left alone.
2. **The settings template holds the floor.** `home/.claude/settings.json` sets `minimumVersion` to the
   pin, so that "moving to the `"stable"` channel does not downgrade you", and keeps `autoUpdatesChannel`
   at `stable`. (Superseded in part 2026-11-10 by ADR 0065 rules 1 and 2: the template names `latest`,
   and the floor stays.) Both reach a machine only through the bootstrap's user-config step. Without it a machine
   sits at the pin with no floor, on whatever channel the installer saved for a version argument, which
   the docs do not say.
3. **`scripts/canon lint` holds the pin and the floor together.** It fails when the template's
   `minimumVersion` differs from the registry's pin, and when the `claude-code` entry is not tracked as a
   release tag of dotted numbers, the only shape the bootstrap can install.
4. **`scripts/bootstrap --check` names a CLI below the pin `BEHIND`**, with the command that fixes it, and
   one whose version it cannot read `UNKNOWN`. Both exit non-zero.
5. **The machine's default model is `fable`**, the owner's choice for the sessions they talk to.
   (Superseded 2026-11-10 by ADR 0059 rule 3: the default is the build lane's `opus`, and the lead
   starts with `--model claude-fable-5-2`.) ADR 0005
   gives that model to the orchestrator lane and sets no machine default, so this adds one: a session
   started with no model named, in any project on the machine, now runs on it. Build and bulk work name
   their lane's model.

Option 1 is what failed. Option 2 is what the vendor's page advises for this case, "To run the model now,
move to the latest channel"; it is not taken because the pin with a floor reaches the same version and
keeps the week's cooldown ADR 0008 chose the stable channel for. Option 4 would keep fixes out until
someone moves the pin by hand.

Supersedes, in ADR 0008, the Claude Code row of its table and its note that the CLI follows the stable
channel. The rest of it stands.

### Consequences

* Good: a fresh machine starts at the pin, a CLI behind it is named with its fix, and the `fable` default
  resolves to the model the lane table names.
* Bad: moving the registry's pin now changes the settings template too. A machine whose settings already
  differ from the template gets the new floor as a proposal beside its file, for the owner to apply.
* Bad: the floor stops a move below the pin only. Above it, the stable channel can still move the CLI
  down from a newer build, and the records were read at the pin, not at what stable serves later.
* Bad: Fable is the costlier lane (ADR 0005), and it is now what an unnamed session runs on. (No longer
  so from 2026-11-10: ADR 0059 rule 3.)
* Bad: while the stable channel serves a version below the floor, each background update is refused and
  the session footer shows `Auto-update failed · Run claude doctor` in red. Nothing is wrong and nothing
  is to be done: the line goes when stable reaches the pin. (From 2026-11-10 the channel is `latest`, ADR
  0065, so this and the stable bullet above describe the record's original channel.)
* The desktop app bundles its own Claude Code and is outside this decision.

### Confirmation

On the owner's machine, 2026-11-10. With 2.1.311, `--model claude-fable-5-2` logged
`unrecognized_model` and a session started with `--model fable` gave its model as `claude-fable-5-1`.
`claude install 2.1.321` replaced it; then `fable` and `claude-fable-5-2` both gave `claude-fable-5-2`
with no warning, and with `model` set to `fable` a session started with no model named gave the same. A
model's answer about its own id is weak evidence; the CLI's warning and the docs' version table are the
firmer part. A session already running kept its 2.1.311 binary. `scripts/bootstrap --check` prints `ok
2.1.321; pinned 2.1.321`. `scripts/test-lanes` has 62 cases and `scripts/test-canon` 157. Later that day the footer showed the red line above; `claude doctor` gave `Last update
attempt: failed (install_failed)`, and `claude update` printed "The stable channel is at 2.1.311, which is
below your minimumVersion setting (2.1.321). Staying on 2.1.321." and changed nothing. That the background
attempt failed for the same reason is inferred from the two. UNVERIFIED: the
installer script with a version argument (the tests stub it, and the machine used `claude install`), the
updater's behaviour on this machine over time, and `--install` on a fresh machine.

## Evidence

Read 2026-11-10. "Second reader": the `evidence-verifier` fetched each page the same day and found the
quoted sentences verbatim.

* https://code.claude.com/docs/en/setup — "To install a specific version number: `curl -fsSL
  https://claude.ai/install.sh | bash -s 2.1.89`"; "The channel you choose at install time becomes your
  default for auto-updates"; `stable`: "use a version that is typically about one week old, skipping
  releases with major regressions"; "Background auto-updates and `claude update` refuse to install any
  version below this value, so moving to the `"stable"` channel does not downgrade you if you are already
  on a newer `"latest"` build."; "A newly launched model can require a Claude Code version newer than the
  stable channel serves. To run the model now, move to the latest channel." (fetched; second reader).
* https://code.claude.com/docs/en/settings — `minimumVersion` may be set in any settings file; switching
  channel in `/config` "asks whether to allow a downgrade" (fetched by the second reader).
* https://code.claude.com/docs/en/model-config — "Fable 5.2 requires Claude Code v2.1.321 or later, and
  Fable 5.1 requires v2.1.257 or later."; its version history: from v2.1.321 "`fable` resolves to Fable
  5.2" (fetched; second reader).
* https://code.claude.com/docs/en/changelog — 2.1.321, November 9, 2026: "Added Claude Fable 5.2
  (`claude-fable-5-2`), now the default Fable model" (fetched; second reader).
* https://code.claude.com/docs/en/troubleshoot-install.md — the red footer line "means Claude Code tried
  to update itself in the background and the update didn't install. You can keep working without doing
  anything"; `claude doctor` "gives a date but no cause", `claude update` shows it (fetched, not
  summarised). `claude doctor` and `claude update` at 2.1.321, and the release server's `stable` and
  `latest` files, 2.1.311 and 2.1.322 (run and fetched, 2026-11-10).
* `claude install --help`, at 2.1.311 and at 2.1.321 — "Use [target] to specify version (stable, latest,
  or specific version)" (bundled).
* `canon/upstreams.json` — `claude-code` at `tag:v2.1.321`; `canon/models.json` — the orchestrator lane,
  `claude-fable-5-2`, alias `fable`; ADR 0005 — `opus` as the default build session (read).
* ADR 0008 — "The `stable` channel gave CLI 2.1.311, while the desktop app runs 2.1.321" and "A newly
  launched model can need a CLI newer than `stable` serves" (read).
* The commands in Confirmation, each run from an empty environment (run; the installed version and the
  three settings confirmed by the second reader).
* Second reader, 2026-11-10: it found the docs' own sentence on downgrades, which the first draft had
  called silent; that the floor reaches a machine only with the user config; that the vendor advises the
  latest channel; and that ADR 0005 sets no machine default. An independent code review had `--install`
  upgrade a CLI behind the pin and the lint refuse a pin the bootstrap cannot install. All are above.
* Pin moved 2026-11-11 to `tag:v2.1.322` → `6e1e44f6433b` (resolved), with `minimumVersion` in the settings
  template. https://github.com/anthropics/claude-code/releases/tag/v2.1.322 — published 2026-11-10, its one
  note "Added fast mode on Vertex in the global, us and eu regions" (`gh`). `claude --version` gave 2.1.322
  on this machine before the move (run).
