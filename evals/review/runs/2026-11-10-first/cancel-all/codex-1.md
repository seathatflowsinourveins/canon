# Review of 4afa6a7c62f0f186761a402cec9df5fb5374f78a

1. **Cleanup does not restore the account before releasing the paper-test lock.** `packages/canon-alpaca/tests/test_paper_execution.py:171–177`, `aapl_left_as_found`.

   The cleanup requests cancellation, checks the position once, and optionally submits a closing order, then returns immediately. Neither cancellation nor liquidation is confirmed. The installed pinned alpaca-py implementation documents `close_position` as placing an order, and returns that order; `cancel_order_by_id` merely sends DELETE. Consequently a failure leaving a paper order open can leave that order pending cancellation when the next test starts. If it fills during cancellation, the single position check can miss the resulting position; if a position is already present, the closing order can remain outstanding (particularly outside the regular session). This contradicts the fixture's promise to leave AAPL flat with no open orders and can make the next test fail its precondition because of the preceding test.

   Demonstrated offline by importing the module and replacing its three account helpers with a stand-in: start flat, add an order and position inside the context, have cancellation return before completion and closure enqueue a closing order. The context returned `{'orders': 1, 'positions': 1}` while both orders and the position remained. No paper API was called. The post-context assertion makes the original test fail, but does not finish cleanup or protect the following test.

2. **The acceptance deadline can expire without failing the cancel-on-stop test.** `packages/canon-alpaca/tests/test_paper_execution.py:263–273`, `run_exec_tester`, and assertions at lines 225–231.

   The wait loop exits on its deadline or normal node termination as well as on the intended accepted-order condition. There is no assertion recording which condition ended it. If the order is still submitted at the deadline and is canceled during shutdown, the resulting single order ending `OrderCanceled` passes every assertion, despite the promised precondition—one accepted order before stop—never being established. This can conceal a broken or delayed acceptance path while reporting that the accepted-order cancel-on-stop scenario passed.

   Demonstrated offline with a fake node and a clock advanced beyond the 60-second deadline: `run_exec_tester` made zero acceptance checks, stopped the node, and returned `{'test-order': ['OrderCanceled']}`; the caller's event-count and last-event assertions both passed. This checks the Python control flow only; the corresponding delayed-acceptance sequence against Alpaca remains UNVERIFIED.

3. **The module description falsely says cancel-all cancels two orders.** `packages/canon-alpaca/tests/test_paper_execution.py:1–2`.

   The new docstring describes “two by one `CancelAllOrders`,” but the new paper test enables limit buys only, disables limit sells, passes `count=1`, and asserts exactly one order. Static inspection establishes that the described two-order paper coverage is absent; the README and test function docstring correctly describe a single buy.

## Verification and limits

Ran `mise exec -- uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py packages/canon-alpaca/tests/test_upstream_contract.py -q`: **89 passed**. Read the parent-to-HEAD package diff, relevant surrounding code, ADR 0036, and installed pinned alpaca-py cancellation/position-closing implementations. Ran the two offline stand-in reproductions described above. No paper tests, credentials, network requests, later history, or `evals/` were used; the checkout was not edited. Real account timing and paper behavior remain UNVERIFIED; fixes and subsequent validation are left to the coordinator.
