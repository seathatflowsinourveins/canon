### 2026-11-13-the-simulated-venue-cannot-sell-a-foreign-holding: The simulated venue cannot fill a sale of a holding another strategy opened

* area: execution
* session: 2026-11-13
* result: On NautilusTrader's simulated venue, the owner protector's reduce-only sale of a long that another strategy, or an external owner, opened was refused before any fill under every setting tried: a cash account refuses it as a short sale, a margin account as a reduce-only order that would increase the position, naming the owner's position ID is denied under netting, and with the venue's reduce-only check off such orders are refused outright. Every protector run on recorded tapes therefore showed releases and no fills for the owner's holding. One synthetic case under each setting
* evidence: the header of `src/canon_trading/owner_protector.py:20-22` at 32389697 and the named limits of pull request 443 (its simulation proof); at the nautilus-trader pin d53849b216e2, `crates/execution/src/matching_engine/mod.rs:5545-5560` (without a cached position link, a netting venue reads the position of the order's own strategy) and `:3583-3667` (a cash account refuses a sell with no such position as a short sale) and `docs/concepts/execution/reconciliation.md:305-335` (the engine's fallback onto an external long runs on a fill); the case runs, kept outside the repository
* lesson: The live path reduces an external long through the engine's fallback on a fill, and the simulated venue refuses before any fill, so no simulation shows that sale filled; a simulation of a protector shows its decisions up to the release, and the fill is shown on paper. A run that books the holding under the protector's own ID shows the fill only once the same tape gives the same decisions both ways
* change: the limit is named in the owner protector's header and on the [simulation page](../simulation.md); no simulation claims the external-long fill
* next: none
* status: confirmed
* supersedes: none
* source: lead
* owner: none
* closes: none
