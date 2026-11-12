"""What ADR 0108 relies on in edgartools at the pin, checked in this project's own environment.

Contract: dgunning/edgartools v6.0.0 at `1ce77f9903e06ec58609a40d01797331e4ead460`:
`edgar/settings.py:95-110` (`set_identity`), `:138-170` (`ask_for_identity`, `get_identity`),
`edgar/httprequests.py:783-800` (the identity check of the request functions) and
`edgar/httpclient.py:320` (the shared client's user agent). No request is made here: the one request
function called stops at its identity check, and the client it would build is replaced by a failure.
The identity below is a made-up one at a reserved example domain; no real one enters a test.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path

import edgar
import pytest
from edgar import httpclient
from edgar import httprequests
from edgar import settings
from edgar.exceptions import IdentityNotSetError

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parent
MADE_UP = "Example Reader reader@example.com"


def test_the_installed_version_is_the_one_every_place_that_names_it_gives() -> None:
    # The lint's version comparison reads the root's uv.lock only, so this test is what holds the pin.
    version = edgar.__version__
    registry = json.loads((REPO / "canon" / "upstreams.json").read_text(encoding="utf-8"))
    entry = next(entry for entry in registry["upstreams"] if entry["id"] == "edgartools")
    assert entry["track"] == f"tag:v{version}"
    manifest = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    assert f"edgartools=={version}" in manifest["project"]["dependencies"]
    architecture = (REPO / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert f"edgartools v{version}" in architecture
    (record,) = (REPO / "docs" / "adr").glob("0108-*.md")
    title = next(
        line for line in record.read_text(encoding="utf-8").splitlines() if line[:2] == "# "
    )
    assert f"edgartools {version}" in title


def test_the_tools_this_project_runs_are_the_roots_pins() -> None:
    # pytest and ty are registry pins declared in the root's manifest; this project declares them a
    # second time for its own environment, and a move of one has to move the other.
    ours = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert ours["dependency-groups"]["dev"]
    assert set(ours["dependency-groups"]["dev"]) <= set(root["dependency-groups"]["dev"])


def test_set_identity_sets_the_variable_and_logs_no_part_of_the_value(caplog, capsys) -> None:
    with caplog.at_level(logging.DEBUG):
        settings.set_identity(MADE_UP)
    assert os.environ["EDGAR_IDENTITY"] == MADE_UP
    # The log is being captured: upstream's own line about the identity is in it, and the value is not.
    assert "Identity of the Edgar REST client set" in caplog.text
    printed = capsys.readouterr()
    for text in (caplog.text, printed.out, printed.err):
        assert not any(word in text for word in MADE_UP.split())


def test_a_request_with_no_identity_raises_before_a_client_is_built(monkeypatch) -> None:
    def no_client(*_args, **_kwargs):
        raise AssertionError("a client was built for a request that carried no identity")

    monkeypatch.setattr(httprequests, "http_client", no_client)
    with pytest.raises(IdentityNotSetError):
        httprequests.get_with_retry("https://www.sec.gov/")


def test_an_empty_identity_passes_the_request_check_and_reaches_the_prompt(monkeypatch) -> None:
    # Why the recorder itself refuses an empty setting: upstream's check raises on a missing variable
    # only, so an empty one goes on to build the client, whose user agent then asks at the prompt.
    class ClientBuilt(Exception):
        pass

    def client(*_args, **_kwargs):
        raise ClientBuilt

    asked = []
    monkeypatch.setenv("EDGAR_IDENTITY", "")
    monkeypatch.setattr(httprequests, "http_client", client)
    monkeypatch.setattr(
        settings, "ask_for_identity", lambda *_a, **_k: asked.append(True) or MADE_UP
    )
    with pytest.raises(ClientBuilt):
        httprequests.get_with_retry("https://www.sec.gov/")
    assert settings.get_identity() == MADE_UP
    assert asked == [True]


def test_the_shared_client_asks_get_identity_which_prompts_when_none_is_set(monkeypatch) -> None:
    # Why the recorder calls set_identity() first, on every path: passing the `identity` argument
    # satisfies the request functions' check, but the client still asks get_identity().
    asked = []

    def prompt(*_args, **_kwargs) -> str:
        asked.append(True)
        return MADE_UP

    monkeypatch.setattr(settings, "ask_for_identity", prompt)
    assert httpclient.get_http_mgr(cache_enabled=False).user_agent_factory is settings.get_identity
    assert settings.get_identity() == MADE_UP
    assert asked == [True]


def test_get_identity_does_not_prompt_once_set_identity_has_run(monkeypatch) -> None:
    def prompt(*_args, **_kwargs) -> str:
        raise AssertionError("prompted although an identity was set")

    monkeypatch.setattr(settings, "ask_for_identity", prompt)
    settings.set_identity(MADE_UP)
    assert settings.get_identity() == MADE_UP
