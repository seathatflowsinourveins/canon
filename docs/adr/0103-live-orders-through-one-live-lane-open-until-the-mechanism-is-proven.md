---
status: proposed
date: 2026-11-12
decision-makers: repo owner (requirement, 2026-11-12); lead orchestrator (Claude) for the mechanism
review-by: 2026-12-12
---

# Live orders through one live lane, a protector first; the owner's authority is given, open until ADR 0110's gaps close on paper or NautilusTrader v2.0.0 stable is pinned

## Context and Problem Statement

ADR 0009 kept agents off live orders and kept trading on paper until NautilusTrader v2.0.0 stable. On
2026-11-12 the owner required that agents may place, modify and cancel orders on the owner's live Alpaca
account automatically. Which sessions may, through what, and what holds the line before a mechanism exists?

## Decision Drivers

* No session ever reads, prints, logs or commits a credential, paper or live (ADR 0009, ADR 0029).
* No hook runs before a tool call (ADR 0020), and the rule text must be safe the moment it lands.
* Nothing about the owner's account enters this public repository.

## Considered Options

1. Keep ADR 0009's two rules and its condition (the status quo).
2. Live open to every session.
3. Live through one lane: one Claude Code session, through landed `mise run live:*` tasks only.
4. Live by hand: agents prepare a runbook and the owner places each order.

## Decision Outcome

Chosen: **option 3**. Option 1 does not meet the requirement, and option 4 meets it only in part: its orders
are not automatic. Option 2 puts the live pair within reach of every lane, scheduled agent and reviewer of
another runtime, and no hook would stop any of them (ADR 0020).

The authority is the owner's and is given. The record stays `proposed` under the format rules because
upstream advises against a release candidate for real capital and the mechanism is unproven: NautilusTrader's
README does not "recommend using release candidates in production environments, such as live trading
controlling real capital"; the pin is `v2.0.0rc8`, `v2.0.0rc9` is tagged, and no `v2.0.0` stable tag exists.
The owner decides; the disagreement is recorded, not resolved. The record closes when ADR 0110 is accepted,
its gaps closed on paper, or NautilusTrader v2.0.0 stable is pinned, whichever the owner takes. The rule
text stands now: it is the owner's requirement, not a consequence of the status.

1. **Authority.** Agents may place, modify and cancel orders on the owner's live account. This withdraws ADR
   0009's two safety rules ("Paper credentials only in any environment an agent can read..." and "Agents do
   not place, modify or cancel live orders. Going live is a human action") and its "paper trading only until
   v2.0.0 stable", and rewrites rule 8 of `AGENTS.md` and `.claude/rules/trading-safety.md`. Every record
   that cites paper only (ADR 0009) as a gate or a driver is read with this one and stands for every
   session but the live lane.
2. **Scope: the live lane**, one Claude Code session started for the purpose. Only it runs the live tasks.
   Every other session stays on paper and never touches live: paper lanes, coordinators, scheduled paper
   agents, subagents, reviewers, and every worker of another runtime (Codex, Muse, Antigravity, DeepSeek
   Harness).
3. **The only path and the gate.** Live orders go only through the live lane's `mise run live:*` tasks.
   Nothing trades live until ADR 0110 is accepted and its live order tasks are on `main`; none is built.
   There is no other path: no direct alpaca-py call, no Alpaca CLI, no MCP server, no claude.ai connector.
4. **Two stages** (the owner's, restated 2026-11-12). First, before the paper lanes' automation lands: a
   protector that keeps ADR 0088's emulated trailing stop-limit, at the per-name distance a later record
   registers (none is on `main` today), on every position in the account, and orders the owner asks the
   live lane for, each protected from its fill. The lane places what is asked and recommends nothing. Then,
   as a paper strategy proves itself, its automation is promoted on merit.
5. **Merit** (the lead's definition, no primary source; the owner may change it): the strategy's code is on
   `main` with its review; its protection is proven on paper in the session part where it will trade,
   including a real kill of the node with the dead-node guard closing the position; its settings come from a
   dated settings file that passed ADR 0010's gate (none exists yet); the live lane writes one promotion
   note per strategy. Paper stays ungated; the gate is for promotion only.
6. **Credentials and records.** The owner enters the live pair into the login keychain by hand, in a service
   of its own; only the live tasks put it in an environment, as ADR 0029 and ADR 0069 do for paper. Nothing
   about the owner's account enters a tracked file, commit or pull request; records of live runs are kept
   outside the repository. This is engineering work, not investment advice.

### Consequences

* Good: one path for the requirement, and every other session's work is unchanged.
* Bad: a rule in text is not a technical barrier, and no hook enforces the scope (ADR 0020). What holds it is
  the keychain service only the owner fills and only `live:*` tasks read, the paper class that keeps
  refusing the live host, and review and the merge gate. A process of this user can still read the keychain
  (ADR 0029). The reason ADR 0020 gives for paper only, no live credential on this machine, which it marks
  UNVERIFIED (its line 67), ends once the owner stores the pair.
* Bad: an emulated order is held by the node's `OrderEmulator`, not by Alpaca, and Alpaca's own trailing
  stop "will not trigger outside of the regular market hours", so protection needs the node alive (ADR 0110).
* Bad: Alpaca says paper fills differ from live (impact, latency slippage, queue position, price
  improvement, quantity not checked against the NBBO). A strategy proven on paper is not proven live.

### Confirmation

`scripts/canon lint` passes. `AGENTS.md` rule 8 and `.claude/rules/trading-safety.md` state the gate in
point 3. No `live:*` task is in `mise.toml` at this record's landing (read). ADR 0110 confirms each item.

## Evidence

Read 2026-11-12.

* The owner's requirement, given to the lead on 2026-11-12 — this repository's own history; not
  re-observable.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/develop/README.md — the release-candidate
  sentence quoted above, lines 279-281. Fetched raw.
* `git ls-remote --tags https://github.com/nautechsystems/nautilus_trader` — tags `v2.0.0rc3` to
  `v2.0.0rc9`, `v2.0.0rc9` peeled to `da6b57aefdd6`; no `v2.0.0` tag. Run.
* `canon/upstreams.lock.json` — `nautilus-trader` at `refs/tags/v2.0.0rc8`, `d53849b216e2`. Resolved;
  `pyproject.toml`, `nautilus-trader==2.0.0rc8`, read. Neither edited.
* https://nautilustrader.io/docs/md/latest/concepts/orders/emulated.md — while held the `OrderEmulator`
  "processes the order in a local matching core", and on trigger releases a `MARKET` or `LIMIT`; "On
  startup, the `OrderEmulator` reactivates emulated orders that the configured cache database restored into
  the cache". Fetched raw, found through https://nautilustrader.io/docs/llms.txt.
* https://docs.alpaca.markets/us/docs/orders-at-alpaca.md — "Only limit orders with `time_in_force` set to
  `day` or `gtc` orders are accepted as extended hours eligible"; "Trailing stop will not trigger outside of
  the regular market hours". Fetched raw, found through https://docs.alpaca.markets/llms.txt.
* https://docs.alpaca.markets/us/docs/245-trading-for-trading-api.md — "Only `limit` orders are supported
  during the overnight session". Fetched raw.
* https://docs.alpaca.markets/us/docs/paper-trading.md — it "does not account for" market impact,
  information leakage, price slippage due to latency, order queue position, price improvement, regulatory
  fees and dividends; "Your order quantity is not checked against the NBBO quantities". Fetched raw.
* Records citing paper only with ADR 0009: `grep -l -i -E "paper (trading )?only"` over `docs/adr/`, then
  those naming `ADR 0009`, less 0009, 0103 and 0110, gives 19 records, among them 0018, 0025, 0037, 0038,
  0043, 0045, 0049, 0050, 0058, 0071 and 0074. Run.
* ADR 0009, 0010, 0020 (line 67), 0029, 0069 and 0088 (`status: proposed`, `trail` 0.02 of the entry
  price, one value for every name). Read.
* evidence-verifier on this record, 2026-11-12, two passes: the split into this record and ADR 0110, point
  4's trail, ADR 0020's own UNVERIFIED and its wording, the status (now `proposed`), the Evidence terms, the
  stable-tag fact, the records read with this one, and one gate stated one way. Applied above.
