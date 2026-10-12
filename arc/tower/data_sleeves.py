"""E22.5 (D97): DTE sleeves for the Tower: meta (window, slots, exit policy) and slot usage.

A trade's sleeve is derived from its entry DTE (:func:`arc.journal.sleeves.sleeve_name`),
never read from a stored field, so pre-D97 trades map by DTE and a DTE that fits no
current sleeve is ``other``. Read-only (SELECT only); the effective settings come from
the route (:func:`arc.tower.routes.deps.effective`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.account_profiles import DEFAULT_SLEEVE
from arc.journal.sleeves import OTHER, entry_dtes, sleeve_name

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from arc.account_profiles import DteSleeve
    from arc.config import ArcSettings

__all__ = [
    "OTHER",
    "SleeveExitView",
    "SleeveMeta",
    "SleeveUsage",
    "named_sleeves",
    "sleeve_label",
    "sleeve_meta",
    "sleeve_usage",
    "sleeves_by_open_hash",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)


def sleeve_label(name: str) -> str:
    """``short`` -> ``Short`` (Title Case, like every Tower value)."""
    return " ".join(w.capitalize() for w in name.split("_"))


def named_sleeves(settings: ArcSettings) -> list[DteSleeve]:
    """The active profile's named sleeves; ``[]`` for a single-window profile."""
    return [s for s in settings.entry_sleeves if s.name != DEFAULT_SLEEVE]


class SleeveExitView(BaseModel):
    model_config = _STRICT

    kind: str
    summary: str = Field(description="ExitPolicy.summary() of the kind in this sleeve")


class SleeveMeta(BaseModel):
    """One named sleeve of the active profile (the InfoTip's window and exit policy)."""

    model_config = _STRICT

    name: str
    label: str
    dte_min: int
    dte_max: int
    max_positions: int | None = Field(None, description="Slot cap; None = uncapped")
    exit_policy: str | None = Field(None, description="exits.yaml `sleeves:` block; None = kind")
    exits: list[SleeveExitView] = Field(default_factory=list)


class SleeveUsage(BaseModel):
    """Open positions in one sleeve vs its slot cap (Overview: ``Short 3/5``)."""

    model_config = _STRICT

    name: str
    label: str
    open: int
    max_positions: int | None = None


def sleeve_meta(settings: ArcSettings) -> list[SleeveMeta]:
    """Named sleeves with each allowed kind's exit-policy summary (effective exits.yaml)."""
    from arc.control.effective import exit_config

    sleeves = named_sleeves(settings)
    if not sleeves:
        return []
    exits = exit_config(settings)
    kinds = settings.profile.allowed_kinds
    return [
        SleeveMeta(
            name=s.name,
            label=sleeve_label(s.name),
            dte_min=s.dte_min,
            dte_max=s.dte_max,
            max_positions=s.max_positions,
            exit_policy=s.exit_policy,
            exits=[
                SleeveExitView(kind=k.value, summary=exits.policy_for(k, s.exit_policy).summary())
                for k in kinds
            ],
        )
        for s in sleeves
    ]


def sleeves_by_open_hash(conn: sqlite3.Connection, sleeves: Sequence[DteSleeve]) -> dict[str, str]:
    """Open proposal hash -> derived sleeve name, for every stored structure."""
    dtes = entry_dtes(conn)
    return {
        str(r[1]): sleeve_name(sleeves, dtes.get(str(r[0])))
        for r in conn.execute("SELECT id, open_proposal_hash FROM open_structures")
    }


def sleeve_usage(conn: sqlite3.Connection, sleeves: Sequence[DteSleeve]) -> list[SleeveUsage]:
    """Open positions per named sleeve (+ ``Other`` when one fits none); [] without sleeves."""
    if not sleeves:
        return []
    dtes = entry_dtes(conn)
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT id FROM open_structures WHERE status = 'open'"):
        name = sleeve_name(sleeves, dtes.get(str(r[0])))
        counts[name] = counts.get(name, 0) + 1
    out = [
        SleeveUsage(
            name=s.name,
            label=sleeve_label(s.name),
            open=counts.get(s.name, 0),
            max_positions=s.max_positions,
        )
        for s in sorted(sleeves, key=lambda s: s.dte_min)
    ]
    if counts.get(OTHER):
        out.append(SleeveUsage(name=OTHER, label=sleeve_label(OTHER), open=counts[OTHER]))
    return out
