---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Muse Code, Antigravity and DeepSeek Harness as review-only workers, and one review lifecycle for every change

## Context and Problem Statement

ADR 0043 adopted Codex as an Orca worker that reads a change and reports. On the owner's instruction of
2026-11-10 three more were installed machine-level through mise, signed in by the owner and tried as
reviewers: Muse Code (`muse` 1.4.15-R8339.1), Antigravity (`agy` 1.5.3) and DeepSeek Harness (`dsh`
0.2.1-rc.3 with the community `dsh-tui` 0.17.0). Under what terms, and how does a review reach a change
the same way every time?

## Decision Drivers

* ADR 0043's terms hold for any reader; a model never blocks (ADR 0017 rule 1). Paper only (ADR 0009).
* The owner's word decides a tool that is cheap to leave; what is untested is UNVERIFIED (ADR 0018).
* A review that waits on someone remembering it gets skipped.

## Considered Options

1. Keep Codex the only second reader.
2. All three under ADR 0043's terms, each review started by hand after landing.
3. Option 2, with the review an Orca task that depends on the build task, and reviewers assigned by trial.

## Decision Outcome

Chosen: **option 3**, on the owner's instruction; the open choices made by the coordinator under the
owner's delegation of 2026-11-10.

1. **ADR 0043's terms, unchanged, for every second reader**: findings only, never an edit, commit, push,
   landing, paper run or credential; a new worktree at the commit, checked clean and removed afterwards;
   never in place of `/code-review` or the `evidence-verifier`, never blocking.
2. **What differs per runtime.** Orca's bypass map pre-fills `--yolo` for Muse and
   `--dangerously-skip-permissions` for Antigravity, so both run as unsandboxed as Codex (ADR 0043 rule
   2; how Orca applies the map was not traced). A reviewer stopped at a prompt is stopped, never answered.
   * *Muse Code* (Meta, proprietary, no repository): approval, an OS sandbox and a Read-only profile of
     its own. Browser or API-key sign-in (which, not read). mise takes `latest` from a channel holding
     one release, past its release-age guard on the owner's word: no lock pin; `mise upgrade` moves it.
   * *Antigravity* (Google, no licence file): `--sandbox`, `--mode plan`; keyring sign-in; Google
     collects interactions data unless opted out. Its self-updater moved it to 1.5.5 past the 1.5.3 pin;
     `AGY_CLI_DISABLE_AUTO_UPDATE=true` stops that, the owner's to set.
   * *DeepSeek Harness* (MIT, pre-release only; agent id `dsh`, no entry in the map): API key in its own
     file (not read). Orca launches `dsh-tui .`, whose `/update` moves past the pin. Open Orca issues: a
     first prompt lost past 30 s (#38131), `git` failing in its terminals (#26594). Session logs go to
     the configured base URL by default. Seven community plugins and two of its own hook packages, from
     a list that is not a review, run in its profile (registry entry `deepseek-harness`).
3. **The review lifecycle.**
   1. A Claude lane builds the change, runs `/code-review` (and the `evidence-verifier` on a record),
      resolves it and lands (ADR 0017, ADR 0019).
   2. For a change on ADR 0043's paths (the adapter's order and connection paths, credential handling,
      the gate code), the coordinator creates the review when it dispatches the build:
      `orchestration task-create --spec <review brief> --deps '["<build task id>"]'`. Orca's guide:
      "On every wake, start the Tasks `task-list --ready` shows." On the wake after the build reports,
      the coordinator checks the change is on `main` and starts the review there (`worker-start --task
      <id> --worktree new-top-level --agent <reviewer>`); a build that did not land gets no review yet.
      Elsewhere a review is optional, under the same terms.
   3. Findings go to the lane that owns the code as claims. That lane reproduces each, by a failing test
      where one can show it and by reading the cited source where none can, fixes what is real in a
      follow-up, and that change's record or commit says a second reader found it. What does not
      reproduce is recorded as such.
4. **Which reviewer reads what**, from the trials (Confirmation): Codex reproduces by running; Muse reads
   widely and finds by reading; Antigravity runs tests and writes reproductions but is slowest;
   DeepSeek Harness has shown nothing yet. The highest-risk paths get two families: Codex and Muse.
   Antigravity stands in for either; DeepSeek Harness gets nothing until a trial reports. A Codex brief
   says an error unrelated to the change is not a failed check. This rests on a handful of reviews and
   is revisited at `review-by`.
5. **The pins**: `muse-code` (`url`, no lock pin), `antigravity-cli` at `tag:1.5.3`, `deepseek-harness`
   at `tag:dsh-v0.2.1-rc.3`, `dsh-tui` at `tag:v0.17.0`, and `pnpm` at `tag:v10.34.9` for `dsh-tui`'s
   profile, the installed versions; each already has newer releases, which `outdated` reports. All are
   machine-level, not in `mise.toml`, as ADR 0043 did for Codex. `canon/models.json`
   and ADR 0005 are unchanged: these are readers, not lanes.

### Consequences

* Good: three more families can read the riskiest code, and the review starts from the task graph rather
  than from memory. By the trial reports, Muse's and Codex's findings overlapped in three of nine.
* Bad: each sign-in is in reach of an unsandboxed worker (ADR 0043). Antigravity updates itself past its
  pin, Muse and `dsh-tui` on any update. Nine packages no one here read run inside DeepSeek Harness. More
  readers, more claims for the owning lane to reproduce.
* Leaving: delete the entry and pin, drop the agent from `AGENTS.md`, `mise uninstall -g` the tool (the
  owner's); this record is then superseded.

### Confirmation

Trials, 2026-11-10, by the coordinator's reports (not re-run here); each reviewer read in its own
workspace, left it unchanged and reported through Orca.

* Codex on the order-modify change (past `/code-review` at high effort): three defects reproduced by
  running (ADR 0043). On cancel-all and stop orders: none after 114 offline tests; twice it stopped
  early, taking an unrelated error (a search outside the workspace, a guessed file) as a failed check.
* Muse on the same modify change: nine findings by reading, three the same as Codex's, six new. On the
  paper node: nine findings, the most severe a phantom external position at start-up.
* Antigravity (1.5.5) on commit `3a00f61`: ran the 64 offline execution-client tests, wrote in-memory
  `LiveNode` reproductions, reported ten findings it calls confirmed, in most of an hour, the longest of
  the four. How many are real or repeat others' is pending with the owning lane.
* DeepSeek Harness: no report by the time of writing.

UNVERIFIED: how Orca applies its bypass-flag map; whether a dependent task becomes ready when its
dependency fails; whether a reviewer's environment carries any key; Orca's fixture versions for DSH.

## Evidence

Read 2026-11-10. `fetched`: downloaded and searched. "Lane": a model summarised the fetched page.

* `muse --version` "1.4.15-R8339.1"; `agy --version` "1.5.5", `agy.<n>.old` beside it; `dsh --version`
  "0.2.1-rc.3"; `dsh-tui --version` "0.17.0"; `pnpm --version` "10.34.9" (run; none started). `agy --help`
  — `--sandbox`, `--mode` "(accept-edits, auto, plan)" (bundled).
* https://dev.meta.ai/docs/muse-code/permissions, /muse-code — "Approval and sandboxing are on by
  default"; Read-only; `--yolo` "disable approval and the sandbox"; "browser sign-in or an API key"; no
  repository or licence (lane).
* https://raw.githubusercontent.com/jdx/mise/main/registry/muse.toml — `muse-stable` "publishes the
  current release rather than a version history" (fetched).
* https://github.com/google-antigravity/antigravity-cli — 1.5.3 (2026-11-07), 1.5.4, 1.5.5 (2026-11-10); no
  licence (API 404); README: "via the system keyring", data opt-out (`gh`, `curl`). → `099f43f0b0b7`
  (resolved). https://antigravity.google/docs/cli/troubleshooting/ — a "self-updater that runs in the
  background"; `AGY_CLI_DISABLE_AUTO_UPDATE` to `true` (lane); the name is in the binary (`strings`).
* https://github.com/deepseek-ai/deepseek-harness — MIT; every release a pre-release (`gh`).
  `tag:dsh-v0.2.1-rc.3` → `1f81c047fc48` (resolved).
* https://raw.githubusercontent.com/ccch1mneyyy/dsh-TUI/v0.17.0/README.md — MIT; `dsh-tui update` "update
  the profile and align the launcher"; the `main` README: "incremental Session logs to the configured
  `baseURL`" (fetched). `tag:v0.17.0` → `103834cb1000` (resolved).
* https://github.com/stablyai/orca/issues/38131, /26594 — open; "30 s submit retry budget", `git` fails in
  DSH terminals (titles, `gh`). https://www.onorca.dev/docs/agents/supported — Antigravity "local
  supervised workers"; bypass flags for "every other agent that exposes one", none named here (lane).
* `orca skills get orchestration --reference references/coordinator-loop.md` and `orca orchestration
  worker-start --help` at Orca 1.4.255 — the quoted line and `task-create --deps` (bundled). Orca's CLI
  bundle at 1.4.255 — bypass map `muse:"--yolo"`, `antigravity:"--dangerously-skip-permissions"`, `codex:`
  as ADR 0043; `launchCmd:"muse --trust-workspace"`; `dsh` with `launchCmd:"dsh-tui ."` (read, `grep`).
* The DeepSeek Harness profile's package files — the plugin versions and licences (read; no credential).
  https://github.com/pnpm/pnpm/releases/tag/v10.34.9 — MIT (`gh`); → `aa97047a81b4` (resolved).
* Second reader, 2026-11-10 (`evidence-verifier`): it corrected the package count, the `/update` quote and
  the trial framing, and found Orca's flag map and the newer releases; all above.
