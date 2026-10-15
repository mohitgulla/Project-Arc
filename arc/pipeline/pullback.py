"""E23.2 (D98): the pullback watch for anti-chase drops (pure rule).

Anti-chase (E16.3) drops a directional long-premium idea whose move already ran
(``stretched_entry``). The interview rule is "don't chase the breakout, wait for the
pullback": the Research step keeps each such drop as a ``pullback_watch`` context
entry (:class:`arc.context.kinds.PullbackWatchPayload`) and, on every later run,
asks :func:`rearm_verdict` whether the stretch has reset:

* ``expired``     - the watch outlived its last session (trading sessions, holiday-aware).
* ``invalidated`` - today's regime label flipped against the stance, or price fell
  through the far side of SMA20 (stance-signed ``(spot - SMA20) / ATR14`` below
  ``invalidate_below_sma20_atr``): a pullback that became a reversal is not bought.
* ``ready``       - the entry is back at a reasonable distance: ``target: value`` (D98
  owner update, default) = spot within ``rearm_level_atr`` ATR above the nearest
  support level (bullish: POC / VAH / the HVN below, at or below the drop price; bears
  mirror with POC / VAL / the HVN above) or already below it; ``target: sma20`` =
  stance-signed SMA20 stretch ``<= rearm_stretch_atr``. Missing levels fall back to
  ``sma20`` (``vp_missing``). With ``anti_chase.vwap`` on, the stance-signed session
  VWAP stretch must also be ``<= rearm_vwap_atr``; a failed VWAP read waits.
* ``waiting``     - anything else, including missing inputs (fail closed for
  re-surfacing).

Deterministic: no LLM, no network, no store; never a gate input. Every number in the
rendered line is computed here.
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic resolves the field types at runtime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PullbackRule",
    "PullbackStatus",
    "PullbackVerdict",
    "WatchView",
    "rearm_verdict",
]

PullbackStatus = Literal["waiting", "ready", "expired", "invalidated"]
PullbackTarget = Literal["value", "sma20"]
Direction = Literal["bullish", "bearish"]
LevelName = Literal["POC", "VAH", "VAL", "HVN"]

#: A regime label that runs against the watched stance (a flip invalidates the watch).
_AGAINST: dict[str, str] = {"bullish": "bear", "bearish": "bull"}


class PullbackRule(BaseModel):
    """Thresholds of the re-arm rule (D98 defaults; ``config/strategy.yaml pullback_watch:``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: PullbackTarget = "value"
    rearm_stretch_atr: float = Field(1.0, ge=0.0, le=2.5)
    rearm_level_atr: float = Field(0.5, ge=0.0, le=2.0)
    rearm_vwap_atr: float = Field(0.25, ge=0.0, le=1.0)
    invalidate_below_sma20_atr: float = Field(-1.0, ge=-5.0, le=0.0)
    vwap: bool = Field(False, description="anti_chase.vwap: the VWAP condition applies")


class WatchView(BaseModel):
    """The watch fields the rule reads (a subset of the ``pullback_watch`` payload)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ticker: str
    stance: Direction
    expires_session: _dt.date
    price_at_drop: float | None = None
    sma20: float | None = None
    atr14: float | None = None


class Level(BaseModel):
    """The support (bullish) / resistance (bearish) level the pullback is measured to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: LevelName
    price: float
    dist_atr: float = Field(..., description="Stance-signed (spot - level) / ATR14")


class PullbackVerdict(BaseModel):
    """The rule's outcome for one watch, with the numbers it read (journal payload)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PullbackStatus
    ticker: str
    stance: Direction
    target: PullbackTarget | None = Field(None, description="The target actually used")
    vp_missing: bool = Field(False, description="target value asked, no levels: sma20 used")
    vwap_missing: bool = Field(False, description="VWAP part on, no session VWAP: waiting")
    spot: float | None = None
    sma20_stretch_atr: float | None = Field(None, description="(spot - SMA20) / ATR14, signed")
    vwap_stretch_atr: float | None = Field(None, description="(spot - VWAP) / ATR14, signed")
    level: Level | None = None
    regime: str | None = None
    missing: list[str] = Field(default_factory=list)
    reason: str = ""
    rule: PullbackRule

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def now_text(self) -> str:
        """``now 0.3 ATR above POC 182.40, +0.6 ATR vs SMA20, +0.1 ATR vs VWAP``."""
        parts: list[str] = []
        if self.level is not None:
            # dist_atr is stance-signed: >= 0 = still on the stance side of the level
            raw = self.level.dist_atr if self.stance == "bullish" else -self.level.dist_atr
            side = "above" if raw >= 0 else "below"
            parts.append(f"{abs(raw):.1f} ATR {side} {self.level.name} {self.level.price:.2f}")
        if self.sma20_stretch_atr is not None:
            parts.append(f"{self.sma20_stretch_atr:+.1f} ATR vs SMA20")
        if self.vwap_stretch_atr is not None:
            parts.append(f"{self.vwap_stretch_atr:+.1f} ATR vs VWAP")
        return "now " + ", ".join(parts) if parts else "now n/a"

    def text(self) -> str:
        """One plain line for the journal, e.g. ``ready: now 0.3 ATR above POC 182.40``."""
        tail = f" ({self.reason})" if self.reason else ""
        return f"{self.status}: {self.now_text()}{tail}"


def _support_levels(
    tech: object, stance: Direction, price_at_drop: float | None
) -> list[tuple[LevelName, float]]:
    """The volume-profile levels a pullback is measured to (positive prices only)."""
    get = lambda name: getattr(tech, name, None)  # noqa: E731 - tiny local accessor
    raw: list[tuple[LevelName, object]] = (
        [("POC", get("vp_poc")), ("VAH", get("vp_vah")), ("HVN", get("vp_hvn_below"))]
        if stance == "bullish"
        else [("POC", get("vp_poc")), ("VAL", get("vp_val")), ("HVN", get("vp_hvn_above"))]
    )
    out: list[tuple[LevelName, float]] = []
    for name, v in raw:
        if not isinstance(v, int | float) or v <= 0:
            continue
        # levels on the far side of the drop price are not a pullback target
        if price_at_drop is not None and (
            (stance == "bullish" and v > price_at_drop)
            or (stance == "bearish" and v < price_at_drop)
        ):
            continue
        out.append((name, float(v)))
    return out


def _nearest_level(
    levels: list[tuple[LevelName, float]],
    spot: float,
    atr: float,
    stance: Direction,
) -> Level:
    """The level on the stance side of spot nearest to it; else (spot past all) the nearest."""
    sign = 1.0 if stance == "bullish" else -1.0
    dists: list[tuple[LevelName, float, float]] = [
        (name, px, sign * (spot - px) / atr) for name, px in levels
    ]
    on_side = [d for d in dists if d[2] >= 0]
    pool = on_side or dists
    best = min(pool, key=lambda d: (abs(d[2]), d[0]))
    return Level(name=best[0], price=round(best[1], 4), dist_atr=round(best[2], 4))


def rearm_verdict(
    watch: WatchView,
    spot: float | None,
    tech_today: object | None,
    vwap_stretch: float | None,
    rule: PullbackRule,
    *,
    today: _dt.date,
    regime: str | None = None,
) -> PullbackVerdict:
    """Is a stretched-and-dropped idea back at a reasonable entry? (see the module doc).

    *tech_today* is today's :class:`arc.features.technicals.TechnicalFeatures` (or any
    object with ``sma20`` / ``atr14`` / ``vp_*`` attributes); its SMA20 / ATR14 win over
    the ones stored at the drop. *vwap_stretch* is ``(spot - session VWAP) / ATR14``
    (``None``: not read or failed). *regime* is today's ``regime.current`` label.
    """
    stance = watch.stance
    base = {"ticker": watch.ticker, "stance": stance, "rule": rule, "spot": spot, "regime": regime}
    if today > watch.expires_session:
        return PullbackVerdict(
            status="expired", reason=f"watched through {watch.expires_session}", **base
        )
    label = (regime or "").strip().lower()
    if label and label == _AGAINST[stance]:
        return PullbackVerdict(status="invalidated", reason=f"regime flipped to {label}", **base)
    sma20 = getattr(tech_today, "sma20", None) or watch.sma20
    atr = getattr(tech_today, "atr14", None) or watch.atr14
    missing = [
        n
        for n, v in (("spot", spot), ("sma20", sma20), ("atr14", atr))
        if v is None or (n != "sma20" and v <= 0)
    ]
    if missing or spot is None or sma20 is None or atr is None:
        return PullbackVerdict(
            status="waiting", missing=missing, reason="missing " + ", ".join(missing), **base
        )
    sign = 1.0 if stance == "bullish" else -1.0
    raw = (spot - sma20) / atr
    signed = sign * raw
    base["sma20_stretch_atr"] = round(raw, 4)
    if signed < rule.invalidate_below_sma20_atr:
        side = "below" if stance == "bullish" else "above"
        return PullbackVerdict(
            status="invalidated",
            reason=f"{abs(raw):.1f} ATR {side} SMA20 "
            f"(limit {abs(rule.invalidate_below_sma20_atr):g})",
            **base,
        )
    target: PullbackTarget = rule.target
    vp_missing = False
    level: Level | None = None
    if target == "value":
        levels = _support_levels(tech_today, stance, watch.price_at_drop)
        if levels:
            level = _nearest_level(levels, spot, atr, stance)
        else:
            target, vp_missing = "sma20", True
    base |= {"target": target, "vp_missing": vp_missing, "level": level}
    if level is not None:
        near = level.dist_atr <= rule.rearm_level_atr
        why = f"{level.dist_atr:.2f} ATR from {level.name} (rearm {rule.rearm_level_atr:g})"
    else:
        near = signed <= rule.rearm_stretch_atr
        why = f"{signed:.2f} ATR stretch vs SMA20 (rearm {rule.rearm_stretch_atr:g})"
    if not near:
        return PullbackVerdict(status="waiting", reason=why, **base)
    if rule.vwap:
        if vwap_stretch is None:
            return PullbackVerdict(
                status="waiting", vwap_missing=True, reason="no session VWAP", **base
            )
        base["vwap_stretch_atr"] = round(vwap_stretch, 4)
        if sign * vwap_stretch > rule.rearm_vwap_atr:
            return PullbackVerdict(
                status="waiting",
                reason=f"{sign * vwap_stretch:.2f} ATR over VWAP (rearm {rule.rearm_vwap_atr:g})",
                **base,
            )
    return PullbackVerdict(status="ready", reason=why, **base)
