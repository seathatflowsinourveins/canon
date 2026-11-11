# Report: Alpaca replace handling when updates arrive late, twice or out of order

Task `task_7eca1155f223`. The change is in the working tree and is not committed; nothing was landed.

## What was wrong

`AlpacaExecutionClient` (`packages/canon-alpaca/src/canon_alpaca/execution.py`) handled a modify through
Alpaca's replace as if Alpaca's answers and trade updates always arrived once, in order, and all of them.
Three things were missing:

- **Which Alpaca order the node's order follows.** The client relied on the cache's `venue_order_id`.
  NautilusTrader queues generated events, so the cache lags the events the client has already sent
  (`generate_*` in `nautilus_trader/live/clients.py`: "Queue the ... event").
- **Whether a replace had been settled.** The client kept no record of which replaces were still waiting
  for an outcome.
- **Some cases had no handling at all.** A filled replacement was skipped by `_order_report`.
  `pending_replace` had no status mapping. `_replaced` read only one hop of the replace chain. And the
  `_finished` early return came before the branch for a rejected replacement.

As a result:

1. A filled replacement whose `replaced` never arrived was skipped in reconciliation. The skip was meant
   only for a replacement rejected after the old order filled, but it covered any closed status.
2. A refusal both answered over HTTP and streamed gave two rejections.
3. A `replaced` delivered twice gave two `OrderUpdated` events.
4. A fill on the replacement before its `replaced` was applied under the old terms.
5. A late cancel or expiry of the replaced order closed the node's order.
6. `pending_replace` failed reconciliation.
7. A replacement that was itself pending replace, or already replaced again, raised instead of being
   followed.
8. A second modify refused "order already replaced" was taken for alpaca-py's retry of the first.
9. Answers that came back after their `replaced` piled up in `_answers`.
10. The rejection of a replacement after the old order filled was dropped without a word.

## What I changed, and why that way

All code changes are in `execution.py`. I added no dependency and edited no existing test.

**New state on the client**, cleared in `_disconnect`:

- `_current`: the Alpaca order ID each node order follows. It is set on acceptance and on each
  `OrderUpdated` this client generates. `_following()` falls back to the cache's ID, for example after a
  restart.
- `_modifying`: replaces sent and not yet settled, keyed by the Alpaca order each one targets. The value
  is the node's client order ID, as the test for defect 10 expects. Whichever of Alpaca's outcomes comes
  first settles the entry (`_settle_modify`), so one modify gets one outcome.
- `_modified`: node orders this client sent a replace to. If a streamed rejection finds no outstanding
  entry, the rejection is dropped only when this client has already settled that modify. When the
  replace was sent before a restart, the rejection is still generated.
- `_finish()` replaces the bare `_finished.add`. It also drops the order's modify state and answers.

**Per defect:**

1. **Reconciliation.** `_order_report` now skips a traced replacement only when it is `rejected` and is
   not the order the node follows. That is the case Alpaca documents ("the replacing (new) order is
   rejected" when the old order fills first). A filled, canceled or expired replacement did take over,
   so it is reported under the node's ID. The `replaced` old order is still skipped. Its log line now
   says why, instead of claiming an unmapped type.
2. **One rejection per refusal.** The HTTP refusal and the stream's `order_replace_rejected` both go
   through `_settle_modify`. Only the first to settle the entry generates `OrderModifyRejected`.
3. and 7. **`replaced` handling.** `_replaced` reads the replacement (from the PATCH answer or Alpaca).
   It follows `replaced_by` while Alpaca shows that order replaced, up to `MAX_REPLACE_CHAIN`
   (`_live_order`). It applies the result only if that order follows, through `replaces`, the order the
   node follows now (`_succeeds`). A duplicate, or a `replaced` already overtaken, changes nothing.
   Mapping `pending_replace` (defect 6) means a replacement still pending replace can be reported.
4. **Fill before `replaced`.** A replacement's own `fill`, `partial_fill`, `canceled` or `expired`,
   arriving before its `replaced`, first generates `OrderUpdated` with the replacement's terms
   (`_take_over`), then the event. The replacement's `new` does not count, because a replacement can
   still be rejected after `new`. An existing test pins that.
5. **Late cancel of the old order.** A `canceled` or `expired` for an Alpaca order the node's order no
   longer follows, and that does not succeed it, is logged and ignored. Fills on such an order are still
   applied, because they are real executions.
6. **`pending_replace` status.** It is now in `OPEN_STATUSES` as `ACCEPTED`. `order_status_report`
   reports it `PARTIALLY_FILLED` when it has fills (`WORKING_WITH_FILLS`).
8. **Second modify refused.** Before sending a replace, `_modify_order` notes the replacements it already
   knows for the order: the answers it holds, plus the order it follows. A refusal that shows the old
   order replaced by one of those answered an earlier modify, so this modify is rejected. The retry path
   is unchanged for a replacement first seen after the send.
9. **Answers piling up.** An answer is stored only while its modify is still outstanding. `_take_over`
   drops the order's answers for any target other than the order it now follows.
10. **Rejected replacement after the old order filled.** That branch now runs before the
    `_finished` check. If the order has ended, it logs "…which had filled first; the fill is the outcome
    of the modify" and the modify is settled. Otherwise it generates `OrderModifyRejected` once.

**Docs made untrue by the change, now corrected:**

- The `execution.py` header: the Modify paragraph, the `pending_replace` note, and a new known limit.
- The `order_status_report` docstring.
- The package README:
  - the Modify bullet;
  - the Stream bullet, which listed `replaced` as unmapped, already untrue before this change;
  - the test-table row.
- ADR 0036's Modify outcome: "`OrderUpdated` comes only with `replaced`" was no longer true. `scripts/canon
  lint` is clean after the edit.

## Upstream API or contract followed

- **NautilusTrader's `ExecutionClient` `generate_*` methods**, v2.0.0rc8 as installed. I read them in
  `nautilus_trader/live/clients.py`; they queue events. The calls are `generate_order_updated`,
  `generate_order_modify_rejected` and `generate_order_filled`, with the signatures the module already
  uses. Execution policies, "Command outcomes": one outcome per command.
- **NautilusTrader's `OrderStatusReport`** for reconciliation.
- **Alpaca's "Replace Order by ID" and its trade-update events**: `replaced`, `order_replace_rejected`,
  `pending_replace`, and a replacement's `rejected`. Also Alpaca's `OrderStatus.PENDING_REPLACE` in
  alpaca-py 0.45.0 (`alpaca/trading/enums.py`, read this session). Plus the paper observations ADR 0036
  records: `replaced` carries the old order with `replaced_by`, and a second replace is refused "order
  already replaced".

## What I ran, with results

All commands were run from the repository root through `mise exec --`.

| Check | Result |
|---|---|
| `uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py` (before the change) | 13 failed (the 11 named tests; two are parametrised), 101 passed |
| `uv run --locked pytest packages/canon-alpaca/tests/test_execution_client.py` (after) | 114 passed |
| The same file, `-k "replace or modify or pending"`, 3 more runs | 30 passed each time |
| `uv run --locked pytest` (whole workspace, run twice) | 370 passed, 21 skipped both times |
| `uv run --locked ruff check .` | All checks passed |
| `uv run --locked ruff format --check .` | 98 files already formatted |
| `uv run --locked ty check` | All checks passed |
| `scripts/canon lint` | lint clean |

The 21 skipped are the paper tests, which skip without keys. I ran no paper test.

I did not run `scripts/ci`: the brief says this copy lacks files it expects. I also did not run
`/code-review` or the `evidence-verifier`, because the task says no subagents. The repository's
review gate (ADR 0017) has therefore not been met. This is a ~375-line change to the execution client;
I recommend `/code-review` at high effort before it lands. It is also a candidate for the owner's
`/code-review ultra`.

## What I am unsure of (UNVERIFIED)

- **What `pending_replace` means.** I took it to mean the order still works on its old terms until
  `replaced`. The Alpaca pages quoted in this repository do not say so. The comment marks this
  UNVERIFIED.
- **Whether a fill or end of the replacement proves it took over.** I treat Alpaca's update for the
  replacement as proof the replace happened. That is my reading; no paper run has shown a replacement's
  fill arriving before its `replaced`.
- **That NautilusTrader drops a modify rejection for a filled order.** I took this from the comment in
  the test for defect 10; I did not check it separately.
- **Known limit: one tracked replace per Alpaca order.** Suppose a second modify is sent to the same
  Alpaca order before the first settles, and Alpaca refuses it both over HTTP and on the stream. Then
  the streamed rejection settles the first modify's entry and a second rejection is generated. Alpaca's
  stream update does not say which replace it answers. The header and README state this limit.
- **The defect-10 log line names a fill** even if the order ended some other way. A fill is the case
  Alpaca documents, but the client does not record why an order ended.
- **No new tests.** The stand-in venue and node harness live in `test_execution_client.py`, which I may
  not edit, and copying them into a new file seemed worse than adding none. The named tests cover the
  ten defects. The two orderings of defect 2 (HTTP refusal first or stream first) are exercised only
  by whichever order the loop happens to run.
