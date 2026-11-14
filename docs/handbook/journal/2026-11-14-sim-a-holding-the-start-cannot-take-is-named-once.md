### 2026-11-14-sim-a-holding-the-start-cannot-take-is-named-once: A holding the protector cannot take is named once with its reason, and the others stay protected

* area: risk
* session: 2026-11-14
* result: Mechanism runs, ranking nothing, on 2026-11-13's recorded SIP tapes (a forward date), with the owner protector at two branch heads not yet on main and a scripted reader of positions, orders and assets standing in for the account (the deputy's ruling of 2026-11-14). At the first head, three scripted holdings the start cannot take (a fractional one, one under an open OCO sell, one whose asset reads inactive) were each named once by an ALERT with its reason at the first read and never again over 50 reads; the run went on and the tape's holdings were protected and filled. At the second head, two more were added whose reads fail with a server error (an asset read, and the trace of an open replacing order): all five were named once and none repeated over 29 reads, the failed reads were made again at each read, and a buy made after the start was still judged beside them. 0 handler errors in 497 and 328 log lines
* evidence: the run farm's report e-R8 (its part F2) and its runs, kept outside the repository; the scripted answers are in each run's own record there
* lesson: A holding outside what the protector can take is said once, by its reason, and does not stop the protection of the rest nor the reading of a later buy; a read that fails is retried at each read rather than taken as an answer. These runs show the head's handling of a reported account state, never how Alpaca itself reports one
* change: none on main; both heads are units still to land
* next: The same scripted runs at each unit's landing head on main, then a paper window on an account that holds one such holding
* status: open
* supersedes: none
* source: lead
* owner: the run farm lane
* closes: the farm's rerun of the scripted holdings at the landing head on main, with each named once and the rest protected
