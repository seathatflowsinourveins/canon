"""A failed case's LiveNode does not fail the next case that builds one.

At the NautilusTrader pin a thread holds one LiveNode until it is dropped
(`crates/live/src/node/mod.rs:344-354`), and pytest keeps the last failure, its frame and that frame's
node, in `sys.last_*` (`_pytest/runner.py:173-193` at pytest 9.2.0). So the next case that builds a node
without collecting first failed with "A LiveNode already exists", and a run read as many failures what
was one. Each case here runs a pair of inner cases in a pytest of its own: the first fails by force with
its node disposed, and the second builds a node. Without `no_live_node_kept_by_the_last_failure` (in
`tests/conftest.py`) the second fails; with it, the second passes and the first still fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")
INNER = """
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveNodeConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import TraderId


def build():
    config = LiveNodeConfig(environment=Environment.SANDBOX, trader_id=TraderId("TESTER-001"))
    return LiveNode.build("CASCADE-TEST", config)


def test_a_fails_with_its_node_disposed():
    node = build()
    try:
        raise AssertionError("forced failure")
    finally:
        node.dispose()


def test_b_builds_the_next_node():
    build().dispose()
"""
WITH_FIXTURE = f"""
import importlib.util

spec = importlib.util.spec_from_file_location("canon_tests_conftest", {str(CONFTEST)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
no_live_node_kept_by_the_last_failure = module.no_live_node_kept_by_the_last_failure
"""


def inner_run(folder: Path, conftest: str) -> str:
    (folder / "test_inner.py").write_text(INNER)
    (folder / "conftest.py").write_text(conftest)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA"]
        + ["--rootdir", str(folder), "-c", os.devnull, str(folder)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=folder,
        check=False,
    )
    return done.stdout + done.stderr


def test_without_the_release_the_next_node_fails(tmp_path) -> None:
    out = inner_run(tmp_path, "")
    assert "FAILED test_inner.py::test_a_fails_with_its_node_disposed" in out, out
    assert "FAILED test_inner.py::test_b_builds_the_next_node" in out, out
    assert "A LiveNode already exists" in out, out


def test_with_the_release_only_the_forced_failure_fails(tmp_path) -> None:
    out = inner_run(tmp_path, WITH_FIXTURE)
    assert "FAILED test_inner.py::test_a_fails_with_its_node_disposed" in out, out
    assert "PASSED test_inner.py::test_b_builds_the_next_node" in out, out
    assert "A LiveNode already exists" not in out, out
