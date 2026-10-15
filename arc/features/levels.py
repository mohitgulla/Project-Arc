"""The ``levels`` context kind and its merge into regime technicals (E23.0, PLAN D98).

The pre-market ``levels`` job (``arc.levels.refresh``) writes one :class:`LevelsPayload`
per ticker: the 20-session composite volume profile from SIP 15-min bars and the
options OI walls of the swing-sleeve expiries. The regime step then folds them into
the day's :class:`~arc.features.technicals.TechnicalFeatures` with
:func:`merge_levels`, measured against the technicals' own close and ATR14 (no network
in the trading loop).

Pure: no network, no LLM, no clock. Context only, never a gate input.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - used at runtime in the pydantic model
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.features.oi_walls import OiWalls  # noqa: TC001 - pydantic field
from arc.features.volume_profile import VolumeProfile, nearest_above, nearest_below

if TYPE_CHECKING:
    from arc.features.technicals import TechnicalFeatures

__all__ = ["LevelsPayload", "VolumeProfileLevels", "merge_levels"]


class VolumeProfileLevels(VolumeProfile):
    """A :class:`VolumeProfile` plus the window and feed it was built from."""

    model_config = ConfigDict(extra="forbid")

    sessions: int = Field(..., ge=1, description="Completed sessions in the profile")
    first_session: dt.date
    last_session: dt.date
    bar: str = Field(..., description="Bar timeframe, e.g. 15Min")
    feed: Literal["sip", "iex"] | None = None
    bars: int = Field(..., ge=0, description="RTH bars used")
    atr14: float | None = Field(None, description="ATR14 from the bars' sessions (bin width)")


class LevelsPayload(BaseModel):
    """One ticker's pre-market levels (``levels`` kind, subject = ticker)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: dt.date = Field(..., description="Trading day the levels are for (ET)")
    volume_profile: VolumeProfileLevels | None = None
    oi_walls: OiWalls | None = None
    missing: list[str] = Field(default_factory=list)


def _round(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(v + 0.0, nd)


def merge_levels(
    tech: TechnicalFeatures,
    levels: LevelsPayload | None,
    *,
    volume_profile: bool = True,
    oi_walls: bool = True,
) -> TechnicalFeatures:
    """*tech* with the E23.0 level fields set from *levels*.

    Distances use ``tech.close`` and ``tech.atr14`` (``None`` without a positive ATR).
    A part switched off (*volume_profile* / *oi_walls*) or missing leaves its fields
    ``None``; with both off (or no *levels*) *tech* is returned unchanged.
    """
    if levels is None or not (volume_profile or oi_walls):
        return tech
    close = tech.close
    atr = tech.atr14 if tech.atr14 is not None and tech.atr14 > 0 else None

    def dist(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None or atr is None else _round((a - b) / atr)

    upd: dict[str, float | None] = {}
    vp = levels.volume_profile if volume_profile else None
    if vp is not None:
        width = vp.vah - vp.val
        upd.update(
            vp_poc=vp.poc,
            vp_vah=vp.vah,
            vp_val=vp.val,
            vp_hvn_above=nearest_above(vp.hvns, close),
            vp_hvn_below=nearest_below(vp.hvns, close),
            vp_pos=_round((close - vp.val) / width) if width > 0 else None,
            dist_poc_atr=dist(close, vp.poc),
            dist_vah_atr=dist(close, vp.vah),
            dist_val_atr=dist(close, vp.val),
        )
    walls = levels.oi_walls if oi_walls else None
    if walls is not None:
        upd.update(
            oi_call_wall=walls.call_wall,
            oi_put_wall=walls.put_wall,
            oi_max_strike=walls.max_oi_strike,
            dist_call_wall_atr=dist(walls.call_wall, close),
            dist_put_wall_atr=dist(close, walls.put_wall),
        )
    return tech.model_copy(update=upd) if upd else tech
