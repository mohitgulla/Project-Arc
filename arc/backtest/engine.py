"""Cost-aware, hold-to-expiry backtest engine for defined-risk option structures.

Mechanics (one :class:`Trade` = one unit of one structure):

1. **Entry** on session *d* at the EOD chain: legs chosen by
   :func:`arc.backtest.strategies.select_legs` on mid/Δ; every leg fills at
   ``mid ± x·spread`` (:class:`arc.backtest.costs.CostModel`) and pays
   ``commission_per_contract``.
2. **Exit** = hold to expiration (PLAN D4 evidence: management rules don't beat
   hold-to-expiry). Settlement at intrinsic vs the underlying's raw close on the
   expiration date (last close on/before it). ITM legs pay a closing
   commission. European/cash-settled approximation: early assignment and pin
   risk are ignored.
3. **Risk** per unit = max loss of the filled legs via
   :func:`arc.structures.max_gain_loss` (E2.2), so return-on-risk is measured
   against the post-cost worst case.

Trades whose expiry is after the last available underlying close stay open and
are not reported. Everything here is deterministic and offline.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — pydantic fields
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict

from arc.backtest.chain import prepare_chain
from arc.backtest.regime import label_trend, label_vol
from arc.backtest.strategies import (
    LegPick,
    StrategySpec,
    pick_expirations,
    select_legs,
)
from arc.models import Leg, LegIntent
from arc.pricing.bs import OptionKind
from arc.structures import format_occ, is_defined_risk, max_gain_loss

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.backtest.costs import CostModel

log = structlog.get_logger()

__all__ = [
    "MULT",
    "Trade",
    "TradeLeg",
    "open_trade",
    "prepare_chains",
    "run_backtest",
    "settle",
    "trades_frame",
]

MULT = 100.0


class TradeLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    right: str
    strike: float
    side: int
    mid: float
    spread: float
    fill: float
    delta: float
    iv: float


class Trade(BaseModel):
    """One closed unit trade. Money fields are dollars per unit; prices per share."""

    model_config = ConfigDict(frozen=True)

    underlying: str
    spec: str
    kind: str
    entry_date: dt.date
    expiration: dt.date
    dte: int
    spot_entry: float
    spot_exit: float
    legs: list[TradeLeg]
    entry_net: float  # per share at fills, + = debit paid, − = credit received
    entry_net_mid: float  # same at mid (frictionless)
    fees: float
    pnl: float  # net of slippage + fees
    pnl_mid: float  # at mid, no fees (cost-free reference)
    max_loss: float  # post-cost worst case, > 0
    trend: str = "unknown"
    vol: str = "unknown"

    @property
    def ror(self) -> float:
        return self.pnl / self.max_loss

    @property
    def cost(self) -> float:
        return self.pnl_mid - self.pnl


def prepare_chains(
    history: pd.DataFrame,
    closes: pd.Series,
    *,
    cost: CostModel,
    r: float,
    q: float = 0.0,
    dte_min: int = 1,
    dte_max: int = 60,
) -> dict[dt.date, pd.DataFrame]:
    """Split raw EOD history by session and attach mid/spread/IV/Δ (only DTE in window)."""
    if history.empty:
        return {}
    h = history.copy()
    h["dte"] = (pd.to_datetime(h["expiration"]) - pd.to_datetime(h["date"])).dt.days
    h = h[(h["dte"] >= dte_min) & (h["dte"] <= dte_max)].drop(columns=["dte"])
    out: dict[dt.date, pd.DataFrame] = {}
    for day, rows in h.groupby("date", sort=True):
        spot = closes.get(day)
        if spot is None or not pd.notna(spot):
            continue
        out[day] = prepare_chain(rows, float(spot), cost=cost, r=r, q=q)
    return out


def _intrinsic(right: str, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if right == "call" else max(strike - spot, 0.0)


def _max_loss(legs: Sequence[TradeLeg], underlying: str, expiration: dt.date) -> float | None:
    mleg = [
        Leg(
            occ_symbol=format_occ(
                underlying,
                expiration,
                OptionKind.CALL if lg.right == "call" else OptionKind.PUT,
                Decimal(str(lg.strike)),
            ),
            side=LegIntent.LONG if lg.side > 0 else LegIntent.SHORT,
            ratio=1,
            intent="",
            premium=Decimal(str(round(lg.fill, 6))),
        )
        for lg in legs
    ]
    if not is_defined_risk(mleg):  # e.g. a naked short put has bounded but undefined risk
        return None
    ml = max_gain_loss(mleg)[1]
    return None if ml is None else float(ml)


class _Open(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying: str
    spec: str
    kind: str
    entry_date: dt.date
    expiration: dt.date
    dte: int
    spot_entry: float
    legs: list[TradeLeg]
    open_fees: float
    max_loss: float


def open_trade(
    picks: Sequence[LegPick],
    *,
    spec: StrategySpec,
    underlying: str,
    entry_date: dt.date,
    spot: float,
    cost: CostModel,
) -> _Open | None:
    """Price *picks* with the cost model; ``None`` if post-cost risk is not positive."""
    legs = [
        TradeLeg(
            symbol=p.symbol,
            right=p.right,
            strike=p.strike,
            side=p.side,
            mid=p.mid,
            spread=p.spread,
            fill=cost.fill(p.mid, p.spread, p.side),
            delta=p.delta,
            iv=p.iv,
        )
        for p in picks
    ]
    expiration = picks[0].expiration
    fees = cost.fees(len(legs))
    ml = _max_loss(legs, underlying, expiration)
    if ml is None:
        msg = f"{spec.label}: selected legs are not defined-risk"
        raise ValueError(msg)
    ml += fees
    if ml <= 0:  # arbitrage-looking quote (e.g. stale close); skip rather than divide by ~0
        return None
    return _Open(
        underlying=underlying,
        spec=spec.label,
        kind=str(spec.kind),
        entry_date=entry_date,
        expiration=expiration,
        dte=(expiration - entry_date).days,
        spot_entry=spot,
        legs=legs,
        open_fees=fees,
        max_loss=ml,
    )


def settle(o: _Open, spot_exit: float, cost: CostModel) -> Trade:
    """Close *o* at expiry intrinsic value."""
    entry_net = sum(lg.side * lg.fill for lg in o.legs)
    entry_net_mid = sum(lg.side * lg.mid for lg in o.legs)
    value = sum(lg.side * _intrinsic(lg.right, lg.strike, spot_exit) for lg in o.legs)
    itm = sum(1 for lg in o.legs if _intrinsic(lg.right, lg.strike, spot_exit) > 0)
    fees = o.open_fees + cost.fees(itm)
    return Trade(
        underlying=o.underlying,
        spec=o.spec,
        kind=o.kind,
        entry_date=o.entry_date,
        expiration=o.expiration,
        dte=o.dte,
        spot_entry=o.spot_entry,
        spot_exit=spot_exit,
        legs=o.legs,
        entry_net=entry_net,
        entry_net_mid=entry_net_mid,
        fees=fees,
        pnl=(value - entry_net) * MULT - fees,
        pnl_mid=(value - entry_net_mid) * MULT,
        max_loss=o.max_loss + cost.fees(itm),
    )


def _spot_at_or_before(closes: pd.Series, day: dt.date) -> float | None:
    if len(closes) == 0 or day > max(closes.index):
        return None
    s = closes[pd.Index(closes.index) <= day]
    return None if s.empty else float(s.iloc[-1])


def run_backtest(
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    specs: Iterable[StrategySpec],
    *,
    underlying: str,
    cost: CostModel,
    entry_dates: Iterable[dt.date] | None = None,
) -> list[Trade]:
    """Open every *spec* on every entry session (default: every chain session); settle at expiry."""
    closes = closes.sort_index()
    trend = label_trend(closes)
    vol = label_vol(closes)
    specs = list(specs)
    days = sorted(chains) if entry_dates is None else sorted(set(entry_dates) & set(chains))
    trades: list[Trade] = []
    skipped_open = 0
    for day in days:
        chain = chains[day]
        spot = float(closes[day])
        for spec in specs:
            for exp in pick_expirations(chain, spec):
                picks = select_legs(chain, spec, spot, exp)
                if picks is None:
                    continue
                o = open_trade(
                    picks, spec=spec, underlying=underlying, entry_date=day, spot=spot, cost=cost
                )
                if o is None:
                    continue
                exit_spot = _spot_at_or_before(closes, exp)
                if exit_spot is None:
                    skipped_open += 1
                    continue
                t = settle(o, exit_spot, cost)
                trades.append(t.model_copy(update={"trend": str(trend[day]), "vol": str(vol[day])}))
    log.info(
        "backtest.run",
        underlying=underlying,
        sessions=len(days),
        trades=len(trades),
        still_open=skipped_open,
    )
    return trades


def trades_frame(trades: Sequence[Trade]) -> pd.DataFrame:
    """Flat per-trade DataFrame (legs summarised) for metrics and CSV export."""
    cols = [
        "underlying",
        "spec",
        "kind",
        "entry_date",
        "expiration",
        "dte",
        "spot_entry",
        "spot_exit",
        "entry_net",
        "entry_net_mid",
        "fees",
        "pnl",
        "pnl_mid",
        "max_loss",
        "ror",
        "trend",
        "vol",
        "legs",
    ]
    rows = [
        {
            **t.model_dump(exclude={"legs"}),
            "ror": t.ror,
            "legs": " ".join(f"{'+' if lg.side > 0 else '-'}{lg.symbol}" for lg in t.legs),
        }
        for t in trades
    ]
    return pd.DataFrame(rows, columns=cols)
