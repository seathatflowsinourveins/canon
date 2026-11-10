---
status: accepted
date: 2026-11-09
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2027-02-09
---

# Toolchain on macOS: Command Line Tools, mise with a lockfile, uv, native Claude Code

## Context and Problem Statement

On 2026-11-09 this Mac (Apple silicon, macOS 27.2) had no developer tools at all: no Command Line Tools, so no
`git` or `python3`; no Homebrew, Node, `gh`, `uv` or Claude Code CLI. What gets installed, from where, and how
are versions held?

## Decision Drivers

* Versions pinned and reproducible (ADR 0001).
* Installable without `sudo` where possible, so an agent can run the bootstrap.
* As few moving parts as the stack needs.

## Considered Options

1. Homebrew for everything.
2. Nix or devbox.
3. Xcode Command Line Tools for git and compilers; mise for tool versions with `mise.lock`; uv for Python;
   Claude Code's native installer.

## Decision Outcome

Chosen: **option 3**.

| Tool | Source | Version held by |
|---|---|---|
| git, clang, make | Xcode Command Line Tools (`xcode-select --install`) | Apple |
| mise | `https://mise.run` installer, into `~/.local/bin`, no sudo | `MISE_VERSION=v2026.11.5` in `scripts/bootstrap` |
| Python 3.14, uv 0.13.7, Node, gh | mise | `mise.toml` plus `mise.lock` (versions, URLs, checksums) |
| Claude Code CLI | `https://claude.ai/install.sh`, into `~/.local/bin` | `stable` channel |
| ccusage | run on demand with `npx ccusage@20.0.48` | version in the command |

Notes:

* Python 3.14 because the pinned NautilusTrader release ships wheels for 3.13 and 3.14 only (3.15 is a
  source build) and upstream recommends 3.14.
* Claude Code is the one tool allowed to auto-update. It follows the `stable` channel (about a week behind
  `latest`), which serves as a cooldown. The desktop app updates on its own schedule regardless. A newly
  launched model can need a CLI newer than `stable` serves.
  ADR 0022 supersedes the Claude Code row above and this note in part: the CLI is installed at the
  registry's pin and held at or above it.
* Homebrew is not installed: it needs `sudo` and its formulae roll forward (UNVERIFIED: no source was read
  for this). Add it later if a tool needs it.
* mise applies a `minimum_release_age` cooldown of 24 hours by default, so a release under a day old is
  not picked up. A longer window is not configured: its placement in `mise.toml` was not confirmed on a
  fetched page (UNVERIFIED).

### Consequences

* Good: one file pins the toolchain; the bootstrap needs no password after Command Line Tools are in.
* Bad: Command Line Tools still need a click from the owner. The choice of mise rests on the drivers above;
  no adoption comparison against asdf, Nix or devbox was found (UNVERIFIED), so this is not a convergence
  claim.

### Confirmation

`scripts/bootstrap --check` reports each tool's presence and version without installing anything.

Installed on 2026-11-09 by `scripts/bootstrap --install`: mise 2026.11.5, Python 3.14.10, uv 0.13.7 (build
`316c827e0`, the same commit the canon pins), Node 24.22.2, gh 2.103.3, Claude Code CLI 2.1.311. At that
point `mise.lock` held four tools across 28 platform targets, each with a URL and checksum,
`mise install --locked` reproduced the install, and it took about 600 MB. Three tools were added later
(ADR 0013), and `~/.local` is now about 840 MB.

The `stable` channel gave CLI 2.1.311, while the desktop app runs 2.1.321, the version the canon pins and
whose docs ADR 0003 cites. Not verified: that every setting under `.claude/` behaves the same on 2.1.311.

## Evidence

Read 2026-11-09 (fetched unless noted).

* https://mise.jdx.dev/installing-mise.html — `curl https://mise.run | sh` installs to `~/.local/bin`;
  `MISE_VERSION` pins; the installer verifies checksums.
* https://mise.jdx.dev/dev-tools/mise-lock.html — `[settings] lockfile = true`; `mise.lock` records resolved
  versions, download URLs and checksums; `mise install --locked` (fetched by the orchestrator).
* https://mise.jdx.dev/configuration/settings.html — signature and attestation checks (`aqua.cosign`,
  `aqua.slsa`, `github_attestations`) default to true (fetched by the orchestrator).
* https://github.com/jdx/mise/releases — v2026.11.5 (resolved to `32c96c4d51d3`).
* https://docs.astral.sh/uv/getting-started/installation/ and https://github.com/astral-sh/uv/releases
  — uv 0.13.7 (resolved to `316c827e058c`).
* https://github.com/nautechsystems/nautilus_trader — lists Python 3.13 to 3.15 with 3.14 recommended, and
  says 3.15 needs a source build; install with uv into a virtual environment; conda unsupported.
* Second reader, 2026-11-09 (`evidence-verifier`): the install, lockfile and channel claims are supported.
  The Python range, the lock and disk figures and the cooldown note were stale or too broad and are
  corrected above; `minimum_release_age` defaulting to 24 hours is from the same settings page.
* https://code.claude.com/docs/en/setup.md — native installer is the recommended install; `stable` channel;
  version pinning with `bash -s <version>`; requires macOS 13 or later.
* https://github.com/anthropics/claude-code — npm install marked deprecated in favour of the native
  installer.
* https://www.python.org/downloads/release/python-31410/ — Python 3.14.10 (fetched 2026-11-10; resolved to
  `b703b2c5e704`; second reader).
* https://nodejs.org/en/blog/release/v24.22.2 — Node 24.22.2, an LTS line in maintenance since 2026-10-20
  (fetched 2026-11-10; resolved to `f7f273a9bb64`; second reader).
* https://github.com/cli/cli/releases/tag/v2.103.3 — gh 2.103.3 (fetched 2026-11-10; resolved to
  `63837fb778d6`; second reader). These three were registered that day, and `scripts/canon lint` now fails
  when `mise.toml` installs a tool that no registry entry names.
