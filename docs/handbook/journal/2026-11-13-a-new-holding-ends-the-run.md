### 2026-11-13-a-new-holding-ends-the-run: A holding bought after the protector's start ends the run, so its successor reads the account fresh

* area: exits
* session: 2026-11-13
* result: On main a symbol first bought after the owner protector's start gets no exit and is not named until the run stops. The unit built for it has the running protector ALERT and end by name when a new symbol is bought, and the runner's successor read the account fresh; in one engine run the predecessor ended about half a minute after the fill and the successor sent the new holding's trail across a modelled seam. Its landing review then found a P1, reproduced failing: the roster read ends the run on a new long even when the same read holds one it cannot read, and the runner then refuses the next start, leaving every holding without a stop of ours until the account changes
* evidence: `src/canon_trading/protector_node.py:158` and `:502-504` (the roster is the start's account snapshot) and `scripts/automation:1287` (a refused start ends the firing) at 32389697; the engine run, the unit's review report and the lead's log of 2026-11-13, kept outside the repository
* lesson: A protector that reads its roster once cannot protect what is bought after it; ending the run and starting fresh is a sound means only when nothing in the same read makes the next start refuse, and every holding pays one seam without a working stop of ours
* change: none on main; the unit is built and stopped on its landing review, so the means is UNVERIFIED here
* next: The unit's fix for the P1 with its failing test, then its engine run and a paper run with a holding bought after the start
* status: open
* supersedes: none
* source: review
* owner: the owner protector's build lane
* closes: the new-holding unit on main, its P1 fixed with a failing test first and its engine run named in its pull request
