---
status: proposed
date: 2026-11-11
decision-makers: master orchestrator (Claude); repo owner ratifies
review-by: 2026-12-11
---

# Enhancing a runtime: each skill, plugin or profile adopted on its own, run as a named configuration, kept or dropped by the two evals; open: containment first, and what "stock" is for DeepSeek Harness

## Context and Problem Statement

Plugin lists for the reader runtimes are long, and each runtime can load skills, plugins and profiles.
An addition could make a reader or a builder better, or leak a transcript, keep state across reviews, or
run with the host's privileges. How is an addition trialled, then kept or dropped, without loosening the
review gate, the benchmark's independence or paper-only?

## Decision Drivers

* Nothing is installed, imported, enabled or copied before it is adopted (`AGENTS.md` rule 2, ADR 0004).
* A verdict belongs to a worker label, and a worker is a runtime, model, effort and configuration
  (`evals/build/README.md`, "A worker").
* A reviewer must not see what the benchmark keeps from it (ADR 0046 rule 2).

## Considered Options

1. Install from a community list into the runtime's one profile (how DeepSeek Harness's profile got its
   plugins, ADR 0045).
2. Adopt each addition on its own, run it as a named configuration beside the stock one, and let the review
   and build evals decide.
3. No additions to any runtime.

## Decision Outcome

Proposed: **option 2**. Option 1 trusts a list that says it is no review. Option 3 gives up gains that the
two evals exist to measure. It stays `proposed` until the containment record (ADR 0034) is accepted.

1. **One adoption per addition.** Each skill, plugin or profile goes through `adopt-upstream`, pinned to a
   commit. A lane reads its source for network use and persistence before it is installed anywhere.
2. **A named configuration.** It runs as `<runtime>/<model>/<effort>/<name>`, beside the stock
   configuration on the same cases. The review benchmark and the build eval keep or drop it (ADR 0046,
   ADR 0057). No result carries over from one configuration to another.
3. **A list is not a source of trust.** The DeepSeek Harness plugin list has 10,426 entries and
   says "Being on this list is not a security review". DeepSeek Harness documents no signing or pinning of
   plugins. A third-party audit, with no maintainer reply, reports that removing a plugin can leave code behind.
4. **"Stock" for DeepSeek Harness is not plugin-free.** Its profile already carries seven community plugins
   and two hook packages (registry entry `deepseek-harness`, ADR 0045), with session-log upload and
   dsh-memento off. Among them, dsh-advisor sends transcript deltas to a configured second model (whether
   one is configured here is UNVERIFIED). So an eval labelled `stock` for DeepSeek Harness runs those
   plugins, and a plugin-free run is a separate, named configuration.
5. **Containment first.** A plugin runs in-process with the host's privileges. An enhancement trial waits
   for the containment gap's first step (ADR 0071, gap 2; ADR 0034), or runs in a throwaway runtime home with nothing
   of the owner's in it. The throwaway home fits DeepSeek Harness's own model: a profile is
   `$DSH_HOME/profiles/<name>`, and `dsh plugin --profile <name> add` installs into one. But dsh-TUI
   documents no per-project profile, and Orca launches `dsh-tui .` with no per-task overlay, so choosing a
   profile per task from the cockpit is UNVERIFIED.
6. **The reviewer arm stays clean.** An addition that keeps state across sessions, or sends the transcript
   to a second model, runs only in the build arm.
7. **First candidates, if a trial runs.** For readers, a read-only code-intelligence plugin:
   `dsh-code-intel`, read-only by design but with little history; the more used `dsh-lsp-actions` writes,
   through format and rename. For builders, Aegis, which is already in the profile, and whose only
   evidence of benefit is its author's.
8. **Hermes is not adopted.** Its adoption record would have to cover the skills the agent can write for
   itself (unless `skills.write_approval` is set) and Python plugins with `pre_tool_call` hooks. A
   third-party audit of v0.8.0 (April 2026) reported 4 critical and 9 high findings; the latest release is
   v0.23.1, which no audit read here covers. That is input for whoever adopts
   it.

### Consequences

* Good: an addition is measured before it is relied on, and a reviewer's independence is kept.
* Bad: until containment exists, a trial is limited to a throwaway home, and the cockpit's support for one
  per task is not known.
* Bad: DeepSeek Harness's past results are for a configuration that already carries plugins.

### Confirmation

An eval sheet names every configuration it ran and lists its additions. A runtime's registry entry lists
what its profile carries, so a configuration's additions can be read against it.

## Evidence

Read 2026-11-10 and 2026-11-11. The web sources below were fetched by a research report of 2026-11-10 and
are not re-fetched here: UNVERIFIED by this record.

* https://github.com/awesome-dsh-plugin/awesome-dsh-plugin — README: "Being on this list is not a security
  review"; "Tool approvals don't sandbox plugin code"; the entry count is inferred from a truncated
  listing (the report). A second reader fetched it on 2026-11-11 and counted 10,426 entries.
* https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/dsh-v0.2.1-rc.3/docs/cli-help.md —
  `dsh plugin --profile <name> add`; no signing or pinning documented (the report).
* https://github.com/deepseek-ai/deepseek-harness/discussions/454 — a third-party audit: `dsh plugin
  remove` leaves code behind; three comments, none from a maintainer (the report, and a second reader's
  fetch of 2026-11-11).
* https://github.com/lengrufeng2008/dsh-code-intel and https://github.com/PerryLink/dsh-lsp-actions —
  read-only tools against tools that format and rename (the report).
* https://github.com/GanyuanRan/Aegis — the author's own figures, "bounded advisory evidence" (the report).
* https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins and
  https://hermes-agent.nousresearch.com/docs/user-guide/features/skills — Python plugins with
  `pre_tool_call`; `skills.write_approval` (the report).
* https://labs.cloudsecurityalliance.org/research/csa-research-note-hermes-agent-cves-20260504-csa-styled/
  — an audit of Hermes v0.8.0: 4 critical, 9 high (the report).
* https://api.github.com/repos/NousResearch/hermes-agent/releases/latest — v0.23.1, published 2026-11-10
  (fetched by a second reader on 2026-11-11).
* `canon/upstreams.json`, entry `deepseek-harness` — the seven plugins and two hook packages (read).
* ADR 0045, rule 6 — session-log upload and dsh-memento off; no per-review overlay (read).
* `evals/build/README.md`, "A worker" — `stock` is "the runtime as its adoption record installs it" (read).
