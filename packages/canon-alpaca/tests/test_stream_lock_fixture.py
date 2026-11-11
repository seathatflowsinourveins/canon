"""The `alpaca_stream` fixture locks the file the paper task names, and no other (ADR 0069).

The file is named only by `CANON_ALPACA_STREAM_LOCK`, which `mise run paper:test` sets to account A's
stream lock (`scripts/test-lanes` checks that line). This shows the fixture takes the file that variable
names: an inner pytest run, with placeholder keys, a deadline and a sentinel path in its environment, holds
a lock on the sentinel for its marked test. Nothing reaches Alpaca. The same file is in `tests/` and in
`packages/canon-alpaca/tests/`, each beside its folder's copy of the fixture.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

INNER = "CANON_STREAM_LOCK_FIXTURE_INNER"


@pytest.mark.alpaca_stream
def test_inner_the_marked_test_holds_the_named_file() -> None:
    if os.environ.get(INNER) != "1":
        pytest.skip("run by test_the_fixture_locks_the_file_the_task_names")
    fd = os.open(os.environ["CANON_ALPACA_STREAM_LOCK"], os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_the_fixture_locks_the_file_the_task_names(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel-stream.lock"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ALPACA_", "APCA_", "CANON_ALPACA_", "CANON_PAPER_"))
    }
    env |= {
        "ALPACA_API_KEY": "offline-placeholder",
        "ALPACA_SECRET_KEY": "offline-placeholder",
        "CANON_ALPACA_STREAM_LOCK": str(sentinel),
        "CANON_PAPER_TEST_DEADLINE": str(int(time.time()) + 3600),
        INNER: "1",
    }
    ran = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{__file__}::test_inner_the_marked_test_holds_the_named_file",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "1 passed" in ran.stdout
    assert sentinel.is_file()
