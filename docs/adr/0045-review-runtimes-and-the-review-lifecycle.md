---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Muse Code, Antigravity and DeepSeek Harness as review-only workers, and one review lifecycle for every change

*Muse Code deferred 2026-11-13 by the owner: no Muse reviewer is started until the owner lifts it. Its row, pin and launch notes are kept as they are, so it can be resumed as written; Codex and Antigravity read in its place, and DeepSeek Harness stays on Flash.*

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
     the pin followed once 1.5.4's and 1.5.5's notes were read (Auto mode for `agy acp`, a `/swarm`
     preview, hook-input fields, models, fixes; none changes a reader's terms; a brief starts no swarm).
     Its docs set `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the shell profile, so `scripts/bootstrap
     --install` writes it to `~/.zshrc`. (Reversed 2026-11-10 by ADR 0065 rule 3: the updater is on
     again, and `--install` removes that line.) (2026-11-13: it ran 1.5.6 from 2026-11-12 and 1.5.8 from
     2026-11-13, by agy's own logs, and the pin followed once the notes of 1.5.6 to 1.5.8 were read.
     1.5.6 loosens Auto mode's review of commands that leave the terminal sandbox. 1.5.7 makes Auto the
     default Agent Mode where no mode is saved and Auto is available, headless `-p` included, unless a
     `strict` Tool Permission is set. Whether that reaches a launch carrying the bypass flag above is
     UNVERIFIED.)
   * *DeepSeek Harness* (MIT, pre-release only; agent id `dsh`, no entry in the map): API key in its own
     file (not read). Orca launches `dsh-tui .`, whose `/update` moves past the pin. Open Orca issues: a
     first prompt lost past 30 s (#38131), `git` failing in its terminals (#26594). Session logs go to
     the configured base URL by default (`session-log-deepseek`); dsh-TUI documents a user-patch row
     that turns this off, and `scripts/bootstrap --install` adds it, with dsh-memento's master switch
     off (`memento`), to `profiles/dsh-tui/cordis.patch.yml`, keeping the owner's rows (rule 6). Seven
     community plugins and two of its own hook packages, from
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
   DeepSeek Harness, on one trial, finds by running probes. The highest-risk paths get two families:
   Codex and Muse. Antigravity stands in for either, and so may DeepSeek Harness, only while
   `scripts/bootstrap --check` shows its review patch ok (rule 6). A Codex brief
   says an error unrelated to the change is not a failed check. This rests on a handful of reviews and
   is revisited at `review-by`.
5. **The pins**: `muse-code` (`url`, no lock pin), `antigravity-cli` (see below), `deepseek-harness`
   at `tag:dsh-v0.2.1-rc.3`, `dsh-tui` at `tag:v0.17.0`, and `pnpm` at `tag:v10.34.9` for `dsh-tui`'s
   profile, the installed versions; each already has newer releases, which `outdated` reports.
   `antigravity-cli` was adopted at `tag:1.5.3` and moved to `tag:1.5.5`, the version its updater had
   installed, then on 2026-11-13 to `tag:1.5.8`, the version installed by then. All are
   machine-level, not in `mise.toml`, as ADR 0043 did for Codex. `canon/models.json`
   and ADR 0005 are unchanged: these are readers, not lanes.
6. **DeepSeek Harness after its trial** (decided by the coordinator under the owner's delegation of
   2026-11-10, from the trial in Confirmation): it reads under ADR 0043's terms, as rule 4 says, with two
   conditions. Its profile's session-log upload is off, since every request otherwise carries the
   session's prompts, code and tool results to the base URL. Its cross-session memory is off, not
   scoped: ADR 0046 rule 2 keeps from a reviewer what it should not have seen, and dsh-memento both
   injects a memory snapshot and recalls past sessions' history (`memory_recall`, 30 days by default);
   the profile is shared by every launch and Orca's `dsh-tui .` takes no per-review overlay, so no
   switch scopes it to one review. Until the owner runs `scripts/bootstrap --install` and `--check`
   shows the patch ok, the coordinator starts no DeepSeek Harness review. Each review gets a new
   workspace, as rule 1 says (the two trial attempts shared one), and its brief says to write the report
   early, after a first attempt that stalled. (Amended 2026-11-13 by ADR 0136: a third condition, that the
   profile refuses every approval request instead of asking, held by two more rows in the same patch.)
7. **Codex reads on GPT-6.1 Sol at `max`** (the owner's instruction, 2026-11-10: GPT-6.1 Sol at maximum
   quality). This replaces ADR 0043 rule 5, Codex's own default model and effort; the other three stay
   on their defaults. The model is `gpt-6.1-sol` and the key `model_reasoning_effort`. The catalog
   bundled with 0.167.0 lists `low`, `medium`, `high`, `xhigh`, `max` and `ultra` for it, `low` its
   default. `max` is the most reasoning one task gets. `ultra` "uses subagents to handle separate parts
   of a complex task in parallel"; a reader is one worker, and at 0.167.0 `ultra` sends the reader's own
   requests at `xhigh`, so it is not used. The coordinator starts a review with `--agent codex --model
   gpt-6.1-sol --effort max`. On the first such start (Orca 1.4.255) the receipt read ready, and the
   process line carried `-m gpt-6.1-sol` and `-c model_reasoning_effort=max`. A signed-in session
   fetches its own catalog and may list less; Codex sends any non-empty effort string unchanged without
   checking it against the model's levels, and what the server does with one it does not list is
   UNVERIFIED. So a report names the model and effort its session showed, and where `max` is not
   offered the coordinator starts the next review at the highest level below `ultra` that is. If the
   flags start no worker, the same two keys in the machine's Codex config do it: the owner's step, since
   `scripts/bootstrap` writes only its own table there. (From 2026-11-10, ADR 0059 rule 4:
   `scripts/bootstrap --user-config` writes both keys there, from `home/.codex/defaults.toml`, so flags
   and config agree; its rules 5 and 6 give Antigravity and Muse a default of their own, and rule 7 keeps
   DeepSeek Harness on its bundle's.) From 2026-11-12 the model is `gpt-6-astra`, still at `max`, by the owner's
   standing instruction that the GPT lane runs the most capable current model at a high effort (ADR 0005,
   amendment of 2026-11-12; ADR 0059 rule 4): the coordinator starts a review with `--agent codex --model
   gpt-6-astra --effort max`, and a report from a session that ran another model, `gpt-6-luna` after the
   rate-limit prompt included, says so.
   **Before this rule** every Codex review named no model or effort: both reviewer benchmark runs
   (`evals/review/runs/2026-11-10-first` and `-second`; their sheets say "on its own defaults") and, by
   ADR 0043 rule 5, the trials below. The machine's Codex config sets neither, so the catalog's defaults
   applied: by the bundled catalog `gpt-6.1-sol` at `low`. A Codex score or finding from before this
   rule is not comparable with one after it, and a sheet that holds both says so.

### Consequences

* Good: three more families can read the riskiest code, and the review starts from the task graph rather
  than from memory. By the trial reports, Muse's and Codex's findings overlapped in three of nine.
* Bad: each sign-in is in reach of an unsandboxed worker (ADR 0043). Antigravity updates itself past its
  pin until `~/.zshrc` sets its switch (from 2026-11-10 the switch is off on purpose and updating past
  the pin is intended, ADR 0065), Muse and `dsh-tui` on any update. Nine packages no one here read run
  inside DeepSeek Harness. More readers, more claims for the owning lane to reproduce.
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
* DeepSeek Harness (`dsh` 0.2.1-rc.3, `dsh-tui` 0.17.0) on commit `3a00f61`, the brief the other three had.
  A first attempt stalled for about two hours after 27 tool calls with no output and was stopped. The retry,
  briefed to write its report early, took about ten minutes by its session log. It ran the offline
  execution-client tests, 74 by its report where Antigravity's gave 64 (not reconciled), after setting
  `UV_CACHE_DIR` to a temporary folder (its sandbox refused uv's cache under `~/.cache`), wrote probes only
  outside the workspace, and reported eight findings, six confirmed by running and two by reading. Against
  the ten defects `evals/review/cases/order-modify.toml` confirms (one scorer, the coordinator; not a
  benchmark run):

  | Defect | DSH |
  |---|---|
  | `filled-replacement-lost-when-replaced-missed` | found (finding 1) |
  | `refused-replace-rejected-twice` | found (5; 7 says its test cannot tell the paths apart) |
  | `replaced-applied-twice` | missed |
  | `replacement-fill-before-replaced` | disputed, so missed (4 names `_apply` but a lost `replaced` and a cache-index error, not the old terms or a late update) |
  | `late-cancel-of-replaced-order` | missed |
  | `pending-replace-fails-startup` | missed |
  | `replacement-moved-on-before-replaced` | missed |
  | `second-modify-refusal-taken-for-retry` | found (2, after a restart rather than a second modify; same check) |
  | `answers-kept-for-the-session` | found (8, by reading) |
  | `rejected-replacement-after-fill-never-reached` | found (3; 6 says its test passes without it) |

  Five of ten; a second reader's marks agreed on nine. It is not comparable with the others: those ten are
  their own findings (the case's bias), and the retry was not shown to be independent. Both attempts ran in
  one workspace folder, not a new one each, and its profile carried dsh-memento, whose `workspace` layer is
  keyed by that folder and whose `memory_recall` can return the stalled attempt's session; memento's audit
  table shows the retry's injected snapshot empty. The seven community plugins were added during that first
  attempt. The brief did not forbid later history: the fix (`52ac69c`) was on `main` before the retry began,
  and the case's defect list landed about seven minutes into it. Its findings name only `3a00f61`'s code.
  Its session log went to the base URL by default (the repository is public; the report says no credential
  was touched).

UNVERIFIED: whether the DSH retry called `memory_recall` (its recall events are declared, not emitted) or
read later commits; the first attempt's tool-call count and length (its log is compressed, not read);
whether agy's background updater, started outside an interactive zsh, sees the `~/.zshrc` variable; whether
a running session honours the two rows (`--dump-config` shows them composed); whether another of the nine
packages reads past sessions; how Orca applies its bypass-flag map; whether a dependent task becomes ready
when its dependency fails; whether a reviewer's environment carries any key; Orca's fixture versions for
DSH; which model and effort the Codex sessions before rule 7 were given by the catalog they fetched;
whether the account's catalog offers `max`, which effort the first session under rule 7 applied (its
flags were read, not its session), and what the server does with an effort the model does not list.

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
  (resolved, the 1.5.3 pin). https://antigravity.google/docs/cli/troubleshooting/ — a "self-updater that
  runs in the background"; `AGY_CLI_DISABLE_AUTO_UPDATE` to `true` "inside your shell profile (`~/.bashrc`
  or `~/.zshrc`)" (lane); the name is in the binary (`strings`). Releases 1.5.4 and 1.5.5, notes read (`gh
  release view`); `tag:1.5.5` → `3e9fbcc9773e` (resolved). Releases 1.5.6 (2026-11-11), 1.5.7
  (2026-11-12) and 1.5.8 (2026-11-13), notes read 2026-11-13 (`gh api`); `tag:1.5.8` → `1554cf9fc71d`
  (resolved). The versions this machine ran, from the version strings in agy's own CLI logs, read
  2026-11-13: 1.5.6 from 2026-11-12 and 1.5.8 from 2026-11-13, with no log naming 1.5.7.
* https://github.com/deepseek-ai/deepseek-harness — MIT; every release a pre-release (`gh`).
  `tag:dsh-v0.2.1-rc.3` → `1f81c047fc48` (resolved).
* https://raw.githubusercontent.com/ccch1mneyyy/dsh-TUI/v0.17.0/README.md — MIT; `dsh-tui update` "update
  the profile and align the launcher"; the `main` README: "incremental Session logs to the configured
  `baseURL`" (fetched). `tag:v0.17.0` → `103834cb1000` (resolved). Its `docs/configuration.en.md` at
  `v0.17.0`: user configuration at `$DSH_HOME/profiles/dsh-tui/cordis.patch.yml`, applied last; "its
  `config` block is replaced as a whole"; `session-log-deepseek.enabled` default `true`; "To stop API
  request metadata, disable either field in the user profile patch" (fetched).
* https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/dsh-v0.2.1-rc.3/packages/session/session-log-deepseek/README.md
  — "`false` stops Session-log upload"; "Re-enabling resumes the unaccepted suffix, including events
  recorded while disabled" (fetched). dsh-memento 0.5.28's README in the profile — "Override under the
  `memento` row"; `enabled` "Master switch"; `memory_recall` "recent session-history matches"; store at
  `$DSH_HOME/dsh-memento/memory.db` (read). `dsh --profile dsh-tui --dump-config` on a copy of the
  profile, after `scripts/bootstrap`'s merge: both rows composed with `enabled: false` (run).
* Newer tags, read 2026-11-10, held: https://github.com/ccch1mneyyy/dsh-TUI/releases/tag/v0.19.0
  (2026-11-06) — validated against DSH `0.2.2-rc.1`, `0.2.1-rc.3` still in its contract; `/update` now
  also moves the global dsh engine (`DSH_TUI_UPDATE_ENGINE=0` pins it); DSH 0.2.2 needs Node `>=24.9`
  (`gh`). Its `docs/configuration.en.md` at `v0.19.0` against `v0.17.0`: the user patch section unchanged;
  the diff adds environment variables and moves two DSH version labels (fetched, diffed).
  https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1 (2026-11-04) — Cordis Core
  4.1, plugin tool APIs to migrate, Node 24.9 minimum (`gh`); its session-log README keeps `enabled`
  (`false` stops upload), adds plugin upload exclusions, and puts Auto review calls under the same upload
  eligibility (fetched, diffed against `dsh-v0.2.1-rc.3`).
  Whether the nine profile packages load on 0.2.2 is UNVERIFIED. Both pins move together, after the
  owner upgrades both and `scripts/bootstrap --check` still shows the review patch ok; look again
  2026-11-13.
* The DSH retry's report and its brief (read; not in this repository); `evals/review/cases/order-modify.toml`
  (read); the session folder and memento's audit row for the retry (read by the second reader).
* https://github.com/stablyai/orca/issues/38131, /26594 — open; "30 s submit retry budget", `git` fails in
  DSH terminals (titles, `gh`). https://www.onorca.dev/docs/agents/supported — Antigravity "local
  supervised workers"; bypass flags for "every other agent that exposes one", none named here (lane).
* `orca skills get orchestration --reference references/coordinator-loop.md` and `orca orchestration
  worker-start --help` at Orca 1.4.255 — the quoted line and `task-create --deps` (bundled). Orca's CLI
  bundle at 1.4.255 — bypass map `muse:"--yolo"`, `antigravity:"--dangerously-skip-permissions"`, `codex:`
  as ADR 0043; `launchCmd:"muse --trust-workspace"`; `dsh` with `launchCmd:"dsh-tui ."` (read, `grep`).
* The DeepSeek Harness profile's package files — the plugin versions and licences (read; no credential).
  https://github.com/pnpm/pnpm/releases/tag/v10.34.9 — MIT (`gh`); → `aa97047a81b4` (resolved).
* Rule 7, read 2026-11-10. https://raw.githubusercontent.com/openai/codex/rust-v0.167.0/codex-rs/protocol/src/openai_models.rs
  — `ReasoningEffort`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, `persistent`;
  `from_str` turns any other non-empty string into `Custom` (fetched).
  https://raw.githubusercontent.com/openai/codex/rust-v0.167.0/codex-rs/models-manager/models.json —
  `gpt-6.1-sol`: `default_reasoning_level` `low`, six `supported_reasoning_levels`, `ultra` "Maximum
  reasoning with automatic task delegation", `priority` 1, the lowest (fetched); `codex debug models
  --bundled` on the installed 0.167.0 gives the same (run; it "dump[s] only the bundled catalog",
  no sign-in used). `models-manager/src/manager.rs` and `mark_default_by_picker_visibility` in the file
  above, at the tag — with no model named, the first the picker shows once sorted by `priority`
  (fetched). `codex --help` — `-m, --model`, `-c key=value` (bundled). `protocol/src/openai_models/reasoning_effort.rs`
  and `core/src/session/mod.rs` at the tag — the configured effort is copied into the session unchecked;
  `ultra` resolves to the model's `multi_agent_reasoning_effort`, `xhigh` here (read by the second reader).
* https://learn.chatgpt.com/docs/models — "Max gives the selected model more time to reason about a single
  task"; "Ultra uses subagents to handle separate parts of a complex task in parallel"; "If you don't
  specify a model, the ... Codex CLI ... uses a recommended model"; `codex --model gpt-6.1-sol` (fetched,
  searched). https://learn.chatgpt.com/docs/config-file/config-reference — `model_reasoning_effort`:
  "Reasoning effort advertised by the selected model, such as `low`, `medium`, `high`, `xhigh`, `max`, or
  `ultra`. Available levels depend on the model and client" (fetched, searched).
* Orca's bundle at 1.4.255, `shared/agent-session/options/agent-session-option-catalog-claude-codex.js` —
  Codex's model applied with `-m`, its effort as `-c` with key `model_reasoning_effort`; the seed list has
  no `gpt-6.1-sol`, and an unknown model is offered efforts up to `ultra`; its comments say both that
  "unsupported efforts fall back to the model default instead of failing" and that "Codex can clamp
  higher values", neither of which Codex's source at the tag shows (read; second reader). `orca
  orchestration worker-start --help` — "`--effort` requires `--model`" (bundled). The machine's Codex
  config — no `model`, `model_reasoning_effort` or profile key, and no profile file (key names read, no
  value).
* The first start under rule 7, 2026-11-10, Orca 1.4.255 and codex-cli 0.167.0 — the receipt "ready" with
  the model and effort asked for (the coordinator's report); the worker's process line, of which only
  the bypass flag, `-m gpt-6.1-sol` and `model_reasoning_effort=max` were read (`ps`).
* ADR 0058, 2026-11-10: these runtimes may also be given build work, under that record's terms; rule
  1 holds for a review.
* Second reader of rule 7, 2026-11-10 (`evidence-verifier`): the levels, the default and the quotes held.
  It found the fallback claim unsupported by Codex's source and what `ultra` sends, both corrected
  above, and noted that this record is past the length at which a record is split.
* Second reader, 2026-11-10 (`evidence-verifier`): it corrected the package count, the `/update` quote and
  the trial framing, and found Orca's flag map and the newer releases; all above.
* Second reader, 2026-11-10, after the DSH trial (`evidence-verifier`): the configuration quotes held;
  it re-marked one defect (six to five of ten), dated the case's defect list to during the retry, found
  the shared workspace folder and the empty snapshot, and the pins sentence; all above.
* ADR 0034, 2026-11-11: Muse moves from `--yolo` to `--disable-approval --sandbox-network restricted`
  once the owner sets it, a sandbox that still leaves `/tmp` and the repository's git storage writable;
  Antigravity and DeepSeek Harness keep rule 2's posture.
