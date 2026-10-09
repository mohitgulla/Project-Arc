"""Request plumbing shared by the tower routes: config, read-only DB, effective config."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from arc.tower.api import TowerConfig, TowerError
from arc.tower.data import connect_ro

if TYPE_CHECKING:
    from collections.abc import Iterator

    from arc.config import ArcSettings
    from arc.routines.config import RoutinesConfig

__all__ = ["Conn", "Tower", "db_conn", "effective", "tower_config"]


def tower_config(request: Request) -> TowerConfig:
    cfg: TowerConfig = request.app.state.tower
    return cfg


Tower = Annotated[TowerConfig, Depends(tower_config)]


def db_conn(cfg: Tower) -> Iterator[sqlite3.Connection]:
    """A request-scoped ``mode=ro`` connection; 503 when the store is missing/unreadable."""
    try:
        conn = connect_ro(cfg.db_path)
    except FileNotFoundError as exc:
        raise TowerError(503, "db_unavailable", str(exc)) from exc
    except sqlite3.Error as exc:
        raise TowerError(503, "db_unavailable", f"cannot open {cfg.db_path}: {exc}") from exc
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


Conn = Annotated[sqlite3.Connection, Depends(db_conn)]


def effective(cfg: TowerConfig) -> tuple[ArcSettings, RoutinesConfig]:
    """Settings + routines with the D26 overrides in the store (opened ``mode=ro``)."""
    from arc.control.effective import effective_from_path
    from arc.store.identity import StoreEnvMismatchError

    try:
        return effective_from_path(cfg.db_path, base=cfg.settings, routines_path=cfg.routines_path)
    except (sqlite3.Error, OSError, ValueError, StoreEnvMismatchError) as exc:
        raise TowerError(503, "config_unavailable", f"{type(exc).__name__}: {exc}") from exc
