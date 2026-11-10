---
paths:
  - "packages/canon-alpaca/**"
---

# Working on the Alpaca adapter

* Read `packages/canon-alpaca/README.md` first. It holds the module layout, the contract and the known
  limits, each taken from upstream.
* Before writing against the adapter interface, read the current page:
  `https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md`. The interface changed
  between v1 and v2 (`LiveDataClient` became `DataClient`, `_request` became `_request_data`).
* Use upstream's five modules and nothing else until a sixth is needed: `constants.py`, `providers.py`,
  `data.py`, `execution.py`, `factories.py`.
* Each module's header names the upstream contract it implements (ADR 0001, rule 3).
* Do not copy code from upstream's template or from the community adapters. They are LGPL-3.0 or
  unreviewed; read them for shape only.
* No strategies and no node configuration in this package.
* Every client needs tests for factory registration, a fill and shutdown before anything depends on it.
