"""E14.5 (D60): Reddit mention velocity, pure and dependency-light.

``velocity = (mentions + k) / (mentions_24h_ago + k)`` from ApeWisdom's two counts,
with a smoothing constant ``k`` (so a 3 -> 12 jump reads 2.1×, not 4×) and a
``min_mentions`` floor. A missing 24 h count gives ``None``, never 0 (that would read as
infinite growth). The trending ranker's ``scoring: velocity`` arm, the Scout's velocity
lines and the Tower all compute it here. This module imports no broker, market data or
symbol master, so the read-only Tower may use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from arc.context.kinds import RetailBuzzPayload

__all__ = [
    "Velocity",
    "VelocityOptions",
    "buzz_velocities",
    "format_velocity",
    "mention_velocity",
    "velocity_text",
]

#: ``(velocity, mentions, mentions_24h_ago)``; velocity ``None`` when not computable.
Velocity = tuple[float | None, float | None, float | None]


class VelocityOptions(BaseModel):
    """Mention-velocity knobs (``universe.trending.velocity`` in ``config/routines.yaml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    smoothing: float = Field(5.0, gt=0.0, le=1000.0, description="k: a 3 -> 12 jump is 2.1x")
    min_mentions: float = Field(10.0, ge=0.0, description="Below this (24 h) -> velocity None")


def mention_velocity(
    mentions: float | None, mentions_24h_ago: float | None, opts: VelocityOptions
) -> float | None:
    """``(m + k) / (m24 + k)`` (pure). ``None`` when either count is missing or *mentions*
    is below ``min_mentions``: a missing 24 h count is never read as 0."""
    if mentions is None or mentions_24h_ago is None or mentions < opts.min_mentions:
        return None
    k = opts.smoothing
    return round((mentions + k) / (max(mentions_24h_ago, 0.0) + k), 6)


def format_velocity(v: float) -> str:
    """``4.6×`` below 10, ``21×`` from 10 up."""
    return f"{v:.0f}\u00d7" if v >= 10 else f"{v:.1f}\u00d7"  # noqa: PLR2004


def velocity_text(vel: Velocity | None) -> str | None:
    """``4.6× (157 vs 30)`` for a ``(velocity, m, m24)`` with a velocity, else ``None``."""
    if vel is None or vel[0] is None:
        return None
    return f"{format_velocity(vel[0])} ({vel[1] or 0:,.0f} vs {vel[2] or 0:,.0f})"


def buzz_velocities(buzz: RetailBuzzPayload | None, opts: VelocityOptions) -> dict[str, Velocity]:
    """``symbol -> (velocity, mentions, mentions_24h_ago)`` over every live Reddit
    (``apewisdom``) input; the first listing of a symbol wins (pure)."""
    out: dict[str, Velocity] = {}
    if buzz is None:
        return out
    for inp in buzz.inputs.values():
        if inp.type != "apewisdom" or inp.status != "ok":
            continue
        for r in inp.rows:
            v = mention_velocity(r.mentions, r.mentions_24h_ago, opts)
            out.setdefault(r.symbol, (v, r.mentions, r.mentions_24h_ago))
    return out
