"""Effective config for the D26 control panel: defaults < YAML/env < DB overrides.

Every entry point builds its settings here (the routines tick, ``arc propose``,
approvals, execution, reconcile), so the deterministic gate, sizing, the
scanner and the exit policy all read the same effective values:

- :func:`effective_settings` returns :class:`arc.config.ArcSettings` with every
  applied ``config_changes`` override, ``config_version`` set, and the YAML
  overrides attached (read them via :func:`exit_config` / :func:`cost_model` /
  :func:`effective_routines`).
- Overrides re-run the settings validators (``model_validate``), so a stored
  value that no longer validates (e.g. after a code change narrowed a bound)
  is skipped with an error log instead of breaking the tick.

Values apply at the next tick with no restart: nothing here is cached.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from arc.account_profiles import DEFAULT_PROFILES_PATH, load_account_profiles
from arc.backtest.costs import DEFAULT_COSTS_PATH, load_cost_model
from arc.config import ArcSettings
from arc.control.registry import Target, TunableError, lookup, write_raw
from arc.control.store import ConfigChange, ConfigChangeRepo
from arc.exits.policy import DEFAULT_EXITS_PATH, load_exit_config
from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines

if TYPE_CHECKING:
    from arc.backtest.costs import CostModel
    from arc.exits.policy import ExitConfig
    from arc.routines.config import RoutinesConfig

__all__ = [
    "YAML_PATHS",
    "cost_model",
    "effective_routines",
    "effective_settings",
    "exit_config",
    "open_store",
    "raw_yaml",
    "yaml_overrides",
]

log = structlog.get_logger(__name__)

YAML_PATHS: dict[Target, Path] = {
    Target.EXITS: DEFAULT_EXITS_PATH,
    Target.COSTS: DEFAULT_COSTS_PATH,
    Target.PROFILES: DEFAULT_PROFILES_PATH,
    Target.ROUTINES: DEFAULT_ROUTINES_PATH,
}


def open_store(db_path: Path | str | None) -> sqlite3.Connection:
    """Connect and migrate (the override tables live in the main Arc DB)."""
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(db_path)
    migrate(conn)
    return conn


def effective_from_path(
    db_path: Path | str | None,
    *,
    base: ArcSettings | None = None,
    routines_path: Path | str | None = None,
) -> tuple[ArcSettings, RoutinesConfig]:
    """Effective settings + routines from the DB at *db_path*, opened **read-only**.

    Never creates or migrates a DB (``arc propose --dry-run`` must not write one):
    a missing file, ``:memory:`` or a DB without the override tables means no
    overrides (``config_version`` 0).
    """
    from arc.store.db import DEFAULT_DB_PATH

    base = base if base is not None else ArcSettings()
    p = Path(db_path) if db_path not in (None, "") else (base.db_path or DEFAULT_DB_PATH)
    if str(p) == ":memory:" or not Path(p).is_file():
        return apply_changes(base, {}, version=0), load_routines(routines_path)
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        settings = effective_settings(conn, base=base)
        routines = effective_routines(conn, routines_path)
    finally:
        conn.close()
    return settings, routines


def raw_yaml(target: Target, path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else YAML_PATHS[target]
    data = yaml.safe_load(p.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _active(conn: sqlite3.Connection) -> dict[str, ConfigChange]:
    try:
        return ConfigChangeRepo(conn).active()
    except sqlite3.OperationalError:  # store not migrated yet: no overrides
        return {}


def yaml_overrides(
    changes: dict[str, ConfigChange],
    *,
    routines_path: Path | str | None = None,
    profiles_path: Path | str | None = None,
) -> dict[str, dict[tuple[str, ...], Any]]:
    """``target -> {path: value}`` for every applied YAML-targeted override."""
    out: dict[str, dict[tuple[str, ...], Any]] = {}
    raws: dict[Target, dict[str, Any]] = {}
    paths = {Target.ROUTINES: routines_path, Target.PROFILES: profiles_path}
    for key, change in sorted(changes.items(), key=lambda kv: kv[1].id):
        try:
            t = lookup(key)
        except TunableError:
            log.error("control.override_unknown_key", key=key, change_id=change.id)
            continue
        if t.target is Target.SETTINGS:
            continue
        try:
            if t.target not in raws:
                raws[t.target] = raw_yaml(t.target, paths.get(t.target))
            pairs = write_raw(t, change.new, raws[t.target])
        except (TunableError, OSError) as exc:
            log.error("control.override_skipped", key=key, change_id=change.id, error=str(exc))
            continue
        bucket = out.setdefault(t.target.value, {})
        for path, value in pairs:
            bucket[path] = value
    return out


def effective_settings(
    conn: sqlite3.Connection | None = None,
    *,
    base: ArcSettings | None = None,
    db_path: Path | str | None = None,
) -> ArcSettings:
    """*base* (default ``ArcSettings()``) with every applied DB override.

    Opens ``db_path`` (default ``base.db_path``) when no *conn* is given.
    ``config_version`` is the latest ``config_changes.id`` (0 = none ever).
    """
    base = base if base is not None else ArcSettings()
    own = conn is None
    c = conn if conn is not None else open_store(db_path or base.db_path)
    try:
        changes = _active(c)
        try:
            version = ConfigChangeRepo(c).version()
        except sqlite3.OperationalError:
            version = 0
    finally:
        if own:
            c.close()
    return apply_changes(base, changes, version=version)


def apply_changes(
    base: ArcSettings, changes: dict[str, ConfigChange], *, version: int
) -> ArcSettings:
    """Pure core of :func:`effective_settings` (no DB).

    D34: a per-env switch (``auto_approve.live`` etc.) only applies to the
    running ``ARC_ENV``. In live the settings validator forces the env-var
    shortcut off, so the switches whose value came from the store are passed as
    validation context (``store_switches``) and survive it.
    """
    data = base.model_dump()
    env = base.env.value
    store_switches: set[str] = set()
    for key, change in sorted(changes.items(), key=lambda kv: kv[1].id):
        try:
            t = lookup(key)
        except TunableError:
            log.error("control.override_unknown_key", key=key, change_id=change.id)
            continue
        if t.target is not Target.SETTINGS or t.field is None:
            continue
        if t.env is not None and t.env != env:
            continue  # e.g. auto_approve.live while running paper
        trial = {**data, t.field: change.new}
        switches = store_switches | ({t.field} if t.env is not None else set())
        try:
            ArcSettings.model_validate(trial, context={"store_switches": switches})
        except ValueError as exc:
            log.error("control.override_invalid", key=key, change_id=change.id, error=str(exc))
            continue
        data = trial
        store_switches = switches
    yaml_ov = yaml_overrides(changes, profiles_path=base.account_profiles_file)
    if data.get("account_profile") != base.account_profile or yaml_ov.get("account_profiles"):
        data["account_profile_spec"] = None
    data["config_version"] = version
    settings = ArcSettings.model_validate(data, context={"store_switches": store_switches})
    profiles = load_account_profiles(
        settings.account_profiles_file, overrides=yaml_ov.get("account_profiles")
    )
    settings = settings.model_copy(
        update={"account_profile_spec": profiles.get(settings.account_profile)}
    )
    settings._yaml_overrides = yaml_ov  # noqa: SLF001 - owned by arc.control
    return settings


def exit_config(settings: ArcSettings | None = None, path: Path | str | None = None) -> ExitConfig:
    """``config/exits.yaml`` with the D26 overrides carried by *settings*."""
    ov = settings.yaml_overrides(Target.EXITS.value) if settings is not None else None
    return load_exit_config(path, overrides=ov)


def cost_model(settings: ArcSettings | None = None, path: Path | str | None = None) -> CostModel:
    """``config/costs.yaml`` with the D26 overrides carried by *settings*."""
    ov = settings.yaml_overrides(Target.COSTS.value) if settings is not None else None
    return load_cost_model(path, overrides=ov)


def effective_routines(
    conn: sqlite3.Connection | None, path: Path | str | None = None
) -> RoutinesConfig:
    """``config/routines.yaml`` with routine enable/cadence overrides from *conn*."""
    if conn is None:
        return load_routines(path)
    ov = yaml_overrides(_active(conn), routines_path=path).get(Target.ROUTINES.value)
    return load_routines(path, overrides=ov)
