"""The live account's request budget, read from a local settings file outside the repository.

Contract: ADR 0110, change (f), "The live request budget, read from an untracked local settings file", under
ADR 0103's point 6: nothing about the owner's account enters a tracked file, a commit or a pull request.
The file is TOML, read with Python's `tomllib` (https://docs.python.org/3/library/tomllib.html:
`tomllib.load` takes a binary file and raises `TOMLDecodeError` on an invalid document).

The file is `canon/live/settings.toml` under the config folder (`CLAUDE_CONFIG_DIR`, else `~/.claude`),
where `mise run paper:session` keeps its window records and `scripts/automation` its state. The owner or
the lead writes it by hand; nothing here writes it. One key is read:

    trading_requests_per_minute = <a positive whole number>

the Trading API's request limit for the live account, a minute. What Alpaca's pages state about that limit
is in ADR 0122's Evidence; the figure of one account is the owner's to take from Alpaca. No figure for the
live account is written in this repository, and none is guessed:

* no file, or a file that sets nothing at all: the paper figure,
  `request_budget.TRADING_REQUEST_LIMIT_PER_MINUTE` (ADR 0083), and the answer says so;
* a file that cannot be read or parsed, is or is under a link whose target is missing, is not TOML, sets
  any name but that key at its top level (a table, a misspelt key, the key in other case) or sets the key
  to anything but a positive whole number: an error by name. It is never read as absent, and what it held
  is never repeated, a name it set included: a wrong value or name may be a slip of the real figure;
* a path that resolves to a place in this checkout, or under a folder holding a `.git` entry, is refused,
  whether or not the file is there and however its folders are spelt. A work tree with no `.git` entry of
  its own (one named by `GIT_DIR` and `GIT_WORK_TREE`, or by `core.worktree`) is not recognised.

The figure stays out of `repr`, `str` and every message here. An error raised here carries no cause and no
context, since the parser's and the decoder's own errors hold the file's text (`TOMLDecodeError.doc`,
`UnicodeDecodeError.object`), and no frame of this module in its traceback holds what the file held.
`dataclasses.asdict` and `astuple` do give the figure, as the field does. A caller that records it does so
in a file outside the repository (ADR 0103, point 6).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

from .request_budget import TRADING_REQUEST_LIMIT_PER_MINUTE
from .request_budget import check_limit

if TYPE_CHECKING:
    from collections.abc import Mapping

SETTINGS_FILE = Path("canon") / "live" / "settings.toml"
"""The local settings file, relative to the config folder."""
KEY = "trading_requests_per_minute"
FROM_SETTINGS = "the local settings file"
NO_FILE = "the paper figure: no local settings file"
NO_KEY = f"the paper figure: the local settings file sets no {KEY}"
THE_FILE = f"the local settings file ({SETTINGS_FILE})"
"""How every refusal names the file: by its place under the config folder, never by the path read."""
CHECKOUT = Path(__file__).resolve().parents[2]
"""The checkout this module runs from: `src/canon_trading/` is two folders under its root."""


class LiveSettingsError(ValueError):
    """The local settings file is there and cannot give the budget."""


@dataclass(frozen=True)
class RequestBudget:
    """The Trading API requests a minute a live node may plan for, and where the figure came from."""

    limit_per_minute: int = field(repr=False)
    source: str

    @property
    def from_settings(self) -> bool:
        """Whether the figure is the local file's; false for the paper figure."""
        return self.source == FROM_SETTINGS


def settings_path(environ: Mapping[str, str] | None = None) -> Path:
    """The local settings file of this machine: under `CLAUDE_CONFIG_DIR`, else under `~/.claude`."""
    env = os.environ if environ is None else environ
    config = env.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / SETTINGS_FILE


def inside_checkout(path: Path) -> bool:
    """Whether `path`, there or not, resolves to a place in this checkout or under a `.git` entry.

    Three clauses. This checkout by name. Then the two of `arm_comparison._inside_checkout`, copied since
    that module imports numpy, pandas and arch: a folder above the path that is this checkout by
    `samefile` (the device and inode numbers, so another spelling on a volume that does not tell upper
    case from lower is still found), or that holds a `.git` entry that is there, a folder in a main
    checkout and a file in a linked worktree (gitrepository-layout(5)). Unlike that one, where this
    checkout cannot be compared this goes on to the `.git` test. The path is resolved first, so a link is
    judged where it leads. A work tree with no `.git` entry of its own is not recognised.
    """
    resolved = path.resolve()
    if resolved.is_relative_to(CHECKOUT):
        return True
    for folder in (resolved, *resolved.parents):
        if (folder / ".git").exists():
            return True
        try:
            if folder.samefile(CHECKOUT):
                return True
        except OSError:  # one of the two is not there, or not to be read: nothing to compare
            continue
    return False


def link_without_target(path: Path) -> bool:
    """Whether `path`, or a folder above it, is a symbolic link whose target is missing."""
    return any(part.is_symlink() and not part.exists() for part in (path, *path.parents))


def _read(path: Path) -> RequestBudget | str:
    """The budget the file at `path` gives, or what is wrong with it, in words that repeat nothing of it.

    A frame of its own, ended before the caller raises on those words: the error then has no context (the
    parser's and the decoder's errors hold the file's text) and its traceback no frame holding the
    file's content.
    """
    try:
        with path.open("rb") as file:
            settings = tomllib.load(file)
    except FileNotFoundError:
        if link_without_target(path):
            return f"{THE_FILE} is, or is under, a link whose target is missing"
        return RequestBudget(TRADING_REQUEST_LIMIT_PER_MINUTE, NO_FILE)
    except tomllib.TOMLDecodeError:
        return f"{THE_FILE} is not valid TOML"
    except (OSError, UnicodeDecodeError) as error:
        return f"{THE_FILE} could not be read ({type(error).__name__})"
    except (ValueError, RecursionError) as error:
        # after the two above, which are ValueErrors too: a number past Python's digit limit for an
        # integer, or tables nested past its recursion limit
        return f"{THE_FILE} could not be parsed ({type(error).__name__})"
    if not settings:
        return RequestBudget(TRADING_REQUEST_LIMIT_PER_MINUTE, NO_KEY)
    if settings.keys() != {KEY}:
        return f"{THE_FILE} sets a name other than {KEY} at its top level; it holds that one key and nothing else"
    try:
        check_limit(settings[KEY])
    except ValueError:
        return f"{KEY} in {THE_FILE} must be a positive whole number"
    return RequestBudget(settings[KEY], FROM_SETTINGS)


def read_request_budget(path: Path | None = None) -> RequestBudget:
    """Read the budget now, from `path` or this machine's settings file (the module's header)."""
    path = settings_path() if path is None else path
    if inside_checkout(path):
        raise LiveSettingsError(
            "the live settings file is inside this checkout or another git checkout; it is kept outside "
            f"every repository ({SETTINGS_FILE} under the config folder)"
        )
    answer = _read(path)
    if isinstance(answer, str):
        raise LiveSettingsError(answer)
    return answer
