# Alpaca replacement lifecycle correction

The requested change is complete and remains in the working tree. All 377 offline tests and the three
graded code checks pass. No existing test file, dependency, registry entry or lockfile was changed.

## What was wrong

The adapter traced replacement identities back to the node's client order ID but did not track modify
command settlement or confirmed replacement adoption separately. Consequently HTTP and stream refusals
could each reject the same command, repeated confirmations could each update it, a replacement's fill
could reach the engine before its new quantity, and a late terminal update for the old venue ID could
close the replacement. An early finished-order return also hid the replacement rejection after the old
order filled, while a late PATCH response could be retained after its confirmation had already run.

Reconciliation excluded every terminal replacement whose venue ID differed from the cached order,
losing genuinely filled replacements when `replaced` was missed. It also did not map `pending_replace`.
Replacement reads stopped at a superseded intermediate order, and retry recognition compared only
explicit PATCH fields, allowing a different command to be mistaken for the first command's retry.

## What changed and why

- Track each modify attempt by object identity and its target venue ID, effective quantity and eventual
  replacement ID. HTTP and stream refusals consume the same pending attempt once. Separate commands
  targeting the same old order remain distinct, even when they request identical terms.
- Check the effective unchanged quantity as well as prices when considering a refused request to be
  alpaca-py's retry, and exclude replacements accounted for by an earlier command.
- Track applied replacement IDs and superseded venue IDs independently of the engine's asynchronous
  cache updates. Queue one `OrderUpdated` before a replacement's execution, so a fill is checked against
  the replacement quantity; ignore stale cancel/expire updates for superseded orders.
- Follow `replaced_by` through bounded replacement chains, including a replacement already replaced
  again. Cyclic, incomplete and excessively long chains fail explicitly. A `pending_replace` order keeps
  its current terms and reports accepted or partly filled according to its filled quantity.
- Report terminal replacements under the node's order ID; keep excluding rejected replacements that
  never took over. This recovers filled replacements after missed confirmations.
- Settle replacement rejection before the finished-order guard. When the old order filled first, log
  that the fill is the outcome rather than emit a rejection the native engine cannot apply.
- Release consumed PATCH snapshots, discard answers arriving after settlement, and release snapshots
  when an order closes while retaining unresolved command identity for its eventual refusal.

The module header, adapter README and ADR 0036 now describe confirmation by execution as well as by
`replaced`. Seven additional offline regression cases in `test_replace_lifecycle.py` cover expiry and
cancel followed by a replacement fill, duplicate confirmations after a partial fill, distinct commands
with identical terms, successive refusals, final chain terms, and canceled replacement reconciliation.

## Upstream contract and evidence

Read this session from the installed, locked packages:

- alpaca-py 0.45.0: `TradingClient.replace_order_by_id`, `Order.replaces`, `Order.replaced_by`,
  `ReplaceOrderRequest` optional fields, `OrderStatus.PENDING_REPLACE`, and `RESTClient._request` retry
  handling. These supply the replacement identity links, request semantics and retry behavior.
- NautilusTrader 2.0.0rc8: `nautilus_trader.live.clients.ExecutionClient`, especially
  `generate_order_updated` and `generate_order_modify_rejected`, which queue events through its native
  output. The offline tests exercise event ordering and reconciliation through real `LiveNode` instances.
- ADR 0036, still within its review date, records the original Alpaca documentation and paper evidence:
  [Replace Order by ID](https://docs.alpaca.markets/us/reference/patchorderbyorderid-1.md),
  [Websocket Streaming](https://docs.alpaca.markets/us/docs/websocket-streaming.md), and NautilusTrader's
  [execution policies](https://nautilustrader.io/docs/md/latest/concepts/execution/policies.md).

The PATCH response remains provisional. Execution on the replacement proves takeover; the adapter uses
that execution's order snapshot for the new terms and queues them before the fill. This follows the
existing upstream order-link and event contracts without introducing a new dependency or venue protocol.

## Verification

| Command | Result |
| --- | --- |
| Initial `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_execution_client.py` | 13 failed, 101 passed, reproducing the supplied parametrized regressions |
| Same execution-client suite after the initial fix | 114 passed |
| `mise exec -- uv run --locked pytest -q packages/canon-alpaca/tests/test_replace_lifecycle.py` | 7 passed |
| Final `mise exec -- uv run --locked pytest -q --ignore-glob='**/test_paper_*.py'` | **377 passed in 62.69 seconds**, including all supplied regressions and new cases |
| `mise exec -- uv run --locked ruff check .` | Passed |
| `mise exec -- uv run --locked ruff format --check .` | Passed; 99 files formatted |
| `mise exec -- uv run --locked ty check` | Passed |
| `scripts/canon lint` | Passed: `canon: lint clean` |
| `git diff --check` | Passed |

## Uncertainty and remaining work

`UNVERIFIED`: behavior against Alpaca's paper service and the live timing/distribution of these delivery
races. Paper tests and credentials were excluded as required; remote documentation and historical paper
observations were not fetched or re-verified. ADR 0036's existing paper uncertainties remain labeled.

No requested implementation or offline verification remains. No agents, paper tests, credential reads,
pushes, pull requests or landing commands were used. `scripts/ci` was not run because this task specifies
the offline suite and three code checks for the reduced checkout. The result is uncommitted, as allowed.
