### 2026-11-13-a-handler-only-stores: An order-event handler only stores, and every engine run fails on a handler error

* area: execution
* session: 2026-11-13
* result: In 6 of 8 engine runs of the owner protector's restart case built off main, the engine logged "Python on_order_released failed: RuntimeError: Already mutably borrowed": the restored level was already through the bid, so the emulator released the trail inside the protector's own submit, and the handler then read the cache. The harness's assertions passed in all 8, so no test caught it. The same class had shown in a paper window of 2026-11-11, and the fix written then was never on main
* evidence: `src/canon_trading/mover.py:230-237` (the Mover's rule: an order-event handler stores only what the event carries) and `src/canon_trading/owner_protector.py:1385-1396` (the protector's `on_order_released` reading the cache) at 32389697; the engine logs and the lead's log of 2026-11-13, kept outside the repository
* lesson: Re-entry at the pin means a handler can run while the strategy is borrowed, so a handler stores what its event carries and the next step reads the engine; a passing assertion is no proof when the engine logged an error, so a run's log is read for "Python <handler> failed" and any such line fails the run
* change: none on main; the protector's fix (its release and cancel-reject handlers store only the order ID and event time) is built and not on main, and no check on main fails a run on a handler-error line, so both are UNVERIFIED here
* next: The fix on main with its failing test, and the handler-error check in every engine harness and in the paper window's end record
* status: open
* supersedes: none
* source: review
* owner: the owner protector's build lane
* closes: the protector's handler fix on main with its failing test, and a tracked check that fails an engine run on a handler-error line
