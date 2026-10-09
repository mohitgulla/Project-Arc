"""E11.1 (D71): broker HTTP timeouts and order lookup by client_order_id. Mocked, no network."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from arc.broker.alpaca_paper import AlpacaPaperBroker, _make_client
from arc.broker.http import TimeoutSession, install_timeouts
from arc.config import ArcSettings

KEYS = {"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}


class RecordingAdapter(requests.adapters.HTTPAdapter):
    """Transport double: records the kwargs ``send`` gets and answers 200 ``{}``."""

    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[Any] = []

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        self.timeouts.append(kwargs.get("timeout"))
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"  # noqa: SLF001
        resp.request = request
        resp.url = str(request.url)
        return resp


def _api_error(status: int, message: str = "x") -> APIError:
    resp = requests.Response()
    resp.status_code = status
    http = requests.HTTPError(response=resp)
    return APIError(json.dumps({"code": status * 100, "message": message}), http)


# ---------------------------------------------------------------------------
# TimeoutSession
# ---------------------------------------------------------------------------


def test_timeout_session_injects_timeout() -> None:
    """Every Session.request carries (connect, read); configurable via settings."""
    s = TimeoutSession(5.0, 15.0)
    adapter = RecordingAdapter()
    s.mount("https://", adapter)
    s.request("GET", "https://paper-api.alpaca.markets/v2/account")
    s.request("POST", "https://paper-api.alpaca.markets/v2/orders", json={}, timeout=None)
    s.get("https://paper-api.alpaca.markets/v2/orders")
    assert adapter.timeouts == [(5.0, 15.0)] * 3

    # Defaults come from ArcSettings (5 s / 15 s) and follow its env overrides.
    default = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert (
        default.execution_broker_connect_timeout_s,
        default.execution_broker_read_timeout_s,
    ) == (5.0, 15.0)
    with patch.dict(
        "os.environ",
        {
            **KEYS,
            "ARC_EXECUTION_BROKER_CONNECT_TIMEOUT_S": "2",
            "ARC_EXECUTION_BROKER_READ_TIMEOUT_S": "7.5",
        },
    ):
        client = _make_client()
    assert isinstance(client._session, TimeoutSession)  # noqa: SLF001
    assert client._session.timeout == (2.0, 7.5)  # noqa: SLF001


def test_timeout_session_keeps_explicit_timeout_and_rejects_nonpositive() -> None:
    s = TimeoutSession(5.0, 15.0)
    adapter = RecordingAdapter()
    s.mount("https://", adapter)
    s.request("GET", "https://example.test/x", timeout=1.0)
    assert adapter.timeouts == [1.0]
    with pytest.raises(ValueError, match="must be > 0"):
        TimeoutSession(0, 15)


def test_alpaca_client_requests_go_through_the_timeout_session() -> None:
    """The real alpaca-py RESTClient path (``_one_request``) carries the timeout."""
    with patch.dict("os.environ", KEYS):
        client = _make_client(timeouts=(3.0, 9.0))
    adapter = RecordingAdapter()
    client._session.mount("https://", adapter)  # noqa: SLF001
    client.get("/account")
    assert adapter.timeouts == [(3.0, 9.0)]


def test_install_timeouts_replaces_session() -> None:
    client = TradingClient("k", "s", paper=True)
    sess = install_timeouts(client, connect_s=1.0, read_s=2.0)
    assert client._session is sess and sess.timeout == (1.0, 2.0)  # noqa: SLF001


def test_broker_passes_timeouts_to_its_client() -> None:
    with patch.dict("os.environ", KEYS):
        b = AlpacaPaperBroker(timeouts=(4.0, 11.0))
    assert b._client._session.timeout == (4.0, 11.0)  # noqa: SLF001


# ---------------------------------------------------------------------------
# by client_order_id
# ---------------------------------------------------------------------------


def _broker(client: MagicMock) -> AlpacaPaperBroker:
    with patch.dict("os.environ", KEYS):
        return AlpacaPaperBroker(client=client)


def _order(status: str = "new") -> MagicMock:
    o = MagicMock()
    o.id = "brk-1"
    o.client_order_id = "arc2.tok.s0"
    o.status = status
    o.filled_qty = "0"
    o.filled_avg_price = None
    o.side = None
    o.legs = None
    o.created_at = None
    o.updated_at = None
    return o


def test_order_status_by_client_id_404_is_none() -> None:
    """APIError 404 -> None; other codes raise."""
    client = MagicMock()
    client.get_order_by_client_id.side_effect = _api_error(404, "order not found")
    assert _broker(client).order_status_by_client_id("arc2.tok.s0") is None

    client.get_order_by_client_id.side_effect = _api_error(500)
    with pytest.raises(APIError):
        _broker(client).order_status_by_client_id("arc2.tok.s0")

    client.get_order_by_client_id.side_effect = requests.ReadTimeout("slow")
    with pytest.raises(requests.ReadTimeout):
        _broker(client).order_status_by_client_id("arc2.tok.s0")


def test_order_status_by_client_id_found() -> None:
    client = MagicMock()
    client.get_order_by_client_id.side_effect = None
    client.get_order_by_client_id.return_value = _order("filled")
    st = _broker(client).order_status_by_client_id("arc2.tok.s0")
    assert st is not None and st.broker_order_id == "brk-1" and st.status == "filled"
    client.get_order_by_client_id.assert_called_once_with("arc2.tok.s0")


def test_order_status_by_client_id_accepts_arm_and_test_formats() -> None:
    """The stored client_order_id is passed through as-is (control, E15.2 arm, test.)."""
    client = MagicMock()
    client.get_order_by_client_id.return_value = _order()
    b = _broker(client)
    for coid in ("arc2.tok.s1", "arc2.t1.tok.s2", "test.arc2.tok.s0", "arc1tok"):
        b.order_status_by_client_id(coid)
    assert [c.args[0] for c in client.get_order_by_client_id.call_args_list] == [
        "arc2.tok.s1",
        "arc2.t1.tok.s2",
        "test.arc2.tok.s0",
        "arc1tok",
    ]


def test_cancel_by_client_id() -> None:
    client = MagicMock()
    client.get_order_by_client_id.return_value = _order()
    _broker(client).cancel_by_client_id("arc2.tok.s0")
    client.cancel_order_by_id.assert_called_once_with("brk-1")

    client = MagicMock()
    client.get_order_by_client_id.side_effect = _api_error(404)
    _broker(client).cancel_by_client_id("arc2.tok.s0")
    client.cancel_order_by_id.assert_not_called()


def test_order_status_dict_fallback_keeps_client_id() -> None:
    client = MagicMock()
    client.get_order_by_id.return_value = {"id": "b", "status": "new", "client_order_id": "c"}
    st = _broker(client).order_status("b")
    assert (st.broker_order_id, st.status, st.client_order_id) == ("b", "new", "c")


def test_broker_notice_says_intraday_reconcile_queued() -> None:
    """D71: the Broker's notice for an unconfirmed ladder names the queued reconcile."""
    from types import SimpleNamespace

    from arc.broker.ladder_job import _notice
    from arc.execution.ladder import ExecStatus, ExecutionOutcome
    from arc.gate.band import PriceBand

    band = PriceBand(lo=Decimal("-0.85"), hi=Decimal("-0.76"), max_steps=3)
    out = ExecutionOutcome("h" * 64, ExecStatus.UNCONFIRMED, band, "SPY", detail="lookups failed")
    on = SimpleNamespace(settings=ArcSettings(_env_file=None))  # type: ignore[call-arg]
    assert _notice(on, out, ticker="SPY", what="open") == (  # type: ignore[arg-type]
        "SPY open UNCONFIRMED — intraday reconcile queued: lookups failed"
    )
    off = SimpleNamespace(
        settings=ArcSettings(_env_file=None, execution_intraday_reconcile=False)  # type: ignore[call-arg]
    )
    assert _notice(off, out, ticker="SPY", what="open") == "SPY open unconfirmed: lookups failed"  # type: ignore[arg-type]
