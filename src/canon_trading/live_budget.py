"""The live account's request budget, read from a local settings file outside the repository.

Contract: ADR 0110, change (f), "The live request budget, read from an untracked local settings file", under
ADR 0103's point 6: nothing about the owner's account enters a tracked file, a commit or a pull request.
The file is TOML, read with Python's `tomllib` (https://docs.python.org/3/library/tomllib.html:
`tomllib.load` takes a binary file and raises `TOMLDecodeError` on an invalid document).

The file is `canon/live/settings.toml` under the config folder (`CLAUDE_CONFIG_DIR`, else `~/.claude`),
where `mise run paper:session` keeps its window records and `scripts/automation` its state. The owner or
the lead writes it by hand; nothing here writes it. One key is read:

    trading_requests_per_minute = <a positive whole number>

the Trading API's request limit for the live account, a minute, as Alpaca gives it for that account. No
figure for the live account is written in this repository, and none is guessed:

* no file, or a file that does not set the key: the paper figure,
  `request_budget.TRADING_REQUEST_LIMIT_PER_MINUTE` (ADR 0083), and the answer says so;
* a file that cannot be read, is not TOML, or sets the key to anything but a positive whole number: an
  error by name. It is never read as absent, and what it held is never repeated: a wrong value may be a
  slip of the real one;
* a path inside this checkout is refused, there or not, so the figure cannot be kept where it could be
  committed.

The figure stays out of `repr`, `str` and every message here. A caller that records it does so in a file
outside the repository (ADR 0103, point 6).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING

from .request_budget import TRADING_REQUEST_LIMIT_PER_MINUTE

if TYPE_CHECKING:
    from collections.abc import Mapping

SETTINGS_FILE = Path("canon") / "live" / "settings.toml"
"""The local settings file, relative to the config folder."""
KEY = "trading_requests_per_minute"
FROM_SETTINGS = "the local settings file"
NO_FILE = "the paper figure: no local settings file"
NO_KEY = f"the paper figure: the local settings file sets no {KEY}"
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


def read_request_budget(path: Path | None = None) -> RequestBudget:
    """Read the budget now, from `path` or this machine's settings file (the module's header)."""
    path = settings_path() if path is None else path
    if path.resolve().is_relative_to(CHECKOUT):
        raise LiveSettingsError(
            "the live settings file is inside this checkout; it is kept outside the repository "
            f"({SETTINGS_FILE} under the config folder)"
        )
    try:
        with path.open("rb") as file:
            settings = tomllib.load(file)
    except FileNotFoundError:
        return RequestBudget(TRADING_REQUEST_LIMIT_PER_MINUTE, NO_FILE)
    except tomllib.TOMLDecodeError:
        # no cause kept: the parser's message is not needed, and nothing of the file is repeated
        raise LiveSettingsError(
            f"the local settings file ({SETTINGS_FILE}) is not valid TOML"
        ) from None
    except (OSError, UnicodeDecodeError) as error:
        raise LiveSettingsError(
            f"the local settings file ({SETTINGS_FILE}) could not be read ({type(error).__name__})"
        ) from None
    if KEY not in settings:
        return RequestBudget(TRADING_REQUEST_LIMIT_PER_MINUTE, NO_KEY)
    value = settings[KEY]
    if type(value) is not int or value <= 0:
        raise LiveSettingsError(
            f"{KEY} in the local settings file ({SETTINGS_FILE}) must be a positive whole number"
        ) from None
    return RequestBudget(value, FROM_SETTINGS)
