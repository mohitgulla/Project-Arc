"""Explicit HTTP timeouts for the broker's REST client (E11.1, D71).

alpaca-py 0.44's ``RESTClient`` calls ``self._session.request(method, url, **opts)``
with no ``timeout`` and its ``TradingClient`` takes no timeout argument, so a
stalled connection blocks forever (``requests``' default). :class:`TimeoutSession`
is a drop-in ``requests.Session`` that puts ``timeout=(connect_s, read_s)`` on
every request; :func:`install_timeouts` swaps it into a client.

A submit whose response never arrives is then a ``requests.ReadTimeout`` after
``read_s`` seconds, which the ladder resolves by ``client_order_id``
(:mod:`arc.execution.resolve`) instead of waiting.
"""

from __future__ import annotations

from typing import Any

import requests
import structlog

__all__ = ["TimeoutSession", "install_timeouts"]

log = structlog.get_logger(__name__)


class TimeoutSession(requests.Session):
    """``requests.Session`` whose every request carries ``timeout=(connect_s, read_s)``.

    D69 (E15.3): with a *bucket* (:class:`arc.broker.ratelimit.AccountRateBucket`) every
    request first takes a token from the account's shared bucket (order submits ahead
    of reads), and a 429 answer empties it and is logged with the bucket state.
    """

    def __init__(self, connect_s: float, read_s: float, bucket: Any = None) -> None:
        if connect_s <= 0 or read_s <= 0:
            msg = f"timeouts must be > 0 (connect {connect_s}, read {read_s})"
            raise ValueError(msg)
        super().__init__()
        self.timeout: tuple[float, float] = (float(connect_s), float(read_s))
        self.bucket = bucket

    def request(  # type: ignore[override]
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        # An explicit per-call timeout is kept; a missing one or None (requests'
        # "wait forever") gets the session's bound.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.timeout
        if self.bucket is None:
            return super().request(method, url, *args, **kwargs)
        from arc.broker.ratelimit import request_kind

        kind = request_kind(method, url)
        waited = self.bucket.acquire(kind)
        resp = super().request(method, url, *args, **kwargs)
        if resp.status_code == 429:
            state = self.bucket.penalise()
            log.warning(
                "alpaca.rate_limited",
                method=method.upper(),
                path=url.split("?", 1)[0].rsplit("/v2", 1)[-1],
                kind=kind,
                waited_s=round(waited, 3),
                bucket=state.as_dict(),
            )
        return resp


def install_timeouts(
    client: Any, *, connect_s: float, read_s: float, bucket: Any = None
) -> TimeoutSession:
    """Replace *client*'s ``_session`` with a :class:`TimeoutSession`; return it.

    The old session's headers carry over (alpaca-py sets its headers per request,
    so there is nothing else to copy). *bucket*: the account's shared rate bucket.
    """
    session = TimeoutSession(connect_s, read_s, bucket)
    old = getattr(client, "_session", None)
    if isinstance(old, requests.Session):
        session.headers.update(old.headers)
        old.close()
    client._session = session  # noqa: SLF001 - alpaca-py exposes no timeout hook
    return session
