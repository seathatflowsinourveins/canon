---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); coordinator (Claude), by the owner's delegation
review-by: 2026-12-10
---

# Codex CLI as a second runtime: an Orca worker that reads a change and reports, and never writes

## Context and Problem Statement

ADR 0018 made Claude Code the only runtime, ADR 0021 held Orca workers to `--agent claude`, and ADR 0017
rule 4 kept review to one model family. The owner's instruction of 2026-11-10 is to use other runtimes as
Orca workers where that raises quality, and to install and sign in to one. On that word, before this
record, the Codex CLI (`openai/codex`, Apache-2.0) was installed at its latest stable release
`rust-v0.167.0` as a machine-level mise tool (`mise use -g codex@0.167.0`, `aqua:openai/codex`) and the
owner signed in. What may it do here, and how is it pinned?

## Decision Drivers

* The owner's word decides a tool that is cheap to leave; what is untested is UNVERIFIED (ADR 0018).
* A model never blocks; checks decide the merge (ADR 0017 rule 1). Paper only, no credential (ADR 0009).

## Considered Options

1. Keep Claude Code the only runtime.
2. Codex as a reviewer only: it reads a change in a workspace and reports findings.
3. Codex as a reviewer that stands as ADR 0017's required review in place of `/code-review`.
4. Codex as a general worker that edits and lands, like a Claude worker.

## Decision Outcome

Chosen: **option 2**, on the owner's instruction; the open choices made by the coordinator under the
owner's delegation of 2026-11-10.

1. **A Codex worker reads and reports.** Its task asks for findings only. It never edits, commits, pushes,
   lands, runs paper tests or reads a credential, and no paper or live key is in its environment
   (UNVERIFIED: Orca's worker environment was not inspected).
2. **Its sandbox is off, so rule 1 is text.** Orca "pre-fills" `--dangerously-bypass-approvals-and-sandbox`
   for Codex launches; Codex's docs call that "No sandbox; no approvals". No `ask` rule of ADR 0028
   prompts for it either: those are Claude Code's, so a push or a `uv sync` by it would not stop for the
   owner, and only the merge gate holds `main`. Orca's hooks in Codex's home, `PreToolUse` and
   `PermissionRequest` among them, post each event to the app, discard the answer and exit 0. Whether
   Orca's settings can give Codex workers a read-only sandbox is UNVERIFIED; that is the owner's setting.
3. **A workspace made for the review**: a new worktree at the commit under review, never the main checkout
   or another worker's. Afterwards the coordinator checks it has no change and no new commit, and removes it.
4. **An additional reader that never blocks.** `/code-review` and the `evidence-verifier` stay the review
   ADR 0017 and ADR 0019 require. A Codex finding goes to the lane that owns the code, which reproduces it
   before changing anything and fixes forward. It runs after landing by default, since only the
   coordinator starts one and no lane waits on it; before landing only for a change the coordinator
   names. It is expected on the paths that get `/code-review` at high effort (the adapter's order and
   connection paths, credential handling, the gate code), optional elsewhere.
5. **Codex's own default model and effort.** Its docs say only that it "uses a recommended model"; which
   one 0.167.0 picks is UNVERIFIED, and `--model` on a Codex worker is untried. `canon/models.json` and
   ADR 0005 are unchanged: this is a reader, not a lane.
6. **The pin**: registry entry `codex-cli`, a `tool` at `tag:rust-v0.167.0`, the version installed. It
   stays machine-level: in `mise.toml` every CI run would install a binary no check uses. The bootstrap
   does not check it; `codex --version` does. Upstream ships five to nine `rust-vX.Y.Z-alpha.N` tags
   (some `alpha.N.M`) per stable release; `scripts/canon outdated` ignores pre-releases for a stable pin
   and does not rank `.N.M`, so no `line` key. `rust-v0.167.1`, tagged 2026-11-10 with no release, was
   seen and not adopted; `outdated` reports it.
7. **The sign-in is the owner's**, kept by Codex in its home or the system credential store, where an
   unsandboxed worker can reach it. This departs from ADR 0018 rule 6, "no credentials in its
   environment", for Codex's own sign-in. No file here names an account; no session runs `codex login`.

The evidence on pairing points both ways. One preprint found Codex's revisions of Claude's drafts lowered
the pass rate and advises the reverse pairing; this record departs from that advice, which is why a
finding is reproduced before it is acted on. Another found a top cross-model reviewer "not significantly
different in F1" from a fresh same-model one, finding partly different errors. Neither model vendor
documents cross-family review; GitHub's Copilot preview runs several providers' models with no accuracy
figure (ADR 0017). So option 3 is not taken; option 4 needs a record of its own.

Supersedes ADR 0018 rule 5 ("Claude Code is the only runtime adopted"), ADR 0021 limits 2 and 5 for
review workers, and ADR 0017 rule 4 ("One model family, for now"), whose reasons were a stored key and
that no source measured what a second reviewer adds.

### Consequences

* Good: a second family reads the riskiest code; one preprint found such findings overlap only partly
  with a fresh same-model reviewer's (unnamed models, Jaccard 41.2%).
* Bad: the reviewer runs unsandboxed, with no `ask` rule and its sign-in in reach; only its task and the
  check afterwards stand behind rule 1. Stable releases come once or twice a week; nothing moves the
  installed tool with the pin.
* Leaving: delete the entry and its pin, restore the Orca limit in `AGENTS.md`, mark this superseded, and
  `mise uninstall -g codex` (the owner's). Orca's hooks stay in Codex's home until its setting removes them.

### Confirmation

Trial, 2026-11-10, by the coordinator's report (not re-run here): a Codex worker (`codex-cli 0.167.0`, its
default model, started by Orca with `--agent codex` in the main checkout) reviewed commit `3a00f61`, the
order-modify change, which had passed `/code-review` at high effort (ten findings, eight fixed) and the
`evidence-verifier`. It read `AGENTS.md` and the record, ran the offline suite (64 passed), and reported
three correctness defects, each reproduced through the stand-in venue and a `LiveNode` without adding
files: reconciliation drops a filled replacement the cache had not adopted, losing its completion and
fill; one refused modify emits two `OrderModifyRejected` events, which a membership-only test missed;
nothing guards against a duplicated replaced update. None was among the earlier findings. It left `git
status` clean, by its report touched no credential, met no refused command, and reported through Orca.
Rule 3 follows from it. UNVERIFIED: a review in a new worktree, a change it is asked to edit, a Codex
update, a larger diff, a read-only sandbox through Orca, `--model`, and whether `read-only` blocks network.
One trial is one data point.

## Evidence

Read 2026-11-10. `fetched`: downloaded and searched, not summarised. "Lane": a research subagent fetched the
page and a model summarised it. "Second reader": the `evidence-verifier`, the same day.

* `codex --help`, `codex exec --help`, `codex review --help` at 0.167.0 — `--sandbox` "read-only,
  workspace-write, danger-full-access"; the bypass flag "Skip all confirmation prompts and execute commands
  without sandboxing"; `review` "Run a code review non-interactively" (bundled).
* https://github.com/openai/codex/releases — `rust-v0.167.0` published 2026-11-06, latest; 17 of the 20
  newest entries pre-release alphas (fetched through `gh`; second reader).
* `rust-v0.167.0` → `086ccc833bc9` (resolved). Tag `rust-v0.167.1`, 2026-11-10 18:02 UTC, "Release
  0.167.1", no release (fetched through `gh api`); alpha counts per stable from `git ls-remote` (second reader).
* https://raw.githubusercontent.com/openai/codex/rust-v0.167.0/LICENSE — "Apache License / Version 2.0"
  (lane; second reader).
* https://www.onorca.dev/docs/agents/supported — "Orca pre-fills each supported CLI's permission-bypass
  flag for new launches — ... `--dangerously-bypass-approvals-and-sandbox` for Codex"; Agent Permissions
  "between Yolo and Manual" (fetched).
* https://www.onorca.dev/docs/cli/orchestration — `--agent codex`; `--model` takes "opaque provider model
  IDs" (lane; second reader).
* https://learn.chatgpt.com/docs/agent-approvals-security — "By default, the agent runs with network access
  turned off"; `--sandbox read-only --ask-for-approval on-request` for read-only use; "No sandbox; no
  approvals" (fetched; second reader).
* https://learn.chatgpt.com/docs/auth — sign-in cached "in a plaintext file at `~/.codex/auth.json` or in
  your OS-specific credential store" (fetched; that file not read).
* https://learn.chatgpt.com/docs/models — "uses a recommended model" (fetched).
* https://learn.chatgpt.com/docs/agent-configuration/agents-md — "Codex includes at most one file per
  directory", so it reads this repo's `AGENTS.md` (fetched).
* Codex's home on the owner's machine — `hooks.json` and Orca's `codex-hook.sh` read in full: nine hook
  entries; the script sends output to `/dev/null` and ends in `exit 0`. Of `config.toml` only section
  names were read (read; second reader).
* https://arxiv.org/abs/2607.21656 (preprint, 116 competitive-programming tasks) — "Codex reviewing Claude
  drafts drops the pass rate from 91.4% to 82.8%"; "use Claude to review Codex, not the other way around";
  "The reviewer sees the problem and the writer's draft but cannot execute tests" (abstract fetched).
* https://arxiv.org/abs/2610.01471 (preprint, one author, planted errors) — "not significantly different
  in F1 from same-model review in a fresh session"; "partly different errors (Jaccard 41.2%)"; "model
  difference and reviewer capability are not separated" (abstract fetched).
* https://code.claude.com/docs/en/code-review, https://learn.chatgpt.com/docs/third-party/github — neither
  mentions review by another family (lane; second reader). No project guide requiring it was found (lane).
* Second reader, 2026-11-10: it corrected the supersede list, rule 4's reasons, the alpha and release
  counts and the reading of both preprints, and had the departure from ADR 0018 rule 6 stated; all above.
