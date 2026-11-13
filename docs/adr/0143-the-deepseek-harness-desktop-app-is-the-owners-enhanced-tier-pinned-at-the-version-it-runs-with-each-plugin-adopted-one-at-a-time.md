---
status: accepted
date: 2026-11-13
decision-makers: the owner for the tier, local models, GitHub automation under the Fable lead and the newest feed (2026-11-12); the lead (Claude Fable 5.2) for the app's ownership, the inspector and the plugin terms (2026-11-13); the DeepSeek desktop lane (Claude Opus 5.5) for the rest, under the owner's delegation (ADR 0074)
review-by: 2026-12-13
---

# The DeepSeek Harness desktop app is the owner's enhanced tier, pinned at the version it runs, with each plugin adopted one at a time into its own profile, the inspector bundle, session-log upload and product analytics off, and the updater on the nightly channel

## Context and Problem Statement

The owner runs DeepSeek Harness's desktop app beside Orca's dsh workers, on the same `~/.dsh`, and on 2026-11-12
made it the enhanced tier. Nothing recorded it: on 2026-11-13 no record listed its profile's 16 packages at the
versions it carries, its inspector bundle served a DevTools endpoint with no token on 127.0.0.1:9230, its
session-log and product-analytics rows were on by the upstream default, and the 0.2.2-rc.2 app refused four
first-party plugins declared at rc.1.

## Decision Drivers

* The owner's requirements of 2026-11-12: the desktop is the enhanced tier, with more plugins than the workers'
  lean set; OpenViking and WeKnora are kept, on local models; GitHub automation is on while Fable 5.2 leads, and
  Canon still lands only through `scripts/land`; updates follow the vendor's newest native setting.
* The lead's terms of 2026-11-13 (rules 2, 3 and 6 below); rule 2 of `AGENTS.md`, rule 8 and ADR 0103; ADR 0045
  rule 6.

## Considered Options

1. Leave the app unrecorded, as the owner's own tool outside the canon (the state before 2026-11-13).
2. Record it under the `deepseek-harness` pin, the CLI engine Orca's workers run (0.2.1-rc.3).
3. Record it as an entry of its own, at the version the installed app runs, with terms of its own.
4. Option 3 with the app on its stable channel, so that a release could be read before it runs.

## Decision Outcome

Chosen: **option 3**. Option 1 leaves rule 2 unmet, as the drift above shows. Option 2 ties the app to an engine
it has left: its updater moves the app, while the engine pin moves with the CLI and the workers (ADR 0045). Option
4 is set aside by the owner's choice of the newest feed, and stable is not free of release candidates either: the
publisher chooses each channel's versions, and stable can deliver them too. That corrects the plan this record came
from, which said stable receives nothing while every release is a pre-release, and which had the owner turn product
analytics off in Settings, where the app has no such switch.

1. **Its own pin.** `dsh-desktop` tracks the tag of the version the installed app runs, `dsh-v0.2.2-rc.2`, the
   version last read, as ADR 0065 rule 3 sets for Antigravity's self-updater. After an app update it moves through
   adopt-upstream's "Moving a pin", in one change with `app_version` in `home/dsh/desktop.json`, which
   `scripts/dsh-desktop-check` compares with the installed app. It never moves the `deepseek-harness` pin; the app
   running ahead of the workers is not drift.
2. **The enhanced tier.** The `desktop` profile belongs to no Orca arm and is no eval's configuration. A plugin
   enters it only through `adopt-upstream`: an exact version or commit, every file read for network, storage and
   credential use and for any MCP server it registers at run time, named to the deputy first, one at a time, and
   installed into the desktop profile itself (amending ADR 0094 rule 1 for this profile alone). A compatibility
   exemption is allowed here only, with its exact pair recorded. Nothing comes from the app's Discover tab without
   an entry, and nothing that opens a listener without a token, needs a credential a session would enter, or
   touches the trading path or the paper and live pairs. A plugin reaches the workers only as a configuration the
   evals keep (ADR 0057, ADR 0079).
3. **The app is the owner's.** No session quits or restarts it without telling the owner first, each time. A
   session changes the profile only with the app quit, since the app reloads `dsh.profile.bundles` while it runs,
   and through the app's own `dsh` command ("Manage dsh Command…") or its plugin settings, as the upstream leaves
   desktop package operations to the desktop shell.
4. **Updates.** The updater stays on, on the nightly channel (`updates.channel` is `nightly` in the app's settings
   file). A check downloads nothing; the owner starts each download and approves each install, so releases run
   before anyone here reads them. An update strands the first-party plugins declared at the release before, since
   they name their `@deepseek-ai/dsh-*` peers at exact versions: on 2026-11-13 the rc.2 app refused the two
   subagents and the two hook packages at rc.1 until each was moved to rc.2. By the same exact peers an update to
   0.2.2-rc.3 will strand the rc.2 plugins (inferred, not yet seen). After each update the plugins are aligned
   first, then this pin moves.
5. **Data.** The desktop patch sets `session-log-deepseek` to `enabled: false`, as ADR 0045 rule 6 does for the
   workers, and `product-analytics` too. The app's Settings switch "Upload Session Log when using the official
   model API" stays off: turning it back on uploads the events recorded while it was off. Product analytics has no
   switch in the app; its row is the control, and a row in the home-level `~/.dsh/cordis.patch.yml` would outrank
   it (`scripts/dsh-desktop-check` reads both). `session-telemetry-otel` keeps its default, `FEEDBACK_ONLY`: feedback
   or a rating the owner gives in the app sends that session, its context included.
6. **The inspector.** `@deepseek-ai/dsh-experimental-inspector-profile` is out of `dsh.profile.bundles`: its
   endpoint on 127.0.0.1:9230 has no token and grants code execution in the app's Host. The package stays
   installed until the profile's packages are settled; the line that puts it back is in the Confirmation.
7. **GitHub.** While Fable 5.2 leads this repository, the desktop may run GitHub automation on it, each piece
   adopted under rule 2; none is on yet. Canon lands only through `scripts/land`: the desktop posts no `review` or
   `signoff` status, merges nothing and writes to no branch a landing holds. Only instruction holds this, since the
   owner's GitHub sign-in can do all of it. Other repositories are the owner's.
8. **No trading, no credential.** No `paper:*` or `live:*` task, brokerage MCP server or connector runs in it, and
   no session enters, reads or prints a credential in it (rule 8, ADR 0103).
9. **Local models.** OpenViking and WeKnora are kept for the desktop, on local models, each adopted on its own;
   until then the unpinned community `dsh-openviking` package in the profile is drift.
10. **Worktrees.** A worktree the app makes of this repository must leave `core.hooksPath` at `.githooks`; it is
    read after the first one, and a change stops the app's worktrees here.

### Consequences

* Good: a pin, written terms, and two read-only checks that name drift: `scripts/dsh-desktop-check` (version,
  channel, inspector, both rows, first-party plugins out of step) and `scripts/dsh-profiles` (packages in no record).
* Bad: releases run unread, and between an update and the pin's move the check reads the version as differing.
* Bad: on 2026-11-13 no record lists the profile's 16 packages at the versions it carries (some are recorded for
  other profiles), among them three bundles still declared at rc.1 (the app resolves its own copies first) and the
  unpinned `dsh-openviking`; rule 2 is unmet for each until it is adopted or removed.
* Bad: plugins run in the app's process, as the owner, held only by the source read. The sandbox mode the app's
  sessions run under was not read; the profile patch's comments describe approvals never asked (`UNVERIFIED`).
* Bad: the GitHub terms hold by instruction only, and a mandatory-update policy from the vendor's service is
  outside this record.

### Confirmation

* `scripts/dsh-desktop-check` reads ok for the version, the channel, the inspector bundle and both rows; on
  2026-11-13 it named three first-party bundles declared at 0.2.2-rc.1 against the app's 0.2.2-rc.2.
* `scripts/dsh-profiles` lists the desktop profile as `UNRECORDED` until its packages are recorded.
* A listen check only, `lsof -nP -iTCP:9230 -sTCP:LISTEN`, prints nothing; nothing is sent to that port or to the
  app Host's own loopback port.
* With the app quit, this line puts the inspector bundle back after the terminal bundle, and reopening the app
  serves 127.0.0.1:9230 again; the app's plugin settings can switch it on instead:

  ```
  /usr/bin/python3 -I -c 'import json,os;p=os.path.expanduser("~/.dsh/profiles/desktop/package.json");m=json.load(open(p));b=m["dsh"]["profile"]["bundles"];b.insert(b.index("@deepseek-ai/dsh-experimental-terminal-bundle")+1,"@deepseek-ai/dsh-experimental-inspector-profile");open(p,"w").write(json.dumps(m,indent=2,ensure_ascii=False)+"\n")'
  ```

## Evidence

Read 2026-11-13. DeepSeek Harness at the tag `dsh-v0.2.2-rc.2`, the version the app runs, through GitHub's
contents API unless marked otherwise:

* https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.2 — a pre-release, the tag at
  `ddac96760b6e` — fetched; `resolved` by `scripts/canon resolve dsh-desktop`. `dsh-v0.2.2-rc.3`, a pre-release,
  was published 2026-11-13 07:56 UTC at `38a6a5c965aa` — fetched.
* https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/apps/desktop/README.md — L281 "Automatic
  checks never open dialogs or download packages."; L285 "Selecting an available release starts downloading
  immediately", and the install runs only "After approval" of a restart confirmation; L311 "Stable can also deliver
  alpha or RC versions", the choice saved as `updates.channel` in `desktop/settings.json`, an omitted one taking
  the build-injected default; L64 "Manage dsh Command…"; L321-L356 a mandatory update policy, set at packaging —
  fetched. `apps/desktop/src/updates/electron-updater/channel.ts` L12-13: `allowDowngrade = false`,
  `allowPrerelease = true` — fetched. L281 also says packaged apps check "fixed Nightly", at odds with L311; the code
  takes `updates.channel`, and only an absent one takes the packaged channel (`apps/desktop/src/update-settings.ts`
  L6, `apps/desktop/src/update-channel.ts` L14-17) — fetched.
* https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/packages/boot/plugin-manager/README.md#L182
  — "Desktop package operations remain owned by the Desktop shell." — fetched.
* https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/packages/experimental/inspector/README.md#L147
  — "The CDP target grants arbitrary code execution in both Host and connected Client realms through
  `Runtime.evaluate`"; "The CDP socket itself has no token"; L72-73 bind `127.0.0.1`, port `9230`. The profile's
  own README, `packages/experimental/inspector-profile/README.md` L31: "Enabling this bundle starts the local
  debugging backend and unredacted Host fetch capture" — fetched.
* https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.2/packages/session/session-log-deepseek/README.md#L31-L36
  — `enabled` defaults to `true`; "Re-enabling resumes the unaccepted suffix, including events recorded while
  disabled." `packages/client/product-analytics/README.md` L12 and L24: on "by default, without a user-facing
  control"; `packages/bundle/web-app/cordis.patch.yml` L49-53 mounts it, `enabled: true`, in the `desktop` profile
  only. `packages/session/session-telemetry-otel/README.md` L14 and L36: `FEEDBACK_ONLY`, the default, releases a
  session's records, context included, through explicit feedback; `DISABLED` constructs no transport — fetched.
* https://registry.npmjs.org/@deepseek-ai/dsh-subagent-codex — 0.2.2-rc.1 names `dsh-llm`, `dsh-session`,
  `dsh-timeout`, `dsh-subagent` and `dsh-subprocess` at exactly 0.2.2-rc.1, and 0.2.2-rc.2 at exactly 0.2.2-rc.2 —
  fetched (each version's document).
* The installed app, read on this machine with `plutil`, `codesign -dv` and `spctl -a -vv`: 0.2.2-rc.2,
  `com.deepseek.dsh`, a notarized Developer ID, "Hangzhou DeepSeek Artificial Intelligence Co., Ltd (NAN929V4UM)";
  `Contents/Resources/app-update.yml` gives a generic feed on the `nightly` channel.
* The changes of 2026-11-13 (New York time), made with the app quit: the inspector bundle out at 09:59:55, both rows
  and the channel in the same window; the subagents and hook packages moved to rc.2 with the app's own
  `dsh plugin --profile desktop add` at 10:06. Listen checks at 10:00:30, 10:01:09, 10:06:57, 10:07:02 and 12:49:00
  printed nothing. From 10:02:52 to 10:03:11 the bundle was back and 9230 listened, after an edit meant for a copy
  ran on the real file; it was removed again at once. Nothing was sent to the port.
* Not run, so `UNVERIFIED`: a session in the app, the plugins' working beyond loading, any GitHub automation, the
  app's worktrees, and whether a mandatory-update policy can install without the owner's approval.
* Second reader, 2026-11-13: the evidence-verifier (Claude Opus 5.5) re-checked 27 claims against the sources
  above and the installed app; 20 held, 6 in part and 1 not. The text now says no record lists the profile's
  packages at the versions it carries, where it said none had one; names the home-level row that outranks the
  analytics row and the feedback upload; cites ADR 0065 rule 3 as Antigravity's; marks the rc.3 stranding as
  inferred; and gives the mandatory policy's lines as L321-L356.
