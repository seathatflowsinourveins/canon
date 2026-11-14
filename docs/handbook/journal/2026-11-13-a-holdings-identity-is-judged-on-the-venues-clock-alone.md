### 2026-11-13-a-holdings-identity-is-judged-on-the-venues-clock-alone: A carried level belongs to its holding only when both sides of the comparison are on the venue's clock

* area: exits
* session: 2026-11-13
* result: The composed protector judged whether a book entry's holding was still the one held by comparing the venue's fill stamps with the host's clock at the write; a sell-out and an exact re-buy the node had not applied when it saved, stamped by the venue before the write, were never counted, and the next start armed the new holding at the old holding's level (one reproduction, the review's P2-2 case); the same review found the book's write dropped what its locked re-read rejected, putting back a row's older copy and replacing an unreadable book, with nothing logged (four reproductions)
* evidence: tests/test_owner_protector_same_clock.py; tests/test_owner_protector_book_write.py; the proof-3 tape run on NautilusTrader's simulated venue, kept outside the repository
* lesson: A rule that compares two times holds only when both are read from one clock, and when the answer cannot be known the verdict is unknown, which arms cold, never the same holding on a guess; a writer that merges must carry what it cannot read instead of what it last read
* change: the protector keeps a venue reading per book entry (Alpaca's clock, then the orders on both sides, then the position) and matches each applied order by a hash of its venue order IDs; a write carries every row it cannot read unchanged, never overwrites an unreadable book, never lowers another run's newer level for the same holding, and says each reader alert once
* next: The supplied-level binding still compares the venue's buy fill stamps with the host's clock; it is a follow-up on the same rule
* status: adopted
* supersedes: none
* source: review
* owner: none
* closes: none
