---
paths:
  - "canon/**"
  - "scripts/canon"
---

# Editing the canon

* `canon/upstreams.json` is hand-edited; `canon/upstreams.lock.json` is written only by
  `scripts/canon resolve`.
* Every entry needs `id`, `field`, `repo`, `track`, `role`, `license`, `why`, `evidence` (at least one URL you
  fetched this session) and `adr` (a four-digit ADR that exists).
* `track` is `tag:<name>`, `branch:<name>` or `default`. Prefer a release tag for anything we install or run.
  Use `default` only for repos that publish no tags.
* Optional `line` (for example `"v1."`) holds an entry to one release line, so `scripts/canon outdated`
  ignores tags outside it.
* Optional `mise` (the tool's key in `mise.toml`) and `pypi` (the package's name in `uv.lock`) say where an
  entry's version is installed. `scripts/canon lint` compares them with the entry, and fails when
  `mise.toml` installs a tool that no entry names.
* Roles: `foundation`, `platform`, `standard`, `tool`, `source`, `reference`, `watch`. A `watch` entry is not
  adopted; do not install or import it.
* Write `UNVERIFIED` in `license` rather than guessing.
* Moving a pin: read the upstream changelog between the old and new ref first, and record anything breaking
  in the entry's ADR.
* `canon/models.json` changes only together with ADR 0005.
