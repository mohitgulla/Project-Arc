"""Build :class:`~arc.journal.analytics.ProposalAnalytics` for a re-priced proposal (E6.1a).

Pure: it takes the :class:`~arc.pipeline.market.PricedStructure` (the same chain
snapshot the proposal was priced with), the ticker's ``regime`` context payload,
the shared cost model and the E2.4 exit model result, and returns the stored
analytics. No I/O, no LLM, no live quote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arc.backtest.costs import MULTIPLIER, CostModel, FeeBreakdown
from arc.journal.analytics import (
    BreakevenStat,
    LegAnalytics,
    ProposalAnalytics,
    VolStats,
    be_atr_multiple,
    expected_move,
    moneyness_pct,
    otm,
    sigma_distance,
    sigma_t,
)
from arc.structures import parse_occ

if TYPE_CHECKING:
    from arc.exits.model import ExitModelResult
    from arc.pipeline.market import PricedStructure

__all__ = ["build_analytics", "regime_atr14"]


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _vol(regime: dict[str, Any] | None, atm_iv: float | None) -> VolStats:
    vol = (regime or {}).get("vol") or {}
    iv = atm_iv if atm_iv is not None else _f(vol.get("iv"))
    hv20 = _f(vol.get("hv20"))
    return VolStats(
        atm_iv=iv,
        iv_rank=_f(vol.get("iv_rank")),
        iv_percentile=_f(vol.get("iv_percentile")),
        hv20=hv20,
        hv60=_f(vol.get("hv60")),
        iv_hv20=(iv / hv20) if iv is not None and hv20 else None,
        features_as_of=vol.get("as_of") or (regime or {}).get("as_of"),
    )


def regime_atr14(regime: dict[str, Any] | None) -> float | None:
    """E16.5: ATR14 ($) from the ``regime`` entry's E16.2 ``technicals`` (None if absent)."""
    tech = (regime or {}).get("technicals")
    atr = _f(tech.get("atr14")) if isinstance(tech, dict) else None
    return atr if atr is not None and atr > 0 else None


def build_analytics(
    priced: PricedStructure,
    *,
    cost: CostModel,
    regime: dict[str, Any] | None = None,
    exit_model: ExitModelResult | None = None,
    account_profile: str | None = None,
) -> ProposalAnalytics:
    """Analytics for *priced* (raises ``ValueError`` without a spot)."""
    st = priced.structure
    spot = priced.spot
    if spot is None or spot <= 0:
        msg = "analytics need a positive spot"
        raise ValueError(msg)
    iv = priced.atm_iv
    dte = st.dte
    legs: list[LegAnalytics] = []
    slip_total = 0.0
    fees_total = FeeBreakdown()
    for leg in st.legs:
        occ = parse_occ(leg.occ_symbol)
        c = priced.contracts.get(occ.format())
        kind = "call" if occ.kind.name == "CALL" else "put"
        strike = float(occ.strike)
        side = 1 if leg.side.value == "long" else -1
        bid = c.bid if c else None
        ask = c.ask if c else None
        mid = float(leg.premium) if leg.premium is not None else (c.mid if c else None)
        spread = ask - bid if bid is not None and ask is not None else None
        fill = slip = None
        fees = FeeBreakdown()
        if mid is not None:
            sp = cost.spread(mid, bid, ask)
            fill = cost.fill(mid, sp, side)
            slip = abs(fill - mid) * leg.ratio * MULTIPLIER
            fees = cost.fee_breakdown(leg.ratio, side, fill)
            slip_total += slip
            fees_total = fees_total + fees
        legs.append(
            LegAnalytics(
                occ_symbol=occ.format(),
                side=leg.side.value,
                ratio=leg.ratio,
                kind=kind,
                strike=strike,
                expiration=occ.expiration,
                bid=bid,
                ask=ask,
                mid=mid,
                bid_size=c.bid_size if c else None,
                ask_size=c.ask_size if c else None,
                open_interest=c.open_interest if c else None,
                volume=c.volume if c else None,
                iv=c.implied_volatility if c else None,
                delta=(c.greeks.delta if c and c.greeks else None),
                quote_ts=c.quote_timestamp if c else None,
                spread=spread,
                spread_pct=(spread / mid) if spread is not None and mid else None,
                moneyness_pct=moneyness_pct(strike, spot),
                sigma_distance=sigma_distance(strike, spot, iv, dte),
                otm=otm(kind, strike, spot),
                entry_fill=fill,
                entry_slippage=round(slip or 0.0, 4),
                entry_fees=fees,
            )
        )
    prev = _f((regime or {}).get("last_close"))
    atr14 = regime_atr14(regime)  # E16.5 (D76): breakevens in ATR terms
    return ProposalAnalytics(
        spot=spot,
        spot_as_of=priced.spot_as_of,
        prev_close=prev,
        prev_close_as_of=(regime or {}).get("as_of"),
        day_change_pct=(spot / prev - 1.0) if prev else None,
        dte=dte,
        sigma_t=sigma_t(iv, dte),
        expected_move=expected_move(spot, iv, dte),
        breakevens=[
            BreakevenStat(
                price=float(b),
                pct=moneyness_pct(float(b), spot),
                sigma=sigma_distance(float(b), spot, iv, dte),
                atr_multiple=be_atr_multiple(float(b), spot, atr14, dte),
            )
            for b in st.breakevens
        ],
        legs=legs,
        vol=_vol(regime, iv),
        cost_model=cost,
        entry_slippage=round(slip_total, 4),
        entry_fees=fees_total,
        exit_model=exit_model,
        account_profile=account_profile,
    )
