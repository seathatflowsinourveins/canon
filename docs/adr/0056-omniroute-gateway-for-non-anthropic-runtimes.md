---
status: accepted
date: 2026-11-10
decision-makers: repo owner (2026-11-10); build lane (Claude)
review-by: 2026-12-10
---

# OmniRoute as a local gateway for GPT and other non-Anthropic runtimes, started on demand, never holding a Claude credential

## Context and Problem Statement

The owner wants more GPT-powered and other runtimes, reached through one local gateway, OmniRoute, and
decided on 2026-11-10 that it carries GPT and other providers only, never Anthropic: Claude stays native.
OmniRoute will hold
provider credentials. How is it pinned, installed, run and configured, and what holds the rule?

## Decision Drivers

* A tool the owner names that is cheap to leave is adopted on the owner's word, untested parts UNVERIFIED
  (ADR 0018). Non-Claude runtimes read and report only (ADR 0043, ADR 0045).
* A secret is in no file, command line or conversation, and reaches only the process that needs it (ADR 0029).
* Its defaults are not safe here (Evidence): every interface, no API key on `/v1`, a published password.

## Considered Options

1. No gateway: each runtime keeps its own sign-in (as now).
2. OmniRoute from npm through mise, started on demand by a mise task that fixes its settings.
3. Option 2 as a launchd agent, through OmniRoute's own `autostart` or `scripts/automation`.
4. OmniRoute in Docker, as its trial guide suggests.
5. Option 2 with Claude or Anthropic connected as well.
6. A build worker on another provider through it, decided here.

## Decision Outcome

Chosen: **option 2**, on the owner's instruction.

1. **No Anthropic or Claude credential, account or model is configured in OmniRoute, ever; Claude Code keeps
   its own login.** Claude Code's legal and compliance page says OAuth is for "ordinary use of Claude Code
   and other native Anthropic applications", that developers may not "route requests through Free, Pro,
   Max, or Ultra plan credentials on behalf of their users" nor "collect, store, or intermediate Claude.ai
   credentials", and that enforcement may come "without prior notice". In OmniRoute's own tracker a user reports an Anthropic ban after routing a Claude
   account through it; the maintainer answered that it rewrites every Claude OAuth request (system prompt,
   identity, headers, field order) and "There is no raw or transparent mode for this path"; the request
   for one (#13893) was closed "not planned" on 2026-10-10. A second issue,
   opened by the maintainer from two relayed reports, has Team/Max accounts banned about a minute after
   connecting. `scripts/bootstrap --check` fails when a connection's provider, labels or default model says
   Anthropic or Claude, when a connection is not an API key, and when an API key is not no-log: it reads the
   `provider_connections` table OmniRoute's `providers list` reads and `api_keys`, with
   Apple's `sqlite3` under `PRAGMA query_only`, selecting no key column (OmniRoute's CLI would open the
   database for writing and read a `.env` from its working folder). A Claude model reached otherwise (a
   combo, an alias, Bedrock or Vertex) is held by this rule, not by the check.
2. **The pin, installed only on request**: `omniroute` `tag:v3.8.53` (`191dce8d534c`), `npm:omniroute` in
   mise's `gateway` config environment, `mise.gateway.toml`, locked with its dependency graph in
   `mise.gateway.lock` and `.mise/locks/mise.gateway/`; it is installed with `mise -E gateway install
   --locked npm:omniroute`. A plain `mise install`, `scripts/bootstrap --install` and hosted CI read only
   `mise.toml`, so none installs it (ADR 0043's reason for keeping Codex out of `mise.toml`). The lint reads
   `mise.toml` alone, so the entry has no `mise` field; `scripts/test-lanes` holds the environment's version
   to the pin and `scripts/bootstrap --check` the installed one. A pin move reads the next release's notes and
   provenance, names that version in the exemption, and regenerates the lock and graph with `mise -E gateway
   lock`. It is past both fixes for GHSA-hf57-cqmx-p4gr: 3.8.49 made `/api/acp/agents` loopback-only, 3.8.50 limits the
   version probe to a bare flag. mise's installer refused 3.8.53: 3.8.49 and 3.8.50 carried the
   maintainer's 2FA approval, later releases do not (the publish workflow's new default). Two versions are
   exempted by name: 3.8.53, its provenance read, and the dependency `@pierre/theme@1.1.0`, whose new
   publisher gave none. mise's guide says to exempt "After reviewing such a dependency" (tarball,
   publisher); that one was not reviewed further, a departure stated here. 3.8.53 is the first release
   built after the repository moved: it is the same repository and the same maintainer, transferred to a
   new organisation, `cheaper-inference`, created 2026-08-25, whose members are not public. GitHub redirects
   `diegosouzapw/OmniRoute` there with the same repository id, not a fork; the same account made every
   release from 3.8.49 to 3.8.53; npm's maintainer is unchanged; no statement by the project on the move was
   found. 3.8.50, the last release with the approval, was not chosen: it lacks 3.8.51's and 3.8.52's
   authentication and credential-export fixes, and provenance is intact on every release.
3. **Its settings, fixed in `mise -E gateway run omniroute:serve`** (sources in Evidence), which clears every
   exported variable but a few before it starts it, so nothing of the caller's environment (a proxy, `NODE_OPTIONS`, a CA
   bundle, a port) reaches it, and which its `.env` loading never overrides: `OMNIROUTE_SERVER_HOST` and
   `API_HOST` at 127.0.0.1, one port (`--port 20128`, `API_PORT` and `DASHBOARD_PORT` equal: ports that
   differ start a second, API-only listener, `apiBridgeServer.ts`), `--no-tray`, `REQUIRE_API_KEY=true`,
   `OMNIROUTE_DISABLE_CLI_TOKEN=true`, `ENABLE_REQUEST_LOGS=false`,
   both update-check switches, `ARENA_ELO_SYNC_ENABLED=false`, `OPENROUTER_PROVIDER_STATS_ENABLED=false`,
   Cloud Sync unset (`cloudSync.ts` reads "off" as off, but the start-up check in `runtimeEnv.ts` refuses
   any value that is not an http URL), no package `.env` (with `CHANGEME`) read,
   and a key file in the data folder stops the start. OmniRoute applies a `REQUIRE_API_KEY` override stored
   in its database over the environment, with no setting to reverse that (`featureFlags.ts`), so the task
   refuses to start on one that turns it off. Nothing is typed but the provider's key: `omniroute:store`
   makes the storage key (AES-256-GCM over each stored credential) with openssl into the login keychain, and
   serve reads it into the gateway alone; with the gateway running, `omniroute:setup` turns the dashboard
   login off through OmniRoute's own skip-password route and mints the gateway's API key with `noLog`
   through `POST /api/keys`, straight into the keychain. The owner decided on 2026-11-11 that the dashboard
   opens without a password; the cost: any local process or account on this machine can open the dashboard
   and export the stored provider keys (`operatorCredential.ts`, login off). Every task reaches the gateway
   with `/usr/bin/curl -q --noproxy '*'` (no curlrc, no proxy), and asks whether the key is required with a
   made-up key on an inference route, since `GET /v1/models` follows the login, not `REQUIRE_API_KEY`
   (`docs/reference/ENVIRONMENT.md`). The interception feature needs a sudo password typed in its dashboard
   to touch the trust store; nothing here starts it.
4. **Started on demand, not as an agent.** OmniRoute's macOS `autostart` writes a LaunchAgent that runs
   `serve` with no environment, so its settings and key would come from a file. `scripts/automation` is not
   changed.
5. **Codex reaches it by Codex's own provider configuration**, `mise -E gateway run omniroute:codex`: `-c` overrides
   (`model_provider`, `base_url`, `wire_api = "responses"`), so `~/.codex/config.toml` is untouched, and the
   gateway's key (the one `omniroute:setup` minted) through Codex's documented `auth.command`, which runs
   `security` and reads the key from its stdout. OmniRoute's own guide uses `env_key` instead; a command
   keeps the key out of every environment and every file.
   A Codex Orca starts takes the same `-c` arguments in that agent's Orca settings (UNVERIFIED).
6. **Provider API keys only, never a sign-in, entered by the owner** (decided 2026-11-11) at
   `omniroute:provider <id>`'s hidden prompt, which registers it through `POST /api/providers` (an API-key
   connection always) and tests it; a key made for the gateway alone with a spend limit at the provider is
   cheap to revoke. An OAuth login would also open a listener on every interface while it waits
   (`src/lib/oauth/utils/server.ts`, hard-coded). For an OpenAI API key: OpenAI calls API keys "the recommended default for automation" and for "programmatic
   Codex CLI workflows", billed at API rates; the maintainer rates API-key providers "Low risk". For a
   ChatGPT or Codex subscription login: the maintainer rates it "Higher risk", "technically outside the
   intended use case", and "For casual/personal use, OAuth providers like Codex are generally fine if you
   use them at normal human-like rates"; in the same thread a user reports "13 account" banned and the
   maintainer answers there is "always a higher risk of bans". OpenAI's auth page has Codex sign in "with
   ChatGPT or an API key" behind "an LLM proxy server" (`requires_openai_auth`), and says nothing of a
   third-party gateway holding the ChatGPT login; OpenAI's Terms of Use could not be read (403).
   This record gives no legal opinion.
7. **What a runtime started through it may do is unchanged**: what that runtime's own records allow, reading
   and reporting under ADR 0043 and ADR 0045 and building under ADR 0058's terms. The gateway grants
   nothing of its own.

Option 3 puts the key in a file or needs an untried login-time keychain read; option 4 needs Docker, not
in the registry; option 5 is refused by the owner's rule and the sources above. Option 6 is ADR 0058's
to decide, and it has: an adopted runtime may build under its terms, with ADR 0057's build eval as the
measure that keeps or retires it, whichever provider its model comes from.

### Consequences

* Good: GPT and other providers through one loopback endpoint, its key in no file or environment variable.
* Bad: one more process holds provider credentials. "Telemetry is disabled by default" (README image text)
  is the project's claim; no independent review was found. At start-up it asks the npm registry and
  GitHub's API for the latest versions of the CLIs it imitates (`claudeCodeClient.ts`, `codexClient.ts`,
  `geminiCliClient.ts` and others); per-CLI version overrides exist, untested as a way to stop that. Call
  metadata stays in its database (its clean-up logs 90 days); app logs in `~/.omniroute/logs/application/` (50 MB a file, 20 files, 7 days). Releases are
  large (1,162 commits in 3.8.53) and no longer carry the maintainer's approval.
* Known limits, from two further readers (Codex, Antigravity, 2026-11-10) and the review of the fixes,
  read against the pin on 2026-11-11. With no setting to close them: a `REQUIRE_API_KEY` override made while
  the gateway runs applies within seconds, and serve refuses it only at the next start; an OAuth login
  listens on `0.0.0.0` (rule 6 keeps sign-ins out). Closed by settings above: the loopback CLI token's
  export route (`OMNIROUTE_DISABLE_CLI_TOKEN`), though with login off any local caller has that reach anyway,
  and a process of this user could write its own password hash into the unencrypted database; prompt and
  response bodies under `call_logs/`, which `ENABLE_REQUEST_LOGS` does not govern, kept out by a no-log key
  only (`docs/security/COMPLIANCE.md`; minted so, and checked). The storage key is in the server's
  environment; `ps eww` from another process of the same user did not show a made-up variable on macOS
  27.2 (run). In the wrapper: the provider check does not read `provider_nodes` base URLs or Claude-only
  gateway providers; the version row reads any failure of `mise where` (an untrusted worktree, another
  version installed) as absent; a gateway task run with the package absent lets mise install it unlocked
  (`task.run_auto_install`); a caller's `MISE_ENV=gateway` reaches `scripts/bootstrap --install` and
  `scripts/ci`.
* Leaving: delete the entry, the pin, `mise.gateway.toml`, `mise.gateway.lock`, `.mise/locks/mise.gateway/`,
  the bootstrap rows and the lines in `AGENTS.md`, `README.md` and `docs/architecture.md`; `mise -E gateway
  run omniroute:remove`; delete `~/.omniroute` (the owner's).

### Confirmation

The no-credential trial, 2026-11-10 (`omniroute` 3.8.53, Node 24.22.2, macOS 27.2), with throwaway random
secrets and a throwaway data folder, `omniroute serve --no-open` with the settings of rule 3 but
`EMBED_WS_PROXY_HOST` (its default is loopback, and the listener below was), and in run 2 without the two
sync switches and `OMNIROUTE_NO_UPDATE_NOTIFIER`:

* Listening, `lsof -nP -a -p <pids> -i` and `lsof -nP -iTCP -sTCP:LISTEN`: `127.0.0.1:20128` (dashboard and API), `127.0.0.1:20131`
  (embedded-service WebSocket proxy), `127.0.0.1:20132` (live WebSocket); nothing else. From the LAN
  address, `curl` to each port: no connection.
* Unauthenticated, each 401: `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` ("Invalid
  API key" with a made-up one), `POST /api/settings/require-login` with `{"requireLogin":false}` (the
  advisory's setup window), `POST /api/acp/agents`, and a login with `CHANGEME`.
* Outbound, `lsof -nP -i` on the supervisor and its server process about 1.6 times a second and `nettop -x
  -p` on the server. Run 2: in the first 47 s, the npm registry, GitHub's API,
  `openrouter.ai` (676 KB in), `api.wulong.dev` and a Hugging Face CDN address, then nothing for three
  minutes. Run 3, with them: at 2 to 8 s after start, 6 connections to the npm registry (30 KB in) and 3 to
  GitHub's API (83 KB in), as in run 2, so the server's update switch made no visible difference there;
  then nothing for 290 s, idle and while refusing the requests above. A first run
  watched only the supervisor at start-up, which is how it missed the server's start-up fetches.
* No `.env` or `server.env` was written; the server logged the JWT and API-key secrets as generated and
  persisted, which `src/lib/db/secrets.ts` stores encrypted in the database.
  `omniroute providers list --json` on that database: `{"providers": []}`, no key read. On a scratch
  database that CLI made, the bootstrap's `sqlite3` read (`PRAGMA query_only=1`, then the select) listed
  rows and refused a `DELETE` ("attempt to write a readonly database"), the row count unchanged.
* Codex 0.167.0 (`codex exec --ephemeral --ignore-user-config`, an empty folder, the `-c` overrides of rule
  5, `auth.command=/bin/echo` with a made-up key): it sent that key to `/v1/models` and `/v1/responses`,
  each 401 "Invalid API key". Codex itself also opened six connections to an address `chatgpt.com` and
  `ab.chatgpt.com` resolve to; what they carried was not inspected.
* Stopped with SIGINT; after run 3 no process was left (run 2's process check matched the watching shells
  and proves nothing); nothing listening on the ports; the data folders removed.

UNVERIFIED: anything with a provider credential (the second trial below); what each start-up GitHub call
asks for; that the telemetry claim holds with providers connected; a connection shorter than the sampling
interval; Codex through Orca; the call-log retention a key without no-log gets.

The second trial, after `omniroute:store`, `omniroute:serve`, `omniroute:setup` and the owner's
`omniroute:provider <id>`: in a new empty folder, `mise -E gateway
run omniroute:codex -- exec --ephemeral --skip-git-repo-check -s read-only "Reply with the word ok"` from the
checkout. Look at: the answer; `scripts/bootstrap --check` (`omniroute providers` ok, no Anthropic); `lsof
-nP -i -a -p <server pid>` during the request (loopback and the provider's own host only); the dashboard's
call log (metadata, no body). Stop if: anything listens beyond 127.0.0.1, an address other than the
provider's and the start-up lookups above appears, a body is stored, or a request answers without a key.

Confirmation, 2026-11-11, the lead's live run at commit 2847368:

* The locked install confirmed 3.8.53.
* `omniroute:store` made the storage key into the keychain.
* `omniroute:serve` listened on 127.0.0.1 only: 20128, 20131 and 20132.
* `omniroute:setup` turned the dashboard login off and minted a no-log API key into the keychain.
* `omniroute:check` showed both items, and "running, its API key required".
* An API call with a made-up key got 401.
* The dashboard answered 307 to `/dashboard`, then 200 with no login.
* The bootstrap `omniroute providers` row read: no connections, none Anthropic, API keys only, every key
  no-log.

### Addition of 2026-11-11: the gateway kept running as a launchd agent

Once OmniRoute is installed for the checkout, `scripts/automation install` writes a fifth launchd user
agent, `local.canon.omniroute`, beside the paper and maintenance agents. Without OmniRoute installed it
writes nothing and says so. When mise says it is not installed, install also removes an agent an
earlier install wrote; when mise gives no clear answer, the agent is kept and install fails. The agent runs mise.gateway.toml's own task, `mise -E gateway run
omniroute:serve`, from the checkout:

* `KeepAlive`, started at load, with its log in `~/.claude/canon/watch/omniroute.log`.
* Its environment sets `PATH`, `MISE_TASK_RUN_AUTO_INSTALL=false` ("Install missing tools automatically
  before tasks run", mise's settings reference) and `CANON_GATEWAY_CONFIG`, which the task does not read.
  With the first, a missing package should fail instead of being installed unlocked, closing that limit
  above for the agent (from the reference; not run, UNVERIFIED).
* `CANON_GATEWAY_CONFIG` is a checksum of `mise.gateway.toml`. A change to the task therefore changes the
  plist, and the maintenance agent (ADR 0065, addition of 2026-11-11) reinstalls it. A change to its lock
  alone does not.

The task's settings, the loopback listener and the keychain reads are unchanged: the agent invents none.
mise passes SIGTERM on to its task (run: `mise run` of a task that `exec`s a sleep, with SIGTERM sent to
mise, ended the sleep), so a bootout should stop the server (not run against the gateway itself).

The gateway takes no paper lock. `scripts/automation status` gains an `omniroute gateway` row:

* ok when launchd shows the agent running and an inference route refuses a made-up key with 401, as
  `omniroute:check` asks;
* FAILED when nothing answers, or when such a key is taken;
* UNKNOWN when another copy holds the port.

The `scripts/bootstrap --check` version row now gives that probe's answer too, where before it said only
"the package on disk". A copy started by hand with `omniroute:serve` holds the port, so it must stop
before the agent's copy can listen. Rule 2's "installed only on request" stands: the agent runs only once
the package is installed.

UNVERIFIED until the agent runs on this machine:

* that its keychain reads succeed from a launchd job without a dialog;
* that a stop through launchd leaves no process of the gateway.

### Correction of 2026-11-11: the stored-settings read opens the database normally, under `query_only`

The agent's first start failed in `omniroute:serve`'s read of the stored `REQUIRE_API_KEY` override.
OmniRoute keeps `storage.sqlite` in WAL mode (header bytes 18 and 19 are 2), and a clean stop removes its
`-wal` and `-shm` files. SQLite's WAL page says a WAL database can be read only if those files exist or can
be created. A `-readonly` connection does not create them, so Apple's `sqlite3` 3.54.0 refused the open
("unable to open database file (14)") and the gateway could not start again after a clean stop. The task now
opens the database as `scripts/bootstrap --check` does: a normal open, with `PRAGMA query_only=1` ahead of
the select, which refuses a statement that writes ("attempt to write a readonly database"). The open is
not inert. It creates the side files, and after a crash SQLite's recovery checkpoints a leftover WAL or
rolls back a hot journal into `storage.sqlite`, which OmniRoute's own open would do next anyway (the
review reproduced the checkpoint on a copied WAL). The task's comment had said that bootstrap's read was
read-only; it now says what both do. `scripts/test-lanes` covers it with a WAL
database that has no side files. That database is read, an override in it is still refused, and an INSERT
through the task's own open is refused with nothing stored. Those three cases failed on the task as it was.

## Evidence

Read 2026-11-10; `fetched`: downloaded and searched, not summarised. Source lines are at `v3.8.53` in a
clone of the tag unless named otherwise (`read`).

* https://github.com/cheaper-inference/OmniRoute — `gh api repos/diegosouzapw/OmniRoute` answers as
  `cheaper-inference/OmniRoute`, MIT, not a fork; releases v3.8.49 to v3.8.53, the last 2026-11-06 (fetched).
  `tag:v3.8.53` → `191dce8d534c` (resolved). LICENSE: "MIT License" (fetched).
* https://github.com/cheaper-inference/OmniRoute/security/advisories/GHSA-hf57-cqmx-p4gr — critical, CVE-
  2026-88062, `POST /api/acp/agents` runs a client-chosen command, anonymous under `requireLogin=false` or
  before a password is set; "< 3.8.49", patched 3.8.49 (fetched). https://github.com/advisories/GHSA-hf57-cqmx-p4gr
  says "<= 3.8.50", no patched version, and cites fix PR #11028, merged 2026-08-21 into `release/v3.8.50`
  (fetched). The two disagree; the tags show 3.8.49 adding `/api/acp/agents` to the loopback-only list
  (`src/server/authz/routeGuard.ts`) and 3.8.50 adding the version-probe limit (`src/lib/acp/registry.ts`),
  both still at 3.8.53 (fetched per tag).
* Release notes v3.8.49 to v3.8.53 (`gh release view`, fetched; the attached full changelogs not read):
  3.8.50 lists #11028 among security fixes; 3.8.51 "Security hardening" (an isolated realm for hooks,
  connection-judged IP rules, management auth on OAuth routes, constant-time token comparison, a loopback-
  only version manager, traversal refused in the Codex `/v1/responses` path) and a boot warning for a
  non-loopback bind without `REQUIRE_API_KEY`; 3.8.52 one security entry; 3.8.53 no security count;
  `CHANGELOG.md` for 3.8.53 adds `Origin` checks on `/api/mcp/*` and spawn limits for remote callers (read).
* https://registry.npmjs.org/omniroute and its attestations — 3.8.49, 3.8.50 published with an
  `approver`; 3.8.51 to 3.8.53 by Trusted Publishing without one; 3.8.53's SLSA provenance names
  `cheaper-inference/OmniRoute`, `refs/tags/v3.8.53`, commit `191dce8d534c`, `npm-publish.yml` (fetched).
  `npm-publish.yml` at v3.8.50 defaults to "staged = npm stage publish (owner approves with 2FA)", at v3.8.53
  to "auto = ... Trusted Publishing (OIDC, no token, no 2FA prompt — the default)" (fetched).
  https://registry.npmjs.org/@pierre%2ftheme — 1.1.0 by another publisher, no attestation (fetched).
* https://raw.githubusercontent.com/jdx/mise/v2026.11.5/docs/dev-tools/backends/npm.md — the embedded
  installer, `trust_policy_excludes` "After reviewing such a dependency", dependency scripts off by default, the lock
  sidecar; `docs/dev-tools/index.md` — `os`; `docs/configuration/environments.md` and
  `docs/dev-tools/mise-lock.md` — `mise.<env>.toml` loads only with `-E`, `MISE_ENV` or a `.miserc.toml`
  default (this repository has none), locks to
  `mise.<env>.lock`, "CI jobs that do not set `MISE_ENV` depend only on `mise.lock`" (fetched). `mise
  install` output: both refusals; `mise install --locked --dry-run npm:omniroute` refused without `-E
  gateway`, "already installed" with it (run).
* `.env.example` — `INITIAL_PASSWORD=CHANGEME` "publicly known", `REQUIRE_API_KEY=false` "only safe when
  ... loopback only", `HOST` default `0.0.0.0`, `ENABLE_REQUEST_LOGS`, the app-log bounds, the two sync
  switches ("ON by default"), `OMNIROUTE_DISABLE_BACKGROUND_SERVICES` (also stops retention clean-up, so not
  used), `CALL_LOG_RETENTION_DAYS` "Default: 7" against the 90 days the trial's clean-up logged (read).
  `bin/cli/utils/serverHost.mjs` — `return "0.0.0.0"`; `bin/omniroute.mjs` — `.env` fills only unset
  variables, a missing storage key is generated into `DATA_DIR/.env`, `update-notifier`;
  `src/lib/db/encryption.ts` — AES-256-GCM per field, plaintext without a key; `src/lib/auth/
  managementPassword.ts`; `src/lib/db/secrets.ts`; `src/lib/cloudSync.ts` and `src/lib/env/runtimeEnv.ts`
  (`CLOUD_URL: optionalHttpUrl`); `src/lib/system/versionCheck.ts`, `src/shared/constants/claudeCodeClient.ts`,
  `open-sse/config/codexClient.ts`,
  `src/mitm/cert/install.ts` (a sudo password, `security add-trusted-cert`); `src/lib/usage/callLogs.ts`
  and `src/lib/compliance/noLog.ts` (bodies kept unless no-log); `bin/cli/tray/autostart.mjs` (the plist);
  `bin/cli/commands/providers.mjs`, `bin/cli/provider-store.mjs` and `bin/cli/sqlite.mjs` (`providers list`
  reads `provider_connections` after opening the database for writing); `src/lib/db/migrations/
  001_initial_schema.sql` (`default_model`);
  `docs/guides/CODEX-CLI-CONFIGURATION.md` (`env_key`, `wire_api = "responses"`) (read).
* README — "telemetry is disabled by default" (image text) and Radar "opt-in and GET-only"; no independent
  review of either was found (read).
* https://github.com/cheaper-inference/OmniRoute/discussions/13869 (2026-09-16), /discussions/1698
  (2026-04-28), /issues/4118 (2026-06-17, closed completed), /issues/13893 (closed not planned 2026-10-10)
  — the ban reports and the maintainer's answers quoted above (fetched through `gh`).
  https://api.github.com/orgs/cheaper-inference — created 2026-08-25, no public members; the npm document's
  maintainers: one, unchanged (fetched).
* https://code.claude.com/docs/en/legal-and-compliance.md — the quotes in rule 1 (fetched).
* https://learn.chatgpt.com/docs/auth.md — API keys for "programmatic Codex CLI workflows", "the
  recommended default for automation"; `requires_openai_auth` for "an LLM proxy server"; nothing on a
  third-party gateway holding the ChatGPT login (fetched). https://openai.com/policies/row-terms-of-use/
  — HTTP 403 to `curl` and to the fetch tool; not read (UNVERIFIED).
* https://learn.chatgpt.com/docs/config-file/config-advanced.md and config-reference.md — custom
  providers, `auth.command` ("must print the token to stdout"), `wire_api` "`responses` is the only
  supported value" (fetched). https://raw.githubusercontent.com/openai/codex/rust-v0.167.0/codex-rs/protocol/src/config_types.rs
  — `ModelProviderAuthInfo` in the pinned Codex (fetched). `codex --help` — `-c` (bundled).
* https://www.onorca.dev/docs/agents/supported — "edit that agent's default arguments or environment in
  Settings" (fetched).
* The trial — commands and outputs as in Confirmation (run).
* ADR 0058 (accepted 2026-11-10, landed while this was written): a build worker of another adopted runtime,
  under its terms; rule 7 and option 6 follow it (read).
* `/code-review` (high), 2026-11-10: ten findings, all fixed: the check had used OmniRoute's CLI, which
  writes to the database and reads a `.env` from its folder, and ignored the default model; a version it
  could not read passed as absent; `omniroute:store` could make a new key over an existing database; the
  prompts trimmed spaces; and four smaller ones.
* Second reader (`evidence-verifier`), 2026-11-10: it corrected the run-3 GitHub count and bytes, the auth
  page's proxy line, the Anthropic and maintainer quotes, the mise review wording, the sampling rate and
  the trial's settings, and found the untried per-CLI overrides, `runtimeEnv.ts`, `secrets.ts`, #13893 and
  the organisation's age; all above.
* Installation decided, 2026-11-11: the owner decided to install OmniRoute as the management gateway for
  future runtimes, SDKs and actions, knowing of the repository's transfer, on 3.8.53 with its two
  exemptions (rule 2). It had been held earlier that day until the transfer, the pin and the readers'
  findings had answers; those are rule 2, rule 3 and Consequences. The package from the no-credential trial
  is on disk, so the bootstrap's `omniroute` row reads ok, and says it reports the package, not a
  configured or running gateway.
* https://api.github.com/repos/diegosouzapw/OmniRoute and https://github.com/diegosouzapw/OmniRoute: 301 to
  repository 1157102282 and to `cheaper-inference/OmniRoute`; that repository: created 2026-02-13, not a
  fork; https://api.github.com/orgs/cheaper-inference: created 2026-08-25, no public members; the releases
  `v3.8.49` to `v3.8.53`: each made by `diegosouzapw`; a search of its README, CHANGELOG, issues and
  discussions for a statement on the move found none (fetched through `gh`, 2026-11-11).
  https://registry.npmjs.org/omniroute and its attestations: `_npmUser` the GitHub Actions OIDC publisher
  for 3.8.49 to 3.8.53, the approver present for 3.8.49 and 3.8.50 only, provenance naming
  `diegosouzapw/OmniRoute` up to 3.8.52 and `cheaper-inference/OmniRoute` for 3.8.53 (fetched). CHANGELOG at
  the tag: 3.8.51's security items and 3.8.52's GHSA fixes, among them credential export and require-login
  made operator-only and an authentication bypass through uppercase paths (read).
* OmniRoute at `tag:v3.8.53` (`191dce8d534c`), cloned at the tag and read 2026-11-11:
  `src/lib/apiBridgeServer.ts` (a second listener on `API_HOST`, default loopback, whenever `API_PORT` and
  `DASHBOARD_PORT` differ), `src/lib/runtime/ports.ts`, `bin/cli/commands/serve.mjs` (its ports, `--port`,
  `--no-tray` and the saved tray preference), `src/shared/utils/featureFlags.ts` ("DB override >
  process.env"), `requireApiKeyValue.mjs`, `src/shared/utils/apiAuth.ts`, `src/lib/db/featureFlags.ts` and
  `settings.ts` (the `key_value` rows), `src/lib/auth/managementPassword.ts`,
  `src/lib/middleware/cliTokenAuth.ts`, `src/server/authz/peerContext.ts`, `docs/security/CLI_TOKEN.md`,
  `src/app/api/settings/export-json/route.ts`, `src/lib/usage/callLogs.ts`, `src/lib/compliance/noLog.ts`,
  `open-sse/handlers/chatCore.ts`, `docs/security/COMPLIANCE.md`, `src/lib/oauth/utils/server.ts` (read).
* curl(1): `-q` "If used as the first parameter on the command line, the curlrc config file is not read or
  used"; `--noproxy` with "*" "effectively disables the proxy" (bundled).
* The setup and provider routes at the pin (read 2026-11-11): `src/app/api/settings/require-login/route.ts`
  (no password yet: the write is allowed; loopback is exempt in `apiAuth.ts`), `src/app/api/keys/route.ts`
  (201 with `key`, `noLog` honoured) and `keys/[id]/route.ts` (`DELETE`), `src/app/api/providers/route.ts`
  (`authType: "apikey"`, `isActive: false` until tested), `providers/[id]/test/route.ts` (`valid`),
  `src/lib/api/operatorCredential.ts` (`open_instance` with login off), `docs/reference/ENVIRONMENT.md`
  ("This flag does not gate `GET /v1/models`").
* `scripts/test-lanes`, the gateway task cases, 2026-11-11: on the first fixes, seven of ten failed on the
  tasks as they were (ports and API_HOST inherited, stored overrides started, the store-key probe through a
  caller's proxy); with the tasks as they are now, every case passes, among them the real curl under a
  caller's proxy and curlrc reaching neither, a key minted no-log and read back, and the provider key on
  curl's input alone (run).
* Second reader (`evidence-verifier`), 2026-11-11, on the first fixes: it upheld them, and corrected the CLI
  token line (the setting closes one route, not a same-user process, which can write the unencrypted
  database), the five-second override lag and the test line's causes; all above. `/code-review` (high),
  2026-11-11: the made-up key had gone to `GET /v1/models`, which `REQUIRE_API_KEY` does not gate; the
  caller's `NODE_OPTIONS`, proxy and CA reached the gateway; keychain errors read as absent; a newline
  could pass the override check; curl from `PATH`; and smaller ones: all fixed above.
* https://www.sqlite.org/wal.html, 2026-11-11 (fetched). Section 5, "Read-Only Databases": a WAL database
  "can still be read as long as" "The -shm and -wal files already exist and are readable", the directory can
  be written so that they "can be created", or the connection uses `immutable`. Section 1: the opening process
  needs write access to the `-shm` file or, if that file does not exist, to the directory. Section 4: the WAL
  file "is deleted automatically when the last connection to the database closes". Apple's `/usr/bin/sqlite3`
  3.54.0 on a scratch WAL database, checkpointed and with its side files removed (run). `-readonly`: "unable
  to open database file (14)". A normal open with `PRAGMA query_only=1`: the select returned, and an INSERT
  got "attempt to write a readonly database". That open left new `-wal` and `-shm` files.
