---
status: accepted
date: 2026-11-10
decision-makers: master orchestrator (Claude), under the owner's delegation of 2026-11-10
review-by: 2026-12-10
---

# The paper node's risk limits: 50 submits and 25 modifies a minute against Alpaca's 200 calls, $1,000 an order

*Amended in part 2026-11-12 by ADR 0106 (proposed), which carries the Mover composition's changes to this record.*

## Context and Problem Statement

The owner asked (2026-11-10) for paper trading on native features, capped at 200 orders a minute. The paper
node of ADR 0037 runs the risk engine; which limits does it set, and on what figure for Alpaca's own limit?

## Decision Drivers

* Upstream's code over ours (ADR 0001); Alpaca's own limit over the owner's figure.
* Paper only, one share an order, the account left as found (ADR 0009); the position cap is ADR 0044's.

## Considered Options

1. The risk engine's defaults in `LiveRiskEngineConfig`: `100/00:00:01` for submits and for modifies.
2. Its settings set from Alpaca's limit, with every call counted against it.
3. The owner's figure as the submit rate, `200/00:01:00`.

## Decision Outcome

Chosen: **option 2**, the risk engine on with `risk_limits`, split from ADR 0037 on 2026-11-10 with no
change of decision (option 3 and the reasons were added on the split, see Evidence). Option 1 allows
6,000 submits a minute, 30 times Alpaca's 200; option 3 spends all 200 on submits and leaves nothing for
cancels or queries.
`canon_trading.paper_node.ema_cross_paper_node` builds the node with them:

| Setting | Value | Source |
|---|---|---|
| `max_order_submit_rate` | `50/00:01:00` | A quarter of Alpaca's 200 calls a minute |
| `max_order_modify_rate` | `25/00:01:00` | An eighth of it |
| `max_notional_per_order` | $1,000 for the symbol | One share of SPY (about $750) passes, two do not |

**Alpaca's limit.** The owner's figure was 200 a minute. No current page of Alpaca's documentation states a
Trading API limit (its `llms.txt` lists the Broker API's only). The figure rests on archived documentation,
"The rate limit is 200 requests every minute per API key", answered with HTTP 429, and on an Alpaca
developer-relations post, "a default API rate limit of 200/min per account (ie each paper and live
account is separate)". Alpaca's Trading API MCP page says only "Alpaca's API has rate limits per
account". So 200 is a working figure, every call counted, not a documented number today; per account
has the current page behind it, per key only the archived one (one pair here). alpaca-py retries a 429
three times, three seconds apart.

**Why 50 and 25.** The risk engine throttles submits and modifies only; cancels and queries "route
directly" past it. With 50 submits, 25 modifies and one cancel per order submitted (an assumption: a
strategy may cancel more), order traffic is about 125 calls; the adapter's queries before it rejects a
refused submit or cancel (ADR 0036) and start-up reconciliation come out of the remaining 75. The
engine's in-flight checks query every order in flight over 5 s every 2 s, uncapped; the adapter
implements no query, so they do not reach Alpaca today. It sends a modify as Alpaca's replace (ADR
0042), which the 25 budgets. Windows are one minute, Alpaca's unit. A submit over the limit gets `OrderDenied`,
`RATE_LIMIT_EXCEEDED`, and never leaves the node; a modify gets `OrderModifyRejected`, "Exceeded
MAX_ORDER_MODIFY_RATE" (the docs say `OrderModifyDenied`); a denied order does not use up the rate.

A paper test may set a tighter budget of its own; `EmaCross`'s is ADR 0037's.

### Consequences

* Good: upstream's throttle and cap, with no limit code of ours.
* Bad: nothing caps cancels or reconciliation queries; 200 is a working figure, not a documented one.

### Confirmation

Offline, in `scripts/ci` (`tests/test_risk_limits.py`, `BacktestEngine`, a simulated `ALPACA` cash venue,
the same `risk_limits`): the configs report the values; of 51 submits at once 50 pass and one is
`RATE_LIMIT_EXCEEDED`, one passes 70 s later; of 26 modifies one is rejected; the probe is denied
"NOTIONAL_EXCEEDS_MAX_PER_ORDER: max=1000.00 USD, notional=1000.01 USD" without using the rate. On paper,
the probe of ADR 0037's test is denied with `max=1000.00 USD`.

## Evidence

Read 2026-11-10. "raw": fetched with `curl` and read whole. NautilusTrader files at the pin, `d53849b216e2`.

* `crates/risk/src/engine/{config,mod}.rs`, `crates/risk/src/python/config.rs`,
  `crates/common/src/throttler.rs`, `crates/model/src/events/order/denied_reason.rs`: defaults
  `100/00:00:01`, the form, drop-mode throttling, denial texts (fetched, raw).
* https://nautilustrader.io/docs/md/latest/concepts/execution/index.md, "Risk engine" (fetched, raw).
* nautilus-trader 2.0.0rc8 in `.venv`, `trading/__init__.pyi`, `live/__init__.pyi` (`bundled`).
* https://docs.alpaca.markets/llms.txt: rate-limit pages for the Broker API only (fetched, raw).
* https://raw.githubusercontent.com/alpacahq/alpaca-docs/master/content/api-references/trading-api/_index.md:
  the archived sentence; the repository is archived (fetched, raw).
* https://forum.alpaca.markets/t/what-constitutes-an-api-call/16502 (2025-03-24) and
  https://forum.alpaca.markets/t/429-rate-limit-exceeded-when-creating-orders/14120 (2024-05-02):
  Alpaca developer relations, 200 calls a minute (fetched, raw JSON).
* https://raw.githubusercontent.com/alpacahq/alpaca-py/v0.45.0/alpaca/common/constants.py: 429 and 504
  retried three times, three seconds apart (fetched, summarised).
* https://stockanalysis.com/etf/spy/history/: SPY closed at $752.69 on 2026-11-09 (search, summarised).
* https://docs.alpaca.markets/us/docs/trading-api-mcp.md (updated 2026-10-06): "rate limits per
  account" (fetched, raw).
* The offline tests and ADR 0037's paper runs (run).
* https://nautilustrader.io/docs/md/latest/how_to/configure_live_trading.md: reconciliation and the
  in-flight check's defaults, every 2 s over 5 s (fetched, raw); `crates/live/src/execution/manager.rs`
  (`check_inflight_orders`, no per-cycle cap) and `crates/live/src/node/config.rs` at the pin (fetched by
  the second reader, raw).
* Second reader (`evidence-verifier`), 2026-11-10, on ADR 0037: corrected the in-flight cap and "per
  account", and found the MCP page. All applied above. It noted v2.0.0rc9 is tagged, with no change to
  these claims found.
* Second reader (`evidence-verifier`), 2026-11-10, on this split: corrected the record cited for modify
  (ADR 0042, not 0036), added option 3 and the reasons against options 1 and 3, the in-flight sources and
  the driver's position cap; rc9's defaults and denial texts re-checked, unchanged.
