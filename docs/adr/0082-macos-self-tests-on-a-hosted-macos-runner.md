---
status: accepted
date: 2026-11-11
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-11
---

# The self-test cases Ubuntu skips run on a hosted macOS runner, reporting first and required later

## Context and Problem Statement

The hosted `checks` job (ADR 0014) runs `scripts/ci` on `ubuntu-24.04`. That image has no `lockf(1)`,
`plutil(1)` or `security(1)` at `/usr/bin`, and no `expect(1)` that opens a pseudo-terminal there, so
`scripts/test-lanes` skips ten cases (the paper session runner, the paper lock's deadline, the sandbox
node's stream lock, the LaunchAgent install rehearsal, among them) and `scripts/test-maintenance` skips
as a whole. They ran only on this Mac's commit gate, and the skips were quiet lines in a long log. Where
do they run on GitHub, and does that run hold a landing?

## Decision Drivers

* The paper lock, its deadlines and the launchd definitions guard the paper runner (ADR 0050); a check
  that only one machine runs is not independent.
* No credential, paper run, keychain item or launchd load on a hosted machine (ADR 0009, ADR 0033).
* No cost: the repository is public.
* A new hosted check must not hold `scripts/land` past its nine-minute wait before it has a record.

## Considered Options

1. Leave them to the Mac's commit gate, with louder skips.
2. A second job on a hosted macOS runner that runs the two self-tests, reporting until it has a record,
   then required.
3. The whole of `scripts/ci` on macOS as well as Ubuntu.

## Decision Outcome

Chosen: **option 2**. Option 3 repeats eight minutes of checks that already run on Ubuntu, to gain the
same two scripts.

1. **A job `macOS self-tests` on `macos-26`.** That label is the arm64 standard runner. It is a version
   label, not `macos-latest`, so the OS version moves only when the workflow does, as the runner-images
   README advises; the image itself is updated weekly. The job installs the same mise toolchain with the
   same pinned actions as `checks`, then runs `scripts/test-lanes` and `scripts/test-maintenance` and
   nothing else. `launchctl` is a stand-in on PATH in both scripts, so nothing is loaded into launchd.
2. **Nothing it exists to run may skip.** A first step names any of the `/usr/bin` tools the cases need
   that is missing, and fails. `CANON_TEST_ALLOWED_SKIPS` names the one case allowed to skip,
   `paper:store on a terminal`, which writes to the login keychain and is skipped there by name
   (`CANON_TEST_NO_KEYCHAIN`). Any other skipped case of `scripts/test-lanes` fails, and the
   `test-maintenance` step fails unless the script's last line reports cases passed and none failed.
3. **Skips are loud where they remain.** `scripts/test-lanes` ends with one line giving the count and
   the names of its skipped cases. `scripts/ci`, on its line before the verdict, counts and names every
   case a passing self-test skipped, including a script that skips as a whole. On GitHub it also raises
   that list as a warning annotation.
4. **Reporting first, then required.** The job is not among the `needs` of `all required checks
   passed`, the one job the ruleset requires, so it does not hold a landing; a failure is fixed forward.
   Once it has passed on three landings, it joins those `needs`. That is a workflow change and needs no
   repository setting. Its duration and queue time decide whether it fits inside `scripts/land`'s wait:
   on 2026-11-11 the `checks` job took from 6 m 35 s to 8 m 10 s over its 31 successful runs.
5. **One landing never cancels the run of the one before.** The concurrency group is the pull request's
   head branch, else the run id, as GitHub's concurrency docs give it for cancelling on pull requests
   only: each push to `main` gets a group of its own, and a pull request's newer push still cancels its
   older run. Three runs on `main` were cancelled this way on 2026-11-11. Otherwise the macOS job, slower than the
   landings that follow it, could seldom finish on `main`.
6. **Cost: none.** Standard GitHub-hosted runners, macOS included, are free in public repositories.
   GitHub's free plans allow five concurrent macOS jobs.

### Consequences

* Good: the paper runner's lock and deadlines and the plists are checked on a clean macOS machine on
  every pull request and landing, and a skipped case is named, never silent.
* Bad: until the job is required, a macOS-only regression can land and is caught only by its run, after
  the merge. The first runs are slower than this Mac, which ran the two scripts in 167 s and 54 s.
* Bad: should the repository become private, each macOS minute would be billed at $0.062. That would be
  the owner's decision, and this record would be re-read first.

### Confirmation

The workflow runs the job on every pull request and push to `main`, and actionlint checks it.
`scripts/test-ci` ("skipsum") shows the `scripts/ci` summary line, its annotation, and the line a
script that skips as a whole leaves. The first hosted runs' duration, queue time and tool list are
added here once they have run.

UNVERIFIED until the first hosted run: that `macos-26` has at `/usr/bin` each tool the job's first step
lists (`lockf`, `timeout`, `plutil`, `expect`, `getconf`, `jq`, `nc`, `sqlite3`, `security`; macOS 27
on this Mac has them all, the image readme lists none by path, and no Apple page found says which
release first shipped `timeout`), that its `expect` opens a pseudo-terminal, and that the `mise`
mise-action puts on PATH is the one the cases "run under mise" find. Whatever is missing fails the job
by name.

## Evidence

Read 2026-11-11. "Fetched" means the raw page was fetched with `curl` (docs.github.com pages through its
`api/article/body` markdown endpoint) and read; "model-summarised" means read through a summarising fetch.

* https://docs.github.com/en/billing/concepts/product-billing/github-actions — "The following usage
  remains free independently and does not reduce the included compute usage: Standard GitHub-hosted
  runners in public repositories"; "Larger runners are billed for public repositories"; the rate row
  "macOS 3-core or 4-core (M1 or Intel) | `actions_macos` | $0.062" (fetched).
* https://docs.github.com/en/actions/reference/runners/github-hosted-runners — "Use of the standard
  GitHub-hosted runners is free and unlimited on public repositories". The macOS standard row reads
  "3 (M1)", "7 GB", "14 GB", "arm64", with labels `macos-latest`, `macos-15`, `macos-26` and `xcode-27`.
  "The Linux and macOS virtual machines both run using passwordless `sudo`". It says nothing of a login
  keychain (fetched).
* https://docs.github.com/en/actions/reference/limits — "Standard GitHub-hosted runner | Free | 20 | 5",
  whose fourth column is "Maximum concurrent macOS jobs". The Pro row also allows 5. The page gives no
  queue time for hosted runners (fetched).
* https://raw.githubusercontent.com/actions/runner-images/main/README.md — "macOS 26 Arm64 | arm64 |
  `macos-latest`, `macos-26` or `macos-26-xlarge`"; "To avoid unwanted migration, users can specify a
  specific OS version in the yaml file"; "All macOS 14 images retired from GitHub Actions on November 2,
  2026"; "We support (at maximum) 2 GA images and 1 beta image at a time". No retirement date is
  announced for `macos-26` or `ubuntu-24.04` (fetched).
* https://raw.githubusercontent.com/actions/runner-images/main/images/macos/macos-26-arm64-Readme.md —
  "OS Version: macOS 26.7.2 (25G332)", "Image Version: 20261102.0473.1", "Bash 3.2.57(1)-release",
  "jq 1.8.2". It lists none of `lockf`, `timeout`, `plutil` or `expect`, so the job's first step checks
  them (fetched).
* https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
  — the group `${{ github.head_ref || github.run_id }}` "cancels in-progress jobs or runs on
  `pull_request` events only"; "By default, any existing `pending` job or workflow in the same concurrency
  group will be canceled" (fetched, model-summarised). The workflow uses that group with a `ci-` prefix.
* https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax — an unspecified
  shell runs `bash -e {0}`, while `shell: bash` runs `bash --noprofile --norc -eo pipefail {0}`, so the job
  sets `shell: bash` for its `tee` (fetched, model-summarised).
* https://raw.githubusercontent.com/actions/toolkit/main/packages/core/src/command.ts — `escapeData`
  replaces `%`, `\r` and `\n` with `%25`, `%0D` and `%0A`, as `scripts/ci` does for its annotation
  (fetched).
* The Ubuntu job's log of 2026-11-11 (`gh run view --log`) shows ten `test-lanes` skip lines.
  `test-maintenance` showed only `ok`, since `scripts/ci` then kept only lines starting `  skip  `; its
  whole-script skip is read from its code (read).
* On this Mac, with the job's two variables: `scripts/test-lanes` passed 474 cases in 167 s. Its one
  other skip, `omniroute:setup with the real curl`, failed as it should, since a gateway listens here.
  `scripts/test-maintenance` passed 86 cases in 54 s (run).
* The `checks` job's durations, and the three `main` runs cancelled by the next landing (43033918539,
  43033663940, 43005794552), read from the repository's runs of 2026-11-11 by the second reader (`gh`).
* Second reader, 2026-11-11 (`evidence-verifier`, on this record): the quotes and the repository claims
  held. It corrected the duration range, added the untested `/usr/bin` tools and `mise` to UNVERIFIED,
  and found the concurrency group's first form rested on an uncited expression idiom; the workflow now
  uses the docs' own group.
* Second reader, 2026-11-11 (`evidence-verifier`, on this decision's first draft as an addition to
  ADR 0014). The quotes held. It corrected two claims: Ubuntu 24.04 does ship `/usr/bin/timeout`
  (coreutils), and a version label pins the OS version, not the image. It judged the change a new
  decision rather than an addition, so it is this record.
