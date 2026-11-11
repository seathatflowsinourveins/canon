Both execution-client defects are fixed in the working tree. All 497 offline tests pass, including all five cases from the four supplied regression tests. No existing test file, dependency, pin or lockfile was changed.

**What was wrong and what changed.** Cancel-all excluded `PENDING_CANCEL` even though NautilusTrader classifies that status as both open and in flight. A cancel with an unknown outcome could therefore leave an order working at Alpaca indefinitely despite later cancel-all commands. `_cancel_all_orders` now includes those orders, still deduplicates the open/in-flight selection, re-reads each order before cancelling, skips closed orders, and uses the existing per-order cancellation and refusal handling.

Order tracing constructed `ClientOrderId` before `order_status_report` could apply its existing conversion-error policy. Empty or non-ASCII IDs therefore aborted mass status without the intended closed/open distinction or a useful order-specific reason. `_root_id` now returns `None` for an unrepresentable own ID, leaving conversion to the report builder: a closed order is skipped with a warning; an open order raises with its Alpaca order ID, symbol and underlying validation reason. The warning now explicitly includes client order IDs among the fields that cannot be represented. Cache lookup for stream updates also handles an unrepresentable ID as an order the node cannot hold.

When an ancestor in a replacement chain has an invalid client ID, tracing now warns and stops before that ancestor, retaining and caching the last valid ID. A valid direct replacement is consequently reported under its own ID. Valid replacement chains retain their existing mapping. IDs are validated by NautilusTrader's constructor, rather than duplicated validation rules or manufactured identifiers.

The module header, method comments, adapter README, and ADRs 0036 and 0042 were corrected to describe these behaviors and their evidence.

**Upstream contracts checked this session.** These were checked locally against the packages installed from this checkout's `uv.lock`, with no source-repository or documentation-network access:

- NautilusTrader `2.0.0rc8`, canon commit `d53849b216e20f2c122ca01b4dd0da377638b186`: the installed `OrderStatus` docstring explicitly includes `PENDING_CANCEL` in both open and in-flight states. `live/clients.py` provides the execution and reconciliation hooks, and `live/__init__.pyi` documents the cache's instrument and side filters.
- The installed NautilusTrader `ClientOrderId` constructor was exercised offline: an empty string raises `ValueError` stating it was empty; a non-ASCII ID raises `ValueError` naming the non-ASCII string; a valid ASCII ID is accepted. Its interface is also present in `model/__init__.pyi`.
- alpaca-py `0.45.0`, canon commit `c42add7c0a7026e2ead3ed64e76f4def2b99fda4`: installed `trading/models.py` defines `Order.client_order_id` as an unconstrained string and `replaces`/`replaced_by` as UUID links. Installed `trading/client.py` implements `get_orders`, `get_order_by_id`, and `cancel_order_by_id`, the last deleting `/orders/{order_id}`. The change continues to use those APIs and the report converter's existing policy.

**Validation.** All commands below exited zero unless explicitly noted.

| Check | Result |
|---|---|
| Four supplied regression selectors, before changes | 5 failed, reproducing both defects; the closed-order test has two parameters |
| Same four selectors after changes, through `mise exec -- uv run --locked pytest -q --show-capture=no --basetemp=.pytest_cache/targeted-tmp` | 5 passed in 3.58 seconds |
| Full offline suite, command below | 497 passed in 99.07 seconds |
| `mise exec -- uv run --locked ruff check .` | All checks passed |
| `mise exec -- uv run --locked ruff format --check .` | 111 files already formatted |
| `mise exec -- uv run --locked ty check` | All checks passed |
| `scripts/canon lint` | `canon: lint clean` |
| `git diff --check` | Passed |
| `git diff --exit-code -- packages/canon-alpaca/tests tests` | Passed; existing tests unchanged |

The successful full-suite command was:

```sh
GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache" mise exec -- uv run --locked pytest -q --show-capture=no --ignore-glob='**/test_paper_*.py' --basetemp=.pytest_cache/full-tmp
```

The first full-suite run used the same command without `GIT_CEILING_DIRECTORIES` and produced 496 passes and one failure: `test_outside_a_git_checkout_there_is_no_default_ledger`. Keeping temporary files inside this checkout, as required by the write restriction, made its simulated outside-Git directory discover this repository. Git's installed `git(1)` documentation confirms that `GIT_CEILING_DIRECTORIES` bounds parent-directory discovery. Setting it at the test-temp boundary restored that test's intended environment while keeping all temporary files inside the checkout; no code or test change was needed for this failure. Paper test files were excluded from collection on both full-suite runs.

**Limits and remaining work.** Paper/API behavior and current online documentation remain `UNVERIFIED` this session, as required by the offline task constraints. No credentials were read or used, and no paper tests, agents, landing, commits, pushes or pull requests were started. `scripts/ci` was not run because the task identifies omissions in this copy; the full offline suite and all three graded code checks passed. No implementation work remains within the requested scope.
