---
status: accepted
date: 2026-11-11
decision-makers: repo owner (instruction of 2026-11-11); master orchestrator (Claude)
review-by: 2027-02-09
---

# The orchestrator decides by default, work off the trading path gets a proportionality test and one pass, and converged documentation lands a record off that path

## Context and Problem Statement

On 2026-11-10 every review round found new defects, and harness lanes competed with the roadmap's
(ADR 0071, ADR 0073). The records said who reviews and when a path is done, but not who
decides when the owner is not asked, how much work a topic off the trading path deserves, or what evidence
is enough to land a record. The owner decided on 2026-11-11 that the orchestrator decides by default, that
work outside the trading goal is judged for over-engineering first, that security gets one proportionate
pass, and that converged upstream research with a landscape comparison is enough to land a record.

## Decision Drivers

* ADR 0001 rule 2: the owner sets goals and risk limits; evidence decides the means.
* The owner judged on 2026-11-11 that the orchestrator usually holds more of the sources, tools and
  gathered evidence than the owner does.
* A review loop with no end on a harness path takes lanes from roadmap steps 6 to 8.
* Anthropic's guidance points the same way, by analogy: the simplest solution, complexity only when
  needed, and a small enforced set (two pages of one vendor, Evidence; not a survey).
* Gates already held stay held: paper only, no credential in context, review before landing (ADR 0009,
  ADR 0017, ADR 0029).

## Considered Options

1. No rule: each lane judges scope and evidence for itself (the state).
2. One standard for every path: a failing test and an end-to-end run before any record or change lands.
3. Tiers: the trading path keeps its test-and-run standard; everything else lands on converged
   documentation, one verifier pass and one review, after a proportionality test.

## Decision Outcome

Chosen: **option 3**, on the owner's instruction of 2026-11-11. Option 2 would ask for proof that many
items (a vendor's sandbox, a runtime's flag) cannot be given here reliably, and holds lanes on it.

1. **Who decides.** The orchestrator decides by default and proceeds, choosing the means by ADR 0001's
   precedence of sources. This extends the delegations already recorded (ADR 0010's threshold and scope,
   ADR 0024's acceptance on its own paper test). What stays with the owner is unchanged: goals and risk
   limits, credentials and accounts, repository and account settings, anything installed on or billed to
   the owner, and a hard-to-undo choice on which the sources disagree.
2. **The north star** (the orchestrator's reading, for the owner to correct): strategies built on
   NautilusTrader and Alpaca, evaluated through the gate and proven on paper, roadmap steps 6 to 8.
3. **The proportionality test.** Before starting anything outside the north star, a lane asks: does this
   move a roadmap step, or keep the gate honest? If not, it does the smallest documented form, once, or
   records a limit. Foundation and harness records pass the test when they answer a gap on ADR 0071's stopping list (gaps
   1 to 5); gaps 6 to 10 get the smallest form or a limit.
4. **The evidence tiers.**
   * *The trading path*: the adapter's order and data paths, the session window and the gate. A failing
     test first, and a paper or simulation run, as now.
   * *Everything else*: tools, settings, harness practice and side topics. The vendor's or upstream's
     documentation at the pin (its current docs where it has no pin), plus a landscape comparison; what was not run is marked UNVERIFIED; one
     `evidence-verifier` pass for a record; the one review ADR 0017 requires for a change. End-to-end proof
     of each item is not required.
5. **Security off the trading path.** Security is not a goal of this project. A security-flavoured finding
   off the trading path gets one decision, a small documented fix or a recorded limit, with no second
   round and no new lane, unless it bears on a credential the repository handles: the paper key pairs
   (ADR 0029, ADR 0069) or the gateway's storage key (ADR 0056).

**Amendments, by dated pointer in each record.**

* *ADR 0073*: rules 3 and 4 (two clean rounds; a family found twice is redesigned) apply to the trading
  paths. A harness or tooling path gets the review ADR 0017 requires and at most one outside round. This
  takes 0073's "paper runner and automation" path out of the loop, as harness code by this record's
  tiers, except where a change bears on the paper key pairs (the keychain tasks): there rules 3 and 4
  still apply, including the one shared reading of `security`'s status that 0073 names. Its fixes still
  need a failing test where one can show the defect.
* *ADR 0071*: stopping check 2's record is taken from the vendors' own documentation and does not wait on
  an end-to-end trial of each runtime's containment; its second half (ADR 0025's turn-on change landed or
  dropped) stands. Stopping check 5 counts the trading paths only. Gaps 7 to 10 stay optional.
* *ADR 0017*: unchanged.

### Consequences

* Good: lanes stop on a stated rule rather than on fatigue, and most of them go to steps 6 to 8.
* Good: a later session reads the rule in `AGENTS.md` and this record, and needs no instruction.
* Bad: a harness defect a second round would have found can now ship; the tiers accept that.
* Bad: the best-practices page cited below also advises a hook, deny rule or managed setting for what
  must happen every time. This rule is text in `AGENTS.md`, not enforcement, as ADR 0020 and ADR 0071
  already record.
* Bad: "moves a roadmap step" is judged by the lane; the report or record says which step, so a later
  reader can dispute it.

### Confirmation

`AGENTS.md` carries the proportionality and evidence rule (rule 10), and lane briefs repeat it. A record off the trading path lands
with its verifier's note and no end-to-end run; a harness change lands with one review. ADR 0073's
counts (its rule 5) are kept for the trading paths only.

## Evidence

Read 2026-11-11.

* The owner's instruction of 2026-11-11, restated above (a decision of the owner, not a source; not
  checkable here).
* ADR 0001, rule 2 — the precedence of sources, and "the owner sets goals and risk limits; evidence decides
  the means" (read).
* ADR 0010, "Who decided", and ADR 0024, Decision Outcome — the owner's delegations of 2026-11-10 (read).
* ADR 0017 — the one independent review before landing, unchanged here (read).
* ADR 0071 and ADR 0073, read from their branch and again as landed on `main` (#163).
* ADR 0071 — the ten gaps and the six stopping checks; gaps 6 to 10 already off the stopping list (read).
* ADR 0073 — rules 1 to 5 and its four paths, among them "the paper runner and automation" (read).
* `docs/roadmap.md` — steps 6 to 8 and their state (read).
* https://www.anthropic.com/engineering/building-effective-agents — "finding the simplest solution
  possible, and only increasing complexity when needed"; add multi-step systems "only when simpler
  solutions fall short" (fetched, searched in full; advice on designing LLM applications, applied here by
  analogy).
* https://code.claude.com/docs/en/best-practices.md — "keep the enforced set small. Every hook and deny
  rule will occasionally fire when it shouldn't, and someone has to maintain each one" (fetched, searched
  in full).
* The `evidence-verifier`, 2026-11-11, corrected the scope of the gaps off ADR 0071's stopping list, the
  credential exception in `AGENTS.md`, the runner path and stopping check 5, and the strength of the
  vendor evidence (run).
