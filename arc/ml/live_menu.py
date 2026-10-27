"""Adapter: one live Quant menu item (:class:`~arc.scanner.scan.ScanCandidate`) as a
:class:`~arc.ml.live_rows.LiveCandidate` (E27.4, D126).

The ``ctx_*`` facts carry the ledger's semantics (:mod:`arc.ml.ledger.build`): the
same gate rules (earnings blackout), the same ranking hard filter and the same
per-leg spread rule decide them, called here rather than re-implemented. Shadow
only: called after the menus are built; nothing it returns feeds a decision.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

from arc.ml.live_rows import LiveCandidate, LiveLeg
from arc.ml.scores import legs_key

if TYPE_CHECKING:
    from arc.config import ArcSettings
    from arc.exits.model import ExitSummary
    from arc.scanner.rank import RankFilters
    from arc.scanner.scan import ScanCandidate

ET_CLOSE = dt.time(16, 0)


def earnings_blackout(
    c: ScanCandidate,
    next_earnings: dict[str, dt.date | None],
    settings: ArcSettings,
    now: dt.datetime,
) -> bool:
    """The gate's earnings-blackout rule on this item (the ledger's ``ctx_earnings_blackout``)."""
    from arc.gate.inputs import MarketSnapshot
    from arc.gate.rules import Derived, check_earnings_blackout
    from arc.models import Proposal

    st = c.structure
    d = Derived(
        underlying=c.ticker,
        expiration=c.expiration,
        kind=st.kind,  # type: ignore[arg-type]
        defined_risk=True,
        max_loss_total=None,
        net_price=st.net_debit_credit,
        limit_price=st.net_debit_credit,
    )
    market = MarketSnapshot(
        next_earnings={c.ticker: next_earnings[c.ticker]} if c.ticker in next_earnings else {}
    )
    proposal = Proposal.model_construct(earnings_play=False, risk_concurs=False)
    return bool(check_earnings_blackout(proposal, d, market, settings, now))


def from_scan(
    c: ScanCandidate,
    *,
    spot: float,
    summary: ExitSummary | None,
    settings: ArcSettings,
    filters: RankFilters,
    now: dt.datetime,
    next_earnings: dict[str, dt.date | None],
    sleeve: str | None = None,
    est_cost: float | None = None,
    atr14: float | None = None,
    credit_only: bool = False,
    filter_reason: str | None = None,
    etf: bool = False,
    rank_live: int | None = None,
) -> LiveCandidate:
    """The ledger-shaped facts of one live menu item (pure on its inputs)."""
    from arc.backtest.sim import spread_ok
    from arc.scanner.be_atr import structure_be_atr
    from arc.scanner.rank import RankInputs, passes_filters

    st = c.structure
    legs = tuple(
        LiveLeg(
            symbol=leg.occ_symbol,
            side=1 if leg.side.value == "long" else -1,
            mid=float(leg.premium) if leg.premium is not None else math.nan,
            spread=float(c.leg_spreads.get(leg.occ_symbol, math.nan)),
        )
        for leg in st.legs
    )
    net = float(st.net_debit_credit)
    credit = net < 0
    s = summary
    inputs = RankInputs(
        key=legs_key([(leg.symbol, leg.side) for leg in legs]),
        credit=credit,
        vertical=len(legs) == 2,  # noqa: PLR2004
        credit_width=c.credit_width,
        ev_proxy=c.ev_proxy,
        managed_net_ev=None if s is None else s.managed_net_ev,
        managed_pop=None if s is None else s.managed_pop,
        est_cost=est_cost,
    )
    try:
        be = structure_be_atr(st, spot, atr14) if atr14 is not None else None
    except (ValueError, ZeroDivisionError):
        be = None
    return LiveCandidate(
        ticker=c.ticker,
        strategy=c.strategy.value,
        dte=c.dte,
        expiration=c.expiration,
        spot=spot,
        legs=legs,
        net_mid=net,
        max_loss=float(st.max_loss) if st.max_loss is not None else math.nan,
        max_gain=float(st.max_gain) if st.max_gain is not None else None,
        breakevens=tuple(float(b) for b in st.breakevens),
        short_deltas=tuple(c.short_deltas),
        long_deltas=tuple(c.long_deltas),
        width=c.width,
        credit_width=c.credit_width,
        ev_proxy=c.ev_proxy,
        atm_iv=c.atm_iv,
        leg_oi_min=float(st.liquidity.open_interest),
        leg_volume_min=float(st.liquidity.volume),
        sleeve=sleeve,
        static_pop=None if s is None else s.static_pop,
        static_net_ev=None if s is None else s.static_net_ev,
        managed_pop=None if s is None else s.managed_pop,
        managed_net_ev=None if s is None else s.managed_net_ev,
        expected_days_held=None if s is None else s.expected_days_held,
        rorc_day=None if s is None else s.rorc_day,
        vrp=None if s is None else s.vrp,
        est_cost=est_cost,
        be_atr=be,
        passes_filters=passes_filters(inputs, filters),
        entry_spread_ok=all(spread_ok(leg.mid, leg.spread, settings) for leg in legs),
        earnings_blackout=earnings_blackout(c, next_earnings, settings, now),
        # the live menu already lost every kind the closed regime gate refuses (D131)
        regime_blocked=False,
        filter_reason=filter_reason,
        credit_only=credit_only,
        etf=etf,
        rank_live=rank_live,
    )
