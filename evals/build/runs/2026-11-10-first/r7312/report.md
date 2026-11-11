# Alpaca replace lifecycle repair

The change is left in the working tree. No existing test file, dependency declaration, registry or lock
file was changed, and nothing was committed, pushed or landed.

## What was wrong

The execution client treated a node order and its successive Alpaca orders as though they shared one
lifecycle, without recording which replacement had actually taken over or which modify invocation had
settled. This allowed duplicate HTTP/stream refusals and duplicate `replaced` messages to emit duplicate
events, stale predecessor closes to close a working replacement, and late PATCH answers to remain cached.

It also applied replacement fills before updating the node's quantity and prices, discarded all closed
replacement reports when the node still named the predecessor, did not map `pending_replace`, and could
not follow a replacement which had itself been replaced. Retry detection compared only fields present
in a PATCH, so an earlier replacement could be mistaken for the result of a different modify. The
finished-order guard discarded replacement rejections before the modify could be settled or logged.

## What changed and why

In `packages/canon-alpaca/src/canon_alpaca/execution.py`:

- Each PATCH invocation has its own identity and a snapshot of its full expected terms, including the
  unchanged quantity. Its HTTP answer and stream outcome share settlement state, so a definitive refusal
  emits at most one modify rejection and a settled invocation cannot retain a late PATCH answer.
- Confirmed current venue IDs and superseded predecessors are tracked independently of the native
  cache, because generated native events are queued. Replacement terms are applied once, on `replaced`
  or before an execution on the replacement. A PATCH answer or the replacement's `new` alone still does
  not confirm a replacement.
- Replacement lineage is followed through the SDK's `replaces` and `replaced_by` fields, bounded by the
  existing 16-link limit, with cycle detection on the new walks. Later confirmation of an ancestor
  cannot roll the order back. Late predecessor cancels and expiries are ignored, including when only
  the native cache remembers the current venue ID after adapter state is cleared.
- A 422 is treated as an SDK retry only when the replacement matches all expected terms and is not
  already known to belong to another invocation. This also distinguishes a second command with
  identical terms from a retry when the first command's answer is already known.
- `pending_replace` reports the current order as accepted, or partly filled when `filled_qty` is
  positive. Filled, canceled and expired replacements are reported even when `replaced` was missed;
  the existing exclusion remains for a rejected replacement that never took over.
- Terminal event types are retained so a rejected replacement can settle its modify after the original
  fill was emitted, even before the native cache applies that fill. The fill remains the order outcome
  and the replacement rejection is logged.

The package README, module contract/comments and ADR 0036 were corrected to describe these behaviors.
A new `packages/canon-alpaca/tests/test_replace_lifecycle.py` adds nine offline cases for late predecessor
closes, a partial fill before confirmation, reversed replacement chains, an identical second modify,
and canceled/expired replacement reports. It reuses the existing real-node harness and API stand-ins;
no supplied test file was edited.

## Upstream contracts checked

I read the repository's adapter README, ADR 0036 and registry/lock references, then inspected the packages
installed by `uv run --locked` from this checkout's lock:

- **alpaca-py 0.45.0:** `alpaca/trading/client.py` (`replace_order_by_id`, `get_order_by_id`),
  `trading/requests.py` (`ReplaceOrderRequest`), `trading/models.py` (`Order.replaces`,
  `Order.replaced_by`, `TradeUpdate`), `trading/enums.py` (`PENDING_REPLACE`, `REPLACED`), and
  `common/rest.py` (the SDK request/retry path).
- **NautilusTrader 2.0.0rc8:** `nautilus_trader/live/clients.py` (queued
  `generate_order_updated`, `generate_order_modify_rejected`, and other order events) and the installed
  `model`/`live` stubs for `OrderStatusReport` and `ModifyOrder`.

The change implements the same contracts the module already names:
[Alpaca Replace Order by ID](https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md),
[Alpaca trade updates](https://docs.alpaca.markets/us/docs/websocket-streaming.md), and
[NautilusTrader Python execution clients](https://nautilustrader.io/docs/md/latest/developer_guide/python_adapters.md).
The inference is that an execution on a replacement proves it took over; applying its documented order
terms before its fill preserves the native event sequence. It does not treat HTTP success as that proof.
Those documentation URLs were already recorded in this checkout; they were not fetched in this session.

## Verification

All commands ran from the supplied checkout, using its locked environment.

| Command/check | Result |
| --- | --- |
| Baseline selection of the 11 supplied regression functions (13 parameterized cases) | 13 failed before the repair; some later failures were secondary native-node lifetime errors after a timeout |
| `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py -q --tb=short --show-capture=no` | 114 passed |
| `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_replace_lifecycle.py -q --tb=short --show-capture=no` | 9 passed |
| Earlier full offline run, before adding the new test file | 370 passed |
| Final `mise exec -- uv run --locked pytest --ignore-glob='**/test_paper_*.py' -q --tb=short --show-capture=no` | **379 passed in 79.42 seconds**, including all supplied regressions and all nine new cases |
| `mise exec -- uv run --locked ruff check .` | Passed |
| `mise exec -- uv run --locked ruff format --check .` | Passed: 99 files already formatted |
| `mise exec -- uv run --locked ty check` | Passed |
| `scripts/canon lint` | Passed: canon lint clean |
| `git diff --check` | Passed |

## Uncertainty and remaining work

**UNVERIFIED against Alpaca:** the actual delivery/timing of these races on the paper venue. The tests
use the repository's existing stand-ins and a real NautilusTrader node; no paper test was collected or
run and no credential was read or used. No upstream documentation or venue was contacted; `uv` installed
only the packages pinned by this checkout's lock.

`scripts/ci` was not run because the task explicitly identifies missing files in this repository copy.
No landing or independent worker was started. There are no remaining offline failures or pending changes
needed for this task; paper-venue verification remains outside the authorized scope.
