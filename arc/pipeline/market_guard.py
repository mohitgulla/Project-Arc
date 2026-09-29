"""Market-conditions guard (E5.9, D33): deterministic, runs before the Director LLM.

New opens are blocked (``market_unclear``) when

* VIX ≥ ``no_trade_vix_max`` (default 35),
* the VIX term structure is in backwardation (E4.5 ``vol_term`` context) and
  ``no_trade_on_backwardation`` is on, or
* the SPY regime read is transitional: stickiness below
  ``no_trade_transitional_min_confidence``.

Missing VIX data fails closed for new opens (``market_data_missing``) when
``no_trade_require_vix`` is on. VIX comes from the ``vol_term`` context entry
(Cboe closes) or, failing that, the env's VIX
quote (``PipelineEnv.vix_quote``). Exits are never affected: the guard is
consulted by the entry chain only.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Literal

import structlog

from arc.positions.portfolio import MarketGuard, VixReading

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot

__all__ = ["MarketGuard", "VixReading", "market_guard", "read_vix"]

log = structlog.get_logger(__name__)

VOL_TERM_MAX_AGE = _dt.timedelta(hours=30)  # a previous-session close still counts


def read_vix(
    snapshot: ContextSnapshot,
    vix_quote: Callable[[], tuple[float, str] | None] | None,
    *,
    now: _dt.datetime,
) -> VixReading | None:
    """VIX from the ``vol_term`` context (fresh), else *vix_quote*, else ``None``."""
    entry = snapshot.latest("vol_term", "market")
    if entry is not None and now - entry.valid_from <= VOL_TERM_MAX_AGE:
        p = entry.payload
        try:
            return VixReading(
                value=float(p["vix"]),
                as_of=str(p.get("as_of", entry.valid_from.date().isoformat())),
                source="vol_term",
                structure=p.get("structure"),
            )
        except (KeyError, TypeError, ValueError):
            pass
    if vix_quote is None:
        return None
    try:
        got = vix_quote()
    except Exception as exc:  # noqa: BLE001 - a failed quote is a missing reading (fail closed)
        log.warning("pipeline.vix_quote_failed", error=str(exc))
        return None
    if got is None:
        return None
    value, as_of = got
    if not value > 0:
        return None
    return VixReading(value=float(value), as_of=str(as_of), source="market")


def _spy_regime(snapshot: ContextSnapshot) -> tuple[str | None, float | None]:
    entry = snapshot.latest("regime", "SPY")
    if entry is None:
        return None, None
    reg = entry.payload.get("regime") or {}
    if not isinstance(reg, dict):
        return None, None
    current = reg.get("current")
    stick = reg.get("stickiness")
    return (str(current) if current else None), (float(stick) if stick is not None else None)


def market_guard(
    snapshot: ContextSnapshot,
    settings: ArcSettings,
    *,
    now: _dt.datetime,
    vix_quote: Callable[[], tuple[float, str] | None] | None = None,
) -> MarketGuard:
    """Evaluate the D33 guard. Pure with respect to the LLM; reads context + market only."""
    reasons: list[str] = []
    checked: list[str] = []
    code: Literal["market_unclear", "market_data_missing"] | None = None
    vix = read_vix(snapshot, vix_quote, now=now)
    checked.append("vix")
    if vix is None:
        if settings.no_trade_require_vix:
            reasons.append("no VIX reading (vol_term context or market quote)")
            code = "market_data_missing"
    else:
        if vix.value >= settings.no_trade_vix_max:
            reasons.append(f"VIX {vix.value:.1f} >= {settings.no_trade_vix_max:.0f}")
        if settings.no_trade_on_backwardation and vix.structure == "backwardation":
            checked.append("term_structure")
            reasons.append("VIX term structure in backwardation")
    regime, stick = _spy_regime(snapshot)
    if regime is not None:
        checked.append("regime")
        if stick is not None and stick < settings.no_trade_transitional_min_confidence:
            reasons.append(
                f"SPY regime {regime} transitional (stickiness {stick:.2f} < "
                f"{settings.no_trade_transitional_min_confidence:.2f})"
            )
    if reasons and code is None:
        code = "market_unclear"
    guard = MarketGuard(
        opens_allowed=not reasons,
        reason_code=code,
        reasons=reasons,
        vix=vix,
        regime=regime,
        regime_stickiness=stick,
        checked=checked,
    )
    log.info("pipeline.market_guard", opens_allowed=guard.opens_allowed, reasons=reasons)
    return guard
