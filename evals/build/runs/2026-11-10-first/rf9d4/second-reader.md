# Second reader: rf9d4
Mark: fix
Blocker: none
## Findings
1. packages/canon-alpaca/tests/test_execution_unholdable_and_cancel_all.py:44-198 — the new file copies the whole offline harness of `test_execution_client.py` (`venue` fixture at :93, `build_node` :257, `run` :302, `run_with_client` :309, `until_idle` :355, `logged` :372, plus its own `ProbeStrategy` in place of `Trader`/`CancelAll`), about 150 lines, so the package now has two node harnesses to keep in step: a change to how the fake venue or node is wired has to be made twice, and when it isn't, the two drift without anything failing. A review round would ask for the 9 tests to go into `test_execution_client.py` (or the harness to move into `conftest.py`), and for the README test-table row for the new file to be folded in with them. (severity: fix)

Observations that would not hold up landing:
- packages/canon-alpaca/src/canon_alpaca/execution.py:1545 — the `if replaces is not None:` guard on the `for ... else` warning also changes a case outside the statement (a chain of exactly MAX_REPLACE_CHAIN links no longer logs "more than 16 replaces"). The change is right, but no test covers it.
- execution.py:1259, :1668-1671, :1729 — the guards on `_replacements` against an empty `client_order_id` in `_modify_order`, `_finish` and `_adopt` protect against an answer Alpaca does not give (a replacement's ID is Alpaca-made). They do no harm and the report states them accurately.
- ADR 0036 l.41: the in-place edit drops the old "as Coinbase, since a second cancel risks a rejection" reason and replaces it with the 422-lookup argument. That is consistent with `_cancel_refused` (execution.py:1161-1196). The upstream-adapter list it now cites (Kraken spot, Binance spot, Bybit, IB) is the case test's own comment. I did not check it against upstream source (no web), so it is UNVERIFIED by me.

## Per-defect check
1. Cancel-all skips PENDING_CANCEL — removed: execution.py:1115 now skips only `order is None or order.is_closed`. A repeated DELETE on an order Alpaca already took goes through `_cancel_refused`, which returns with no rejection when the order shows canceled or pending_cancel (:1177-1182). Both tests pass: the case's test and the worker's 422 test.
2. Unholdable client order ID — removed on every path I traced:
   - Closed unholdable order: `_root_id` (:1515) returns None, `_order_report` passes `renamed=None`, and `order_status_report` builds `ClientOrderId(order.client_order_id or "")` inside its `try` (:683-687). It returns None and the order is skipped with a warning (:2143-2147). Mass status goes on.
   - Open unholdable order: the same `try` raises `Alpaca order <id> (<symbol>) is open with invalid string ...` (:699-701). Start-up fails and the message names the order.
   - Valid replacement of an unholdable original: the walk keeps the order's own holdable ID and steps past unholdable priors (:1538-1543), so it is reported under its own ID. Several hops give the earliest holdable ID on the chain, which matches the existing rule of reporting under the root.
   - Stream path: `_node_order_id` (:1487-1491) returns None for such an order, and `_apply` drops it with the existing info line.
   - No other `ClientOrderId(...)` built from Alpaca data remains in the package (grep: only :623 and :686).
   - Fail-to-pass flags: `grade.json` `flags` is empty and `restored` is empty. No existing test was changed (`git diff --stat HEAD -- packages/canon-alpaca/tests` is empty). The fix lives in production code and does not recognise the test inputs.

Blockers checked and not found:
- (2) The fix delegates the validity rule to `nautilus_trader.model.ClientOrderId` and adds no dependency.
- (3) `holdable_client_order_id` swallowing ValueError is not a hidden failure: an open order still fails start-up by name in `order_status_report`, a closed one is logged, and a stream update is logged as not held.
- (4) I traced these sequences and none produced a wrong event: cancel then cancel-all; cancel answered 204 then cancel-all (422, lookup, no rejection); a cancel that raced a fill (a truthful 422 rejection, the same as a single cancel); an unholdable original replaced, with its replacement rejected; two-hop chains, then a stream update after reconciliation.
- (5) The report's claims match the diff: the 9 supplemental tests (1+1+2+2+2+1 counted), no existing test file modified, 5 reproducing tests passing (the closed-order test has 2 parameters), 506 passed / 24 skipped as in `grade.json`, and the guards and docstrings it lists.

## What I ran
- `git -C tree2 status --short` / `git diff`: 4 modified files and 1 untracked test file, the same set as `grade.json` "changed".
- `cd tree2 && mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py packages/canon-alpaca/tests/test_execution_unholdable_and_cancel_all.py -q -p no:cacheprovider`: 149 passed in 68.37s.
- I did not run the full workspace suite, ruff, ty or canon lint myself. For those I relied on `grade.json` (506 passed, 24 skipped; ruff lint, ruff format and ty true).
- No network, no paper tests, no credentials.
