"""The entry switch: one arming file, read at each buy decision by the strategy itself.

The lead's decision of 2026-11-12, recorded in ADR 0115: NautilusTrader's `REDUCING` state is not what
stops entries. A strategy reads this file
immediately before it would submit a buy and submits none while it says off. Sells never read it.

The file follows `canon/paper-schedule` (ADR 0050): its first line is the value and nothing else, `on` or
`off`; the rest is a note. One format serves every strategy. What an absent file means goes by account
(`absent_is_on`): on for paper accounts A and B, whose nodes buy by themselves; off for account C and for
a live node, where an automatic buy waits until the file says on (the lead, 2026-11-12). A file that is
there and cannot be read, whose first line is neither value, whose folder is missing or which is a link
to nothing is off, by name, for every account. Nothing here writes the file, caches a value or reads
anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LIVE_ARMING_FILE = Path("canon/live-arming")
"""Where a live node's arming file is to be, relative to the checkout of `main`. ADR 0110's change (e)
makes it a tracked file on `main` in the manner of `canon/paper-schedule`, off by default, which only a
landing changes; no such file is tracked yet, and for a live node an absent file is off (ADR 0115).
`live_arming_path` names it absolutely. A paper node names no file unless a run gives it one, so a
switch set for live never gates paper."""
ON = "on"
OFF = "off"

ABSENT_IS_ON = frozenset({"tests", "windows"})
"""The accounts on which no arming file means armed: paper accounts A and B by their key-store names
(ADR 0069), whose nodes buy by themselves. Every other name, account C (`protect`) and a live node
among them, is off until a file says on."""


def absent_is_on(account: str | None) -> bool:
    """Whether a node on `account` may buy with no arming file: accounts A and B only."""
    return account in ABSENT_IS_ON


def live_arming_path(main_checkout: Path) -> Path:
    """The live arming file, absolute, under the checkout of `main` the caller names: the one the
    maintenance agent keeps current (ADR 0065), as `canon/paper-schedule` is read from `main`
    (ADR 0050), never a branch's checkout a node happens to run from."""
    return main_checkout.resolve() / LIVE_ARMING_FILE


@dataclass(frozen=True)
class Arming:
    """One read of the switch: whether a buy may be submitted, and what was read, for the record."""

    armed: bool
    read: str


def read_arming_for(account: str | None, path: Path | None) -> Arming:
    """Read the switch now for a node on `account`: what no file means is the account's (`absent_is_on`)."""
    return read_arming(path, absent_is_on=absent_is_on(account))


def read_arming(path: Path | None, *, absent_is_on: bool) -> Arming:
    """Read the switch now. `path` None, or a file that is not there in a folder that is, is
    `absent_is_on`; a first line of `on` or `off` is that; anything else is off, by name: a file that
    cannot be read, a first line that is neither value, and a path that names a missing folder or a link
    to nothing, which a mistyped setting would otherwise pass off as an absent file."""
    if path is not None:
        try:
            if path.is_symlink() and not path.exists():
                return Arming(False, "off: the arming file is a link to nothing")
            if not path.parent.is_dir():
                return Arming(False, "off: the arming file's folder is missing")
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as error:
            return Arming(False, f"off: the arming file could not be read ({type(error).__name__})")
        else:
            value = lines[0].strip() if lines else ""
            if value == ON:
                return Arming(True, ON)
            if value == OFF:
                return Arming(False, OFF)
            return Arming(
                False, f"off: the arming file's first line is neither on nor off: {value!r}"
            )
    which = "no arming file is set" if path is None else "the arming file is absent"
    return Arming(absent_is_on, f"{ON if absent_is_on else OFF}: {which}")
