"""Per-env store identity (E11.3, D70): paper and live never share an audit store.

Every store carries a one-row, append-only ``store_identity`` (migration 030)
naming the environment it belongs to. The first read-write open by an Arc
process stamps it with the running ``ARC_ENV``; from then on a process of the
other environment refuses the store (:class:`StoreEnvMismatchError`) before any
broker or LLM call. A live process therefore can never count paper closes as
evidence for the E7.5a scorecard gate or the live size cap, and a paper row can
never leak into a live count by omission (no ``env`` column to forget).

The default path is per env too (:func:`default_db_path`): ``data/arc.db`` for
paper (unchanged), ``data/arc-live.db`` for live. ``ARC_DB_PATH`` / ``--db``
still override it; the identity check is what makes a wrong override fail.

Same pattern as the experiment arm identity (E10.2, ``arm_identity``).
"""

from __future__ import annotations

import contextlib
import datetime as _dt  # noqa: TC003 - pydantic resolves StoreIdentity's annotations at runtime
import sqlite3
from pathlib import Path  # noqa: TC003 - used at runtime by store_path callers' annotations
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from arc.context.ttl import from_db, to_db
from arc.store.db import DEFAULT_DB_DIR, DEFAULT_DB_PATH

if TYPE_CHECKING:
    from arc.config import ArcSettings

__all__ = [
    "DEFAULT_LIVE_DB_PATH",
    "StoreEnv",
    "StoreEnvMismatchError",
    "StoreIdentity",
    "bind_store_env",
    "check_store_env",
    "default_db_path",
    "open_store",
    "read_store_env",
    "store_path",
    "write_store_env",
]

log = structlog.get_logger(__name__)

StoreEnv = Literal["paper", "live"]
DEFAULT_LIVE_DB_PATH = DEFAULT_DB_DIR / "arc-live.db"


class StoreEnvMismatchError(RuntimeError):
    """The store belongs to the other environment (fatal; nothing may run on it)."""

    def __init__(self, store_env: str, running_env: str, path: str | None = None) -> None:
        self.store_env = store_env
        self.running_env = running_env
        where = f" ({path})" if path else ""
        super().__init__(
            f"audit store{where} is a {store_env} store; ARC_ENV={running_env} refuses it "
            "(D70: paper and live never share a store)"
        )


class StoreIdentity(BaseModel):
    """The ``store_identity`` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    env: StoreEnv
    created_at: _dt.datetime


def _env(env: object) -> str:
    return str(getattr(env, "value", env))


def default_db_path(env: object) -> Path:
    """``data/arc.db`` for paper, ``data/arc-live.db`` for live."""
    return DEFAULT_LIVE_DB_PATH if _env(env) == "live" else DEFAULT_DB_PATH


def store_path(settings: ArcSettings, db: Path | str | None = None) -> Path | str:
    """``--db`` > ``ARC_DB_PATH`` > the env's default path."""
    if db not in (None, ""):
        return db  # type: ignore[return-value]
    return settings.db_path or default_db_path(settings.env)


def read_store_env(conn: sqlite3.Connection) -> StoreIdentity | None:
    """The store's identity; ``None`` when not stamped yet (or not migrated to 030)."""
    try:
        r = conn.execute("SELECT env, created_at FROM store_identity WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None:
        return None
    return StoreIdentity(env=r[0], created_at=from_db(r[1]))


def write_store_env(conn: sqlite3.Connection, env: object, now: _dt.datetime) -> None:
    """Stamp the store once (the table refuses updates and deletes)."""
    with conn:
        conn.execute(
            "INSERT INTO store_identity (id, env, created_at) VALUES (1, ?, ?)",
            (_env(env), to_db(now)),
        )


def check_store_env(
    conn: sqlite3.Connection, env: object, *, path: str | None = None
) -> StoreIdentity | None:
    """Raise :class:`StoreEnvMismatchError` when the store is stamped for another env.

    Read-only: an unstamped store passes (returns ``None``); use
    :func:`bind_store_env` on a read-write connection to stamp it.
    """
    ident = read_store_env(conn)
    if ident is not None and ident.env != _env(env):
        raise StoreEnvMismatchError(ident.env, _env(env), path)
    return ident


def bind_store_env(
    conn: sqlite3.Connection,
    env: object,
    *,
    now: _dt.datetime | None = None,
    path: str | None = None,
) -> StoreIdentity:
    """Stamp an unstamped (migrated) store with *env*, or check its stamp matches.

    Fails closed: a mismatch raises :class:`StoreEnvMismatchError`.
    """
    ident = check_store_env(conn, env, path=path)
    if ident is not None:
        return ident
    if now is None:
        from arc.utils.calendar import now_et

        now = now_et()
    # A concurrent process may have stamped it first: suppress, then re-check.
    with contextlib.suppress(sqlite3.IntegrityError):
        write_store_env(conn, env, now)
    ident = check_store_env(conn, env, path=path)
    if ident is None:  # pragma: no cover - the insert above or a concurrent one wrote it
        msg = "store_identity could not be written"
        raise RuntimeError(msg)
    log.info("store.identity_stamped", env=ident.env, path=path)
    return ident


def open_store(
    db: Path | str | None = None,
    *,
    settings: ArcSettings | None = None,
    now: _dt.datetime | None = None,
) -> sqlite3.Connection:
    """Connect, migrate and bind the store to the running env (every CLI's entry point).

    *db* (``--db``) > ``settings.db_path`` (``ARC_DB_PATH``) > :func:`default_db_path`.
    ``:memory:`` stores are stamped too (rule: no exception for dry runs/fixtures).
    """
    from arc.store.db import connect
    from arc.store.migrate import migrate

    if settings is None:
        from arc.config import get_settings

        settings = get_settings()
    path = store_path(settings, db)
    conn = connect(path)
    try:
        migrate(conn)
        bind_store_env(conn, settings.env, now=now, path=str(path))
    except BaseException:
        conn.close()
        raise
    return conn
