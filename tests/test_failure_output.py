"""paper:test never publishes a failed frame or an exception's values.

Contract: pytest 9.2.0's subprocess CLI and TestReport consumers (terminal/JUnit), and
alpaca-py 0.45.0 RESTClient._request -> _one_request -> APIError. Only requests.Session.send is
replaced: request preparation, the real HTTPError and alpaca-py's raise all still run offline.
The policy reaches the child only through the task's own arguments (`-p canon_trading.paper_safe_output
--paper-safe-output`), so removing it from either invocation must fail these cases.
"""

from __future__ import annotations

import os
import secrets
import shlex
import string
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INNER = """
import json
import logging
import os
import sys

import pytest
import requests
from alpaca.common.rest import RESTClient


def message():
    size = int(os.environ["FAKE_MESSAGE_SIZE"])
    secret = os.environ["ALPACA_SECRET_KEY"]
    return secret if size == -1 else secret[:size] or "planted offline failure"


def own_request(options):
    local_only = "LOCAL-SENTINEL-DO-NOT-PRINT"
    raise RuntimeError(message())


def test_request_fails(monkeypatch):
    secret = os.environ["ALPACA_SECRET_KEY"]
    # Exercise captured output in the terminal and JUnit, including teardown's copy of it.
    print(secret)
    print(secret[:32], file=sys.stderr)
    logging.warning("offline request fixture reached")
    options = {"headers": {"APCA-API-SECRET-KEY": secret}}
    if os.environ["FAKE_ORIGIN"] == "own":
        own_request(options)

    def send(session, request, **kwargs):
        assert request.headers["APCA-API-SECRET-KEY"] == secret
        response = requests.Response()
        response.status_code = 400
        response.reason = "Bad Request"
        response.url = request.url
        response.request = request
        response.encoding = "utf-8"
        response._content = json.dumps({"code": 40010001, "message": message()}).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    client = RESTClient(
        "https://paper.invalid", api_key=os.environ["ALPACA_API_KEY"], secret_key=secret
    )
    client._session.trust_env = False
    client.get("/account")
"""


def paper_arguments(invocation: int) -> list[str]:
    """Use both actual task calls, omitting only their test globs and forwarded shell argv."""
    task = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["paper:test"]["run"]
    calls = [shlex.split(line) for line in task.splitlines() if "uv run --locked pytest " in line]
    assert len(calls) == 2
    args = calls[invocation][calls[invocation].index("pytest") + 1 :]
    return [arg for arg in args if "test_paper_*.py" not in arg and arg != "$@"]


@pytest.mark.parametrize("invocation", [0, 1], ids=["terminal", "bounded"])
@pytest.mark.parametrize("verbose", [False, True], ids=["task", "locals-vv"])
@pytest.mark.parametrize("origin,kind", [("alpaca", "APIError"), ("own", "RuntimeError")])
@pytest.mark.parametrize(
    "message_size", [0, -1, 8, 32], ids=["frame", "whole", "prefix8", "prefix32"]
)
def test_paper_failure_writes_no_values(
    tmp_path: Path, invocation: int, verbose: bool, origin: str, kind: str, message_size: int
) -> None:
    # The planted value is passed only in the child's environment, never in its source or argv.
    secret = "FAKESECRET-" + "".join(secrets.choice(string.ascii_letters) for _ in range(80))
    inner = tmp_path / "test_planted.py"
    inner.write_text(INNER)
    output = tmp_path / "output"
    output.mkdir()
    args = paper_arguments(invocation)
    if verbose:
        args += ["--showlocals", "-vv"]
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            *args,
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            "-c",
            os.devnull,
            "--basetemp",
            str(output),
            f"--junitxml={output / 'report.xml'}",
            f"--log-file={output / 'pytest.log'}",
            "-o",
            "junit_logging=all",
            str(inner),
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ALPACA_API_KEY": "PLANTED-KEY-ID",
            "ALPACA_SECRET_KEY": secret,
            "FAKE_ORIGIN": origin,
            "FAKE_MESSAGE_SIZE": str(message_size),
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    (output / "stdout").write_bytes(done.stdout)
    (output / "stderr").write_bytes(done.stderr)
    assert done.returncode == 1, (done.stdout + done.stderr).decode()
    assert (output / "report.xml").is_file()
    assert (output / "pytest.log").is_file()
    assert "offline request fixture reached" in (output / "pytest.log").read_text()
    for path in output.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            for length in range(8, len(secret) + 1):
                assert secret[:length].encode() not in contents, f"secret prefix in {path.name}"
            assert b"LOCAL-SENTINEL-DO-NOT-PRINT" not in contents, path.name
            assert b"APCA-API-SECRET-KEY" not in contents, path.name
    terminal = (done.stdout + done.stderr).decode()
    assert "test_planted.py::test_request_fails" in terminal, terminal
    assert kind in terminal, terminal
    assert kind in (output / "report.xml").read_text()
