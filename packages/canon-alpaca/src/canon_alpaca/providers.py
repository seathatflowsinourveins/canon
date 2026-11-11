"""Instrument provider: US equities from Alpaca's assets endpoint.

Contract: `nautilus_trader.live.providers.InstrumentProvider`, NautilusTrader v2.0.0rc8 (Python adapter
guide, "Instrument providers"): override `load_all_async` and `load_ids_async`, store instruments with
`add`, and leave `initialize` and the synchronous loaders to the base class. The data client sends the
loaded instruments to the core.

`InstrumentProviderConfig.filter_callable` is refused at construction: the Python `InstrumentProvider`
base class in v2.0.0rc8 (`nautilus_trader/live/providers.py`) never reads it, and upstream's own Binance
adapter refuses it in v2 ("Binance v2 does not support instrument filter_callable"), so there is no
supported way to apply it.

Venue side: alpaca-py 0.45.0 `TradingClient.get_all_assets(GetAssetsRequest)` and
`TradingClient.get_asset(symbol)`, which call Alpaca's `/v2/assets` endpoints ("Get Assets" and "Get an
Asset by ID or Symbol", https://docs.alpaca.markets/us/reference/get-v2-assets-1.md). `TradingClient` is
the only alpaca-py client for those endpoints. It is built with `paper=True`, the pinned default, and this
module calls nothing on it but the two asset reads.

Known limits:

* NautilusTrader's `Equity` is whole-share only (nautilus_trader issue 5462, ADR 0030). Alpaca's
  `fractionable` flag is kept in `Equity.info`, but a fractional position cannot be represented.
* One price increment for every equity: see `constants.PRICE_INCREMENT`.
* The assets endpoint carries no timestamp, so `ts_event` and `ts_init` are the time of the load.
* alpaca-py's REST clients are synchronous, so each call runs in a worker thread (`asyncio.to_thread`)
  to keep the node's event loop free.
* alpaca-py 0.45.0 gives its requests no timeout and has no setting for one. Every alpaca-py REST client
  this package builds gets one through `with_http_timeout` (ADR 0039): a transport adapter, requests' own
  documented mechanism ("Transport Adapters"), mounted on the client's `requests.Session`, which alpaca-py
  keeps in the private attribute `RESTClient._session`. That attribute is the one departure from alpaca-py's
  public API; a public timeout option in alpaca-py replaces it. `test_upstream_contract.py` checks the
  attribute at the pin, and a client without it is refused.
* Only active assets are loaded (alpaca-py `Asset.status`); an inactive one is refused, and so is a
  `status` filter other than `active`.
* Data-only mode (`CANON_ALPACA_DATA_ONLY=1`, ADR 0070 term 3): the `TimeoutAdapter` that
  `with_http_timeout` mounts refuses every method but GET before anything is sent, using requests' own
  transport-adapter hook (`HTTPAdapter.send`), so a process that holds the paper pair to read data
  cannot submit, replace, cancel or close through any client this package builds. It guards this
  package's clients only, not other code that holds the pair.
* Request count (`request_counts`): the `TimeoutAdapter` counts what it hands to requests' transport, per
  key pair and per API, in UTC wall-clock minutes, and records each 429 by name, time and path. This is
  glue around alpaca-py's session, on the same transport-adapter hook. NautilusTrader's own quota code is
  a GCRA rate limiter shared by its Rust HTTP clients (nautilus_trader@d53849b216e2:
  crates/network/src/ratelimiter/mod.rs:155, crates/network/src/http/mod.rs:25), with Python bindings for
  none of it (crates/network/src/python/mod.rs:31, which registers only `TransportBackend`), so a Python
  adapter on alpaca-py cannot use it. It throttles; this counts. alpaca-py repeats a 429 itself
  (`RESTClient._request`, `_retry_codes`), and each attempt is counted; nothing here changes that.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Protocol
from typing import override
from urllib.parse import urlsplit

import requests
from alpaca.common.enums import BaseURL
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from alpaca.trading.enums import AssetStatus
from alpaca.trading.models import Asset
from alpaca.trading.requests import GetAssetsRequest
from nautilus_trader.live.providers import InstrumentProvider
from nautilus_trader.model import Equity
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import Symbol
from requests.adapters import HTTPAdapter

from .constants import ENV_API_KEY
from .constants import ENV_SECRET_KEY
from .constants import PRICE_INCREMENT
from .constants import PRICE_PRECISION
from .constants import USD
from .constants import VENUE

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from alpaca.common.types import RawData
    from nautilus_trader.config import InstrumentProviderConfig

DATA_ONLY_ENV = "CANON_ALPACA_DATA_ONLY"
"""Set to `1` by `mise run paper:data` and by the module it runs (ADR 0070): every REST client this package
builds sends GET requests only."""


def data_only() -> bool:
    """Whether this process runs in data-only mode (`DATA_ONLY_ENV` is `1`)."""
    return os.environ.get(DATA_ONLY_ENV) == "1"


class MethodRefused(RuntimeError):
    """A request other than GET in data-only mode, refused before it was sent (ADR 0070)."""


TRADING_API = "trading"
DATA_API = "data"
DATA_HOSTS = frozenset(urlsplit(u.value).hostname for u in (BaseURL.DATA, BaseURL.DATA_SANDBOX))
"""Alpaca's market data API hosts, from alpaca-py 0.45.0's `BaseURL`; a request to any other host counts
against the trading API, so an unknown host is never left out of the trading budget."""

RATE_LIMITED_KEPT = 100
"""The most recent 429s kept by name, time and path for each account and API; every 429 is counted."""


@dataclass(frozen=True)
class RateLimited:
    """One response Alpaca answered 429: its status by name, its UTC time and the endpoint's path, with
    no query string and no header."""

    name: str
    at: datetime
    path: str


@dataclass(frozen=True)
class RequestCount:
    """The requests one account sent to one Alpaca API in this process, as `request_counts` returns them.

    `account` numbers each key pair in the order its first request was sent (1, 2, ...); nothing derived
    from a key is shown. `api` is `TRADING_API` or `DATA_API`. `peak` is the most requests sent in one
    wall-clock minute (UTC), and `peak_minute` that minute's start; the first minute to reach it keeps it.
    `peak_60s` is the most sent within any 60 seconds, which a burst across a minute's end can make
    larger than `peak`. `rate_limited_total` counts every 429, and `rate_limited` keeps the latest
    `RATE_LIMITED_KEPT`, oldest first.
    """

    account: int
    api: str
    total: int
    peak: int
    peak_minute: datetime | None
    peak_60s: int
    rate_limited_total: int
    rate_limited: tuple[RateLimited, ...]


class _Tally:
    def __init__(self) -> None:
        self.total = 0
        self.minute = -1
        self.in_minute = 0
        self.peak = 0
        self.peak_minute = -1
        self.last = float("-inf")
        self.recent: deque[float] = deque()  # send times within the last 60 seconds
        self.peak_60s = 0
        self.rate_limited_total = 0
        self.rate_limited: deque[RateLimited] = deque(maxlen=RATE_LIMITED_KEPT)


class RequestCounter:
    """Requests sent through every `TimeoutAdapter` in this process, per account and per API, in
    wall-clock minutes, and the 429s Alpaca answered. Thread-safe: alpaca-py calls run in worker threads.

    The account is the request's own credential header as alpaca-py sets it (`APCA-API-KEY-ID`, or
    `Authorization` for its other schemes; alpaca-py 0.45.0 `RESTClient._get_auth_headers`), held only as
    an HMAC-SHA256 under a random key made for this counter, so two clients built on one key pair count as
    one account and nothing kept can be matched to a key outside this process.

    The clock is read under the lock, and a minute never moves back: a request stamped before the minute
    in hand, by a clock stepped back, counts in that minute, and in the 60-second window at the latest
    time seen; a 429 is stamped no earlier than that time either.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._salt = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._accounts: dict[bytes, int] = {}
        self._tallies: dict[tuple[int, str], _Tally] = {}

    def _key(self, request: requests.PreparedRequest) -> tuple[bytes, str]:
        """`request`'s account, as a keyed digest, and its API."""
        headers = request.headers
        credential = headers.get("APCA-API-KEY-ID") or headers.get("Authorization") or ""
        if isinstance(credential, str):
            credential = credential.encode()
        digest = hmac.digest(self._salt, credential, hashlib.sha256)
        host = (urlsplit(request.url or "").hostname or "").rstrip(".")
        api = DATA_API if host in DATA_HOSTS else TRADING_API
        return digest, api

    def _tally(self, key: tuple[bytes, str]) -> _Tally:
        """The tally for `key`'s account and API; the caller holds the lock."""
        digest, api = key
        account = self._accounts.setdefault(digest, len(self._accounts) + 1)
        return self._tallies.setdefault((account, api), _Tally())

    def sent(self, request: requests.PreparedRequest) -> None:
        """Count `request`, handed to requests' transport now."""
        key = self._key(request)
        with self._lock:
            tally = self._tally(key)
            now = max(self._clock(), tally.last)
            tally.last = now
            tally.total += 1
            minute = int(now // 60)
            if minute != tally.minute:
                tally.minute, tally.in_minute = minute, 0
            tally.in_minute += 1
            if tally.in_minute > tally.peak:
                tally.peak, tally.peak_minute = tally.in_minute, minute
            tally.recent.append(now)
            while tally.recent[0] <= now - 60:
                tally.recent.popleft()
            tally.peak_60s = max(tally.peak_60s, len(tally.recent))

    def answered(self, request: requests.PreparedRequest, status: int) -> None:
        """Record a 429 answer to `request`; any other status is not kept."""
        if status != HTTPStatus.TOO_MANY_REQUESTS:
            return
        key = self._key(request)
        path = urlsplit(request.url or "").path
        with self._lock:
            tally = self._tally(key)
            record = RateLimited(
                name=f"{status} {HTTPStatus(status).phrase}",
                at=datetime.fromtimestamp(max(self._clock(), tally.last), UTC),
                path=path,
            )
            tally.rate_limited_total += 1
            tally.rate_limited.append(record)

    def counts(self) -> tuple[RequestCount, ...]:
        """A snapshot of every account's and API's counts, by account, then trading before data."""
        with self._lock:
            return tuple(
                RequestCount(
                    account=account,
                    api=api,
                    total=t.total,
                    peak=t.peak,
                    peak_minute=(
                        datetime.fromtimestamp(t.peak_minute * 60, UTC) if t.peak else None
                    ),
                    peak_60s=t.peak_60s,
                    rate_limited_total=t.rate_limited_total,
                    rate_limited=tuple(t.rate_limited),
                )
                for (account, api), t in sorted(
                    self._tallies.items(), key=lambda i: (i[0][0], i[0][1] != TRADING_API)
                )
            )


REQUESTS = RequestCounter()
"""The process's one counter, which every `TimeoutAdapter` adds to."""


def request_counts() -> tuple[RequestCount, ...]:
    """What this process has sent to Alpaca's REST APIs through this package's clients so far, per account
    and API (`RequestCount`), for a node or a session window to read at its end. Read-only: a snapshot."""
    return REQUESTS.counts()


CONNECT_TIMEOUT_SECS = 3.05
"""requests' "Timeouts" (Advanced Usage): "It's a good practice to set connect timeouts to slightly larger
than a multiple of 3, which is the default TCP packet retransmission window"; its example is
`timeout=(3.05, 27)` (ADR 0039). It applies per address tried."""

READ_TIMEOUT_SECS = 27.0
"""The read timeout of the same example: the wait "between bytes sent from the server", not the whole
response (ADR 0039)."""


class TimeoutAdapter(HTTPAdapter):
    """requests' `HTTPAdapter` with a default (connect, read) timeout for a request that passes none.

    It counts the requests each thread sends, so `counted` can tell how many attempts one alpaca-py call
    made: alpaca-py repeats a request answered 429 or 504 (`RESTClient._request`), and a 504 may have
    reached Alpaca. The count is per thread, and a call runs on one thread, so no other call touches it.

    Every request it hands to requests' transport is also added to the process's `RequestCounter`
    (`request_counts`), and a 429 answer recorded there; alpaca-py's own retry is left as it is.

    With `get_only`, every method but GET is refused with `MethodRefused` before anything is sent or
    counted (data-only mode, ADR 0070).
    """

    def __init__(self, timeout: tuple[float, float], *, get_only: bool = False) -> None:
        super().__init__()
        self.timeout = timeout
        self.get_only = get_only
        self._local = threading.local()

    @property
    def sends(self) -> int:
        """Requests sent on the calling thread since its last `reset`."""
        return getattr(self._local, "sends", 0)

    def reset(self) -> None:
        self._local.sends = 0

    def record_send(self) -> None:
        self._local.sends = self.sends + 1

    @override
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        if self.get_only and request.method != "GET":
            raise MethodRefused(
                f"data-only mode ({DATA_ONLY_ENV}=1): {request.method} {request.path_url} refused, "
                "nothing was sent (ADR 0070)"
            )
        self.record_send()
        counter = REQUESTS
        counter.sent(request)
        response = super().send(
            request,
            stream=stream,
            timeout=self.timeout if timeout is None else timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )
        counter.answered(request, response.status_code)
        return response


def session_of(client: object) -> requests.Session | None:
    """The `requests.Session` an alpaca-py REST client keeps at `_session` (alpaca-py 0.45.0,
    `RESTClient.__init__`), or None. The adapter's one use of that private attribute (ADR 0039)."""
    session = getattr(client, "_session", None)
    return session if isinstance(session, requests.Session) else None


def with_http_timeout[C](client: C) -> C:
    """Mount a `TimeoutAdapter` with this package's (connect, read) timeout on an alpaca-py REST client's
    session for both `https://` and `http://`, unless that one adapter is already mounted on both, and
    return the client. A `TimeoutAdapter` with another timeout, or on one scheme only, is replaced. In
    data-only mode the adapter refuses every method but GET, and one that does not is replaced, as is any
    adapter mounted for another prefix, which requests would pick for a longer matching URL; once refusing,
    it stays so.

    Raises `TypeError` if the client holds no `requests.Session` at `_session`, so a client that would
    run without a timeout does not start.
    """
    session = session_of(client)
    if session is None:
        raise TypeError(
            f"{type(client).__name__} has no requests.Session at _session, where alpaca-py 0.45.0 keeps "
            "it; refusing to run without an HTTP timeout (ADR 0039)",
        )
    timeout = (CONNECT_TIMEOUT_SECS, READ_TIMEOUT_SECS)
    mounted = timeout_adapter(client)
    get_only = data_only() or (mounted is not None and mounted.get_only)
    if (
        mounted is None
        or mounted.timeout != timeout
        or mounted.get_only != get_only
        or session.adapters.get("http://") is not mounted
        or (get_only and any(a is not mounted for a in session.adapters.values()))
    ):
        # alpaca-py's session holds requests' two default adapters; they are closed, not kept. A client
        # passed in with an adapter of its own loses that adapter's settings (ADR 0039). In data-only mode
        # an adapter for any other prefix goes too, so no request can bypass the refusal (ADR 0070).
        prefixes = list(session.adapters) if get_only else ["https://", "http://"]
        for old in {id(a): a for a in (session.adapters.get(p) for p in prefixes) if a}.values():
            old.close()
        for prefix in prefixes:
            session.adapters.pop(prefix, None)
        adapter = TimeoutAdapter(timeout, get_only=get_only)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return client


def require_get_only(*clients: object) -> None:
    """In data-only mode, raise `MethodRefused` unless every adapter on each client's session is one
    `TimeoutAdapter` that refuses every method but GET (ADR 0070); outside it, do nothing."""
    if not data_only():
        return
    for client in clients:
        session = session_of(client)
        if session is None:
            raise MethodRefused(
                f"data-only mode ({DATA_ONLY_ENV}=1): {type(client).__name__} has no requests.Session at "
                "_session, so its requests cannot be checked; refusing to start (ADR 0070)"
            )
        adapters = list(session.adapters.values())
        if not adapters or not all(isinstance(a, TimeoutAdapter) and a.get_only for a in adapters):
            raise MethodRefused(
                f"data-only mode ({DATA_ONLY_ENV}=1): {type(client).__name__}'s session has an adapter "
                "that would send a method other than GET; refusing to start (ADR 0070)"
            )


def timeout_adapter(client: object) -> TimeoutAdapter | None:
    """The `TimeoutAdapter` mounted on `client`'s session for HTTPS, or None."""
    session = session_of(client)
    adapter = session.adapters.get("https://") if session is not None else None
    return adapter if isinstance(adapter, TimeoutAdapter) else None


def counted[R](fn: Callable[..., R]) -> Callable[..., R]:
    """`fn`, an alpaca-py client's bound method, with its attempts counted on the thread that runs it.

    A failure leaves the count on the exception as `alpaca_sends`, read by `not_sent`; nothing shared is
    read after the call. A function not bound to a client with a `TimeoutAdapter` is returned as is.
    """
    adapter = timeout_adapter(getattr(fn, "__self__", None))
    if adapter is None:
        return fn

    def call(*args: object) -> R:
        adapter.reset()
        try:
            return fn(*args)
        except BaseException as e:
            e.alpaca_sends = adapter.sends  # ty: ignore[missing-attribute] - read back by `not_sent`
            raise

    call.__name__ = getattr(fn, "__name__", "call")
    return call


def not_sent(error: BaseException) -> bool:
    """Whether `error`, from a `counted` call, proves the call sent nothing: a connect timeout on its only
    attempt. Any other failure, a read timeout included, may have reached Alpaca; so may, as far as this
    can tell, a stall in the TLS handshake, which urllib3 raises as a read timeout."""
    return isinstance(error, requests.ConnectTimeout) and getattr(error, "alpaca_sends", None) == 1


FILTER_KEYS = frozenset({"status", "exchange", "attributes"})
"""`InstrumentProviderConfig.filters` keys passed to `GetAssetsRequest`. Its fourth field, `asset_class`,
is fixed to `us_equity` by this provider."""


class AssetsClient(Protocol):
    """The part of alpaca-py's `TradingClient` this provider uses (signatures as in alpaca-py 0.45.0)."""

    def get_all_assets(self, filter: GetAssetsRequest | None = None) -> list[Asset] | RawData: ...

    def get_asset(self, symbol_or_asset_id: UUID | str) -> Asset | RawData: ...


def credentials_from_env() -> tuple[str, str]:
    """Return the paper key pair from the environment, or raise naming the variables to set.

    The values are never logged or stored outside the alpaca-py client they are passed to.
    """
    api_key = os.environ.get(ENV_API_KEY)
    secret_key = os.environ.get(ENV_SECRET_KEY)
    if not api_key or not secret_key:
        raise RuntimeError(
            f"Alpaca paper credentials are not in the environment: set {ENV_API_KEY} and "
            f"{ENV_SECRET_KEY}",
        )
    return api_key, secret_key


def assets_request(filters: dict | None) -> GetAssetsRequest:
    """Build the assets query for active US equities, narrowed by the provider config's filters."""
    filters = dict(filters or {})
    unknown = set(filters) - FILTER_KEYS
    if unknown:
        raise ValueError(
            f"Unsupported Alpaca asset filters {sorted(unknown)}; supported: {sorted(FILTER_KEYS)}",
        )
    status = filters.get("status", AssetStatus.ACTIVE)
    if status != AssetStatus.ACTIVE:
        raise ValueError(f"Alpaca asset filter status={status!r}: only active assets are loaded")
    return GetAssetsRequest(
        asset_class=AssetClass.US_EQUITY,
        status=status,
        exchange=filters.get("exchange"),
        attributes=filters.get("attributes"),
    )


def parse_equity(asset: Asset, ts_init: int) -> Equity:
    """Convert one active Alpaca asset into a NautilusTrader `Equity` keyed on the Alpaca venue.

    An asset that is not tradable on Alpaca (`tradable=False`) is accepted: this adapter serves market
    data only, and a symbol's data does not depend on whether Alpaca lets the account trade it. The flag
    is kept in `Equity.info`; an execution client must refuse orders for it.
    """
    if asset.asset_class != AssetClass.US_EQUITY:
        raise ValueError(f"{asset.symbol} is {asset.asset_class.value}, not us_equity")
    if asset.status != AssetStatus.ACTIVE:
        raise ValueError(f"{asset.symbol}.{VENUE} is {asset.status.value}, not active")
    symbol = Symbol(asset.symbol)
    return Equity(
        instrument_id=InstrumentId(symbol, VENUE),
        raw_symbol=symbol,
        currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=PRICE_INCREMENT,
        ts_event=ts_init,
        ts_init=ts_init,
        info={
            "asset_id": str(asset.id),
            "name": asset.name,
            "exchange": asset.exchange.value,
            "status": asset.status.value,
            "tradable": asset.tradable,
            "fractionable": asset.fractionable,
            "marginable": asset.marginable,
            "shortable": asset.shortable,
            "borrow_status": asset.borrow_status.value if asset.borrow_status else None,
            "attributes": list(asset.attributes or []),
            "cusip": asset.cusip,
        },
    )


class AlpacaInstrumentProvider(InstrumentProvider):
    """Load US equities from Alpaca's assets endpoint into adapter-local storage."""

    def __init__(
        self,
        config: InstrumentProviderConfig | None = None,
        *,
        assets_client: AssetsClient | None = None,
    ) -> None:
        """Retain configuration only. The alpaca-py client is built on the first load."""
        if config is not None and config.filter_callable is not None:
            raise ValueError(
                "AlpacaInstrumentProvider does not support InstrumentProviderConfig.filter_callable; "
                "use load_ids, or load_all with filters",
            )
        super().__init__(config)
        self._assets_client = assets_client

    def _client(self) -> AssetsClient:
        if self._assets_client is None:
            api_key, secret_key = credentials_from_env()
            self._assets_client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        client = with_http_timeout(self._assets_client)  # a client passed in too (ADR 0039)
        require_get_only(client)  # in data-only mode, before anything is sent on it (ADR 0070)
        return client

    @override
    async def load_all_async(self, filters: dict | None = None) -> None:
        """Load every asset matching `filters` (default: active US equities)."""
        request = assets_request(filters)
        assets = await asyncio.to_thread(self._client().get_all_assets, request)
        if not isinstance(assets, list):
            raise TypeError("alpaca-py returned raw asset data; the client must use raw_data=False")
        ts_init = time.time_ns()
        for asset in assets:
            self.add(parse_equity(asset, ts_init))

    @override
    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """Load each requested equity by symbol, keeping instruments loaded earlier.

        Every ID is attempted; if any cannot be loaded, the error names all of them.
        """
        if filters:
            raise ValueError("Alpaca asset filters apply to load_all only, not to load_ids")
        if not instrument_ids:
            return
        foreign = [str(i) for i in instrument_ids if i.venue != VENUE]
        if foreign:
            raise ValueError(f"Not {VENUE} instruments: {foreign}")

        client = self._client()
        failures = []
        for instrument_id in instrument_ids:
            symbol = instrument_id.symbol.value
            try:
                asset = await asyncio.to_thread(client.get_asset, symbol)
                if not isinstance(asset, Asset):
                    raise TypeError("alpaca-py returned raw asset data")
                if asset.symbol != symbol:
                    raise ValueError(f"Alpaca returned asset {asset.symbol!r} for {symbol!r}")
                self.add(parse_equity(asset, time.time_ns()))
            # alpaca-py turns HTTP error responses into APIError and lets the `requests` exceptions for
            # network failures through (`RESTClient._one_request`); those derive from OSError
            # (`requests.exceptions.RequestException(IOError)`). Validation errors are ValueErrors.
            except (APIError, OSError, ValueError, TypeError) as e:
                failures.append(f"{instrument_id}: {e}")
        if failures:
            raise RuntimeError(f"Could not load Alpaca instruments: {'; '.join(failures)}")
