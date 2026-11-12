"""The ranking and session window's shared helpers, moved from session_window without changing behavior.

Contracts: CPython 3.14.10's tempfile.mkstemp and os.replace (Doc/library/tempfile.rst:230-265,
os.rst:2827-2835 at the pin); the asset-name proxy recorded in proposed ADR 0087. No node imports.
"""

import os
import re
import tempfile
from pathlib import Path


def write_atomically(path: Path, body: str) -> None:
    """Replace `path` whole (`os.replace`), so a reader or a kill never sees half a record, through a
    temporary file of this writer's own (`tempfile.mkstemp`), so two writers never share one."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(body)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


NOT_COMMON = re.compile(r"\b(warrants?|units?|rights?)\b", re.IGNORECASE)
"""What in Alpaca's asset `name` marks a warrant, a unit or a right, which a mover window passes over; common
shares and depositary receipts are kept. A proxy with no upstream behind it: alpaca-py 0.45.0's `Asset` and
Alpaca's assets page give `class` `us_equity` and no security type."""
