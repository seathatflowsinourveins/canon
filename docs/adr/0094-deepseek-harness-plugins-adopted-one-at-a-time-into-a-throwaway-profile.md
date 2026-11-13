---
status: accepted
date: 2026-11-11
decision-makers: the owner for the direction (2026-11-11); master orchestrator (Claude) for the order, the exclusions and the dsh-ci-context override, under the owner's delegation (ADR 0074)
review-by: 2026-12-11
---

# DeepSeek Harness plugins are adopted one at a time, each into a throwaway profile, and the first, dsh-ci-context, runs under an exact-version override

## Context and Problem Statement

The owner's direction is that DeepSeek Harness be built up into harnessed workers for automation, GitHub
automation first, with `/code-review` still the required review (ADR 0017). ADR 0072 says how an addition
is trialled — one adoption each, a named configuration, the two evals decide — and ADR 0086 records stock
and the review and build configurations. A source-only comparison of ten community plugins,
read at pinned commits on 2026-11-11, ranked them by runtime reach. Which are adopted, in what order, and where
are they installed?

## Decision Drivers

* `AGENTS.md` rule 2: nothing is installed before its registry entry, pin and record exist.
* ADR 0072 rule 5: until containment exists, a trial runs in a throwaway runtime home with nothing of the
  owner's in it. Rule 6: an addition that keeps state across sessions, or sends the transcript to a second
  model, runs only in the build arm.
* A worker holds no GitHub credential (ADR 0021, ADR 0058), so a plugin that needs one cannot run in it.
* Every entry is identified by repository and commit: two repositories publish a plugin named
  `dsh-automations`.

## Considered Options

1. Install the ranked plugins into the profiles in use.
2. Adopt each alone through `adopt-upstream`, install it only into a throwaway `DSH_HOME` profile, and
   leave keeping or dropping it to the evals.
3. Adopt none.

## Decision Outcome

Chosen: **option 2**, in the orchestrator's order. Option 1 is what ADR 0086 records as unadopted drift.

1. **Where.** Each plugin is installed with `DSH_HOME=<throwaway> dsh plugin --profile trial-<id> add
   github:<repo>#<commit>`, never into `~/.dsh` or a profile in use. Each is a named configuration and
   stays `UNVERIFIED` until the review and build evals score it (ADR 0057). Amended 2026-11-13 by ADR 0143:
   a plugin adopted for the desktop app goes into its `desktop` profile itself, the owner's enhanced tier.
2. **Order.** dsh-ci-context; then dsh-trellis (`1264459640/dsh-trellis` `d30f232e6419`), dsh-ci-diet
   (`0disoft/dsh-ci-diet` `a98294d0946f`) and the file-based `dsh-automations`
   (`romany-saad/dsh-plugins` `4cd615bf9c19`, `packages/dsh-automations`), the last three in the build
   arm only. Each gets its own source read and entry when its turn comes.
3. **dsh-ci-context** (`lucas-ward/dsh-ci-context`, tag `v0.1.1`, `7d814e3def47`) may run in either arm:
   its source writes nothing, calls no network and keeps no state of its own. It adds a message to the
   current session's log on a turn's first step, again only when the CI data has changed.
4. **The override.** dsh 0.2.1-rc.3 refused it. Its peer range for `@deepseek-ai/dsh-agent` and
   `@deepseek-ai/dsh-llm` is `^0.1.0-rc.6`, which excludes 0.2.1-rc.3, and dsh printed: "Plugin
   dsh-ci-context@0.1.1 is incompatible with dsh 0.2.1-rc.3: requirements
   {"@deepseek-ai/dsh-agent":"^0.1.0-rc.6","@deepseek-ai/dsh-llm":"^0.1.0-rc.6"}. Running it may cause
   crashes or data loss." The orchestrator chose to grant the exact-version exemption dsh offers (`dsh plugin
   --profile trial-ci-context allow-version dsh-ci-context@0.1.1 --dsh-version 0.2.1-rc.3
   --accept-risk`), in the throwaway profile alone; it writes the pair into that profile's
   `compatibility.json`. It is an override of the host's compatibility guard; a crash or data loss in
   either eval retires the plugin. For a git spec dsh judges compatibility only after the fetch, so the
   refused package had been downloaded and installed before dsh restored the profile; it has no install
   script.
5. **Not adopted** (the orchestrator, 2026-11-11), on the source-only comparison's findings, which this
   record did not re-read (`UNVERIFIED` here):
   * dsh-session-superagent (`MYCF711/dsh-session-superagent` `068caab732b7`): writes to GitHub,
     installs from npm, holds secrets.
   * the lifecycle `dsh-automations` (`439436269-ctrl/dsh-automations` `4509f5d4ebe6`): runs arbitrary
     configured shell commands and HTTP requests.
   * dsh-github-skills as shipped (`Starfie1d1272/dsh-github-skills` `8a2adf3fa678`): its instructions
     commit, push and open pull requests through `gh`, and a worker holds no GitHub credential.
   * dsh-github-picker (`bitxeno/dsh-github-picker` `32caff38c16b`): a GUI picker that probes `gh`'s login.
   * dsh-approval-consult (`0tests/dsh-approval-consult` `5b47676d6785`): stays out of the review arm,
     and waits until the build arm has an eval baseline.
   * dsh-plan-note (`0-0-user/dsh-plan-note` `7d08d29c4747`): its drafts live in browser
     `sessionStorage`, so it serves the desktop GUI and gives a headless or ACP worker nothing.

### Consequences

* Good: each plugin has a pin and a record before it runs, and none touches a profile in use.
* Bad: dsh-ci-context runs outside the engine range its author declared; that it loads and works on
  0.2.1-rc.3 is `UNVERIFIED`. Any worker profile that carries it would need the same exemption. Its
  repository was last pushed 2026-08-15, so the override exists because it has not followed dsh to 0.2.
* Bad: a throwaway profile is not a profile Orca launches, so these configurations are not yet reachable
  from the cockpit (ADR 0072 rule 5, ADR 0086).

### Confirmation

Each plugin has its own entry in `canon/upstreams.json` naming this record, and its trial profile's
`package.json` names the pinned commit. An eval sheet that runs one names the configuration.

## Evidence

Read and run 2026-11-11.

* `lucas-ward/dsh-ci-context` at `7d814e3def47` (tag `v0.1.1`): every file read through `scripts/canon
  src` (resolved). `lib/index.js` registers one `agent/pre-step` listener that returns a message on step 1;
  `lib/context.js` reads fixed environment fields and builds URLs without fetching them; no `fs`, `fetch`
  or subprocess call; `cordis.patch.yml` inserts only `ci-context`; MIT `LICENSE`.
* https://raw.githubusercontent.com/lucas-ward/dsh-ci-context/v0.1.1/package.json — the peer ranges
  `^0.1.0-rc.6`, which under semver is `>=0.1.0-rc.6 <0.2.0-0` and so excludes 0.2.1-rc.3 (fetched).
* https://raw.githubusercontent.com/lucas-ward/dsh-ci-context/v0.1.1/README.md — "does not poll provider
  APIs, read logs or test results, trigger or rerun pipelines, or write to repositories"; first release
  targets DeepSeek Harness 0.1.0-rc.6 (fetched).
* https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/dsh-v0.2.1-rc.3/docs/cli-help.md —
  `dsh plugin --profile <name>` forwards to pnpm in `$DSH_HOME/profiles/<name>` (fetched).
* `deepseek-harness` at `dsh-v0.2.1-rc.3`, `packages/boot/plugin-manager/README.md`, "Version
  compatibility and exemptions" (resolved): a git spec is judged after installation and the profile
  restored; an exemption is an exact `package@version` mapped to exact dsh versions in the profile's
  `compatibility.json`; the CLI's `allow-version <package@version> --dsh-version <runtime>
  --accept-risk`. A second reader (2026-11-11) also found, in the same source, that the `agent/pre-step`
  payload, the `{kind: 'enter', messages}` decision and `createUserMessage` the plugin uses still exist
  at 0.2.1-rc.3: static support only.
* `DSH_HOME=<a new empty folder> dsh plugin --profile trial-ci-context add github:lucas-ward/dsh-ci-context#7d814e3…`
  (run): rejected with the message quoted above; dsh restored the profile. After `allow-version …
  --accept-risk` (run; dsh: "allowing incompatible plugin versions can break the application or corrupt
  data"), the add succeeded, and `dsh --profile trial-ci-context --dump-config` (run, exit 0) composes
  `@deepseek-ai/dsh-base` and the `ci-context` row; the profile's `compatibility.json` holds
  `{"dsh-ci-context@0.1.1":["0.2.1-rc.3"]}`. `find ~/.dsh -newer <a file written before the install>`
  printed nothing.
* A source-only ranking of ten plugins at pinned commits, 2026-11-11 (a worker report, read; its
  findings are `UNVERIFIED` here except for dsh-ci-context). `git ls-remote <repo> HEAD` for each of the
  nine other repositories named above returned the commit given for it (run).
* A second reader checked this record on 2026-11-11: no claim failed; it asked for the repositories and
  commits above, the exemption's source and the per-change wording, now added.
* Loading the plugin in a running agent, and both evals: **UNVERIFIED — not run.**
