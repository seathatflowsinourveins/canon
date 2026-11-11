"""Data-only mode (ADR 0070, terms 3 and 4, and its build condition): every alpaca-py REST client this package
builds refuses every method but GET before anything is sent, and the data client refuses to connect if a
client it holds would send another.

Offline: requests' `HTTPAdapter.send`, below the refusal, is replaced by a recorder that answers GET with an
empty JSON body, so a request that got past the refusal would be seen here and never reach Alpaca.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import alpaca
import canon_alpaca
import canon_alpaca.data
import canon_alpaca.providers
import pytest
import requests
from alpaca.common.rest import RESTClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import ReplaceOrderRequest
from canon_alpaca.constants import ENV_API_KEY
from canon_alpaca.constants import ENV_SECRET_KEY
from canon_alpaca.factories import AlpacaDataClientFactory
from canon_alpaca.providers import DATA_ONLY_ENV
from canon_alpaca.providers import AlpacaInstrumentProvider
from canon_alpaca.providers import MethodRefused
from canon_alpaca.providers import require_get_only
from canon_alpaca.providers import timeout_adapter
from canon_alpaca.providers import with_http_timeout
from nautilus_trader.common import Environment
from nautilus_trader.config import DataClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import TraderId
from requests.adapters import HTTPAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

PLACEHOLDER = "offline-placeholder"
ADAPTER_SRC = Path(canon_alpaca.__file__).parent
TRADING_SRC = Path(__file__).resolve().parents[3] / "src" / "canon_trading"
ALPACA_SRC = Path(alpaca.__file__).parent


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, str]]:
    """Data-only mode with placeholder keys; every request that reaches requests' transport is recorded
    and answered offline with `{}`."""
    monkeypatch.setenv(DATA_ONLY_ENV, "1")
    monkeypatch.setenv(ENV_API_KEY, PLACEHOLDER)
    monkeypatch.setenv(ENV_SECRET_KEY, PLACEHOLDER)
    recorded: list[tuple[str, str]] = []

    def send(self, request, **kwargs):
        recorded.append((request.method, request.url))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return recorded


ORDER_CALLS: dict[str, Callable[[TradingClient], object]] = {
    "POST": lambda c: c.submit_order(
        MarketOrderRequest(symbol="SPY", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    ),
    "PATCH": lambda c: c.replace_order_by_id(uuid.uuid4(), ReplaceOrderRequest(qty=1)),
    "DELETE order": lambda c: c.cancel_order_by_id(uuid.uuid4()),
    "DELETE position": lambda c: c.close_position("SPY"),
}


@pytest.mark.parametrize("call", ORDER_CALLS, ids=list(ORDER_CALLS))
def test_submit_replace_cancel_and_close_are_refused_with_nothing_sent(sent, call) -> None:
    """Term 4: through the provider's own `TradingClient`, fetched a second time as each load does, each of
    the four is refused before requests' transport sees it, and a GET still goes out."""
    provider = AlpacaInstrumentProvider()
    provider._client()
    client = provider._client()  # the provider runs `with_http_timeout` on every fetch
    assert isinstance(client, TradingClient)
    with pytest.raises(MethodRefused, match="nothing was sent"):
        ORDER_CALLS[call](client)
    assert sent == []
    adapter = timeout_adapter(client)
    assert adapter is not None and adapter.get_only and adapter.sends == 0
    client.get("/clock")  # GET is the one method allowed (raw: the offline answer is `{}`)
    assert [m for m, _ in sent] == ["GET"]


def test_the_refusing_adapter_is_not_replaced_and_comes_back_over_one_mounted_after_it(
    sent,
) -> None:
    """A second reader's offline run (ADR 0070 term 3): a separate refusing adapter would come off when
    `with_http_timeout` runs again. Here the refusal is that adapter's mode, so running it again, even
    outside data-only mode, keeps it; and a plain adapter mounted for a longer prefix, which requests
    would choose for that host, is refused by the check and replaced on the next mount."""
    client = with_http_timeout(
        TradingClient(api_key=PLACEHOLDER, secret_key=PLACEHOLDER, paper=True)
    )
    adapter = timeout_adapter(client)
    assert adapter is not None and adapter.get_only
    with pytest.MonkeyPatch.context() as m:
        m.delenv(DATA_ONLY_ENV)
        assert timeout_adapter(with_http_timeout(client)) is adapter
    client._session.mount("https://paper-api.alpaca.markets", HTTPAdapter())
    with pytest.raises(MethodRefused, match="refusing to start"):
        require_get_only(client)
    with_http_timeout(client)
    require_get_only(client)
    assert set(client._session.adapters) == {"https://", "http://"}
    with pytest.raises(MethodRefused):
        ORDER_CALLS["POST"](client)
    assert sent == []


def test_outside_data_only_mode_nothing_is_refused_and_other_prefixes_are_kept(
    monkeypatch, sent
) -> None:
    """Outside the mode the adapter sends every method, and `with_http_timeout` replaces only the two
    schemes' adapters, as before ADR 0070: a caller's adapter for another prefix stays."""
    monkeypatch.delenv(DATA_ONLY_ENV)
    client = with_http_timeout(
        TradingClient(api_key=PLACEHOLDER, secret_key=PLACEHOLDER, paper=True)
    )
    require_get_only(client)  # does nothing outside the mode
    adapter = timeout_adapter(client)
    assert adapter is not None and not adapter.get_only
    own = HTTPAdapter()
    client._session.mount("https://data.alpaca.markets", own)
    assert timeout_adapter(with_http_timeout(client)) is adapter
    assert client._session.adapters["https://data.alpaca.markets"] is own


def test_a_client_with_no_session_is_refused_as_such(sent) -> None:
    with pytest.raises(MethodRefused, match="has no requests.Session at _session"):
        require_get_only(object())


class ReplacedSession:
    """Mixed into a REST client stand-in: its session is new at every read, so the mount lands on one
    session and the requests would go out on another, as a session replaced after the mount would."""

    @property
    def _session(self) -> requests.Session:
        return requests.Session()

    @_session.setter
    def _session(self, _: requests.Session) -> None:
        pass


def node_with_data_client(clients: list) -> LiveNode:
    class Capturing(AlpacaDataClientFactory):
        @staticmethod
        def create(**kwargs):
            client = AlpacaDataClientFactory.create(**kwargs)
            clients.append(client)
            return client

    config = DataClientConfig(
        instrument_provider=InstrumentProviderConfig(load_ids=["AAPL.ALPACA"])
    )
    return (
        LiveNode.builder("CANON-DATA-ONLY", TraderId("TESTER-001"), Environment.SANDBOX)
        .with_delay_post_stop_secs(0)
        .with_timeout_connection(2)
        .with_timeout_disconnection_secs(5)
        .add_data_client("ALPACA", Capturing, config)
        .build()
    )


def run_briefly(node: LiveNode, clients: list) -> dict[str, object]:
    """Run `node` until its client connects, the run ends, or 5 s pass; report what was seen."""
    seen: dict[str, object] = {}

    async def main() -> None:
        task = asyncio.ensure_future(node.run_async())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while not task.done() and loop.time() < deadline:
            if clients and clients[0].is_connected:
                break
            await asyncio.sleep(0.05)
        seen["running"] = node.handle().is_running
        seen["connected"] = bool(clients) and clients[0].is_connected
        if not task.done():
            node.handle().stop()
        await asyncio.wait({task}, timeout=15)
        seen["error"] = task.exception() if task.done() else "still running"

    asyncio.run(main())
    return seen


@pytest.mark.parametrize("held", ["historical", "provider"])
def test_the_node_does_not_start_in_data_only_mode_if_a_client_it_holds_could_send_another_method(
    monkeypatch, sent, alpaca, held, capfd
) -> None:
    """ADR 0070's build condition: a node in data-only mode whose historical client, or the provider's
    `TradingClient`, sends on a session the refusing adapter is not on does not start: the data client
    refuses to connect, and the node gives up waiting for it."""
    historical, trading = alpaca.HistoricalClient(), alpaca.TradingClient()
    if held == "historical":
        historical = type("Replaced", (ReplacedSession, type(historical)), {})()
    else:
        trading = type("Replaced", (ReplacedSession, type(trading)), {})()
    monkeypatch.setattr(canon_alpaca.data, "StockHistoricalDataClient", lambda **_: historical)
    monkeypatch.setattr(canon_alpaca.providers, "TradingClient", lambda **_: trading)
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: alpaca.Stream())
    clients: list = []
    node = node_with_data_client(clients)
    try:
        seen = run_briefly(node, clients)
    finally:
        node.dispose()
    out = capfd.readouterr()
    logged = out.out + out.err
    assert seen["running"] is False and seen["connected"] is False
    assert "waiting for engine connections" in str(seen["error"])
    assert f"{type(historical if held == 'historical' else trading).__name__}'s session" in logged
    assert "refusing to start (ADR 0070)" in logged
    assert sent == []
    # the provider's client is checked before its first load, so no asset request was made on it
    assert trading.symbol_requests == [] and trading.all_requests == []


def test_the_same_node_starts_when_every_client_refuses_all_but_get(
    monkeypatch, sent, alpaca
) -> None:
    """The control: with the stand-ins' own sessions the data client connects in data-only mode."""
    monkeypatch.setattr(
        canon_alpaca.data, "StockHistoricalDataClient", lambda **_: alpaca.HistoricalClient()
    )
    monkeypatch.setattr(canon_alpaca.providers, "TradingClient", lambda **_: alpaca.TradingClient())
    monkeypatch.setattr(canon_alpaca.data, "StockDataStream", lambda **_: alpaca.Stream())
    clients: list = []
    node = node_with_data_client(clients)
    try:
        seen = run_briefly(node, clients)
    finally:
        node.dispose()
    assert seen["connected"] is True and seen["error"] is None
    assert sent == []


# --- ADR 0070's build condition: every place a REST client or a `requests.Session` is made. ---

HTTP_LIBRARIES = {"urllib.request", "urllib3", "http.client", "httpx", "aiohttp", "pycurl"}
"""Libraries that could send an HTTP request around requests' transport adapters."""
REQUESTS_CALLS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "Session"}
"""requests' module-level calls, each a request or a session of its own (`requests.api`)."""
REST_CLIENTS = {
    "BrokerClient",
    "CorporateActionsClient",
    "CryptoHistoricalDataClient",
    "NewsClient",
    "OptionHistoricalDataClient",
    "ScreenerClient",
    "StockHistoricalDataClient",
    "TradingClient",
}
"""alpaca-py 0.45.0's `RESTClient` subclasses."""
CONSTRUCTION_SITES = {
    ("canon_alpaca/data.py", "_connect_on", "StockHistoricalDataClient"),
    ("canon_alpaca/execution.py", "_connect_venue", "TradingClient"),
    ("canon_alpaca/providers.py", "_client", "TradingClient"),
    ("canon_trading/movers_screener.py", "screener_client", "ScreenerClient"),
    ("canon_trading/movers_stream.py", "data_client", "StockHistoricalDataClient"),
    ("canon_trading/session_window.py", "paper_trading", "TradingClient"),
    ("canon_trading/session_window.py", "window_actions", "CorporateActionsClient"),
    ("canon_trading/session_window.py", "window_data", "StockHistoricalDataClient"),
    ("canon_trading/session_window.py", "window_screener", "ScreenerClient"),
}
"""Where this repository's source builds an alpaca-py REST client; each passes it to `with_http_timeout`."""


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def callee(node: ast.Call) -> tuple[str | None, str]:
    """`(module or object name, called name)` of a call: `requests.get(...)` is `("requests", "get")`."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return (f.value.id if isinstance(f.value, ast.Name) else None), f.attr
    return None, f.id if isinstance(f, ast.Name) else ""


def functions_with_calls(tree: ast.AST) -> list[tuple[str, ast.Call]]:
    """Each call in `tree` with the name of the innermost function, lambda or class body holding it
    (`<module>` outside one)."""
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name
            elif isinstance(child, ast.Lambda):
                inner = f"<lambda {child.lineno}>"
            if isinstance(child, ast.Call):
                found.append((where, child))
            visit(child, inner)

    visit(tree, "<module>")
    return found


def aliases(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """The names `requests` is imported as, and each name an alpaca-py REST client or one of requests'
    calls is imported as, mapped to what it is (`from requests import post as p` gives `p` -> `post`)."""
    modules, names = {"requests"}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "requests"}
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if (node.module == "requests" and a.name in REQUESTS_CALLS) or (
                    node.module.split(".")[0] == "alpaca" and a.name in REST_CLIENTS
                ):
                    names[a.asname or a.name] = a.name
    return modules, names


def guarded(call: ast.Call, where: str, calls: list[tuple[str, ast.Call]], tree: ast.AST) -> bool:
    """Whether the object `call` builds reaches `with_http_timeout` in the same function: passed to it
    directly, or assigned to a name or attribute that is then passed to it."""
    mounts = [c for w, c in calls if w == where and callee(c)[1] == "with_http_timeout"]
    if any(arg is call for c in mounts for arg in c.args):
        return True
    targets = [
        ast.unparse(t)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and node.value is call
        for t in node.targets
    ]
    return any(ast.unparse(arg) in targets for c in mounts for arg in c.args)


def all_subclasses(cls: type) -> set[type]:
    return {s for c in cls.__subclasses__() for s in {c} | all_subclasses(c)}


def test_alpaca_py_makes_its_one_session_in_rest_client_and_sends_only_through_it() -> None:
    """alpaca-py at the pin: one `Session()`, in `RESTClient.__init__`, the only assignment to `_session`;
    no module-level requests call and no other HTTP library, so each request it makes goes through the
    session's mounted adapters; and its REST clients are the ones listed."""
    sessions, assigned, module_calls, libraries = [], [], [], []
    for path in python_files(ALPACA_SRC):
        tree = ast.parse(path.read_text(), str(path))
        rel = path.relative_to(ALPACA_SRC).as_posix()
        for where, call in functions_with_calls(tree):
            owner, name = callee(call)
            if name == "Session" and owner in (None, "requests"):
                sessions.append((rel, where))
            if owner == "requests" and name in REQUESTS_CALLS:
                module_calls.append((rel, where, name))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Attribute) and t.attr == "_session" for t in targets):
                    assigned.append((rel, node.lineno))
            if isinstance(node, ast.Import):
                libraries += [a.name for a in node.names if a.name in HTTP_LIBRARIES]
            if isinstance(node, ast.ImportFrom) and (node.module or "") in HTTP_LIBRARIES:
                libraries.append(node.module)
    assert sessions == [("common/rest.py", "__init__")]
    assert [rel for rel, _ in assigned] == ["common/rest.py"]
    assert module_calls == [] and libraries == []
    assert "self._session.request(" in inspect.getsource(RESTClient._one_request)
    for path in python_files(ALPACA_SRC):  # every module, so each subclass is defined
        if "(RESTClient)" in path.read_text():
            rel = path.relative_to(ALPACA_SRC.parent).with_suffix("").as_posix()
            __import__(rel.replace("/", "."))
    assert {c.__name__ for c in all_subclasses(RESTClient)} == REST_CLIENTS
    assert issubclass(StockHistoricalDataClient, RESTClient)


def test_every_rest_client_this_repository_builds_gets_the_refusing_adapter() -> None:
    """Every call in the adapter's and `canon_trading`'s source that builds an alpaca-py REST client, a
    `requests.Session` or a request of requests' own, under any name it is imported as, is listed, and the
    object it builds reaches `with_http_timeout` in the same function; no other HTTP library is imported.
    A new site fails here until it is listed. Found by name: a client built indirectly (through a factory
    function or `functools.partial`) is not found, and none is built that way today."""
    sites, libraries, unguarded = set(), [], []
    for root in (ADAPTER_SRC, TRADING_SRC):
        for path in python_files(root):
            tree = ast.parse(path.read_text(), str(path))
            rel = f"{root.name}/{path.relative_to(root).as_posix()}"
            modules, names = aliases(tree)
            calls = functions_with_calls(tree)
            for where, call in calls:
                owner, name = callee(call)
                real = names.get(name, name) if owner is None else name
                if (
                    (owner is None and real in REST_CLIENTS)
                    or (owner in modules and name in REQUESTS_CALLS)
                    or (owner is None and name in names and real in REQUESTS_CALLS)
                ):
                    sites.add((rel, where, real))
                    if not guarded(call, where, calls, tree):
                        unguarded.append((rel, where, real))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    libraries += [a.name for a in node.names if a.name in HTTP_LIBRARIES]
                if isinstance(node, ast.ImportFrom) and (node.module or "") in HTTP_LIBRARIES:
                    libraries.append(node.module)
    assert sites == CONSTRUCTION_SITES
    assert unguarded == [] and libraries == []


def test_the_construction_scan_sees_aliases_and_an_unguarded_client() -> None:
    """The scan above on made-up source: an aliased client mounted on something else, requests called
    through an alias and a name imported from it, and a client built in a lambda, all found and unguarded;
    a client assigned and then mounted, guarded."""
    source = """
import requests as r
from requests import post as p
from alpaca.trading.client import TradingClient as T

def f(other):
    c = T(api_key="x")
    with_http_timeout(other)
    return c

def g():
    r.post("u")
    p("u")
    return lambda: T()

def h():
    self.c = T()
    with_http_timeout(self.c)
"""
    tree = ast.parse(source)
    modules, names = aliases(tree)
    calls = functions_with_calls(tree)
    found = []
    for where, call in calls:
        owner, name = callee(call)
        real = names.get(name, name) if owner is None else name
        if (
            (owner is None and real in REST_CLIENTS)
            or (owner in modules and name in REQUESTS_CALLS)
            or (owner is None and name in names and real in REQUESTS_CALLS)
        ):
            found.append((where, real, guarded(call, where, calls, tree)))
    assert sorted(found) == sorted(
        [
            ("f", "TradingClient", False),
            ("g", "post", False),
            ("g", "post", False),
            ("<lambda 14>", "TradingClient", False),
            ("h", "TradingClient", True),
        ]
    )
