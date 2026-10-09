"""Resolve an unknown submit by its deterministic ``client_order_id`` (E11.1, D71).

``submit()`` raised something other than :class:`~arc.execution.submission.SubmitRefused`:
the request may or may not have reached the venue (a read timeout after the
broker accepted the POST is the classic case). :func:`resolve_unknown_submit`
asks the broker for the order under the attempt's ``client_order_id``:

- found on any lookup → ``accepted`` (the broker has it; its status attached),
- ``None`` on every lookup → ``absent`` (the broker never took it),
- every lookup raised → ``unknown``: one best-effort ``cancel_by_client_id``
  is sent and the outcome records whether it went out.

A broker without ``order_status_by_client_id`` cannot be asked: ``unknown``.

Pure: no DB, no clock, no LLM; the broker is the duck-typed adapter it is handed
(:mod:`arc.broker.base` documents the optional methods) and ``sleep`` is injected.
It never submits: the same ``client_order_id`` is never sent twice (D71).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from arc.broker.base import BrokerOrderStatus  # noqa: TC001 - pydantic field type

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ResolvedSubmit",
    "api_status_code",
    "is_transport_error",
    "resolve_unknown_submit",
]

_TRANSPORT_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
        "ChunkedEncodingError",
        "ProtocolError",
        "RemoteDisconnected",
        "SSLError",
        "ProxyError",
    }
)


class ResolvedSubmit(BaseModel):
    """What the broker says about an attempt whose submit raised."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["accepted", "absent", "unknown"]
    broker_order_id: str | None = None
    status: BrokerOrderStatus | None = None  # when accepted
    lookups: int = 0  # attempts made
    cancel_sent: bool = False  # only when outcome == "unknown"
    error: str = ""  # last transport/API error text


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def is_transport_error(exc: BaseException) -> bool:
    """True for a connect/read timeout or a dropped connection (never an API answer).

    Matched on the exception's class hierarchy names so this module needs no
    network import: ``requests``' ``ConnectionError``/``Timeout`` family, the
    builtin ``ConnectionError``/``TimeoutError`` and ``urllib3`` protocol errors.
    """
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return any(cls.__name__ in _TRANSPORT_NAMES for cls in type(exc).__mro__)


def api_status_code(exc: BaseException) -> int | None:
    """The HTTP status an API error carries (alpaca-py ``APIError.status_code``), else None."""
    try:
        code: Any = getattr(exc, "status_code", None)
    except Exception:  # noqa: BLE001 - a broken property means "no status"
        return None
    return code if isinstance(code, int) else None


def resolve_unknown_submit(
    broker: Any,
    *,
    client_order_id: str,
    attempts: int,
    sleep: Callable[[float], None],
    wait_seconds: float,
) -> ResolvedSubmit:
    """Look *client_order_id* up at most *attempts* times, *wait_seconds* apart."""
    lookup = getattr(broker, "order_status_by_client_id", None)
    if not callable(lookup):
        return _unknown(broker, client_order_id, 0, "broker cannot look orders up by client id")
    attempts = max(1, attempts)
    error = ""
    failed = 0
    for i in range(attempts):
        if i:
            sleep(wait_seconds)
        try:
            st: BrokerOrderStatus | None = lookup(client_order_id)
        except Exception as exc:  # noqa: BLE001 - recorded; decides absent vs unknown
            failed += 1
            error = _err(exc)
            continue
        if st is not None:
            return ResolvedSubmit(
                outcome="accepted",
                broker_order_id=st.broker_order_id,
                status=st,
                lookups=i + 1,
                error=error,
            )
    if failed < attempts:
        # At least one lookup answered "no such order" (404) after the POST had
        # already returned: the broker never took it.
        return ResolvedSubmit(outcome="absent", lookups=attempts, error=error)
    return _unknown(broker, client_order_id, attempts, error)


def _unknown(broker: Any, client_order_id: str, lookups: int, error: str) -> ResolvedSubmit:
    cancel = getattr(broker, "cancel_by_client_id", None)
    sent = False
    if callable(cancel):
        try:
            cancel(client_order_id)
            sent = True
        except Exception as exc:  # noqa: BLE001 - best effort; reconcile resolves it
            sent = True  # the request went out; its answer is unknown
            error = f"{error}; cancel: {_err(exc)}" if error else f"cancel: {_err(exc)}"
    return ResolvedSubmit(outcome="unknown", lookups=lookups, cancel_sent=sent, error=error)
