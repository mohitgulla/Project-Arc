"""E11.1 (D71): ``resolve_unknown_submit``, pure and deterministic (fake broker, fake sleep)."""

from __future__ import annotations

from decimal import Decimal

import pytest
import requests

from arc.broker.base import BrokerOrderStatus
from arc.execution.resolve import (
    ResolvedSubmit,
    api_status_code,
    is_transport_error,
    resolve_unknown_submit,
)

COID = "arc2.tok.s0"


def st(status: str = "new") -> BrokerOrderStatus:
    return BrokerOrderStatus(broker_order_id="brk-9", status=status, filled_qty=Decimal(0))


class Fake:
    """``answers`` per lookup: a status, None (404) or an exception to raise."""

    def __init__(self, answers: list[object], cancel_error: Exception | None = None) -> None:
        self.answers = answers
        self.lookups: list[str] = []
        self.cancels: list[str] = []
        self.cancel_error = cancel_error

    def order_status_by_client_id(self, coid: str) -> BrokerOrderStatus | None:
        self.lookups.append(coid)
        a = self.answers[min(len(self.lookups) - 1, len(self.answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return a  # type: ignore[return-value]

    def cancel_by_client_id(self, coid: str) -> None:
        self.cancels.append(coid)
        if self.cancel_error is not None:
            raise self.cancel_error


def resolve(broker: object, attempts: int = 3) -> tuple[ResolvedSubmit, list[float]]:
    slept: list[float] = []
    res = resolve_unknown_submit(
        broker, client_order_id=COID, attempts=attempts, sleep=slept.append, wait_seconds=2.0
    )
    return res, slept


def test_accepted_after_timeout() -> None:
    """Submit raised ReadTimeout but the broker has the order: accepted on lookup 1."""
    b = Fake([st("new")])
    res, slept = resolve(b)
    assert res.outcome == "accepted" and res.lookups == 1
    assert res.broker_order_id == "brk-9" and res.status == st("new")
    assert slept == [] and b.cancels == []


def test_accepted_on_a_later_lookup_after_errors() -> None:
    b = Fake([requests.ReadTimeout("slow"), st("filled")])
    res, slept = resolve(b)
    assert res.outcome == "accepted" and res.lookups == 2 and slept == [2.0]
    assert "ReadTimeout" in res.error


def test_absent_after_connect_error() -> None:
    b = Fake([None])
    res, slept = resolve(b)
    assert res.outcome == "absent" and res.lookups == 3 and res.cancel_sent is False
    assert b.lookups == [COID] * 3 and slept == [2.0, 2.0] and b.cancels == []


def test_absent_when_any_lookup_answered_404() -> None:
    b = Fake([requests.ConnectionError("x"), None, requests.ConnectionError("y")])
    res, _ = resolve(b)
    assert res.outcome == "absent" and res.cancel_sent is False and b.cancels == []


def test_unknown_sends_cancel_by_client_id() -> None:
    b = Fake([requests.ConnectionError("down")], cancel_error=RuntimeError("cancel 500"))
    res, _ = resolve(b)
    assert res.outcome == "unknown" and res.cancel_sent is True and res.lookups == 3
    assert b.cancels == [COID]
    assert "ConnectionError" in res.error and "cancel: RuntimeError: cancel 500" in res.error


def test_unknown_cancel_ok() -> None:
    b = Fake([requests.ReadTimeout("x")])
    res, _ = resolve(b, attempts=1)
    assert res.outcome == "unknown" and res.cancel_sent and res.lookups == 1
    assert "cancel" not in res.error


def test_broker_without_lookup_is_unknown_and_cancel_not_sent() -> None:
    res, _ = resolve(object())
    assert res.outcome == "unknown" and res.lookups == 0 and res.cancel_sent is False
    assert "cannot look orders up" in res.error


def test_broker_without_lookup_but_with_cancel() -> None:
    class CancelOnly:
        def __init__(self) -> None:
            self.cancels: list[str] = []

        def cancel_by_client_id(self, coid: str) -> None:
            self.cancels.append(coid)

    b = CancelOnly()
    res, _ = resolve(b)
    assert res.outcome == "unknown" and res.cancel_sent and b.cancels == [COID]


def test_attempts_floor_is_one() -> None:
    b = Fake([None])
    res, _ = resolve(b, attempts=0)
    assert res.lookups == 1 and res.outcome == "absent"


def test_resolved_submit_is_frozen_and_strict() -> None:
    r = ResolvedSubmit(outcome="absent")
    with pytest.raises(ValueError):
        ResolvedSubmit(outcome="absent", extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        r.lookups = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("exc", "transport"),
    [
        (requests.ReadTimeout("t"), True),
        (requests.ConnectTimeout("t"), True),
        (requests.ConnectionError("t"), True),
        (ConnectionResetError("t"), True),
        (TimeoutError("t"), True),
        (RuntimeError("503"), False),
        (ValueError("bad"), False),
    ],
)
def test_is_transport_error(exc: Exception, transport: bool) -> None:
    assert is_transport_error(exc) is transport


def test_api_status_code() -> None:
    class E(Exception):
        status_code = 422

    class Broken(Exception):
        @property
        def status_code(self) -> int:
            raise RuntimeError("no response")

    assert api_status_code(E()) == 422
    assert api_status_code(RuntimeError()) is None
    assert api_status_code(Broken()) is None
