# Alpaca execution-client repair

## What was wrong

Cancel-all excluded `PENDING_CANCEL` after selecting the cache's open and in-flight orders. An unanswered cancel leaves that state behind even when the order still works at Alpaca, so a later cancel-all never retried it.

Replacement tracing constructed NautilusTrader `ClientOrderId` objects before reaching the report conversion's open/closed error handling. An empty or non-ASCII ID therefore aborted the whole mass status, including when the offending order was closed or only an ancestor of a valid replacement.

## What changed and why

- Cancel-all now skips only missing or closed orders when it re-reads each selected order. Pending cancels use the existing per-ID cancel path, including its rejection and unknown-outcome handling.
- `_root_id` returns `None` when the reported order's own ID cannot be represented. The existing `order_status_report` conversion then skips a closed order, with the caller logging a warning, or raises an error naming an open order, which fails start-up.
- Tracing stops before an ancestor whose ID `ClientOrderId` rejects, preserving and caching the last representable ID. A valid replacement directly following such an ancestor is reported under its own ID. Ordinary replacement tracing, caching and chain limits continue to apply.
- The execution module's contract comments, package README, ADR 0036 and ADR 0042 now describe these behaviors. The warning for a skipped closed order includes unrepresentable IDs among the possible causes.

No existing test file, dependency declaration or lockfile was changed. The change is left in the working tree; nothing was committed or landed.

## Upstream contracts checked

The installed packages were obtained with `uv run --locked`, using this checkout's pins: NautilusTrader `2.0.0rc8` (canon commit `d53849b216e20f2c122ca01b4dd0da377638b186`) and alpaca-py `0.45.0`.

- NautilusTrader's installed `live/clients.py` defines `ExecutionClient._cancel_all_orders`, the status-report hooks and the default batch-cancel delegation. Its `live/__init__.pyi` defines `ClientCache.orders_open`, `orders_inflight` and their instrument/side filters. The existing adapter selects from these APIs and calls the single-order cancel path.
- alpaca-py's installed `trading/client.py` documents and implements `TradingClient.cancel_order_by_id`, `get_orders` and `get_order_by_id`. Cancellation continues to use the individual venue order ID.
- alpaca-py's installed `trading/models.py` defines `Order.client_order_id` as a string and `replaces`/`replaced_by` as optional UUIDs. NautilusTrader's installed `model/__init__.pyi` defines `ClientOrderId` and the typed ID accepted by `OrderStatusReport`. Direct probes of the pinned constructor confirmed `ValueError` for empty, whitespace-only and non-ASCII strings, and acceptance of an ordinary ASCII ID. The fix uses that constructor's validation rather than duplicating its rules.
- The surrounding module names NautilusTrader's Python adapter and execution/reconciliation contracts and Alpaca's cancel/replace endpoints. ADRs 0030, 0036 and 0042 were read; their review dates have not expired.

## Validation

All commands ran from `tree/`. `uv` cache and test temporary files were kept inside the checkout. After installation, subsequent `uv` commands used `--offline`.

- Before the change, the four requested test functions produced **5 failed cases** (the closed-ID test has two parameters).
- After the change, the same selection produced **5 passed** in 2.66 seconds.
- The first full offline run produced **496 passed, 1 failed** in 136.44 seconds. `tests/test_ledger.py::test_outside_a_git_checkout_there_is_no_default_ledger` failed because placing pytest's temporary files inside this checkout made its outside-Git fixture discover the enclosing repository. No ledger or test code was changed. Git's installed manual documents `GIT_CEILING_DIRECTORIES`; setting it to the temporary directory restores that fixture's intended Git isolation while keeping all temporary writes inside the checkout. The isolated ledger test then passed.
- Final full offline run with temporary-directory Git isolation: **497 passed** in 88.64 seconds, including all five reported regression cases.
- `mise exec -- uv run --locked --offline --cache-dir .pytest_cache/uv --no-env-file ruff check .`: **passed**.
- `mise exec -- uv run --locked --offline --cache-dir .pytest_cache/uv --no-env-file ruff format --check .`: **passed**, 111 files already formatted.
- `mise exec -- uv run --locked --offline --cache-dir .pytest_cache/uv --no-env-file ty check`: **passed**.
- `scripts/canon lint`: **passed**, lint clean.
- `git diff --check`: **passed**.

Full offline-suite command:

```sh
GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache/local-tmp" \
TMPDIR="$PWD/.pytest_cache/local-tmp" \
mise exec -- uv run --locked --offline --cache-dir .pytest_cache/uv --no-env-file \
  pytest --ignore-glob='**/test_paper_*.py'
```

## Limits and uncertainty

Paper tests were excluded from collection, no credential was read or used, and no trading endpoint was contacted. Behavior against Alpaca itself remains **UNVERIFIED in this session**; this repair is verified offline with the existing stand-in venue and real NautilusTrader nodes. External documentation and Rust adapter sources cited by the existing records were not fetched because this task prohibits that network access; the installed pinned Python sources and constructor behavior above were checked directly. `scripts/ci`, `scripts/land` and additional agents were not run, as instructed. No requested offline repair or verification work remains.
