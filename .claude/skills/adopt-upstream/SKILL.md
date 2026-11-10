---
name: adopt-upstream
description: Add or upgrade an upstream this repo builds on, whether a library, tool, skill, plugin, standard or practice. Use before installing, importing, enabling or copying anything that is not already in canon/upstreams.json, and whenever an existing pin needs to move.
---

# Adopt an upstream

The goal is a registry entry, a resolved pin and an ADR that a stranger could check. Nothing is installed or
imported until those exist.

## Adding something new

1. **Look first.** `scripts/canon list`. If it is already there with a suitable role, use the pinned version
   and stop.
2. **Research.** Delegate to the `upstream-scout` subagent with the field and the questions. Give it the
   question, not your preferred answer. Independent fields can go to separate scouts in parallel.
3. **Choose a role.** Official upstream for the job comes first, then the vendor's own tooling, then
   community work. If sources have not converged on something hard to reverse (a foundation, a format,
   the merge workflow), the role is `watch` and the ADR is `proposed`. A tool that is cheap to leave can
   be adopted on the owner's word, with what is untested marked `UNVERIFIED` (ADR 0018).
4. **Skills and plugins.** Read every file before enabling: `SKILL.md`, scripts, hook definitions, MCP
   config. They run with the agent's permissions (ADR 0004).
5. **Register.** Add the entry to `canon/upstreams.json` (fields are in `.claude/rules/canon.md`). Track a
   release tag where one exists.
6. **Resolve.** `scripts/canon resolve <id>` pins that one entry and leaves every other pin as it is. It
   asks for permission; that is intended. A Python dependency is then added with `uv add`, pinned exactly.
7. **Decide in writing.** Add to the relevant ADR or create one with the `new-adr` skill.
8. **Second reader.** For `foundation`, `platform` and `source` roles, run the `evidence-verifier` subagent on
   the ADR and fix what it finds.
9. **Lint.** `scripts/canon lint` must pass.

## Moving an existing pin

1. `scripts/canon check` to see what moved.
2. Read the upstream changelog or release notes between the old and new ref. Note breaking changes.
3. Change `track` in the registry, run `scripts/canon resolve <id>`, and update the ADR's Evidence and
   `review-by`. For a Python dependency, change the pin in `pyproject.toml` and run `uv lock` as well.
4. If the upstream is a `foundation`, run whatever tests depend on it before relying on the new pin.

## When there is no upstream

Write an ADR with status `proposed` that states the gap and what evidence would close it. Do not build a
replacement.
