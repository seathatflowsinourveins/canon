# canon-filings

SEC filings for the names the ranking lists, read through `edgartools` (ADR 0108). The recorder is not
built yet: today this folder holds the pin, its lock and the tests of what the record relies on.

## A project of its own

This folder is not a member of the workspace at the repository root. It has its own `uv.lock` and its own
environment, so that what `edgartools` requires (pyarrow among it) never enters the environment the gate,
the ledger and the studies run in: with pyarrow installed pandas changes how it stores text columns. The
form is uv's own for a package that wants a separate environment: an independent project, with commands
run from its folder.

```sh
cd filings
uv run --locked pytest     # creates .venv here from uv.lock, then runs tests/
uv run --locked ty check
```

`scripts/ci` runs the same for every tracked `uv.lock` below the root: `uv lock --check`, then ty and
pytest in that folder's environment. The root's own ty leaves this folder out (`ty.toml` at the root),
and the empty `ty.toml` here keeps ty from taking the root's. A move of the pin is
`uv add edgartools==<version>` from this folder, with the registry entry, ADR 0108's title and the row in
`docs/architecture.md`; a test here holds those and the installed version to one, and holds pytest and
ty to the root's pins.

## Terms the recorder keeps

* **GET only.** It names each call it makes and the endpoint behind it, and a test refuses any other method.
* **Identity.** The SEC asks a client to declare who it is. The owner enters that value on the machine
  that records, in a setting git does not track. A session never enters, reads or prints it. The
  recorder refuses to start with none, and otherwise calls `set_identity()` before any request, on every
  request path: without that, `edgartools` asks at a terminal prompt. No default, none derived.
* **Records outside the repository**, and `EDGAR_LOCAL_DATA_DIR` and `EDGAR_CACHE_DIR` set to the
  recorder's own folder.
* **One process at a time**: the limiter that keeps to the SEC's request rate counts one process.
* `edgar.ai` is never imported, and the console scripts `edgartools` and `edgartools-mcp` are never run.

The tests here make no request and use a made-up identity at a reserved example domain.
