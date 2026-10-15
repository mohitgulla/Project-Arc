"""D114: bounded retry of transient Alpaca market-data reads. Mocked transport, no network.

XP-1 day 1 (2026-10-14 ~14:00Z): ``rpc error: code = Unavailable desc = name resolver
error`` failed production ``research`` and two arms' ``risk.open`` for a whole slot.
"""

from __future__ import annotations

import random
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from structlog.testing import capture_logs

from arc.config import ArcSettings
from arc.data.alpaca import AlpacaMarketData
from arc.utils.http_retry import (
    ReadRetryPolicy,
    ReadRetrySession,
    install_read_retry,
    is_transient_error,
)

KEYS = {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}
UNAVAILABLE = (
    b'{"message":"rpc error: code = Unavailable desc = name resolver error: lookup '
    b'marketcache-indicative.marketdata on 10.62.16.10:53: no such host"}'
)
QUOTE = (
    b'{"quotes":{"AAPL":{"t":"2026-10-14T14:00:00Z","bp":250.0,"bs":1,"ap":250.2,'
    b'"as":1,"ax":"V","bx":"V","c":["R"],"z":"C"}}}'
)


class Script(requests.adapters.HTTPAdapter):
    """Transport double: answers each request with the next scripted step.

    A step is ``(status, body)`` or an exception instance to raise.
    """

    def __init__(self, steps: list[tuple[int, bytes] | BaseException]) -> None:
        super().__init__()
        self.steps = list(steps)
        self.calls: list[str] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.calls.append(f"{request.method} {request.url}")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        status, body = step
        resp = requests.Response()
        resp.status_code = status
        resp._content = body  # noqa: SLF001
        resp.request = request
        resp.url = str(request.url)
        return resp


class Bucket:
    """Rate-bucket double: counts the tokens taken."""

    def __init__(self) -> None:
        self.taken: list[str] = []

    def acquire(self, kind: str = "read") -> float:
        self.taken.append(kind)
        return 0.0


class Clock:
    """Monotonic clock double driven by the sleep double."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _session(
    steps: list[tuple[int, bytes] | BaseException],
    policy: ReadRetryPolicy | None = None,
    *,
    seed: int = 7,
) -> tuple[ReadRetrySession, Script, Bucket, Clock]:
    clock, bucket = Clock(), Bucket()
    s = ReadRetrySession(
        policy or ReadRetryPolicy(),
        bucket=bucket,
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(seed),
    )
    script = Script(steps)
    s.mount("https://", script)
    return s, script, bucket, clock


def _dns_error() -> requests.ConnectionError:
    return requests.ConnectionError("Failed to resolve 'data.alpaca.markets' (no such host)")


URL = "https://data.alpaca.markets/v2/stocks/quotes/latest?symbols=AAPL"


# ---------------------------------------------------------------------------
# Session: transient failures twice then success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail",
    [(503, UNAVAILABLE), (500, UNAVAILABLE), (502, b"bad gateway")],
)
def test_unavailable_twice_then_success(fail: tuple[int, bytes]) -> None:
    s, script, bucket, clock = _session([fail, fail, (200, QUOTE)])
    with capture_logs() as logs:
        resp = s.get(URL)
    assert resp.status_code == 200
    assert len(script.calls) == 3
    # 0.5 s then 1.5 s, each jittered within +/-25%
    assert len(clock.sleeps) == 2
    assert 0.375 <= clock.sleeps[0] <= 0.625
    assert 1.125 <= clock.sleeps[1] <= 1.875
    assert sum(clock.sleeps) <= 3.0
    assert bucket.taken == ["read", "read"]  # one token per retry, none for the first try
    retries = [e for e in logs if e["event"] == "marketdata.retry"]
    assert [e["attempt"] for e in retries] == [1, 2]
    assert retries[0]["error_class"] == f"HTTP{fail[0]}"
    assert retries[0]["path"] == "v2/stocks/quotes/latest"
    assert not [e for e in logs if e["event"] == "marketdata.retry_exhausted"]


@pytest.mark.parametrize(
    "exc",
    [
        _dns_error(),
        requests.ConnectionError("Connection reset by peer"),
        requests.ReadTimeout("read timed out"),
        requests.ConnectTimeout("connect timed out"),
        requests.exceptions.ChunkedEncodingError("connection broken"),
    ],
)
def test_network_errors_twice_then_success(exc: BaseException) -> None:
    s, script, bucket, clock = _session([exc, exc, (200, QUOTE)])
    with capture_logs() as logs:
        assert s.get(URL).status_code == 200
    assert len(script.calls) == 3
    assert bucket.taken == ["read", "read"]
    assert [e["error_class"] for e in logs if e["event"] == "marketdata.retry"] == [
        type(exc).__name__
    ] * 2


# ---------------------------------------------------------------------------
# Exhaustion: today's failure, unchanged
# ---------------------------------------------------------------------------


def test_three_unavailable_returns_the_last_response() -> None:
    s, script, bucket, clock = _session([(503, UNAVAILABLE)] * 3)
    with capture_logs() as logs:
        resp = s.get(URL)
    assert resp.status_code == 503 and resp.content == UNAVAILABLE
    assert len(script.calls) == 3
    assert bucket.taken == ["read", "read"]
    exhausted = [e for e in logs if e["event"] == "marketdata.retry_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["attempts"] == 3 and exhausted[0]["error_class"] == "HTTP503"


def test_three_dns_errors_reraise_the_last_error() -> None:
    last = _dns_error()
    s, script, _, _ = _session([_dns_error(), _dns_error(), last])
    with capture_logs() as logs, pytest.raises(requests.ConnectionError) as info:
        s.get(URL)
    assert info.value is last
    assert len(script.calls) == 3
    assert [e["event"] for e in logs] == [
        "marketdata.retry",
        "marketdata.retry",
        "marketdata.retry_exhausted",
    ]


def test_budget_caps_total_sleep() -> None:
    policy = ReadRetryPolicy(retries=3, base_s=2.0, factor=3.0, jitter=0.0, budget_s=3.0)
    s, script, _, clock = _session([(503, UNAVAILABLE)] * 4, policy)
    assert s.get(URL).status_code == 503
    # 2.0 s, then the 1.0 s left of the budget, then nothing left: exhausted after 3 tries
    assert clock.sleeps == [2.0, 1.0]
    assert len(script.calls) == 3


def test_retries_zero_is_off() -> None:
    s, script, bucket, clock = _session([(503, UNAVAILABLE)], ReadRetryPolicy(retries=0))
    assert s.get(URL).status_code == 503
    assert len(script.calls) == 1 and clock.sleeps == [] and bucket.taken == []


# ---------------------------------------------------------------------------
# Never retried: 4xx/auth, alpaca-py's own codes, non-transient errors, writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 504])
def test_4xx_auth_and_alpaca_owned_codes_are_not_retried(status: int) -> None:
    s, script, bucket, clock = _session([(status, b'{"message":"forbidden"}'), (200, QUOTE)])
    with capture_logs() as logs:
        assert s.get(URL).status_code == status
    assert len(script.calls) == 1
    assert clock.sleeps == [] and bucket.taken == []
    assert logs == []


def test_non_transient_exception_is_not_retried() -> None:
    s, script, _, clock = _session([ValueError("bad url"), (200, QUOTE)])
    with pytest.raises(ValueError, match="bad url"):
        s.get(URL)
    assert len(script.calls) == 1 and clock.sleeps == []


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_order_submit_and_other_writes_are_never_retried(method: str) -> None:
    for fail in (_dns_error(), (503, UNAVAILABLE)):
        s, script, bucket, clock = _session([fail, (200, b"{}")])
        url = "https://paper-api.alpaca.markets/v2/orders"
        if isinstance(fail, BaseException):
            with pytest.raises(requests.ConnectionError):
                s.request(method, url, json={})
        else:
            assert s.request(method, url, json={}).status_code == 503
        assert len(script.calls) == 1
        assert clock.sleeps == [] and bucket.taken == []


def test_is_transient_error() -> None:
    assert is_transient_error(_dns_error())
    assert is_transient_error(requests.ReadTimeout())
    assert not is_transient_error(requests.HTTPError())
    assert not is_transient_error(ValueError())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"retries": -1},
        {"base_s": -0.1},
        {"budget_s": -1.0},
        {"factor": 0.5},
        {"jitter": 1.0},
        {"jitter": -0.1},
    ],
)
def test_policy_rejects_bad_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be|bad retry policy"):
        ReadRetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_policy_delay_spent_budget_returns_none() -> None:
    p = ReadRetryPolicy()
    assert p.delay(1, 3.0, random.Random(1)) is None
    assert p.delay(3, 0.0, random.Random(1)) is None


# ---------------------------------------------------------------------------
# install_read_retry + the adapter wiring
# ---------------------------------------------------------------------------


def test_install_keeps_headers_and_adapters_and_is_idempotent() -> None:
    client = StockHistoricalDataClient(api_key="k", secret_key="s")
    client._session.headers["X-Probe"] = "1"  # noqa: SLF001
    script = Script([])
    client._session.mount("https://data.alpaca.markets", script)  # noqa: SLF001
    session = install_read_retry(client, ReadRetryPolicy())
    assert isinstance(session, ReadRetrySession)
    assert client._session is session  # noqa: SLF001
    assert session.headers["X-Probe"] == "1"
    assert session.get_adapter("https://data.alpaca.markets/v2") is script
    assert install_read_retry(client, ReadRetryPolicy()) is session


def test_install_leaves_a_test_double_alone() -> None:
    double = MagicMock()
    double._session = MagicMock()  # noqa: SLF001
    assert install_read_retry(double, ReadRetryPolicy()) is None
    assert install_read_retry(object(), ReadRetryPolicy()) is None


def _adapter(
    steps: list[tuple[int, bytes] | BaseException], policy: ReadRetryPolicy | None = None
) -> tuple[AlpacaMarketData, Script, Bucket, Clock]:
    """A real-client AlpacaMarketData whose stock transport is *steps*."""
    bucket = Bucket()
    with patch.dict("os.environ", KEYS):
        md = AlpacaMarketData(
            data_feed="iex",
            options_feed="indicative",
            read_retry=policy or ReadRetryPolicy(),
            rate_bucket=bucket,
        )
    session = md._stock_client._session  # noqa: SLF001
    assert isinstance(session, ReadRetrySession)
    clock = Clock()
    session._sleep, session._clock = clock.sleep, clock  # noqa: SLF001
    session._rng = random.Random(3)  # noqa: SLF001
    script = Script(steps)
    session.mount("https://", script)
    return md, script, bucket, clock


def test_every_data_client_gets_the_retry_session() -> None:
    with patch.dict("os.environ", KEYS):
        md = AlpacaMarketData(data_feed="iex", options_feed="indicative", rate_bucket=Bucket())
    for client in (
        md._option_client,  # noqa: SLF001
        md._stock_client,  # noqa: SLF001
        md._contracts_client,  # noqa: SLF001
        md._raw_option_client,  # noqa: SLF001
    ):
        assert isinstance(client._session, ReadRetrySession)  # noqa: SLF001
    assert isinstance(md._contracts_client, TradingClient)  # noqa: SLF001


def test_underlying_quote_survives_two_unavailable() -> None:
    md, script, bucket, _ = _adapter([(503, UNAVAILABLE), _dns_error(), (200, QUOTE)])
    q = md.underlying_quote("AAPL")
    assert (q.bid, q.ask) == (250.0, 250.2)
    assert len(script.calls) == 3
    assert bucket.taken == ["read", "read"]


def test_underlying_quote_three_unavailable_raises_todays_apierror() -> None:
    md, script, _, _ = _adapter([(503, UNAVAILABLE)] * 3)
    with pytest.raises(APIError, match="code = Unavailable"):
        md.underlying_quote("AAPL")
    assert len(script.calls) == 3


def test_underlying_quote_403_raises_at_once() -> None:
    body = b'{"message":"subscription does not permit querying recent SIP data"}'
    md, script, bucket, clock = _adapter([(403, body), (200, QUOTE)])
    with pytest.raises(APIError, match="subscription does not permit"):
        md.underlying_quote("AAPL")
    assert len(script.calls) == 1 and clock.sleeps == [] and bucket.taken == []


def test_policy_defaults_come_from_settings() -> None:
    default = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert (
        default.alpaca_read_retries,
        default.alpaca_read_retry_base_s,
        default.alpaca_read_retry_budget_s,
    ) == (2, 0.5, 3.0)
    env = {**KEYS, "ARC_ALPACA_READ_RETRIES": "0", "ARC_ALPACA_READ_RETRY_BUDGET_S": "1.5"}
    with patch.dict("os.environ", env):
        md = AlpacaMarketData(data_feed="iex", options_feed="indicative")
    assert md.read_retry.retries == 0
    assert md.read_retry.budget_s == 1.5
    session = md._stock_client._session  # noqa: SLF001
    assert isinstance(session, ReadRetrySession)
    assert session.bucket is not None and session.bucket.rate_per_min == 180
