"""Request plumbing shared by the tower routes: config, read-only DB, effective config.

E15.6 (D69): every route takes ``?arm=<experiment id>:<arm>``. :func:`arm_scope`
resolves it once per request (:mod:`arc.tower.scope`). :data:`Tower` / :data:`Conn`
are the *scoped* config and connection (the arm's store with ``?arm``, control's
without); :data:`ControlTower` / :data:`ControlConn` always read control, for the
pages fed by control-only data (they still validate ``?arm``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Query, Request

from arc.tower.api import TowerConfig, TowerError
from arc.tower.data import connect_ro
from arc.tower.scope import CONTROL, ArmScope, UnknownArmError, resolve_scope

if TYPE_CHECKING:
    from collections.abc import Iterator

    from arc.config import ArcSettings
    from arc.routines.config import RoutinesConfig

__all__ = [
    "Conn",
    "ControlConn",
    "ControlTower",
    "Scope",
    "Tower",
    "arm_scope",
    "control_conn",
    "db_conn",
    "effective",
    "scoped_config",
    "tower_config",
]


def tower_config(request: Request) -> TowerConfig:
    cfg: TowerConfig = request.app.state.tower
    return cfg


ControlTower = Annotated[TowerConfig, Depends(tower_config)]

_ARM_QUERY = Query(
    description=(
        "E15.6 (D69): read one experiment arm's store ('<experiment id>:<arm>', e.g. "
        "'XP-12:t1'; the ids are in /api/meta arms). Omitted or 'control' = Arc Investor."
    ),
    max_length=64,
)


def _ro(cfg: TowerConfig) -> sqlite3.Connection:
    try:
        return connect_ro(cfg.db_path)
    except FileNotFoundError as exc:
        raise TowerError(503, "db_unavailable", str(exc)) from exc
    except sqlite3.Error as exc:
        raise TowerError(503, "db_unavailable", f"cannot open {cfg.db_path}: {exc}") from exc


def arm_scope(cfg: ControlTower, arm: Annotated[str | None, _ARM_QUERY] = None) -> ArmScope:
    """The store this request reads; 404 ``unknown_arm`` (listing the valid arms) for an
    unknown, stopped or store-less arm. No param / ``control``: control, no DB lookup."""
    if arm in (None, "", CONTROL):
        return resolve_scope(cfg.db_path, None, arm)
    control = _ro(cfg)
    try:
        return resolve_scope(cfg.db_path, control, arm)
    except UnknownArmError as exc:
        raise TowerError(404, "unknown_arm", str(exc)) from exc
    except sqlite3.Error as exc:
        raise TowerError(503, "db_error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        control.close()


Scope = Annotated[ArmScope, Depends(arm_scope)]


def scoped_config(cfg: ControlTower, scope: Scope) -> TowerConfig:
    """Control's config with ``db_path`` pointed at the scoped store."""
    return dataclasses.replace(cfg, db_path=scope.db_path) if scope.is_arm else cfg


Tower = Annotated[TowerConfig, Depends(scoped_config)]


def _open(cfg: TowerConfig) -> Iterator[sqlite3.Connection]:
    """A request-scoped ``mode=ro`` connection; 503 when the store is missing/unreadable."""
    conn = _ro(cfg)
    try:
        from arc.store.identity import StoreEnvMismatchError, check_store_env

        try:  # D70: a tower never serves the other env's store
            if cfg.settings is not None:
                check_store_env(conn, cfg.settings.env, path=str(cfg.db_path))
        except StoreEnvMismatchError as exc:
            raise TowerError(503, "store_env_mismatch", str(exc)) from exc
        yield conn
    except sqlite3.Error as exc:
        raise TowerError(503, "db_error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def db_conn(cfg: Tower) -> Iterator[sqlite3.Connection]:
    """The scoped store (the arm's with ``?arm=``, else control's), ``mode=ro``."""
    yield from _open(cfg)


def control_conn(cfg: ControlTower, scope: Scope) -> Iterator[sqlite3.Connection]:
    """Control's store, ``mode=ro``, whatever ``?arm=`` names (*scope* still validates it)."""
    del scope
    yield from _open(cfg)


Conn = Annotated[sqlite3.Connection, Depends(db_conn)]
ControlConn = Annotated[sqlite3.Connection, Depends(control_conn)]


def feed_config(cfg: TowerConfig, scope: ArmScope, *personas: str) -> TowerConfig:
    """The store a feed page reads: the arm's when it runs one of *personas* itself
    (``ArmPlan.arm_personas``), else control's (shared input)."""
    return scoped_config(cfg, scope) if scope.owns(*personas) else cfg


open_ro = contextlib.contextmanager(_open)
"""``with open_ro(cfg) as conn``: :func:`_open` for a route that picks its store late."""


def effective(cfg: TowerConfig) -> tuple[ArcSettings, RoutinesConfig]:
    """Settings + routines with the D26 overrides in the store (opened ``mode=ro``).

    On an arm store: control's overrides plus the arm's overlay (E10.2,
    :func:`arc.control.effective.effective_settings`).
    """
    from arc.control.effective import effective_from_path
    from arc.store.identity import StoreEnvMismatchError

    try:
        return effective_from_path(cfg.db_path, base=cfg.settings, routines_path=cfg.routines_path)
    except (sqlite3.Error, OSError, ValueError, StoreEnvMismatchError) as exc:
        raise TowerError(503, "config_unavailable", f"{type(exc).__name__}: {exc}") from exc
