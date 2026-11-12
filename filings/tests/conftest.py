"""Keep edgartools' folders and the SEC identity out of the home folder and out of the tests' way.

edgartools writes under `EDGAR_LOCAL_DATA_DIR` and `EDGAR_CACHE_DIR` (`edgar/paths.py:51-75` at the pin;
by default `.edgar` and `.edgar_cache` in the home folder), and reads the identity from
`EDGAR_IDENTITY`. Both folders are pointed at a temporary one before `edgar` is imported, and every test
starts with no identity and leaves none behind.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_SCRATCH = tempfile.mkdtemp(prefix="canon-filings-tests-")
os.environ["EDGAR_LOCAL_DATA_DIR"] = os.path.join(_SCRATCH, "data")
os.environ["EDGAR_CACHE_DIR"] = os.path.join(_SCRATCH, "cache")
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_identity():
    """No `EDGAR_IDENTITY` before a test, and whatever the test set removed after it."""
    kept = os.environ.pop("EDGAR_IDENTITY", None)
    try:
        yield
    finally:
        os.environ.pop("EDGAR_IDENTITY", None)
        if kept is not None:
            os.environ["EDGAR_IDENTITY"] = kept
