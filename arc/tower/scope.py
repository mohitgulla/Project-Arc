"""Arm-scoped reads for the tower (E15.6, D69): which store a request reads.

Every ``/api/*`` GET takes an optional ``?arm=<experiment id>:<arm>`` (e.g.
``XP-12:t1``). Without it, or with ``arm=control``, the request reads the tower's
own store (production, shown as "Arc Investor", D92) exactly as before. With a
running arm it reads that arm's store, ``mode=ro``, at the path control recorded at
t0 (:func:`arc.experiments.arms.arm_stores`). The same loaders run on it, so
equity, positions, trades and performance come from the arm's virtual account and
its attributed fills (E15.2: the arm's ``pnl_snapshots`` / ``monitor`` rows carry
the virtual equity), never the shared broker account.

Pages fed by control-only data (sources, universe, context, Scout/Scalp LLM spend,
ops, experiments) keep reading control and say so with ``shared_from_control``.

Every response carries ``scope: {arm_id}`` (``null`` = control) and
``shared_from_control``. Unknown or not-running arms are a 404 listing the arms
that can be viewed. Read-only throughout: no broker, no network, no writes.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves field annotations at runtime
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "CONTROL",
    "ArmMeta",
    "ArmScope",
    "ScopeInfo",
    "arm_label",
    "list_arms",
    "resolve_scope",
    "scoped",
]

#: The ``?arm=`` value for the production account (also the default).
CONTROL = "control"

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ScopeInfo(BaseModel):
    """Which store a response was read from."""

    model_config = _STRICT

    arm_id: str | None = Field(
        default=None, description="'<experiment id>:<arm>' for an arm; null = Arc Investor"
    )


class ArmMeta(BaseModel):
    """One experiment arm the tower can be scoped to (``/api/meta`` ``arms``)."""

    model_config = _STRICT

    id: str = Field(description="The ?arm= value, '<experiment id>:<arm>'")
    experiment_id: str
    arm: str = Field(description="Runner arm name (treatment, t1, t2, ...)")
    label: str = Field(description="Display name, e.g. 'XP-12 · T1'")
    status: str = Field(description="The experiment's status (running, stopped, ...)")
    store_age_s: int | None = Field(
        default=None, description="Seconds since the arm store was last written (null: missing)"
    )
    viewable: bool = Field(description="True when ?arm=<id> is served (running, store present)")


@dataclass(frozen=True)
class ArmScope:
    """The resolved store of one request."""

    db_path: Path
    arm_id: str | None = None
    #: the non-loop personas the arm runs on its own store (ArmPlan.arm_personas)
    arm_personas: frozenset[str] = frozenset()

    @property
    def is_arm(self) -> bool:
        return self.arm_id is not None

    def owns(self, *personas: str) -> bool:
        """True when the arm runs any of *personas* itself (its output is the arm's)."""
        return bool(self.arm_personas.intersection(personas))

    def info(self) -> ScopeInfo:
        return ScopeInfo(arm_id=self.arm_id)


def arm_short(arm: str) -> str:
    """``treatment`` -> ``T1``; ``t2`` -> ``T2``; else Title Case (web ``armShort``)."""
    if arm == "treatment":
        return "T1"
    if len(arm) > 1 and arm[0] in "tT" and arm[1:].isdigit():
        return arm.upper()
    return arm.replace("_", " ").title()


def arm_label(experiment_id: str, arm: str) -> str:
    """``XP-12 · T1`` (D92: arms show their treatment name, never "Control")."""
    return f"{experiment_id} · {arm_short(arm)}"


def _ro_identity(path: Path) -> tuple[str, str, str] | None:
    """``(arm_id, experiment_id, arm)`` of the arm store at *path*, read-only."""
    if not path.is_file():
        return None
    try:
        c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        r = c.execute("SELECT arm_id, experiment_id, arm FROM arm_identity WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        c.close()
    return (str(r[0]), str(r[1]), str(r[2])) if r is not None else None


def _store_age_s(path: Path, now: _dt.datetime) -> int | None:
    """Seconds since the store (or its WAL) was last written; None when missing."""
    stamps: list[float] = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            continue
    if not stamps or not path.is_file():
        return None
    return max(0, int(now.timestamp() - max(stamps)))


def _statuses(control: sqlite3.Connection) -> dict[str, str]:
    """``experiment id -> status`` from control's registry ({} without the tables)."""
    from arc.experiments.store import ExperimentStore

    try:
        return {s.experiment_id: s.status.value for s in ExperimentStore(control).all()}
    except sqlite3.OperationalError:
        return {}


def _arm_rows(control: sqlite3.Connection) -> list[tuple[str, str, str, Path]]:
    """``(arm_id, experiment_id, arm, path)`` for every arm store control recorded."""
    from arc.experiments.arms import arm_stores

    try:
        stores = arm_stores(control)
    except sqlite3.OperationalError:
        return []
    out: list[tuple[str, str, str, Path]] = []
    for key, path in stores.items():
        xp, sep, arm = key.rpartition(":")
        if not sep:  # pre-D69 legacy key: the bare arm name; the store names its experiment
            ident = _ro_identity(path)
            if ident is None:
                continue
            key, xp, arm = ident
        out.append((key, xp, arm, path))
    return out


def list_arms(control: sqlite3.Connection, *, now: _dt.datetime) -> list[ArmMeta]:
    """Every recorded arm, running first, then by id."""
    statuses = _statuses(control)
    arms: list[ArmMeta] = []
    for aid, xp, arm, path in _arm_rows(control):
        status = statuses.get(xp, "unknown")
        age = _store_age_s(path, now)
        arms.append(
            ArmMeta(
                id=aid,
                experiment_id=xp,
                arm=arm,
                label=arm_label(xp, arm),
                status=status,
                store_age_s=age,
                viewable=status == "running" and age is not None,
            )
        )
    return sorted(arms, key=lambda a: (a.status != "running", a.id))


class UnknownArmError(LookupError):
    """``?arm=`` names no running arm; ``valid`` lists the ones that can be viewed."""

    def __init__(self, arm: str, valid: list[str]) -> None:
        self.arm = arm
        self.valid = valid
        super().__init__(f"no running arm {arm!r}; valid arms: {', '.join(valid)}")


def resolve_scope(
    control_path: Path, control: sqlite3.Connection | None, arm: str | None
) -> ArmScope:
    """The store *arm* reads. ``None`` / ``control`` -> control, with no lookup at all.

    *control* is a read-only connection to the control store (only needed for an arm).
    Raises :class:`UnknownArmError` for an unknown, stopped or store-less arm.
    """
    if arm is None or arm in ("", CONTROL):
        return ArmScope(db_path=control_path)
    rows = [] if control is None else _arm_rows(control)
    statuses = {} if control is None else _statuses(control)
    live = {
        aid: path for aid, xp, _, path in rows if statuses.get(xp) == "running" and path.is_file()
    }
    if arm not in live:
        raise UnknownArmError(arm, [CONTROL, *sorted(live)])
    path = live[arm].resolve()
    return ArmScope(db_path=path, arm_id=arm, arm_personas=_arm_personas(path))


def _arm_personas(path: Path) -> frozenset[str]:
    """``ArmPlan.arm_personas`` from the arm store's identity (read-only); {} if none."""
    from arc.experiments.arms import read_identity

    try:
        c = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return frozenset()
    try:
        ident = read_identity(c)
    except sqlite3.Error:
        return frozenset()
    finally:
        c.close()
    plan = ident.plan if ident is not None else None
    return frozenset(str(p) for p in (plan or {}).get("arm_personas") or [])


def scoped[M: BaseModel](model: type[M]) -> type[M]:
    """*model* plus ``scope`` and ``shared_from_control`` (same schema name).

    Built once per response model at import time by each route module; the OpenAPI
    schema keeps the original component name, so the generated web types only gain
    the two fields.
    """
    fields: Mapping[str, Any] = {
        "scope": (ScopeInfo, Field(default_factory=ScopeInfo, description="E15.6 (D69)")),
        "shared_from_control": (
            bool,
            Field(
                default=False,
                description="True when an arm view serves Arc Investor's (control's) data",
            ),
        ),
    }
    out = create_model(  # type: ignore[call-overload]
        model.__name__,
        __base__=model,
        __module__=model.__module__,
        __doc__=model.__doc__,
        **fields,
    )
    return cast("type[M]", out)


def wrap[M: BaseModel](
    model: type[M], value: BaseModel, scope: ArmScope, *, shared: bool = False
) -> M:
    """*value* (an instance of *model*'s base) re-typed as the scoped *model*.

    Field-by-field copy (no re-validation of nested models, no JSON round trip), so
    the payload is exactly the loader's.
    """
    data = {k: getattr(value, k) for k in type(value).model_fields}
    return model.model_construct(
        **data, scope=scope.info(), shared_from_control=shared and scope.is_arm
    )
