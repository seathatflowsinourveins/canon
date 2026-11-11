# Alpaca replacement delivery fix

The change is complete and left uncommitted in the working tree. All 377 offline tests pass, including
the supplied regressions and seven added cases; all three graded code checks pass. Existing test files
and dependency pins are unchanged.

## What was wrong

The execution client identified a replacement's owning order, but did not track individual modify
commands and their outcomes. HTTP and stream refusals could each reject the same modify, duplicate
confirmations could update the order twice, and a late HTTP answer could be retained after its
confirmation had already been consumed. The final-order guard also discarded a replacement rejection
after the original order filled, before the branch intended to settle that modify could run.

A replacement fill was routed to the original order without first applying the new quantity and
prices. A larger fill could therefore be rejected as an overfill. Conversely, a late cancel or expiry
of a superseded venue ID was applied to the still-working logical order.

Reconciliation suppressed every closed replacement whose ID the node had not yet adopted, including
filled replacements whose confirmation was missed. It also lacked `pending_replace` status mapping.
Confirmation handling assumed the immediate replacement was reportable instead of following it when
it had already been replaced again. Retry detection compared only explicitly supplied fields, omitting
the unchanged quantity and confusing a separate modify with an SDK retry.

## What changed and why

In `packages/canon-alpaca/src/canon_alpaca/execution.py`:

- Track each modify invocation separately, with its original venue ID, complete expected terms, and
  replacement ID when known. HTTP and stream outcomes settle that invocation once. A known replacement
  belonging to another invocation is not evidence that the refused command succeeded on a retry.
- Keep PATCH answers only while their invocation is pending. Confirmation, execution, or rejection
  removes the corresponding answer; an answer arriving after settlement is discarded.
- Remember confirmed replacement IDs and superseded IDs independently of the node cache, since emitted
  events are queued. This deduplicates confirmations and ignores stale closes of the old venue order.
- Apply a replacement's quantity and prices before emitting its first fill. A PATCH answer or `new`
  event alone still does not establish that the replacement took over.
- Follow `replaced_by` links to the current order when a confirmation's replacement has moved on,
  retaining the existing bounded-chain policy and logging failures rather than inventing terms.
- Map `pending_replace` to accepted or partially filled according to its filled quantity. Report filled,
  canceled, and expired replacements even when the node still names the original; continue suppressing
  rejected replacements that never took over.
- Settle a late modify rejection even after the original fills. Log that the fill is the outcome instead
  of attempting to reject the filled order.

Updated the module contract, package README, and ADR 0036 to describe these behaviors and their offline
verification. Added `packages/canon-alpaca/tests/test_replace_lifecycle.py`, covering stale closes,
out-of-order duplicate confirmations across two modifies, partial fills preceding confirmation,
separate commands with identical terms, and following changed terms to the latest replacement. No
dependency was added.

## Upstream contracts checked

Read the installed packages pinned by this checkout, without fetching upstream pages:

- **alpaca-py 0.45.0:** `trading/client.py::TradingClient.replace_order_by_id`,
  `trading/requests.py::ReplaceOrderRequest`, `trading/models.py::Order`,
  `trading/enums.py::OrderStatus`, and `common/rest.py::_request`. These define PATCH requests,
  replacement links, quantities, pending-replace status, and SDK retries.
- **NautilusTrader 2.0.0rc8:** `live/clients.py::ExecutionClient` event-generation methods,
  `live/__init__.pyi::ModifyOrder`, and the order and `OrderStatusReport` contracts in
  `model/__init__.pyi`. The adapter emits the existing `OrderUpdated`, `OrderFilled`, and
  `OrderModifyRejected` events using those interfaces.
- Read ADR 0036, whose review date has not expired. Its cited Alpaca Replace Order by ID and stream
  contracts remain named in the module header. Historical paper observations were not rerun.

## Validation

| Command | Result |
| --- | --- |
| `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_execution_client.py` before changes | 13 failed, 101 passed, reproducing the defect and its parametrized cases |
| `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_execution_client.py -k 'modify or replace' --tb=short --show-capture=no` | 30 passed, 84 deselected |
| `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_replace_lifecycle.py --tb=short --show-capture=no` | 7 passed |
| `mise exec -- uv run --locked pytest -q --ignore-glob='**/test_paper_*.py' --tb=short --show-capture=no` | 377 passed in 73.64 seconds |
| `mise exec -- uv run --locked ruff check .` | Passed |
| `mise exec -- uv run --locked ruff format --check .` | Passed; 99 files already formatted |
| `mise exec -- uv run --locked ty check` | Passed |
| `scripts/canon lint` | Passed |
| `git diff --check` | Passed |

Development-time formatting and type-check findings were corrected before the final passing checks.
The only subsequent Python edit reflowed docstring text.

## Uncertainty and remaining work

**UNVERIFIED:** these late, duplicate, and reordered delivery scenarios against Alpaca itself; the
verification here uses the real NautilusTrader node with offline Alpaca stand-ins. Paper tests and
upstream network verification were excluded by the task, and `scripts/ci` was not run because the copy
omits files it requires. No offline check is blocked or left failing, and no landing was attempted.
