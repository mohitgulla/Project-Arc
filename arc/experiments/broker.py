"""The broker an Arc process may trade through, chosen by its store (E10.2, D44).

Every trading entry point (the loop's ``PipelineEnv.live``, the Broker ladder,
the Broker reconcile, ``arc execute``, ``arc budget``) builds its broker here
from the store connection it already holds:

* a control store (no ``arm_identity``): the production paper broker,
  ``resolve_broker(settings)`` (E13.11: ``alpaca/paper/rest`` on ``ALPACA_API_KEY``
  exactly as before; any other venue/env/transport raises ``BrokerNotAvailable``);
* an experiment arm store: ``resolve_broker(settings, keys_env=<arm keys_env>)``,
  ``AlpacaPaperBroker`` on the arm's own keys
  (``<keys_env>_API_KEY``; :func:`arc.experiments.arms.arm_keys` refuses
  ``ALPACA`` / ``ALPACA_TEST`` and a key equal to either), wrapped in
  :class:`~arc.experiments.virtual.VirtualBroker` so ``account()`` is the arm's
  virtual account.

So a process pointed at an arm store cannot trade the production account, and
an arm's sizing and gate never see the paper account's balance.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from arc.experiments.arms import ArmIdentity, arm_keys, read_identity

if TYPE_CHECKING:
    import datetime as _dt

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.experiments.virtual import VirtualBroker

__all__ = ["BrokerFactory", "arm_t0", "trading_broker"]

BrokerFactory = Callable[[str | None, str | None], "BrokerAdapter"]


def arm_t0(conn: sqlite3.Connection, arm_id: str) -> _dt.datetime:
    """When the arm's virtual account opened (its ``open`` ledger row)."""
    from arc.context.ttl import from_db

    r = conn.execute(
        "SELECT at FROM virtual_ledger WHERE arm_id = ? AND kind = 'open'", (arm_id,)
    ).fetchone()
    if r is None:
        msg = f"arm {arm_id} has no virtual account; run `arc experiment start` first"
        raise RuntimeError(msg)
    return from_db(r[0])


def _control_opener(ident: ArmIdentity) -> Callable[[], sqlite3.Connection | None]:
    def _open() -> sqlite3.Connection | None:
        p = Path(ident.control_db)
        if not p.is_file():
            return None
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)

    return _open


def trading_broker(
    conn: sqlite3.Connection,
    settings: ArcSettings | None = None,
    *,
    now: Callable[[], _dt.datetime] | None = None,
    factory: BrokerFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> BrokerAdapter:
    """The broker for the store *conn* (see the module doc).

    *factory* ``(api_key, secret_key) -> broker`` (tests); ``(None, None)`` means
    the production default keys. *settings* is the store's effective settings
    (its account profile decides cash settlement); computed when omitted.
    """
    ident = read_identity(conn)
    if factory is None:
        from arc.broker.registry import resolve_broker
        from arc.config import get_settings

        # Venue, env and transport are NEVER_TUNABLE, so the base settings decide them.
        s = settings if settings is not None else get_settings()
        if ident is None:
            return resolve_broker(s)
        inner = resolve_broker(
            s, keys_env=ident.keys_env, environ=environ, account_label=f"exp:{ident.arm_id}"
        )
    elif ident is None:
        return factory(None, None)
    else:
        key, secret = arm_keys(ident.keys_env, environ)
        inner = factory(key, secret)
    vb = virtual_broker(conn, ident, inner, settings=settings, now=now)
    return cast("BrokerAdapter", vb)  # the real broker's methods via __getattr__


def virtual_broker(
    conn: sqlite3.Connection,
    ident: ArmIdentity,
    inner: BrokerAdapter,
    *,
    settings: ArcSettings | None = None,
    now: Callable[[], _dt.datetime] | None = None,
) -> VirtualBroker:
    from arc.account_profiles import BuyingPower
    from arc.control.effective import effective_settings
    from arc.experiments.virtual import VirtualBroker
    from arc.utils.calendar import now_et

    s = settings if settings is not None else effective_settings(conn)
    return VirtualBroker(
        inner,
        conn,
        arm_id=ident.arm_id,
        t0=arm_t0(conn, ident.arm_id),
        cash_settlement=s.profile.buying_power is BuyingPower.CASH_SETTLED,
        now=now or now_et,
        control=_control_opener(ident),
    )
