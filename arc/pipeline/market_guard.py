"""Market-conditions guard (E5.9, D33): deterministic, runs before Research LLM.

New opens are blocked (``market_unclear``) when

* VIX ≥ ``no_trade_vix_max`` (default 35),
* the VIX term structure is in backwardation (E4.5 ``vol_term`` context) and
  ``no_trade_on_backwardation`` is on, or
* the SPY regime read is transitional (D77, E17.2): on a v2 entry the trend label
  has held fewer than ``regime_guard_min_run`` sessions, or its z sits closer than
  ``regime_guard_min_margin_z`` to a bull/bear threshold. An entry without those
  confirmation fields (v1 / pre-v2 rows) falls back to the D33 stickiness test,
  ``stickiness < no_trade_transitional_min_confidence`` (``regime_check: legacy``).
  No SPY regime entry at all skips the check (the VIX checks still fail closed).

Missing VIX data fails closed for new opens (``market_data_missing``) when
``no_trade_require_vix`` is on. VIX comes from the ``vol_term`` context entry
(Cboe closes) or, failing that, the env's VIX
quote (``PipelineEnv.vix_quote``). Exits are never affected: the guard is
consulted by the entry chain only.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Literal, NamedTuple

import structlog

from arc.positions.portfolio import MarketGuard, VixReading

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot

__all__ = [
    "MarketGuard",
    "SpyRegime",
    "VixReading",
    "market_guard",
    "read_vix",
    "transitional_reason",
]

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


class SpyRegime(NamedTuple):
    """The SPY regime fields the guard reads (confirmation fields are v2 only)."""

    current: str
    stickiness: float | None = None
    run_length: int | None = None
    margin_z: float | None = None


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _spy_regime(snapshot: ContextSnapshot) -> SpyRegime | None:
    entry = snapshot.latest("regime", "SPY")
    if entry is None:
        return None
    reg = entry.payload.get("regime") or {}
    if not isinstance(reg, dict) or not reg.get("current"):
        return None
    run = _num(reg.get("run_length"))
    return SpyRegime(
        current=str(reg["current"]),
        stickiness=_num(reg.get("stickiness")),
        run_length=int(run) if run is not None else None,
        margin_z=_num(reg.get("margin_z")),
    )


def transitional_reason(
    reg: SpyRegime, settings: ArcSettings
) -> tuple[Literal["confirmation", "legacy"], str | None]:
    """Which transitional test ran and, when SPY is transitional, the reason text.

    v2 entries (``run_length`` and ``margin_z`` present) use the D77 confirmation
    test; anything else falls back to the D33 stickiness test.
    """
    if reg.run_length is not None and reg.margin_z is not None:
        min_run, min_z = settings.regime_guard_min_run, settings.regime_guard_min_margin_z
        short_run = reg.run_length < min_run
        near = reg.margin_z < min_z
        if not (short_run or near):
            return "confirmation", None
        run_op = "<" if short_run else ">="
        z_op = "<" if near else ">="
        return "confirmation", (
            f"SPY regime {reg.current} transitional (run {reg.run_length}d {run_op} {min_run}, "
            f"margin z {reg.margin_z:.2f} {z_op} {min_z:.2f})"
        )
    floor = settings.no_trade_transitional_min_confidence
    if reg.stickiness is not None and reg.stickiness < floor:
        return "legacy", (
            f"SPY regime {reg.current} transitional (stickiness {reg.stickiness:.2f} < {floor:.2f})"
        )
    return "legacy", None


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
    reg = _spy_regime(snapshot)
    regime_check: Literal["confirmation", "legacy"] | None = None
    if reg is not None:
        checked.append("regime")
        regime_check, why = transitional_reason(reg, settings)
        if why is not None:
            reasons.append(why)
    if reasons and code is None:
        code = "market_unclear"
    guard = MarketGuard(
        opens_allowed=not reasons,
        reason_code=code,
        reasons=reasons,
        vix=vix,
        regime=reg.current if reg else None,
        regime_stickiness=reg.stickiness if reg else None,
        regime_run_length=reg.run_length if reg else None,
        regime_margin_z=reg.margin_z if reg else None,
        regime_check=regime_check,
        checked=checked,
    )
    log.info("pipeline.market_guard", opens_allowed=guard.opens_allowed, reasons=reasons)
    return guard
