# Canon

A harness repo for building on Alpaca and NautilusTrader with coding agents. Nothing here is invented: every
dependency, pattern and practice traces to a pinned upstream or a dated source. The decisions are in
`docs/adr/`. They outrank this summary, the owner's recollection, and yours.

## Rules

1. **Evidence over memory.** Your training data is older than this repo's pins. Before using an upstream API,
   config key, model name or CLI flag, read the pinned source or its current docs. Docs for agents:
   `https://nautilustrader.io/docs/llms.txt`, `https://docs.alpaca.markets/llms.txt`,
   `https://code.claude.com/docs/llms.txt`. `scripts/canon src <id>` is the way to read an upstream's source at
   its pin: it fetches the locked commit into a read-only folder and prints the path.
2. **Adopt through the canon.** Anything not already in `canon/upstreams.json` (library, tool, skill, plugin,
   practice) goes through the `adopt-upstream` skill before it is installed, imported, enabled or copied.
3. **Glue yes, invention no.** Code here calls an upstream API as documented or implements a contract an
   upstream documents, and its header names that contract. If no upstream covers a need, write an ADR with
   status `proposed` stating the gap, and stop.
4. **Label uncertainty.** Anything not checked this session against a source is `UNVERIFIED`. Never present a
   guess as a finding.
5. **No silent fallback.** If a check, fetch, install or test cannot run, stop and report it. Do not substitute
   something else and carry on.
6. **Pins move deliberately.** Only `scripts/canon resolve` writes the lock, after reading the upstream
   changelog. Never edit `canon/upstreams.lock.json` by hand.
7. **Decisions expire.** An ADR past its `review-by` date is re-verified before you rely on it.
8. **Paper for every session but the live lane.** Every session but the live lane trades on paper alone.
   The live lane, one Claude Code session started for it, may place, modify and cancel live orders, and only
   through its `mise run live:*` tasks. Nothing trades live until ADR 0110 is accepted and its live order
   tasks are on `main`. There is no other path: no direct alpaca-py call, CLI, MCP server or connector. No
   session ever reads, prints, logs or commits a credential, paper or live, and nothing about the owner's
   account enters a tracked file (ADR 0103).
9. **One model per conversation.** Switching model mid-session discards the prompt cache; delegate bulky or
   cheap work to a subagent instead. Changing effort is fine (ADR 0005, ADR 0006).
10. **Proportion and evidence.** The north star is the trading work: before starting anything outside it,
   ask whether it moves a roadmap step or keeps the gate honest, and if not, do the smallest documented
   form once or record a limit. Security is not a goal here, so a security-flavoured finding off the
   trading path gets one decision, a small documented fix or a recorded limit, and no second round,
   unless it bears on a credential the repository handles. Off the trading path, the upstream's
   documentation and a landscape comparison are enough to land a record, with what was not run marked
   `UNVERIFIED` and one `evidence-verifier` pass as its review. The
   trading path (the adapter's order and data paths, the session window, the gate) still needs a failing
   test first and a paper or simulation run (ADR 0074).

## Commands

| Command | Does |
|---|---|
| `scripts/canon list` | Show every upstream, its role and pinned commit |
| `scripts/canon lint` | Check registry, lock and ADRs for required evidence (offline) |
| `scripts/ci` | Run every check: registry, shell, self-tests and `scripts/test-*`, lockfile, ruff, ty, pytest, workflow, secrets; and for a project kept apart (a `uv.lock` below the root), its own lockfile, ty and pytest in its own environment |
| `scripts/land` | Land the current branch on `main` once its checks pass here and GitHub's required check passes; no manual merge step. `--pr <n>` lands a pull request the dependency bot opened |
| `scripts/canon src [id]` | Fetch one pin's repository (every git pin without an id) at its locked commit into `~/.cache/canon/upstream-src/<id>@<commit>`, read-only, and print the path; a folder already there is checked against the lock |
| `scripts/canon check` | Compare pins with live upstream; exit 3 if a pinned release tag was re-pointed, 4 if the lock is stale against the registry |
| `scripts/canon outdated` | Show upstreams that have a newer release tag than the one pinned |
| `scripts/canon resolve <id>` | Resolve one pin and merge it into the lock; without an id, re-resolve all (ask first) |
| `uv sync` | Install exactly what `uv.lock` records into `.venv` |
| `uv run --locked pytest` | Run every test in the workspace; `--locked` fails instead of rewriting `uv.lock` |
| `scripts/lanes` | Report every Claude Code session on this machine: state, failures, spend; `--rates` lists every runtime's workers with output tokens per second where a documented source gives it |
| `scripts/automation status` | Show every standing automation and whether it is healthy |
| `scripts/survey-github report canon/github-practice.json` | Print what a measured sample of repositories converges on for pull requests and GitHub automation |
| `scripts/bootstrap --check` | Show which toolchain pieces are installed, and whether the Claude Code CLI is behind its pin |
| `scripts/manifest` | Join the toolchain, runtime defaults, standing automations and pinned upstreams; `--json` emits the same report as one object, with unavailable values and source failures named |
| `mise run paper:store`, `paper:check`, `paper:remove`, `paper:test` | Enter the Alpaca paper key pair into the login keychain (the owner, in their own terminal), check it is there, remove it, run the paper tests with it under a lock, one session at a time (ADR 0029, ADR 0033); `paper:store`, `paper:check` and `paper:remove` take `windows` for account B's pair, which `paper:session`, `paper:long-only windows`, `paper:account windows`, `paper:close windows` and `paper:protect windows --cancel` read (ADR 0069), and `protect` for account C's, which only `paper:account protect`, `paper:protect protect` and, after its proof on account A and with the lead's go, `paper:protector protect` read (ADR 0114, ADR 0142) |
| `mise run paper:session` | Run one bounded `SessionMaCross` window (`--session regular\|extended`, `--minutes`, at most 60) on paper account B under its own lock and the stream lock; it starts flat, ends flat or fails, and logs outside the repository (ADR 0044, ADR 0069) |
| `mise run paper:protector tests\|windows\|protect` | Run one `OwnerProtector` alone, leaving carried holdings EXTERNAL, with required `--row OWN1\|OWN3` (no default; F2 refused), optional starting `--levels SYMBOL=PRICE,...`, and explicit `--holds-dir` and `--inbox` read together at start (unread or nonempty: refused); under the account's own lock and stream lock, held: refused; A refused on weekdays 04:00–20:00 New York by Alpaca's clock, the movers recorder's hours; never from the main checkout, only from a checkout of its own at a commit; SIP across regular and extended hours, BOATS overnight, IEX refused, `--minutes` default 30, at most the current feed span (sixteen hours on SIP); C requires the lead's go naming account and row, carried as `--lead-go 'protect OWN1'` or `'protect OWN3'`, at a commit of main; its sells are of the protect lineage (`canon-protect-`, `custody.PROTECT`); stop cancels only this instance's orders, sells nothing, and names each holding left unprotected by the node; a new owner fill after start on B is SIMULATION ONLY today, its paper showing later on A (ADR 0146, ADR 0142, ADR 0069; `src/canon_trading/protector_node.py`) |
| `scripts/automation install-protector tests\|windows\|protect OWN1\|OWN3` | Install the protector runner for one account (the owner; plain `install` never does): its launchd agent, its instance folder with `holds/` and `inbox/` (any entry stops it) and, on C, the lead's standing go read from `go` at every start; `uninstall-protector <account>` removes the agent and keeps the records (`scripts/test-protector-runner`) |
| `mise run paper:account tests\|windows\|protect` | Print one paper account, GET only: equity, cash, buying power, each position, each open order with its time in force and position intent as Alpaca returns them (`not shown` when absent) and whether its client order ID is of our nodes' lineage or foreign, and `no_shorting`, then the New York trade date's ended orders submitted that date with their result and times (one submitted earlier is not listed); no account number or order ID (`src/canon_trading/custody.py`) |
| `mise run paper:exit-monitor tests\|windows` | Write one account's exit findings, status, per-trade-date counts and alert outbox under its own `canon/exit-monitor/A` or `B` folder, GET only with a 45-second pass bound, no order and no account or stream lock; C is refused pending the lead's named word, and the same run delivers pending outbox alerts through a credential-free macOS carrier, while scheduling and status display belong to unit two (`src/canon_trading/exit_monitor.py`) |
| `mise run paper:close tests\|windows` | By hand, under the account's lock: cancel each open order of our nodes' lineage and close, one attempt each, the long our orders opened (a DAY market sell in the regular session, else a DAY limit marked extended hours through the latest quote, canceled after 60 s); a symbol with a foreign order, a short or a quantity ours did not open is frozen and named, and nothing in it but our own open orders is touched; exit 0 flat, 1 something of ours left, 2 only frozen symbols (`src/canon_trading/custody.py`) |
| `mise run paper:protect tests\|protect`, `paper:protect windows --cancel` | In the regular session by Alpaca's clock, on account A or C, under the account's own lock (held: refused): one GTC trailing stop Alpaca holds, marked `sell_to_close`, for each long no working sell covers, at the distance of the registered trail row `--row F2\|OWN1\|OWN3` names (no default: a send without one is refused), never under the spread floor or Alpaca's smallest trail, then exit; `--plan-only` makes every read, names what each stop would be under every row and sends nothing, and is an account's first run; the stop is of the protect lineage, ours by name in `paper:account` and never canceled by `paper:close`, which freezes its symbol; it holds its shares, so another sell of them is refused until it is canceled, which `--cancel` does, with no row and no session asked, for the open stops of the protect lineage, every one or `--symbol`'s, and nothing else (a stop that filled in part first, or a named symbol with none, is not a success); a short, or a long covered in part by a foreign order, is named and untouched, and a long that changed since the read gets no stop; a run on C that sends needs the lead's go naming the account and the row, and one that cancels the lead's go naming the account; `windows` (B) takes `--cancel` alone, to close a protect-lineage sell left there, and a send on it is refused by name (ADR 0142, ADR 0129; `src/canon_trading/protect.py`) |
| `mise run paper:data sandbox` | Run the sandbox node (`--minutes`, at most 60): NautilusTrader's sandbox execution client on Alpaca's live data with account A's pair, every request but GET refused and no Alpaca execution client; `quotes` is listed and refused until it is built (ADR 0070) |
| `python -m canon_trading.screener_board` | Print the latest complete screener board from `~/.claude/canon/watch/` (`--board-dir` to choose another folder), newest lists first, with each list's `last_updated`, the poll's age and its top rows (`--top`, default 20), reading only the local file that each complete poll of `mise run paper:data movers-screener` writes; display only, never an input to the ranking, an entry or a protector; exit 0 fresh, 1 older than 120 seconds, 2 missing, 3 malformed |
| `mise run paper:history <tests\|windows>` | Load historical `sip` minute and day bars, with `--data trades` `sip` trade prints, or with `--data quotes` IEX quotes from 2025-11-04 New York on (or, with `--feed sip`, SIP quotes for at most 8 symbols named with `--symbols` over at most 60 minutes, default root `catalog/sip-quotes`), or with `--feed boats` the overnight feed's 1-minute bars, prints or quotes for at most 8 symbols named with `--symbols` over at most 8 hours (default roots `catalog/boats` and `catalog/boats-quotes`; `--feed overnight` is refused, as Alpaca's docs say), into a root of their own, for `--symbols` or `--symbols-file` over a bounded `--start`/`--end` into a catalog root (`--catalog`, default `catalog/`), with one account's pair, GET only, paced by ADR 0093's row, with no account or stream lock but one catalog lock per root; a 429 or a refused feed stops it by name (ADR 0093, ADR 0066, ADR 0151) |

## Layout

`catalog/` (market data) and `ledger/` (the trial ledger) are local and ignored by git;
`canon/upstreams.lock.json` is generated by `scripts/canon resolve`; `home/` reaches `~/.claude/` only
through `scripts/bootstrap`. Strategies and node configuration go in `src/canon_trading/`; the Alpaca adapter
goes in `packages/canon-alpaca/`, whose README comes first. `filings/` is a project of its own, outside the
workspace, with its own `uv.lock` and environment (ADR 0108): run its commands from that folder, and add
nothing it needs to the root. How the pieces fit together:
`docs/architecture.md`; what comes next: `docs/roadmap.md`.

## Decisions

The records are `docs/adr/NNNN-title.md`; the title line of each says what it decides, its front matter
its `status`, and the session start lists those still `proposed`. `docs/architecture.md` cites the ones that
shape the system.

## Automation

These run without being asked (ADR 0011, ADR 0013, ADR 0026). Every commit runs `scripts/ci --staged` through
`.githooks/pre-commit`: the checks that take seconds, with the rest left to GitHub's required check (ADR 0138, ADR 0144). Every push runs `scripts/check-addresses` through `.githooks/pre-push`, which
refuses an email address that is not a known service or example address in what the push would publish.
Every edit to a Python file runs ruff on it, and every edit under `canon/`,
`docs/adr/` or `.agents/`, or to an `AGENTS.md`, runs the canon lint. Once a day a session-start hook compares the canon with upstream and the
next session start prints the result; a scheduled workflow does the same comparison on GitHub every day
and keeps one locked `canon-drift` issue in step with what has drifted, which the brief names (ADR 0064).
A background watchdog raises a macOS notification when a session
stalls or fails repeatedly. A local OpenTelemetry Collector on 127.0.0.1 keeps the token events `scripts/lanes --rates` reads (ADR 0007). Once the owner installs it, a second agent runs a paper session window
70 s after the last firing ends when Alpaca's clock says a session is open and `canon/paper-schedule` on main says to, and
`scripts/automation status` shows its last result (ADR 0050). The movers recorder (ADR 0070) runs the
data-only screener on weekdays 04:00–20:00 New York from its own clone of `main`, in back-to-back batches of
at most 80 polls; a failed batch ends that firing by name and the next firing starts fresh, and its status row reports the result. Once the owner runs
`scripts/automation install-protector <account> <row>`, the protector runner starts `paper:protector` back to back for that
account, each run in a detached worktree of its own clone of main, its successor launched 60 s before the run's end to wait on
the account's lock; a hold stops it, C needs the lead's standing go, account A is never started in the recorder's hours,
and its status row names a holding frozen in the current run (a freeze is not carried into the next firing) and reports,
for each start, the interval from the earlier of the predecessor's planned end and observed exit to the successor's
first protective log line the runner saw, naming the predecessor's last judged quote as not logged; that interval is a
seam after a boundary, a failure or a kill, a pause after the schedule's end, a hold gap after a hold, and a refusal gap after a start the node refused.
A maintenance agent keeps this machine on main by itself (ADR 0065). Every 30
minutes, unless a paper run holds its lock, it fast-forwards the main checkout and runs the installer
for whatever differs from main: `scripts/automation install` or `scripts/bootstrap --user-config`. Once
a day it runs `claude update` and `agy update`. The same install keeps the OmniRoute gateway running,
once it is installed (ADR 0056). The owner has pre-approved this end-to-end automation of what is
already adopted, paper runs included. Installing from a worker's branch still asks, because only main
has passed review (ADR 0028). On Mondays a scheduled session runs the `landscape-refresh` skill. Dependabot
opens at most two grouped pull requests a week, for the indirect packages in `uv.lock` and for the
workflow's actions; the session brief lists the open ones (ADR 0016).

No hook runs before a tool call (ADR 0020). The rules are held where they cannot be skipped: GitHub merges
nothing that has not passed `scripts/ci` and does not carry the `signoff` status `scripts/land` posts,
and `scripts/ci` fails when `uv.lock` is out of date. No credential enters this repo or an agent's context;
that is a rule in text, held by where the pairs are stored and which tasks read them, the live pair only
the live lane's `live:*` tasks (ADR 0103). `.claude/settings.json` pre-approves a short list of commands,
among them `scripts/land`, `scripts/canon resolve <id>` for one pin and `uv add name==version` pinned
exactly, so a lane following a skill is not stopped. What rewrites everything or reaches outside the
checkout asks the owner first: `scripts/canon resolve` with no id, every `uv sync` (a lane syncs with
`uv run --locked`), `uv add` with a range, a URL or an upgrade flag, the installers, a direct `git push`,
and a direct `gh` write of the kinds ADR 0028 lists. If a prompt or a check stops you, do not look for
another route: say what you wanted to do and why.

This is the same for every runtime, Claude Code or not: use no credentials, leave live to the live lane,
and leave landing to `scripts/land` (ADR 0018, ADR 0103).

## Orca

A worker of another runtime takes its terms from `canon-builder` or `canon-reader`, and reports with `canon-report`, in `.agents/skills/`.
Orca is the cockpit (ADR 0018). From a session Orca started, its `orca-cli` and `orchestration` skills may
be used within these limits (ADR 0021):

* Handoffs use `--agent claude`. A worker that builds is a Claude worker or, under ADR 0058's terms, a
  worker of another adopted runtime (the agent ids below). Such a worker has its own workspace and a
  task that names the paths it may change; it leaves its work in its tree and reports, and never
  commits, lands, pushes, installs anything, runs paper tests or reads a credential; the Claude lane
  that reproduces its tests commits it, runs the review ADR 0017 requires and lands it (ADR 0079). The build eval (ADR 0057) is
  the measure that keeps or retires it. A reviewer may be Codex (`--agent codex`),
  Muse Code (`--agent muse`; deferred by the owner on 2026-11-13, so none is started) or Antigravity
  (`--agent antigravity`); DeepSeek Harness (`--agent dsh`) may
  stand in like Antigravity once `scripts/bootstrap --check` shows its review patch ok (ADR 0043, ADR 0045).
* A reviewer reads a change and reports findings. It never edits, commits, pushes, lands, runs paper tests
  or reads a credential. Orca pre-fills a bypass flag for Codex, Muse and Antigravity, so they run with
  no sandbox and no approvals, and no `ask` rule prompts for any of them, so its task says findings
  only. It gets a new worktree at the commit under review, never the main checkout or another worker's
  workspace; afterwards the coordinator checks the worktree has no change and no new commit, and
  removes it. It is an additional reader, never ADR 0017's required review, and
  never blocks: only the coordinator starts one, after landing unless it names the change, and no lane
  waits on it. Its findings are claims the owning lane reproduces before changing anything. Codex runs
  on the model and effort ADR 0045 names; the others run on the defaults ADR 0059 names: Antigravity's
  is in its settings file, and Muse is started with `--model muse-wonder-1.0 --effort max`.
* The review lifecycle (ADR 0045): a Claude lane builds, runs `/code-review` and lands; a trading-path
  change is landed by a Claude lane started for it, with Fable as its advisor (ADR 0141, rule 8 of
  `.claude/rules/dispatch-practices.md`). For a change on
  the paths that get `/code-review` at high effort, the coordinator creates the review as a task that
  depends on the build (`task-create --deps`) and starts it when `task-list --ready` shows it and the
  change is on `main`: Codex and Muse there, Antigravity in place of either (Antigravity in Muse's place
  while Muse is deferred, 2026-11-13). The owning lane reproduces
  each finding, by a failing test where one can show it, and fixes forward, crediting the second reader.
* A non-Claude runtime may reach GPT and other providers through OmniRoute, a local gateway on 127.0.0.1
  installed and started on request (`mise -E gateway run omniroute:serve`); Codex through it is
  `mise -E gateway run omniroute:codex` (ADR 0056). That changes none of the terms above. No Anthropic or
  Claude credential, account or model is ever configured in it, and Claude Code keeps its own login. A provider credential goes in only by the
  owner, at omniroute:provider's hidden prompt or in its dashboard; a session never enters, reads or prints one.
* A worker that changes files gets its own workspace (`--worktree new-top-level`). Share yours only for
  work that reads.
* Do not run through an Orca terminal what a prompt would have asked the owner, and do not answer another
  agent's permission prompt. A worker's change lands like any other: reviewed first, then `scripts/land`.
* Research, record checks and code review stay subagents, apart from the reviewers above. Use an
  Orca worker when the work needs its own workspace or the owner asks to watch it, on the model
  `canon/models.json` gives that work (`--model`).
* Not used: remote placement, the browser and desktop control, Orca's plugins, automations, session search,
  and Orca's other bundled skills. Orca loads one plugin of its own into each session, `orca-agent-view`
  (ADR 0023). The one other plugin is Claude HUD, the status line's renderer, which the lead or the owner
  installs (ADR 0023's exception); a session installs, loads or enables none, and never runs its `/hud` install.
* Do not start Orca from a session (`orca open`): the app would inherit that session's environment.

## Landing

The owner's standing instruction (2026-11-09): work lands automatically once its checks pass. Do not ask
"merge into main?" (ADR 0014).

1. One branch per change. A session that runs in parallel with another works in its own git worktree;
   two sessions never write in one checkout.
2. Commit, then run `scripts/land`. It runs the landing checks (`scripts/ci --landing`: the quick set and a
   secret scan of the whole history, and on macOS `scripts/test-lanes` and `scripts/test-maintenance`) on
   what is committed, on a branch that contains the current `main`,
   and lands the branch once GitHub's required check, the full suite, passes; with no `origin` remote it
   runs every check here (ADR 0144). A branch behind `main` is rebased if it was never pushed;
   once pushed, `origin/main` is merged into it instead, since a rebase could only be pushed with force. With an `origin` remote, `gh` must be signed in, or it stops
   and lands nothing; it checks every address it would publish, pushes, opens a pull request, posts the
   `review` and `signoff` statuses, and waits up to 45 minutes (`LAND_WAIT_SECONDS`, default 2700) for
   GitHub to squash-merge exactly the checked commit with the checked subject and message. If the wait
   runs out first, it stops and says what it last read; with auto-merge requested, GitHub may still merge
   once the checks pass, so look at the pull request. With no `origin` remote
   it fast-forwards the local `main`. The same checks run on this machine and on GitHub's runners
   (ADR 0014).
3. Every change needs an independent review before it lands, unless every path it changes is
   `README.md`, `packages/*/README.md`, `catalog/README.md`, an edit to an existing
   `docs/adr/NNNN-*.md` or an edit to `uv.lock`; a new or renamed record needs one. `/code-review` reads
   code, the `evidence-verifier` reads a record. Resolve what it finds, then land with
   `--reviewed "<what ran and what came of it>"`; `scripts/land` refuses such a change without it and
   records the note on the commit as the `review` status. With no `origin` remote the note is printed and
   not recorded anywhere (ADR 0017, ADR 0019).
4. Pull request text has `Summary`, `Test Plan` and `Evidence`, written for a later reader: no conversation
   history, discarded attempts or tool usage. Titles are a plain sentence, with no `feat:` style prefix.
   A commit that fixes a finding ends with a `Fails-before: <test file>::<test function>` trailer for each
   test that failed before it, and `Confirms: <round or reader>/<finding>` where it answers a review, which
   `scripts/land` carries into the squash commit, warns about when a trading-path fix lacks one, and
   `scripts/fix-index` lists (ADR 0073).
5. This covers work a session of the owner's produced. It does not cover creating a remote, changing a
   repository's visibility, or anything a permission rule asks about; those still go to the owner.
6. It also covers the dependency bot's pull requests (ADR 0016). When the session brief lists any, land
   them with `scripts/land --pr <n>`, which refuses a pull request from a fork, checks the pull request
   out in its own worktree, merges `main` into it if it is behind, lands it under its own title and
   description, and removes the worktree and branch. An edit to `uv.lock` alone lands on passing checks, with no note. One that moves a
   workflow action fails the canon lint until its registry entry moves on the same branch, through
   `adopt-upstream`, and then lands with a `--reviewed` note like any other workflow change.

## Reporting

End a task by saying what you verified and how, what you could not verify, and what is left.
