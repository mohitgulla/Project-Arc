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
from arc.experiments.config import DEFAULT_EXPERIMENTS_PATH, load_experiments_config
from arc.routines.config import DEFAULT_ROUTINES_PATH, load_routines
from arc.scanner.rank import DEFAULT_RANKING_PATH, load_ranking_config
from arc.universe.config import DEFAULT_UNIVERSE_CONFIG

if TYPE_CHECKING:
    from arc.backtest.costs import CostModel
    from arc.exits.policy import ExitConfig
    from arc.experiments.config import ExperimentsConfig
    from arc.routines.config import RoutinesConfig
    from arc.scanner.be_atr import ScannerFilters
    from arc.scanner.rank import RankingConfig

__all__ = [
    "YAML_PATHS",
    "cost_model",
    "effective_routines",
    "effective_settings",
    "exit_config",
    "experiments_config",
    "open_store",
    "ranking_config",
    "raw_yaml",
    "scanner_filters",
    "yaml_overrides",
]

log = structlog.get_logger(__name__)

YAML_PATHS: dict[Target, Path] = {
    Target.EXITS: DEFAULT_EXITS_PATH,
    Target.COSTS: DEFAULT_COSTS_PATH,
    Target.PROFILES: DEFAULT_PROFILES_PATH,
    Target.ROUTINES: DEFAULT_ROUTINES_PATH,
    Target.RANKING: DEFAULT_RANKING_PATH,
    Target.EXPERIMENTS: DEFAULT_EXPERIMENTS_PATH,
    Target.UNIVERSE: DEFAULT_UNIVERSE_CONFIG,
}


def open_store(
    db_path: Path | str | None, *, settings: ArcSettings | None = None
) -> sqlite3.Connection:
    """Connect, migrate and bind to the running env (the override tables live in the
    main Arc DB). D70: a store of the other env raises ``StoreEnvMismatchError``."""
    from arc.store.identity import open_store as _open

    return _open(db_path, settings=settings)


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
        from arc.store.identity import check_store_env

        check_store_env(conn, base.env, path=str(p))  # D70: never the other env's overrides
        settings = effective_settings(conn, base=base)
        routines = effective_routines(conn, routines_path)
    finally:
        conn.close()
    return settings, routines


# libyaml's C SafeLoader when PyYAML was built with it (same safe tag set, ~8x faster);
# the pure-Python SafeLoader otherwise.
_SAFE_LOADER: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def raw_yaml(target: Target, path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else YAML_PATHS[target]
    data = yaml.load(p.read_text(), Loader=_SAFE_LOADER) or {}  # noqa: S506 - a SafeLoader
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
        except TunableError:  # a removed key: reported and ignored
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


def _arm_source(
    c: sqlite3.Connection,
) -> tuple[dict[str, ConfigChange], int, dict[str, dict[str, Any]] | None]:
    """``(changes, version, overlay)`` for the store *c*.

    E10.2 (D44): an arm store (one with an ``arm_identity``) takes control's D26
    overrides, read **read-only** from the identity's ``control_db``, plus its own
    spec arm's overlay on top. Its own ``config_changes`` are never consulted, so a
    Slack change reaches both arms identically and the overlay is the only
    difference. A plain store returns its own overrides and no overlay.
    """
    from arc.experiments.arms import read_identity

    ident = read_identity(c)
    if ident is None:
        changes = _active(c)
        try:
            version = ConfigChangeRepo(c).version()
        except sqlite3.OperationalError:
            version = 0
        return changes, version, None
    p = Path(ident.control_db)
    changes: dict[str, ConfigChange] = {}
    version = 0
    if p.is_file():
        ctl = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ctl.row_factory = sqlite3.Row
        try:
            changes = _active(ctl)
            try:
                version = ConfigChangeRepo(ctl).version()
            except sqlite3.OperationalError:
                version = 0
        finally:
            ctl.close()
    else:
        log.warning("control.arm_control_db_missing", arm_id=ident.arm_id, control_db=str(p))
    return changes, version, ident.overlay


def _flatten(node: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Leaf ``path -> value`` pairs of a nested mapping (== a deep merge of it)."""
    if isinstance(node, dict) and node:
        out: dict[tuple[str, ...], Any] = {}
        for k, v in node.items():
            out.update(_flatten(v, (*prefix, str(k))))
        return out
    return {prefix: node} if prefix else {}


def overlay_overrides(
    overlay: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[tuple[str, ...], Any]]:
    """An experiment arm overlay (``target -> partial file``) as D26-style overrides."""
    return {t: _flatten(data) for t, data in (overlay or {}).items() if data}


def effective_settings(
    conn: sqlite3.Connection | None = None,
    *,
    base: ArcSettings | None = None,
    db_path: Path | str | None = None,
) -> ArcSettings:
    """*base* (default ``ArcSettings()``) with every applied DB override.

    Opens ``db_path`` (default ``base.db_path``) when no *conn* is given.
    ``config_version`` is the latest ``config_changes.id`` (0 = none ever).
    On an experiment arm store (E10.2) the overrides are control's and the arm's
    overlay is applied on top (:func:`_arm_source`).
    """
    base = base if base is not None else ArcSettings()
    own = conn is None
    c = conn if conn is not None else open_store(db_path or base.db_path, settings=base)
    try:
        changes, version, overlay = _arm_source(c)
    finally:
        if own:
            c.close()
    return apply_changes(base, changes, version=version, overlay=overlay)


def apply_changes(
    base: ArcSettings,
    changes: dict[str, ConfigChange],
    *,
    version: int,
    overlay: dict[str, dict[str, Any]] | None = None,
) -> ArcSettings:
    """Pure core of :func:`effective_settings` (no DB).

    D34: a per-env switch (``auto_approve.live`` etc.) only applies to the
    running ``ARC_ENV``. The settings validator forces every per-env switch off
    in live (the env var is paper-only), so a store value for the *live* key is
    applied after validation, with ``model_copy``: the store is the only path
    that can turn a switch on in live.

    *overlay* (E10.2): an experiment arm's ``target -> partial file`` overlay,
    applied over the D26 YAML overrides (the arm's value wins), so every
    consumer that goes through :func:`exit_config` / :func:`cost_model` /
    :func:`ranking_config` / the profile spec sees the arm's config.
    """
    from arc.config import STORE_ONLY_LIVE_FIELDS

    data = base.model_dump()
    post: dict[str, Any] = {}  # per-env switches applied after validation (live only)
    env = base.env.value
    for key, change in sorted(changes.items(), key=lambda kv: kv[1].id):
        try:
            t = lookup(key)
        except TunableError:  # a removed key: reported and ignored
            log.error("control.override_unknown_key", key=key, change_id=change.id)
            continue
        if t.target is not Target.SETTINGS or t.field is None:
            continue
        if t.env is not None and t.env != env:
            continue  # e.g. auto_approve.live while running paper
        if t.env == "live" and t.field in STORE_ONLY_LIVE_FIELDS:
            post[t.field] = bool(change.new)
            continue
        trial = {**data, t.field: change.new}
        try:
            ArcSettings.model_validate(trial)
        except ValueError as exc:
            log.error("control.override_invalid", key=key, change_id=change.id, error=str(exc))
            continue
        data = trial
    yaml_ov = yaml_overrides(changes, profiles_path=base.account_profiles_file)
    for target, pairs in overlay_overrides(overlay).items():
        if target == Target.ROUTINES.value:
            continue  # routines overlay: effective_routines()
        yaml_ov.setdefault(target, {}).update(pairs)
    if data.get("account_profile") != base.account_profile or yaml_ov.get("account_profiles"):
        data["account_profile_spec"] = None
    data["config_version"] = version
    settings = ArcSettings.model_validate(data)
    profiles = load_account_profiles(
        settings.account_profiles_file, overrides=yaml_ov.get("account_profiles")
    )
    settings = settings.model_copy(
        update={"account_profile_spec": profiles.get(settings.account_profile), **post}
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


def ranking_config(
    settings: ArcSettings | None = None, path: Path | str | None = None
) -> RankingConfig:
    """``config/ranking.yaml`` (``ranking:``) with the D26 overrides carried by *settings*."""
    ov = settings.yaml_overrides(Target.RANKING.value) if settings is not None else None
    return load_ranking_config(path, overrides=ov)


def scanner_filters(
    settings: ArcSettings | None = None, path: Path | str | None = None
) -> ScannerFilters:
    """E16.5: ``config/ranking.yaml`` ``scanner:`` with the D26 overrides carried by *settings*."""
    from arc.scanner.be_atr import load_scanner_filters

    ov = settings.yaml_overrides(Target.RANKING.value) if settings is not None else None
    return load_scanner_filters(path, overrides=ov)


def experiments_config(
    settings: ArcSettings | None = None, path: Path | str | None = None
) -> ExperimentsConfig:
    """``config/experiments.yaml`` (E10.1 defaults) with the D26 overrides carried by *settings*."""
    ov = settings.yaml_overrides(Target.EXPERIMENTS.value) if settings is not None else None
    return load_experiments_config(path, overrides=ov)


def effective_routines(
    conn: sqlite3.Connection | None, path: Path | str | None = None
) -> RoutinesConfig:
    """``config/routines.yaml`` with routine enable/cadence overrides from *conn*.

    On an experiment arm store: control's overrides plus the arm's routines overlay.
    """
    if conn is None:
        return load_routines(path)
    changes, _, overlay = _arm_source(conn)
    ov = yaml_overrides(changes, routines_path=path).get(Target.ROUTINES.value) or {}
    ov = {**ov, **overlay_overrides(overlay).get(Target.ROUTINES.value, {})}
    return load_routines(path, overrides=ov or None)


def routines_for_overlay(
    control: sqlite3.Connection,
    overlay: dict[str, dict[str, Any]] | None,
    path: Path | str | None = None,
) -> RoutinesConfig:
    """What :func:`effective_routines` returns on an arm store with *overlay* (E13.12).

    Read from the *control* store before the arm store exists (``arc experiment start``
    computes each arm's plan from it): control's overrides plus the overlay's routines.
    """
    changes, _, _ = _arm_source(control)
    ov = yaml_overrides(changes, routines_path=path).get(Target.ROUTINES.value) or {}
    ov = {**ov, **overlay_overrides(overlay).get(Target.ROUTINES.value, {})}
    return load_routines(path, overrides=ov or None)
