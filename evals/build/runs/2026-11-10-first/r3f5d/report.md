The execution client had two defects. Cancel-all explicitly skipped `PENDING_CANCEL`, even though an
unanswered cancel can leave an order in that local state while it remains working at Alpaca. Replacement
tracing also constructed NautilusTrader client order IDs before the report converter could apply its
existing rule for unrepresentable orders, so an empty or non-ASCII ID could abort start-up reconciliation
even for a closed order.

In `packages/canon-alpaca/src/canon_alpaca/execution.py`, cancel-all now includes pending cancels in its
existing selection of open and in-flight orders. It still deduplicates that selection, respects the
command's instrument and side, re-reads each order before canceling, skips closed orders, and uses the
existing per-order cancellation and error handling. This changes which orders get the requested cancel;
it adds no retry scheduler or broader account-wide cancellation.

Replacement tracing now catches `ValueError` only around construction of `ClientOrderId`. If the order's
own ID is invalid, tracing returns no root and the normal report conversion handles it: a closed order
is skipped with a warning, while an open order raises an error naming its Alpaca order ID, symbol, and
reason. If a predecessor's ID is invalid, the trace stops with a warning at the last representable order,
so a valid order directly replacing that predecessor is reported under its own ID. Representable roots
remain cached, and network, model-validation, and other tracing failures retain their existing handling.
The warning for a skipped report now includes client IDs among its possible unsupported fields. The
module's contract comments, package README, and existing ADRs 0036 and 0042 were corrected accordingly.
No existing test file, dependency declaration, or pin was changed.

The change follows the already adopted upstream interfaces and report policy:

- NautilusTrader **2.0.0rc8**, locked to `d53849b216e20f2c122ca01b4dd0da377638b186`:
  `nautilus_trader.live.clients.ExecutionClient` supplies `_cancel_all_orders` and the reconciliation
  hooks. I read the installed pinned `live/clients.py` and the `ClientCache` signatures in
  `live/__init__.pyi`; `orders_open` and `orders_inflight` support the instrument and side filters already
  used here. I read the installed `model/__init__.pyi` for `ClientOrderId` and directly checked the native
  constructor: both `""` and `"订单-1"` raise `ValueError`, while `"valid-1"` is accepted. The adapter uses
  that upstream validation.
- Alpaca-py **0.45.0**, locked to `c42add7c0a7026e2ead3ed64e76f4def2b99fda4`: I read the installed
  `alpaca/trading/models.py` and `alpaca/trading/client.py`. `Order.client_order_id` is a string,
  `replaces` is an optional order UUID, `get_order_by_id` reads the predecessor, and
  `cancel_order_by_id` cancels an individual order. These calls and their existing error handling are
  retained. ADR 0036 records the per-order selection policy; the existing `order_status_report` implements
  the open-order failure and closed-order skip policy.

All upstream evidence above was checked locally against the locked installed packages and this checkout;
no upstream website or another repository copy was read. `uv run --locked` installed the packages
recorded in the existing lock into a checkout-local virtual environment, with a checkout-local uv cache.

The targeted pytest run selected these existing tests in
`packages/canon-alpaca/tests/test_execution_client.py`:

- `test_cancel_all_cancels_again_an_order_whose_cancel_had_no_answer`
- `test_closed_order_with_a_client_id_nautilus_cannot_hold_is_skipped`
- `test_open_order_with_a_client_id_nautilus_cannot_hold_fails_startup_by_name`
- `test_replacement_of_an_order_with_an_unholdable_id_is_reported_under_its_own`

I ran `mise exec -- uv run --locked pytest -q --tb=short` with those four full node IDs, before and after
the code change. Before: **5 failed in 1.45 seconds** (the closed-order test has two parameter cases).
After: **5 passed in 1.47 seconds**.

The final full offline test command was:

```sh
env -u ALPACA_API_KEY -u ALPACA_SECRET_KEY \
  UV_CACHE_DIR="$PWD/.uv-cache" \
  TMPDIR="$PWD/.pytest_cache/worker/tmp" \
  GIT_CEILING_DIRECTORIES="$PWD/.pytest_cache/worker/tmp" \
  PYTHONDONTWRITEBYTECODE=1 \
  mise exec -- uv run --locked pytest --ignore-glob='*test_paper*' -q --tb=short
```

Result: **497 passed in 92.68 seconds**. Paper test files were excluded during collection. The log is
`tree/.pytest_cache/worker/offline-tests-isolated.log`, relative to this report.

An earlier full offline run, with the same production change, returned **496 passed, 1 failed in
111.64 seconds**: `tests/test_ledger.py::test_outside_a_git_checkout_there_is_no_default_ledger`.
I had put temporary files inside the checkout to honor the write restriction, so that test's supposedly
external directory inherited this checkout's Git repository. After reading the installed `man git`
documentation, I added its documented `GIT_CEILING_DIRECTORIES` setting for the temporary subtree. This
keeps generated files inside the checkout while preventing Git discovery from walking into the real
repository. The failing test and the positive ledger-location test both passed (**2 passed in
1.47 seconds**), followed by the complete passing run above. No code or test was changed to resolve that
environmental failure. The earlier log remains at `tree/.pytest_cache/worker/offline-tests.log`.

Additional checks, using the same checkout-local uv cache and temporary directory:

| Command | Result |
| --- | --- |
| `mise exec -- uv run --locked ruff check .` | Passed: all checks passed |
| `mise exec -- uv run --locked ruff format --check .` | Passed: 111 files already formatted |
| `mise exec -- uv run --locked ty check` | Passed: all checks passed |
| `scripts/canon lint` | Passed: canon lint clean |
| `git diff --check` | Passed: no whitespace errors |

The four-file change remains uncommitted in the working tree. All requested offline verification is
complete. Behavior against Alpaca itself remains **UNVERIFIED**: paper tests and external API calls were
excluded as instructed, and no credential was read or used.
