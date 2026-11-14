"""Pure interpreter of the plain-limit sale contract in ``sale_table.json``.

Pin: nautilus-trader v2.0.0rc8 at d53849b216e2, whose order transitions are
crates/model/src/orders/mod.rs:207-302, with reconciliation at 1314-1325 and
late fills at 1367-1389. Canonical cells SHA256:
f7bbb487c49c83ad4545501f31b00a490c8292c439b0d2b332272eeefe8bf1ea.
Order-event inventory: crates/model/src/events/order/any.rs:39-57 and
crates/trading/src/strategy/mod.rs:1445-1462. Order events, including local echoes,
are judged in the following step, in the table's step order.

The adjacent data file is loaded once; transitions and whole steps only read
their arguments and the immutable table. No engine, account, clock, cache,
environment or quantity calculation belongs here. ``leg_limit`` implements only
the table's pure price rule; the caller supplies the chase price. Counts of one are
outside this contract. ``named`` is a diagnostic for the caller, never an I/O
operation. ``Event.facts`` describes the facts at that event in the ordered step.

Named caller limits: added shares wait for the next leg; the acknowledgement's
next leg waits up to one STEP_NS for a step; a cancel the venue never answers
holds its row, and so its holding's slot, until it is answered; remaining
quantity after a voided fill that did not reopen its order is unread, with the
holding frozen. A holding that ends ends its slot from every row, a stopped
sale's included: when its leg ends with the long gone the row is X, and nothing
of it, its level least of all, is kept. The owner's hold is the stop (ST) and
its release is RL, which never lifts a recorded fault. A cancel that repeats
(an ended or stopped holding's open sale) is asked only when `cancel_due`, and
its refusal's ALERT is written once per leg (`cancel_refusal_told`); the caller
raises ST at every step while a stopped holding's sale is open.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from json import loads
from pathlib import Path
from types import MappingProxyType
from typing import Any


def leg_limit[Value: (Decimal, int)](
    stepped: Value | None,
    *,
    level: Value,
    previous: Value | None,
    floor: Value | None,
) -> Value | None:
    """Lift the usable quote's chase price to its floor, then apply both caps."""
    if stepped is None:
        return previous
    price = stepped if floor is None else max(stepped, floor)
    price = min(price, level)
    return price if previous is None else min(price, previous)


class SaleTableError(ValueError):
    """A named contract violation; no branch is guessed."""


class UnknownRow(SaleTableError):
    pass


class UnknownEvent(SaleTableError):
    pass


class InvalidState(SaleTableError):
    pass


class InvalidFacts(SaleTableError):
    pass


class MissingPredicate(SaleTableError):
    pass


class NoMatchingBranch(SaleTableError):
    pass


class AmbiguousBranch(SaleTableError):
    pass


class Action(StrEnum):
    SEND = "SEND A LEG"
    CANCEL = "CANCEL OURS"
    ALERT = "ALERT"


@dataclass(frozen=True)
class State:
    row: str
    count: int | None = None


@dataclass(frozen=True)
class Event:
    code: str
    facts: Mapping[str, bool]


@dataclass(frozen=True)
class Transition:
    state: State
    actions: tuple[Action, ...]
    when: str
    named: str | None = None


@dataclass(frozen=True)
class Step:
    state: State
    events: tuple[str, ...]
    transitions: tuple[Transition, ...]

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(action for result in self.transitions for action in result.actions)


@dataclass(frozen=True)
class Branch:
    when: str
    terms: tuple[tuple[str, bool], ...]
    next_row: str
    actions: tuple[Action, ...]
    count: str


@dataclass(frozen=True)
class Cell:
    occurs: str
    branches: tuple[Branch, ...]
    needed: frozenset[str]
    note: str


@dataclass(frozen=True)
class Table:
    rows: tuple[str, ...]
    events: tuple[str, ...]
    predicates: frozenset[str]
    cells: Mapping[tuple[str, str], Cell]
    priorities: Mapping[str, int]
    quotes: frozenset[str]
    order_columns: Mapping[str, tuple[str, ...]]
    catch_all: str


def table_from_data(data: Mapping[str, Any]) -> Table:
    """Compile the JSON's conjunctions without evaluating Python expressions."""
    predicates = frozenset(data["predicates"])
    cells = {}
    for row, events in data["cells"].items():
        for event, raw in events.items():
            branches = []
            for branch in raw["branches"]:
                terms = []
                if branch["when"] != "always":
                    for term in branch["when"].split(" and "):
                        name = term.removeprefix("not ")
                        if name not in predicates:
                            raise InvalidFacts(f"unknown predicate in {row} x {event}: {term}")
                        terms.append((name, not term.startswith("not ")))
                if branch["next"] not in data["rows"]:
                    raise UnknownRow(branch["next"])
                count = branch.get("count", "=")
                if count not in {"0", "+1", "="}:
                    raise InvalidState(f"unknown count operation: {count}")
                branches.append(
                    Branch(
                        branch["when"],
                        tuple(terms),
                        branch["next"],
                        tuple(
                            action
                            for key, action in (
                                ("send", Action.SEND),
                                ("cancel", Action.CANCEL),
                                ("alert", Action.ALERT),
                            )
                            if branch[key]
                        ),
                        count,
                    )
                )
            cells[row, event] = Cell(
                raw["occurs"],
                tuple(branches),
                frozenset(name for branch in branches for name, _ in branch.terms),
                raw.get("note", ""),
            )
    priorities = {}
    quotes = frozenset(e for e, spec in data["events"].items() if spec["kind"] == "market")
    for rank, group in enumerate(data["step_order"]):
        if group == "our order's events, as they arrived":
            priorities.update(
                (e, rank) for e, spec in data["events"].items() if spec["kind"] == "order"
            )
        elif group == "QU, QA or NQ":
            priorities.update((e, rank) for e in quotes)
        elif group in data["events"]:
            priorities[group] = rank
        else:
            raise UnknownEvent(f"unknown step-order group: {group}")
    if set(priorities) != set(data["events"]):
        raise UnknownEvent("step order does not cover every event")
    return Table(
        tuple(data["rows"]),
        tuple(data["events"]),
        predicates,
        MappingProxyType(cells),
        MappingProxyType(priorities),
        quotes,
        MappingProxyType(
            {
                kind: tuple(spec["columns"])
                for kind, spec in data["order_event_inventory"]["kinds"].items()
            }
        ),
        next(
            spec["columns"][0]
            for spec in data["order_event_inventory"]["kinds"].values()
            if spec["treatment"] == "cannot occur"
        ),
    )


TABLE = table_from_data(loads(Path(__file__).with_suffix(".json").read_text()))


def order_event_column(
    kind: str,
    *,
    leg_ended: bool = False,
    leaves_remainder: bool = False,
    table: Table = TABLE,
) -> str:
    """Use only the inventory; its folded fill columns are late, partial, full."""
    columns = table.order_columns.get(kind.removeprefix("Order"), (table.catch_all,))
    if len(columns) == 1:
        return columns[0]
    return columns[0 if leg_ended else 1 if leaves_remainder else 2]


def _validate_state(state: State, n: int, table: Table) -> None:
    if state.row not in table.rows:
        raise UnknownRow(f"unknown row: {state.row}")
    if type(n) is not int or n <= 1:
        raise InvalidState("N must be an integer above one")
    if state.row == "G":
        if type(state.count) is not int or not 0 <= state.count < n:
            raise InvalidState("G count must be an integer from zero through N-1")
    elif state.count is not None:
        raise InvalidState(f"the count exists in G alone, not {state.row}")


def transition(
    state: State,
    n: int,
    event: str,
    facts: Mapping[str, bool],
    *,
    table: Table = TABLE,
) -> Transition:
    """Apply exactly one cell, computing count_complete from the state and N."""
    _validate_state(state, n, table)
    if event not in table.events:
        raise UnknownEvent(f"unknown event: {event}")
    for name, value in facts.items():
        if name == "count_complete" or name not in table.predicates or type(value) is not bool:
            raise InvalidFacts(f"invalid caller predicate: {name}")
    cell = table.cells[state.row, event]
    label = f"{state.row} x {event}"
    if cell.occurs in {"no", "n/a"}:
        reason = "CANNOT OCCUR" if cell.occurs == "no" else "NOT EMITTED"
        return Transition(state, (), "always", f"{label}: {reason}; {cell.note}")
    values = {**facts, "count_complete": state.row == "G" and state.count == n - 1}
    missing = cell.needed - values.keys()
    if missing:
        raise MissingPredicate(f"{label}: missing {', '.join(sorted(missing))}")
    matches = [b for b in cell.branches if all(values[name] == value for name, value in b.terms)]
    if not matches:
        raise NoMatchingBranch(f"{label}: no branch holds")
    if len(matches) != 1:
        raise AmbiguousBranch(f"{label}: {len(matches)} branches hold")
    branch = matches[0]
    count = None
    if branch.next_row == "G":
        count = state.count if state.count is not None else 0
        if branch.count == "0":
            count = 0
        elif branch.count == "+1":
            count += 1
    named = f"{label}: UNEXPECTED; {cell.note}" if cell.occurs == "unexpected" else None
    return Transition(State(branch.next_row, count), branch.actions, branch.when, named)


def order_step(events: Iterable[Event], *, table: Table = TABLE) -> tuple[Event, ...]:
    """Invalidations first, stable order-event arrivals, mismatch, then one quote."""
    seen = tuple(events)
    for event in seen:
        if event.code not in table.events:
            raise UnknownEvent(f"unknown event: {event.code}")
    if sum(event.code in table.quotes for event in seen) > 1:
        raise InvalidFacts("a step has at most one quote event (QU, QA or NQ)")
    return tuple(sorted(seen, key=lambda event: table.priorities[event.code]))


def apply_step(
    state: State,
    n: int,
    events: Iterable[Event],
    *,
    table: Table = TABLE,
) -> Step:
    """Call transition once per event, in the contract's step order."""
    _validate_state(state, n, table)
    ordered = order_step(events, table=table)
    results = []
    for event in ordered:
        result = transition(state, n, event.code, event.facts, table=table)
        results.append(result)
        state = result.state
    return Step(state, tuple(event.code for event in ordered), tuple(results))
