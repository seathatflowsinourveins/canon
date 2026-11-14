"""Bounded account enumeration beside the frozen sale table, without an engine.

Contract: sale_table.json's Words, predicates and properties; NautilusTrader
d53849b216e2 crates/model/src/orders/mod.rs:207-302, 1314-1325, 1367-1389.
Quantities are integers; starting longs are one through three and foreign sells
zero through three. Growth stops at three; actual fills can take a long below zero.
Fills of ended legs are separate from fills of the current leg. No I/O is used
except reading the tracked table and writing statistics in pytest's temporary
directory. Mutation tests change in-memory copies only.
"""

import itertools
import json
import sys
from collections import Counter
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path

import pytest

from canon_trading import sale_table as sale

DATA = json.loads(Path(sale.__file__).with_suffix(".json").read_text())
NO_SALE = {"G", "GC", "RW", "FZ", "FC", "T0", "X"}
CANCEL_ASKED = {"PCR", "PCO", "PCX", "FZP", "FCP", "PCT"}
FAULT = {"FZ", "FZW", "FZP"}
COVER = {"FC", "FCW", "FCP"}
ENDS = {"X", "T0", "OX", "PCX", "PCT", "TW"}
LIVE = {"G", "GC", "RW", "FL", "WK", "PF", "PCR", "PCO"}
INVALIDATES = {"ST", "HE", "HS", "CP", "FRZ"}
MAX_QUANTITY = 3
DEPTH = 8
# Prices, for the price rule alone (ruling 18): whole steps, the level fixed. A counted quote under the
# level has its bid at or under it; one above has it over; a quote that is not counted still prices.
LEVEL = 8
PRICE_WALK = False
# Ruling 23: the leg sent again after a refusal takes the episode's floor. The two switches exist for
# the mutation cases alone.
FLOOR_AT_RESEND = True
CHECK_FLOOR = True
# The quote in whose handler a cancel was asked is the newest usable quote stored until a later one
# arrives. Read otherwise (False), a re-price answered before the next quote sends the old limit again.
QUOTE_THAT_ASKED_COUNTS = True


def through(bid, floor):
    """The chase function's price: a tick through the bid, held at the floor while there is one."""
    return bid - 1 if floor is None else max(bid - 1, floor)


LEG_LIMIT = sale.leg_limit


@dataclass(frozen=True)
class Leg:
    quantity: int
    filled: int = 0
    status: str = "flight"
    pending: bool = False
    silent_told: bool = False
    cancel_told: bool = False

    @property
    def remaining(self):
        return self.quantity - self.filled

    @property
    def cancelable(self):
        return self.status in {"working", "partial"} and not self.pending


@dataclass(frozen=True)
class Account:
    long: int = MAX_QUANTITY
    foreign: int = 0
    legs: tuple[Leg, ...] = ()
    opening: int = 0
    sold: int = 0
    owed: int = 0
    older_owed: int = 0
    total_sold: int = 0
    told: bool = False
    young: bool = False
    unread: bool = False
    bid: int = LEVEL - 1
    fresh: bool = True
    previous: int | None = None
    floor: int | None = None
    newer: bool = True

    @property
    def uncovered(self):
        return max(0, self.long - self.foreign)

    @property
    def next_quantity(self):
        # Ruling 19: every leg is the uncovered quantity read at its send.
        return self.uncovered

    @property
    def mismatch(self):
        return bool(
            self.legs and self.legs[0].cancelable and self.legs[0].remaining > self.uncovered
        )


@dataclass(frozen=True)
class World:
    state: sale.State = field(default_factory=lambda: sale.State("G", 0))
    account: Account = field(default_factory=Account)
    counted: int = 0


@dataclass(frozen=True)
class Seen:
    code: str
    formable: bool = True
    reprice_due: bool = False
    ends_filled: bool = False
    earlier_episode: bool = False
    overdue: bool = False
    bid: int | None = None
    wait_over: bool = False


def facts(account, seen, row):
    leg = account.legs[0] if account.legs else None
    reprice_due = seen.reprice_due
    if PRICE_WALK and seen.code == "QU" and row in {"WK", "PF"} and account.previous is not None:
        # `seen.reprice_due` is then only "the chase interval has passed".
        reprice_due = reprice_due and through(account.bid, account.floor) < account.previous
    return {
        "whole_cover": account.foreign >= account.long,
        "filled_part": bool(leg and leg.filled > 0),
        "long_remains": account.long > 0,
        "leg_positive": (account.uncovered if row == "G" else account.next_quantity) > 0,
        "cancelable": bool(leg and leg.cancelable),
        "formable": seen.formable,
        "reprice_due": reprice_due,
        "ends_filled": seen.ends_filled,
        "refusal_told": account.told,
        "resend_due": not account.young,
        "flight_alert_due": bool(
            seen.overdue
            and leg
            and (
                (leg.pending and not leg.cancel_told)
                or (not leg.pending and leg.status == "flight" and not leg.silent_told)
            )
        ),
    }


def account_event(account, seen):
    """Apply event meaning BEFORE asking the table, especially every actual fill."""
    event = seen.code
    leg = account.legs[0] if account.legs else None
    if event in {"QU", "QA"}:
        bid = seen.bid if seen.bid is not None else (LEVEL - 1 if event == "QU" else LEVEL + 2)
        floor = account.floor
        if (
            seen.wait_over
            and leg is not None
            and leg.cancelable
            and floor is not None
            and account.previous == floor
            and bid - 1 < floor
        ):
            floor = None  # The floor's own rule: its wait has run with the price through it.
        return replace(account, bid=bid, fresh=True, newer=True, floor=floor)
    if event == "FRZ":
        return replace(account, unread=True)
    if event == "UFZ":
        return replace(account, unread=False)
    if event == "HG":
        return replace(account, long=account.long + 1)
    if event == "HS":
        return replace(account, long=account.long - 1)
    if event == "HE":
        return replace(account, long=0)
    if event == "CP":
        return replace(account, foreign=account.foreign + 1)
    if event == "CL":
        return replace(account, foreign=account.foreign - 1)
    if event == "oLF":
        if seen.earlier_episode:
            return replace(
                account,
                long=account.long - 1,
                older_owed=account.older_owed - 1,
                total_sold=account.total_sold + 1,
            )
        return replace(
            account,
            long=account.long - 1,
            sold=account.sold + 1,
            owed=account.owed - 1,
            total_sold=account.total_sold + 1,
        )
    if event == "oVOID":
        if leg and leg.filled:
            leg = replace(leg, filled=leg.filled - 1)
        return replace(
            account,
            long=account.long + 1,
            sold=account.sold - 1,
            total_sold=account.total_sold - 1,
            legs=(leg,) if leg else (),
        )
    if leg is None:
        return account
    if event in {"oPF", "oFF"}:
        quantity = 1 if event == "oPF" else leg.remaining
        leg = replace(leg, filled=leg.filled + quantity, status="partial")
        return replace(
            account,
            told=False,
            long=account.long - quantity,
            sold=account.sold + quantity,
            total_sold=account.total_sold + quantity,
            legs=(leg,) if leg.remaining else (),
        )
    if event in {"oCXL", "oEXP", "oREJ", "oDEN"}:
        owed = leg.remaining if event in {"oCXL", "oEXP"} else 0
        # A venue's refusal starts the interval; time passing (the "~" step) ends it.
        # A venue's refusal, or a cancel by another hand, starts the interval; time passing ends it.
        elsewhere = event == "oCXL" and not leg.pending
        return replace(
            account,
            legs=(),
            owed=account.owed + owed,
            young=account.young or event == "oREJ" or elsewhere,
        )
    if event == "oUPD" and seen.ends_filled:
        # Reconciliation sets quantity to the ALREADY filled quantity, not a new fill.
        return replace(account, legs=())
    if event in {"oACC", "oCRJ", "oUPD"}:
        status = "working" if event == "oACC" else leg.status
        told = account.told and event != "oACC"
        return replace(account, told=told, legs=(replace(leg, pending=False, status=status),))
    return account


def assert_agreement(world):
    row, account = world.state.row, world.account
    assert len(account.legs) <= 1, "one_open_leg"
    assert bool(account.legs) == (row not in NO_SALE), "row_account_agree"
    if row in CANCEL_ASKED:
        assert account.legs[0].pending, "cancel_row_has_pending_cancel"
    elif account.legs:
        assert not account.legs[0].pending, "pending_cancel_has_cancel_row"
    if row == "G":
        assert account.uncovered > 0, "nothing_for_a_whole_cover"
        assert world.state.count == world.counted, "count_matches_quotes"
    else:
        assert world.state.count is None, "count_exists_only_in_counting"
    if PRICE_WALK and row in {"WK", "PF"} and account.legs[0].cancelable:
        limit, floor, bid = account.previous, account.floor, account.bid
        assert limit is not None, "a_working_leg_has_a_limit"
        if CHECK_FLOOR and floor is not None:
            assert limit >= floor, "a_leg_and_its_floor_agree"
        if limit > bid:
            due = through(bid, floor) < limit
            waiting = floor is not None and limit == floor and bid - 1 < floor
            assert due or waiting, "no_leg_rests_above_a_falling_market_in_silence"


ECHOES: dict[str, int] = {}


def advance(world, seen, n, table):
    before = world.account
    account = account_event(before, seen)
    event = seen.code
    if event in {"oPF", "oFF", "oLF"}:
        quantity = before.legs[0].remaining if event == "oFF" else 1
        assert account.long == before.long - quantity, "every_fill_changes_long"
        assert account.total_sold == before.total_sold + quantity, "every_fill_counts"
        if event == "oLF" and seen.earlier_episode:
            assert account.sold == before.sold, "earlier_episode_fill_not_charged_to_this_episode"
        else:
            assert account.sold == before.sold + quantity, "every_episode_fill_counts"
    result = sale.transition(
        world.state, n, event, facts(account, seen, world.state.row), table=table
    )
    count = world.counted
    origin = world.state.row
    unformed = "not formable" in result.when.split(" and ") or " and not formable" in result.when
    if origin in FAULT:
        assert result.state.row in FAULT | ENDS, "a_fault_of_ours_holds_to_the_next_start"
    if event == "FRZ" and origin in LIVE:
        assert result.state.row in COVER, "an_unread_cover_is_not_a_fault"
    if (
        origin in COVER
        and (event in {"oDEN", "oVOID"} or (event in {"oOTH", "oINI", "oPCN"} and result.named))
        and before.legs
    ):
        assert result.state.row in FAULT, "a_fault_while_unread_leaves_the_cover_rows"
    if event == "UFZ":
        leg = before.legs[0] if before.legs else None
        if origin == "FC":
            expected = "GC" if account.foreign >= account.long else "G"
            assert result.state.row == expected, "cover_read_again_ends_the_freeze"
        elif origin == "FCW":
            assert leg is not None
            expected = "FL" if not leg.cancelable else ("PF" if leg.filled else "WK")
            assert result.state.row == expected, "cover_read_again_restores_the_row"
        else:
            assert result.state.row == origin == "FCP", "cover_read_again_waits_for_the_cancel"
        assert not result.actions, "cover_read_again_does_nothing_else"
    if unformed:
        assert result.state.row not in FAULT, "a_leg_that_cannot_be_formed_is_not_a_freeze"
        assert sale.Action.SEND not in result.actions, "a_leg_that_cannot_be_formed_is_not_sent"
    if event == "oCXL" and origin in {"FL", "WK", "PF"}:
        assert result.state.row in {"G", "GC"}, "a_cancel_from_elsewhere_is_not_a_freeze"
    if event == "oREJ" and origin in {"FL", "WK", "PF", "PCR"}:
        assert result.state.row == "RW", "a_venue_refusal_waits"
    if (event == "oREJ" and result.state.row == "RW") or unformed:
        assert (sale.Action.ALERT in result.actions) == (not before.told), (
            "one_alert_per_holding_until_accepted"
        )
        account = replace(account, told=True)
    elif result.state.row == "RW" and origin != "RW":
        raise AssertionError("waiting_only_after_a_venue_refusal")
    if origin == "RW" and not unformed:
        assert sale.Action.ALERT not in result.actions or result.state.row in FAULT | COVER, (
            "one_alert_per_holding_until_accepted"
        )
    if origin in FAULT | COVER:
        assert sale.Action.SEND not in result.actions, "no_send_from_a_frozen_row"
    if world.state.row in CANCEL_ASKED | {"FL"} and event in {"QU", "QA", "NQ"}:
        due = facts(account, seen, world.state.row)["flight_alert_due"]
        assert (sale.Action.ALERT in result.actions) == due, "a_silent_leg_is_told_once"
        assert result.state == world.state, "a_silent_leg_is_never_resent"
        assert not {sale.Action.SEND, sale.Action.CANCEL} & set(result.actions), (
            "a_silent_leg_is_never_resent"
        )
        if due:
            told = "cancel_told" if account.legs[0].pending else "silent_told"
            account = replace(account, legs=(replace(account.legs[0], **{told: True}),))
    if event == "HE" and world.state.row in {"WK", "PF"}:
        assert sale.Action.CANCEL in result.actions, "the_holdings_end_cancels_a_working_sale"
    if event == "oLF" and world.state.row in {"WK", "PF"} and account.long <= 0:
        assert sale.Action.CANCEL in result.actions, "the_holdings_end_cancels_a_working_sale"
    if world.state.row == "G" and event == "QU":
        count += 1
    elif event in {"QA", "HG", "HS", "CP", "CL", "oLF"} or world.state.row != "G":
        count = 0
    if sale.Action.SEND in result.actions:
        quantity = account.uncovered if world.state.row == "G" else account.next_quantity
        assert not account.young, "resend_no_sooner_than_the_interval"
        assert quantity > 0, "send_quantity_positive"
        assert quantity <= account.uncovered, "send_within_uncovered"
        assert quantity == account.uncovered, "every_leg_is_the_whole_uncovered_quantity"
        assert event != "NQ", "no_timer_send"
        assert not any(leg.pending for leg in account.legs), "no_send_until_cancel_ack"
        assert not account.legs, "one_open_leg"
        assert (world.state.row, event) in (
            ("G", "QU"),
            ("RW", "QU"),
            ("PCR", "oCXL"),
        ), "send_requires_counted_quote_or_reprice_ack"
        first = world.state.row == "G"
        floored = not first and (FLOOR_AT_RESEND or world.state.row != "RW")
        limit = LEG_LIMIT(
            account.bid - 1 if account.fresh else None,
            level=LEVEL,
            previous=None if first else account.previous,
            floor=account.floor if floored else None,
        )
        assert limit is not None and limit <= LEVEL, "leg_limit_at_or_under_the_level"
        if not first:
            assert account.previous is not None, "a_later_leg_has_a_leg_before_it"
            assert limit <= account.previous, "leg_limit_at_or_under_the_previous_leg"
            if PRICE_WALK and world.state.row == "PCR" and not account.newer:
                assert limit < account.previous, "a_reprice_with_no_newer_quote_steps_down"
            if CHECK_FLOOR and account.floor is not None:
                assert limit >= account.floor, "a_leg_and_its_floor_agree"
        account = replace(
            account, previous=limit, floor=max(1, limit - 2) if first else account.floor
        )
        if world.state.row == "G":
            assert count == n, "first_leg_requires_fresh_count"
            account = replace(
                account,
                opening=quantity,
                sold=0,
                older_owed=account.older_owed + account.owed,
                owed=0,
            )
        account = replace(account, legs=account.legs + (Leg(quantity),))
        assert len(account.legs) <= 1, "one_open_leg"
    if world.state.row == "TW" and event in {"oACC", "oPF"}:
        assert sale.Action.CANCEL in result.actions, "stopped_sale_owes_cancel"
        assert result.state.row == "PCT", "stopped_sale_cancel_row"
    if sale.Action.CANCEL in result.actions:
        assert account.legs and account.legs[0].cancelable, "cancel_requires_our_cancelable_leg"
        account = replace(
            account,
            fresh=QUOTE_THAT_ASKED_COUNTS,
            newer=False,
            legs=(replace(account.legs[0], pending=True, cancel_told=False),),
        )
    if event == "MO":
        assert sale.Action.CANCEL in result.actions, "mismatch_cancels_in_same_step"
        assert sale.Action.SEND not in result.actions, "mismatch_never_sends"
    if event in {"QA", "NQ"}:
        assert sale.Action.CANCEL not in result.actions, "reprice_only_under_level"
    if event == "NQ":
        assert result.state == world.state, "timer_event_is_inert"
        assert not {sale.Action.SEND, sale.Action.CANCEL} & set(result.actions), (
            "timer_event_is_inert"
        )
        if world.state.row not in CANCEL_ASKED | {"FL"}:
            assert not result.actions, "timer_event_is_inert"
    if world.state.row != "G" and result.state.row == "G":
        assert result.state.count == 0, "count_reentry_is_zero"
    if result.state.row in NO_SALE - {"RW"}:
        # The episode is over: its previous limit and its floor bind no later sale.
        account = replace(account, previous=None, floor=None)
    if result.state.row != "G":
        count = 0
    elif world.state.row == "G" and event == "QU" and count == n:
        # Complete, and held by the interval: the count stands; the next counted quote is judged again.
        assert account.young or unformed, "a_complete_count_is_held_only_by_the_interval"
        count = n - 1
    after = World(result.state, account, count)
    for action, echo in ((sale.Action.SEND, "oINI"), (sale.Action.CANCEL, "oPCN")):
        if action in result.actions:
            ECHOES[echo] = ECHOES.get(echo, 0) + 1
            heard = sale.transition(
                after.state, n, echo, facts(account, Seen(echo), after.state.row), table=table
            )
            assert heard.state == after.state and not heard.actions, "echo_is_inert_where_caused"
            assert heard.named is None, "echo_is_expected_where_caused"
    # A late fill's post-fill holding and cover determine the row immediately.
    if event == "oLF" and world.state.row in {"G", "GC"}:
        expected = "X" if account.long <= 0 else ("GC" if account.foreign >= account.long else "G")
        assert after.state.row == expected, "late_fill_before_holding_and_cover"
    assert_agreement(after)
    return after, result.actions


def step(world, observations, n=2, table=sale.TABLE, *, sends=None):
    """Use the table's ordering; derive each event's facts from the evolving account."""
    if observations == (Seen("~"),):
        return replace(world, account=replace(world.account, young=False))
    if len(observations) == 1 and observations[0].code == "p":
        # A usable quote that is not counted (inside the gap): it changes no row and it prices.
        return replace(
            world, account=replace(world.account, bid=observations[0].bid, fresh=True, newer=True)
        )
    indexed = {str(i): seen for i, seen in enumerate(observations)}
    tagged = [sale.Event(seen.code, {str(i): True}) for i, seen in enumerate(observations)]
    ordered = sale.order_step(tagged, table=table)
    actions = []
    mismatch_checked = False
    for event in ordered:
        seen = indexed[next(iter(event.facts))]
        if event.code in {"QU", "QA", "NQ"} and world.account.mismatch:
            world, asked = advance(world, Seen("MO"), n, table)
            actions.extend(asked)
            mismatch_checked = True
        source_row = world.state.row
        world, emitted = advance(world, seen, n, table)
        if sends is not None and sale.Action.SEND in emitted:
            sends.append(f"{source_row} x {seen.code}")
        actions.extend(emitted)
    if not mismatch_checked and world.account.mismatch:
        world, asked = advance(world, Seen("MO"), n, table)
        actions.extend(asked)
    if any(seen.code in INVALIDATES for seen in observations):
        assert sale.Action.SEND not in actions, "invalidation_first"
    return world


def alternatives(world):
    """All feasible single-event callbacks, and invalidation/trigger coincidences."""
    account = world.account
    if account.young:
        yield (Seen("~"),)
    codes = ["QU", "QA", "NQ", "ST", "FRZ"]
    if world.state.row in COVER:
        # The read may succeed at any step, and the event is emitted in every such step.
        codes.append("UFZ")
    if PRICE_WALK and (account.legs or world.state.row == "RW"):
        for bid in (LEVEL - 6, LEVEL + 2):
            yield (Seen("p", bid=bid),)
    if 0 < account.long < MAX_QUANTITY:
        codes.append("HG")
    if account.long > 1:
        codes.append("HS")
    if account.long > 0:
        codes.append("HE")
    if account.foreign < MAX_QUANTITY:
        codes.append("CP")
    if account.foreign > 0:
        codes.append("CL")
    leg = account.legs[0] if account.legs else None
    if leg:
        codes.extend(("oCXL", "oEXP", "oFF", "oUPD", "oOTH", "oCRJ", "oINI", "oPCN"))
        if leg.status == "flight":
            codes.extend(("oSUB", "oDEN"))
        if leg.status in {"flight", "working"}:
            codes.append("oREJ")
        if leg.status in {"flight", "partial"} or leg.pending:
            codes.append("oACC")
        if leg.remaining > 1:
            codes.append("oPF")
    if account.sold > 0 and account.long < MAX_QUANTITY:
        codes.append("oVOID")
    if account.owed > 0 or account.older_owed > 0:
        codes.append("oLF")
    for code in codes:
        for formable, reprice_due, ends_filled in itertools.product(
            (False, True) if code in {"QU", "oCXL"} else (True,),
            (False, True) if code == "QU" else (False,),
            (False, True) if code == "oUPD" and leg and leg.filled else (False,),
        ):
            sources = (False, True) if code == "oLF" else (False,)
            for earlier in sources:
                if code == "oLF" and not (account.older_owed if earlier else account.owed):
                    continue
                seen = Seen(code, formable, reprice_due, ends_filled, earlier)
                if code in {"QU", "QA", "NQ"} and leg and (leg.status == "flight" or leg.pending):
                    yield (replace(seen, overdue=True),)
                if PRICE_WALK and code == "QU":
                    for bid in (LEVEL - 6, LEVEL - 3):
                        yield (replace(seen, bid=bid),)
                        if account.floor is not None and leg and leg.cancelable:
                            yield (replace(seen, bid=bid, wait_over=True),)
                    seen = replace(seen, bid=LEVEL)
                yield (seen,)
                if code == "QU" or (code == "oCXL" and leg and leg.pending):
                    for invalidation in sorted(INVALIDATES.intersection(codes)):
                        yield (seen, Seen(invalidation))


def enumerate_sequences(depth=DEPTH, table=sale.TABLE):
    """Breadth-first traversal; equal world/N pairs have identical future transitions."""
    starts = [
        (World(account=Account(long=q)), n) for q in range(1, MAX_QUANTITY + 1) for n in (2, 3)
    ]
    queue: deque[tuple[World, int, tuple[tuple[Seen, ...], ...]]] = deque(
        (world, n, ()) for world, n in starts
    )
    visited = set(starts)
    transitions = 0
    layers = {0: len(visited)}
    sending_transitions = Counter()
    while queue:
        world, n, history = queue.popleft()
        if len(history) == depth:
            continue
        for observations in alternatives(world):
            trace = history + (observations,)
            try:
                sends = []
                after = step(world, observations, n, table, sends=sends)
            except AssertionError as error:
                raise AssertionError(
                    f"{error}\nN={n}; before={world}; sequence={trace}; "
                    f"visited={len(visited)} transitions={transitions} depth={len(trace)}"
                ) from error
            transitions += 1
            sending_transitions.update(sends)
            key = (after, n)
            if key not in visited:
                visited.add(key)
                layers[len(trace)] = layers.get(len(trace), 0) + 1
                queue.append((after, n, trace))
    return {
        "depth": depth,
        "states": len(visited),
        "transitions": transitions,
        "layers": layers,
        "sending_transitions": dict(sending_transitions),
    }


def test_bounded_account_enumeration(record_property, tmp_path):
    stats = enumerate_sequences()
    print("ENUM", json.dumps({**stats, "echoes": ECHOES}, sort_keys=True))
    record_property("enumeration", json.dumps(stats, sort_keys=True))
    (tmp_path / "enumeration.json").write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    assert stats["states"] > 100
    assert stats["transitions"] > stats["states"]
    assert stats["sending_transitions"]["G x QU"] > 0
    assert stats["sending_transitions"]["PCR x oCXL"] > 0
    assert stats["sending_transitions"]["RW x QU"] > 0


def test_enumeration_reaches_first_and_next_leg_sends():
    first = walk(WORKING)
    assert first.state.row == "WK" and first.account.opening == MAX_QUANTITY
    next_leg = walk(REPRICING + (Seen("oCXL"),))
    assert next_leg.state.row == "FL"
    assert next_leg.account.owed == MAX_QUANTITY
    assert next_leg.account.legs == (Leg(MAX_QUANTITY),)


def walk(events, table=sale.TABLE, initial=None):
    world = World() if initial is None else initial
    for event in events:
        world = step(world, (event,), table=table)
    return world


WORKING = (Seen("QU"), Seen("QU"), Seen("oACC"))
REPRICING = WORKING + (Seen("QU", reprice_due=True),)


def mutated(row, event, **changes):
    data = deepcopy(DATA)
    for branch in data["cells"][row][event]["branches"]:
        branch.update(changes)
    return sale.table_from_data(data)


@pytest.mark.parametrize(
    "row,event,changes,history,property_name",
    [
        (
            "G",
            "QU",
            {"next": "WK", "send": False, "count": "0"},
            (Seen("QU"),),
            "row_account_agree",
        ),
        ("WK", "NQ", {"send": True}, WORKING + (Seen("NQ"),), "no_timer_send"),
        (
            "GC",
            "QU",
            {"next": "FL", "send": True},
            (Seen("CP"), Seen("CP"), Seen("CP"), Seen("QU")),
            "send_quantity_positive",
        ),
        (
            "WK",
            "QA",
            {"next": "PCR", "cancel": True},
            WORKING + (Seen("QA"),),
            "reprice_only_under_level",
        ),
        (
            "WK",
            "MO",
            {"next": "WK", "cancel": False},
            WORKING + (Seen("HS"),),
            "mismatch_cancels_in_same_step",
        ),
        (
            "PCR",
            "oCXL",
            {"next": "G", "send": False, "count": "+1"},
            REPRICING + (Seen("oCXL"),),
            "count_reentry_is_zero",
        ),
        ("G", "QU", {"next": "FL", "send": True}, (Seen("QU"),), "first_leg_requires_fresh_count"),
        ("PCR", "oPF", {"next": "PF"}, REPRICING + (Seen("oPF"),), "pending_cancel_has_cancel_row"),
        (
            "G",
            "QA",
            {"next": "FL", "send": True},
            (Seen("QA"),),
            "send_requires_counted_quote_or_reprice_ack",
        ),
        (
            "G",
            "NQ",
            {"next": "FZ"},
            (Seen("NQ"),),
            "timer_event_is_inert",
        ),
        (
            "WK",
            "MO",
            {"next": "WK", "cancel": False},
            WORKING + (Seen("CP"),),
            "mismatch_cancels_in_same_step",
        ),
        (
            "PCR",
            "QU",
            {"send": True, "next": "FL"},
            REPRICING + (Seen("QU"),),
            "no_send_until_cancel_ack|a_silent_leg_is_never_resent",
        ),
    ],
)
def test_one_cell_mutation_has_teeth(row, event, changes, history, property_name):
    with pytest.raises(AssertionError, match=property_name):
        walk(history, table=mutated(row, event, **changes))


def test_shrink_before_ack_mutation_has_teeth():
    world = walk(REPRICING)
    with pytest.raises(AssertionError, match="invalidation_first"):
        step(world, (Seen("oCXL"), Seen("HS")), table=mutated("PCR", "HS", next="PCR"))


def test_late_fill_rechecks_cover_mutation_has_teeth():
    world = walk(WORKING + (Seen("oEXP"), Seen("CP"), Seen("CP")))
    with pytest.raises(AssertionError, match="late_fill_before_holding_and_cover"):
        step(world, (Seen("oLF"),), table=mutated("G", "oLF", next="G", count="0"))


def test_late_fill_is_applied_before_mismatch_and_end():
    world = walk(REPRICING + (Seen("oCXL"), Seen("oACC")))
    world = step(world, (Seen("oLF"),))
    assert world.account.sold == 1 and world.account.long == 2
    assert world.state.row == "PCO" and world.account.legs[0].pending
    world = step(world, (Seen("oLF"),))
    world = step(world, (Seen("oLF"),))
    assert world.account.sold == 3 and world.account.long == 0
    assert world.state.row == "PCX"


def test_one_open_leg_assertion_has_teeth():
    # Multiplicity has its own assertion even when a send also has the wrong origin.
    data = deepcopy(DATA)
    data["cells"]["WK"]["QU"]["branches"] = [
        {"when": "always", "next": "FL", "send": True, "cancel": False, "alert": False}
    ]
    world = World(sale.State("WK"), Account(legs=(Leg(3, status="working"),), opening=3))
    with pytest.raises(AssertionError, match="one_open_leg"):
        step(world, (Seen("QU"),), table=sale.table_from_data(data))


def test_zero_size_first_leg_mutation_has_teeth():
    data = deepcopy(DATA)
    for branch in data["cells"]["G"]["QU"]["branches"]:
        if branch["when"] == "count_complete and resend_due and not leg_positive and whole_cover":
            branch.update(next="FL", send=True)
    # A defensive input boundary: the guard still refuses zero when a caller's
    # count precedes its cover observation. Normal enumeration re-reads the cover.
    world = World(sale.State("G", 1), Account(long=1, foreign=1), counted=1)
    with pytest.raises(AssertionError, match="send_quantity_positive"):
        step(world, (Seen("QU"),), table=sale.table_from_data(data))


def test_shrink_to_whole_cover_mutation_has_teeth():
    data = deepcopy(DATA)
    data["cells"]["G"]["HS"]["branches"] = [
        {
            "when": "always",
            "next": "G",
            "count": "0",
            "send": False,
            "cancel": False,
            "alert": False,
        }
    ]
    with pytest.raises(AssertionError, match="nothing_for_a_whole_cover"):
        enumerate_sequences(depth=3, table=sale.table_from_data(data))


def test_stopped_acceptance_mutation_has_teeth():
    with pytest.raises(AssertionError, match="stopped_sale_owes_cancel"):
        enumerate_sequences(depth=4, table=mutated("TW", "oACC", next="TW", cancel=False))


@pytest.mark.parametrize("event", ["oACC", "oPF"])
def test_stopped_flight_asks_cancel_as_soon_as_possible(event):
    world = World(sale.State("TW"), Account(legs=(Leg(3),), opening=3))
    after = step(world, (Seen(event),))
    assert after.state.row == "PCT" and after.account.legs[0].pending


def test_stopped_mismatch_is_emitted_and_has_teeth():
    world = World(sale.State("TW"), Account(long=2, legs=(Leg(3, status="working"),), opening=3))
    after = step(world, (Seen("NQ"),))
    assert after.state.row == "PCT" and after.account.legs[0].pending
    with pytest.raises(AssertionError, match="mismatch_cancels_in_same_step"):
        step(world, (Seen("NQ"),), table=mutated("TW", "MO", next="TW", cancel=False))


@pytest.mark.parametrize("event", ["oPF", "oFF"])
def test_dropped_working_fill_is_detected_before_table_decisions(event, monkeypatch):
    world = World(sale.State("WK"), Account(legs=(Leg(3, status="working"),), opening=3))
    # This checks the account harness, not a table-cell mutation: cells contain
    # no fill arithmetic. Dropping a fill must be noticed before any row decision.
    monkeypatch.setattr(sys.modules[__name__], "account_event", lambda account, seen: account)
    with pytest.raises(AssertionError, match="every_fill_changes_long"):
        step(world, (Seen(event),))


@pytest.mark.parametrize(
    "quantity,bound", [(4, "send_within_uncovered"), (2, "every_leg_is_the_whole")]
)
def test_quantity_oracles_have_teeth(quantity, bound, monkeypatch):
    # A cell emits SEND but has no quantity field to mutate. These are explicit harness mutations,
    # independent evidence for the two arithmetic assertions: never more than is uncovered, and the
    # whole of it (ruling 19).
    world = World(
        sale.State("PCR"),
        Account(
            long=3, opening=2, previous=3, floor=1, legs=(Leg(2, status="working", pending=True),)
        ),
    )
    monkeypatch.setattr(Account, "next_quantity", property(lambda account: quantity))
    with pytest.raises(AssertionError, match=bound):
        step(world, (Seen("oCXL"),))


REFUSED = (Seen("QU"), Seen("QU"), Seen("oREJ"))


def mutated_branch(row, event, when, **changes):
    data = deepcopy(DATA)
    hit = [b for b in data["cells"][row][event]["branches"] if b["when"] == when]
    assert len(hit) == 1
    hit[0].update(changes)
    return sale.table_from_data(data)


@pytest.mark.parametrize(
    "row,event,when,changes,history,property_name",
    [
        # A resend before the interval has passed.
        (
            "RW",
            "QU",
            "not resend_due",
            {"next": "FL", "send": True},
            REFUSED + (Seen("QU"),),
            "resend_no_sooner_than_the_interval",
        ),
        # A first leg two quotes after the price crossed back, inside the interval.
        (
            "G",
            "QU",
            "count_complete and not resend_due",
            {"next": "FL", "send": True},
            REFUSED + (Seen("QA"), Seen("QU"), Seen("QU")),
            "resend_no_sooner_than_the_interval",
        ),
        # A second ALERT for a holding no sale of which was accepted since the first.
        (
            "FL",
            "oREJ",
            "refusal_told",
            {"alert": True},
            REFUSED + (Seen("~"), Seen("QU"), Seen("oREJ")),
            "one_alert_per_holding_until_accepted",
        ),
        # No ALERT at the first refusal.
        (
            "FL",
            "oREJ",
            "not refusal_told",
            {"alert": False},
            REFUSED,
            "one_alert_per_holding_until_accepted",
        ),
        # An ALERT said by the waiting row itself.
        (
            "RW",
            "QU",
            "not resend_due",
            {"alert": True},
            REFUSED + (Seen("QU"),),
            "one_alert_per_holding_until_accepted",
        ),
        # A resend on a timer.
        (
            "RW",
            "NQ",
            "always",
            {"next": "FL", "send": True},
            REFUSED + (Seen("~"), Seen("NQ")),
            "no_timer_send",
        ),
        # A resend on a quote over the level.
        (
            "RW",
            "QA",
            "not whole_cover",
            {"next": "FL", "send": True},
            REFUSED + (Seen("~"), Seen("QA")),
            "send_requires_counted_quote_or_reprice_ack",
        ),
        # A row with no sale of ours that the table says holds one.
        ("FL", "oREJ", "not refusal_told", {"next": "WK"}, REFUSED, "a_venue_refusal_waits"),
    ],
)
def test_refusal_mutations_have_teeth(row, event, when, changes, history, property_name):
    with pytest.raises(AssertionError, match=property_name):
        walk(history, table=mutated_branch(row, event, when, **changes))


def test_resend_of_nothing_mutation_has_teeth():
    # The holding is wholly covered while waiting; a table that sends anyway sends a size of zero.
    table = mutated_branch(
        "RW", "QU", "resend_due and not leg_positive and whole_cover", next="FL", send=True
    )
    world = World(sale.State("RW"), Account(long=1, foreign=1, opening=1, told=True))
    with pytest.raises(AssertionError, match="send_quantity_positive"):
        step(world, (Seen("QU"),), table=table)


def test_resend_is_what_is_uncovered_then():
    # Two of three sold, then refused: the resend is one share, and none when a foreign sell covers it.
    world = walk(
        (Seen("QU"), Seen("QU"), Seen("oACC"), Seen("oPF"), Seen("oPF"), Seen("oACC"), Seen("oREJ"))
    )
    assert world.state.row == "RW" and world.account.long == 1 and world.account.told
    world = step(world, (Seen("~"),))
    sent = step(world, (Seen("QU"),))
    assert sent.state.row == "FL" and sent.account.legs == (Leg(1),)
    covered = step(step(world, (Seen("CP"),)), (Seen("QU"),))
    assert covered.state.row == "GC" and not covered.account.legs


def test_one_alert_across_refusals_and_a_new_one_after_an_acceptance():
    world = walk(REFUSED)
    assert world.account.told and world.account.young
    for _ in range(3):
        world = step(world, (Seen("~"),))
        world = step(world, (Seen("QU"),))
        assert world.state.row == "FL"
        world = step(world, (Seen("oREJ"),))
        assert world.state.row == "RW" and world.account.told
    world = step(step(world, (Seen("~"),)), (Seen("QU"),))
    world = step(world, (Seen("oACC"),))
    assert world.state.row == "WK" and not world.account.told
    world = step(world, (Seen("oREJ"),))
    assert world.state.row == "RW" and world.account.told


IN_FLIGHT = (Seen("QU"), Seen("QU"))


@pytest.mark.parametrize(
    "event,when,changes,history,property_name",
    [
        # A silent leg that is never told.
        (
            "QU",
            "flight_alert_due",
            {"alert": False},
            IN_FLIGHT + (Seen("QU", overdue=True),),
            "a_silent_leg_is_told_once",
        ),
        # An ALERT on every quote.
        (
            "QA",
            "not flight_alert_due",
            {"alert": True},
            IN_FLIGHT + (Seen("QA"),),
            "a_silent_leg_is_told_once",
        ),
        # A second sell beside a leg whose outcome is unknown.
        (
            "QU",
            "flight_alert_due",
            {"send": True},
            IN_FLIGHT + (Seen("QU", overdue=True),),
            "a_silent_leg_is_never_resent",
        ),
        # The silent leg given up as refused.
        (
            "QU",
            "flight_alert_due",
            {"next": "RW"},
            IN_FLIGHT + (Seen("QU", overdue=True),),
            "waiting_only_after_a_venue_refusal|a_silent_leg_is_never_resent",
        ),
    ],
)
def test_silent_leg_mutations_have_teeth(event, when, changes, history, property_name):
    with pytest.raises(AssertionError, match=property_name):
        walk(history, table=mutated_branch("FL", event, when, **changes))


def test_a_silent_leg_is_told_once_and_works_when_its_answer_comes():
    world = walk(IN_FLIGHT + (Seen("QU", overdue=True),))
    assert world.state.row == "FL" and world.account.legs[0].silent_told
    again = step(world, (Seen("QU", overdue=True),))
    assert again == world
    assert step(world, (Seen("oACC"),)).state.row == "WK"
    refused = step(world, (Seen("oREJ"),))
    assert refused.state.row == "RW" and refused.account.told


UNREAD = WORKING + (Seen("FRZ"),)


@pytest.mark.parametrize(
    "row,event,when,changes,history,property_name",
    [
        # An unread cover that latches as a fault.
        ("G", "FRZ", "always", {"next": "FZ"}, (Seen("FRZ"),), "an_unread_cover_is_not_a_fault"),
        ("WK", "FRZ", "always", {"next": "FZW"}, UNREAD, "an_unread_cover_is_not_a_fault"),
        # The cover reads again and nothing ends.
        (
            "FC",
            "UFZ",
            "not whole_cover",
            {"next": "FC"},
            (Seen("FRZ"), Seen("UFZ")),
            "cover_read_again_ends_the_freeze",
        ),
        # The leg kept working comes back in a row its state does not give.
        (
            "FCW",
            "UFZ",
            "cancelable and not filled_part",
            {"next": "PF"},
            UNREAD + (Seen("UFZ"),),
            "cover_read_again_restores_the_row",
        ),
        # The cover reading again sends.
        (
            "FC",
            "UFZ",
            "not whole_cover",
            {"next": "FL", "send": True},
            (Seen("FRZ"), Seen("UFZ")),
            "cover_read_again_ends_the_freeze",
        ),
        # A fault of ours ended by a quote.
        (
            "FZW",
            "QU",
            "always",
            {"next": "WK"},
            WORKING + (Seen("oOTH"), Seen("QU")),
            "a_fault_of_ours_holds_to_the_next_start",
        ),
        # A fault of ours while the cover is unread stays among the rows the cover's read ends.
        (
            "FCW",
            "oOTH",
            "always",
            {"next": "FCW", "alert": False},
            UNREAD + (Seen("oOTH"),),
            "a_fault_while_unread_leaves_the_cover_rows",
        ),
        # The leg our cut left working beside an unread cover.
        (
            "FCW",
            "MO",
            "always",
            {"next": "FCW", "cancel": False},
            UNREAD + (Seen("HS"),),
            "mismatch_cancels_in_same_step",
        ),
        # A leg that cannot be formed freezes.
        (
            "G",
            "QU",
            "count_complete and resend_due and leg_positive and not formable and not refusal_told",
            {"next": "FZ"},
            (Seen("QU"), Seen("QU", formable=False)),
            "a_leg_that_cannot_be_formed_is_not_a_freeze",
        ),
        # A cancel from elsewhere freezes.
        (
            "WK",
            "oCXL",
            "not whole_cover",
            {"next": "FZ"},
            WORKING + (Seen("oCXL"),),
            "a_cancel_from_elsewhere_is_not_a_freeze",
        ),
        # A venue's refusal of a leg whose re-price cancel is pending freezes.
        (
            "PCR",
            "oREJ",
            "not refusal_told",
            {"next": "FZ"},
            REPRICING + (Seen("oREJ"),),
            "a_venue_refusal_waits",
        ),
    ],
)
def test_freeze_class_mutations_have_teeth(row, event, when, changes, history, property_name):
    with pytest.raises(AssertionError, match=property_name):
        walk(history, table=mutated_branch(row, event, when, **changes))


def test_an_unread_cover_ends_when_it_reads_and_a_new_count_sells():
    world = walk((Seen("QU"), Seen("FRZ")))
    assert world.state.row == "FC" and world.account.unread
    world = walk((Seen("QU"), Seen("NQ")), initial=world)
    assert world.state.row == "FC" and not world.account.legs
    world = walk((Seen("UFZ"),), initial=world)
    assert world.state == sale.State("G", 0) and not world.account.unread
    world = walk((Seen("QU"), Seen("QU")), initial=world)
    assert world.state.row == "FL" and world.account.legs == (Leg(MAX_QUANTITY),)


def test_an_unread_cover_beside_a_leg_too_large_cuts_the_leg_then_ends_at_zero():
    world = walk(UNREAD)
    assert world.state.row == "FCW"
    world = walk((Seen("CP"),), initial=world)
    assert world.state.row == "FCP" and world.account.legs[0].pending
    world = walk((Seen("UFZ"),), initial=world)
    assert world.state.row == "FCP"
    world = walk((Seen("oCXL"),), initial=world)
    assert world.state.row == "FC" and not world.account.legs
    world = walk((Seen("UFZ"),), initial=world)
    assert world.state == sale.State("G", 0)


def test_a_cancel_from_elsewhere_starts_the_interval_and_then_a_new_count_sells():
    world = walk(WORKING + (Seen("oCXL"),))
    assert world.state == sale.State("G", 0) and world.account.young
    world = walk((Seen("QU"), Seen("QU"), Seen("QU")), initial=world)
    assert world.state.row == "G" and not world.account.legs
    world = walk((Seen("~"), Seen("QU")), initial=world)
    assert world.state.row == "FL" and world.account.legs == (Leg(MAX_QUANTITY),)


def test_a_sale_inside_the_interval_after_a_cancel_from_elsewhere_has_teeth():
    table = mutated_branch("G", "QU", "count_complete and not resend_due", next="FL", send=True)
    with pytest.raises(AssertionError, match="resend_no_sooner_than_the_interval"):
        walk(WORKING + (Seen("oCXL"), Seen("QU"), Seen("QU")), table=table)


def test_shares_added_while_our_leg_works_are_in_the_next_leg_sent():
    """Ruling 19: growth asks no cancel, and the next leg is the whole uncovered quantity."""
    start = World(account=Account(long=2))
    world = walk(WORKING + (Seen("HG"),), initial=start)
    assert world.state.row == "WK" and world.account.legs == (Leg(2, status="working"),)
    world = walk((Seen("QU", reprice_due=True), Seen("oCXL")), initial=world)
    assert world.state.row == "FL" and world.account.legs == (Leg(3),)


def test_an_unanswered_cancel_is_told_once_and_the_next_leg_waits_for_its_answer():
    world = walk(REPRICING)
    assert world.state.row == "PCR"
    told = walk((Seen("QA", overdue=True),), initial=world)
    assert told.state.row == "PCR" and told.account.legs[0].cancel_told
    assert walk((Seen("QU", overdue=True),), initial=told) == replace(
        told, account=replace(told.account, bid=LEVEL - 1)
    )
    assert walk((Seen("oCXL"),), initial=told).state.row == "FL"


def test_the_holdings_end_cancels_our_working_sale_in_that_step():
    world = walk(WORKING + (Seen("HE"),))
    assert world.state.row == "PCX" and world.account.legs[0].pending
    assert walk((Seen("oCXL"),), initial=world).state.row == "X"


@pytest.mark.parametrize(
    "row,event,when,changes,history,property_name",
    [
        (
            "WK",
            "HE",
            "always",
            {"next": "OX", "cancel": False},
            WORKING + (Seen("HE"),),
            "the_holdings_end_cancels",
        ),
        (
            "PCR",
            "QU",
            "flight_alert_due",
            {"alert": False},
            REPRICING + (Seen("QU", overdue=True),),
            "a_silent_leg_is_told_once",
        ),
        (
            "PCR",
            "QA",
            "not flight_alert_due",
            {"alert": True},
            REPRICING + (Seen("QA"),),
            "a_silent_leg_is_told_once",
        ),
        (
            "PCO",
            "QU",
            "flight_alert_due",
            {"next": "G", "count": "0"},
            WORKING + (Seen("HS"), Seen("QU", overdue=True)),
            "a_silent_leg_is_never_resent",
        ),
    ],
)
def test_seventh_form_mutations_have_teeth(row, event, when, changes, history, property_name):
    with pytest.raises(AssertionError, match=property_name):
        walk(history, table=mutated_branch(row, event, when, **changes))


def uncapped(which):
    def limit(stepped, *, level, previous, floor):
        price = previous if stepped is None else stepped
        if which != "floor" and floor is not None:
            price = max(price, floor)
        if "level" not in which:
            price = min(price, level)
        if "previous" not in which and previous is not None:
            price = min(price, previous)
        return price

    return limit


def test_the_price_rule_holds_at_every_send_over_the_enumeration(monkeypatch, tmp_path):
    """Ruling 18's property: at every send the limit is at or under the level and at or under the
    previous leg's limit, with quotes above, under and far under the level, counted or not."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PRICE_WALK", True)
    monkeypatch.setattr(module, "MAX_QUANTITY", 1)
    stats = enumerate_sequences(depth=7)
    print("PRICE ENUM", json.dumps(stats, sort_keys=True))
    (tmp_path / "price-enumeration.json").write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n"
    )
    assert stats["sending_transitions"]["PCR x oCXL"] > 0
    assert stats["sending_transitions"]["RW x QU"] > 0


@pytest.mark.parametrize(
    "which,property_name",
    [
        ("previous", "leg_limit_at_or_under_the_previous_leg"),
        ("level and previous", "leg_limit_at_or_under_the_(level|previous_leg)"),
        ("floor", "a_leg_and_its_floor_agree"),
    ],
)
def test_a_price_rule_without_one_cap_fails_the_enumeration(which, property_name, monkeypatch):
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PRICE_WALK", True)
    monkeypatch.setattr(module, "MAX_QUANTITY", 1)
    monkeypatch.setattr(module, "LEG_LIMIT", uncapped(which))
    with pytest.raises(AssertionError, match=property_name):
        enumerate_sequences(depth=7)


def test_the_level_cap_binds_by_itself_where_no_walk_can_reach():
    """Over the table a first leg is priced from a counted quote under the level and later legs only
    step down, so the level's cap never binds alone in the walk; the function still holds it."""
    assert LEG_LIMIT(LEVEL + 1, level=LEVEL, previous=None, floor=None) == LEVEL
    assert LEG_LIMIT(None, level=LEVEL, previous=LEVEL - 1, floor=None) == LEVEL - 1
    assert LEG_LIMIT(None, level=LEVEL, previous=None, floor=None) is None


def test_a_first_legs_floor_lifts_it_and_never_above_the_level():
    """The gain lock's applied floor is a first leg's floor; the level's cap is applied after it."""
    assert LEG_LIMIT(3, level=LEVEL, previous=None, floor=5) == 5
    assert LEG_LIMIT(3, level=4, previous=None, floor=5) == 4
    assert LEG_LIMIT(3, level=LEVEL, previous=4, floor=5) == 4


@pytest.mark.parametrize(
    "check_floor,property_name",
    [
        (True, "a_leg_and_its_floor_agree"),
        (False, "no_leg_rests_above_a_falling_market_in_silence"),
    ],
)
def test_a_resend_priced_without_the_episodes_floor_fails_the_enumeration(
    check_floor, property_name, monkeypatch
):
    """The second review's P1: a leg sent again under the episode's floor is never re-priced. With
    the agreement of leg and floor unchecked, the silence itself is what the walk finds."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PRICE_WALK", True)
    monkeypatch.setattr(module, "MAX_QUANTITY", 1)
    monkeypatch.setattr(module, "FLOOR_AT_RESEND", False)
    monkeypatch.setattr(module, "CHECK_FLOOR", check_floor)
    with pytest.raises(AssertionError, match=property_name):
        enumerate_sequences(depth=7)


def test_the_floors_wait_releases_and_the_leg_is_then_repriced():
    """A leg at its floor with the price through it: no re-price until the wait has run, then one."""
    module = sys.modules[__name__]
    patch = pytest.MonkeyPatch()
    patch.setattr(module, "PRICE_WALK", True)
    try:
        start = walk((Seen("QU", bid=LEVEL), Seen("QU", bid=LEVEL), Seen("oACC")))
        assert (start.account.previous, start.account.floor) == (LEVEL - 1, LEVEL - 3)
        fallen = walk(
            (Seen("QU", bid=LEVEL - 6, reprice_due=True), Seen("oCXL"), Seen("oACC")), initial=start
        )
        assert fallen.state.row == "WK" and fallen.account.previous == LEVEL - 3, fallen
        held = walk((Seen("QU", bid=LEVEL - 6, reprice_due=True),), initial=fallen)
        assert held.state.row == "WK", "at the floor the chase asks nothing until the wait has run"
        freed = walk((Seen("QU", bid=LEVEL - 6, reprice_due=True, wait_over=True),), initial=fallen)
        assert freed.state.row == "PCR" and freed.account.floor is None
        after = walk((Seen("p", bid=LEVEL - 6), Seen("oCXL")), initial=freed)
        assert after.state.row == "FL" and after.account.previous == LEVEL - 7
    finally:
        patch.undo()


def test_a_silent_leg_is_told_on_a_timer_step_once():
    world = walk(IN_FLIGHT + (Seen("NQ", overdue=True),))
    assert world.state.row == "FL" and world.account.legs[0].silent_told
    assert walk((Seen("NQ", overdue=True),), initial=world) == world


def test_a_timer_step_that_keeps_a_commands_silence_quiet_has_teeth():
    with pytest.raises(AssertionError, match="a_silent_leg_is_told_once"):
        walk(
            IN_FLIGHT + (Seen("NQ", overdue=True),),
            table=mutated_branch("FL", "NQ", "flight_alert_due", alert=False),
        )


def test_a_reprice_that_forgets_the_quote_that_asked_it_fails_the_enumeration(monkeypatch):
    """Were the quote that asked the cancel not the newest usable quote at the send, a re-price whose
    acknowledgement arrives before the next quote would send the replaced leg's limit again."""
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "PRICE_WALK", True)
    monkeypatch.setattr(module, "MAX_QUANTITY", 1)
    monkeypatch.setattr(module, "QUOTE_THAT_ASKED_COUNTS", False)
    with pytest.raises(AssertionError, match="a_reprice_with_no_newer_quote_steps_down"):
        enumerate_sequences(depth=7)
