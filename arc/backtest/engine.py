"""Cost-aware, hold-to-expiry backtest engine for defined-risk option structures.

Mechanics (one :class:`Trade` = one unit of one structure):

1. **Entry** on session *d* at the EOD chain: legs chosen by
   :func:`arc.backtest.strategies.select_legs` on mid/Δ; every leg fills at
   ``mid ± x·spread`` (:class:`arc.backtest.costs.CostModel`) and pays
   ``commission_per_contract``.
2. **Exit** (``exit_policy``):

   - ``hold_to_expiry`` (default; PLAN D4 evidence: management rules don't beat
     hold-to-expiry). Settlement at intrinsic vs the underlying's raw close on
     the expiration date (last close on/before it). ITM legs pay a closing
     commission. European/cash-settled approximation: early assignment and pin
     risk are ignored.
   - ``policy`` (D19/D23): the structure kind's :class:`arc.exits.ExitPolicy`
     from ``config/exits.yaml`` is checked on every later session's EOD mids
     (stop → take profit → DTE exit, :func:`arc.exits.check_rules`). A fired
     rule closes every leg at ``mid ∓ x·spread`` and pays the commission on
     every contract. Sessions where a leg has no row are skipped (no mark).
     Positions that never fire settle at expiry as above.
3. **Risk** per unit = max loss of the filled legs via
   :func:`arc.structures.max_gain_loss` (E2.2), so return-on-risk is measured
   against the post-cost worst case.

Trades whose expiry is after the last available underlying close stay open and
are not reported. Everything here is deterministic and offline.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 — pydantic fields
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict

from arc.backtest.chain import prepare_chain
from arc.backtest.regime import label_trend, label_vol
from arc.backtest.strategies import (
    LegPick,
    StrategyKind,
    StrategySpec,
    pick_expirations,
    select_legs,
)
from arc.exits.policy import ExitReason, ResolvedRules, check_rules, resolve_rules
from arc.models import Leg, LegIntent, StructureKind
from arc.pricing.bs import OptionKind
from arc.structures import analyze, format_occ, is_defined_risk, max_gain_loss

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from arc.backtest.costs import CostModel
    from arc.exits.policy import ExitConfig

log = structlog.get_logger()

__all__ = [
    "MULT",
    "STRUCTURE_KIND",
    "ExitPolicyMode",
    "Trade",
    "TradeLeg",
    "close_early",
    "open_trade",
    "prepare_chains",
    "run_backtest",
    "settle",
    "trades_frame",
]

MULT = 100.0

ExitPolicyMode = Literal["hold_to_expiry", "policy", "d19_rules"]
# "d19_rules" (E6.4 card name) = "policy": the same config/exits.yaml rules the live
# position evaluator (arc.positions) and the Investor's exits use.
_POLICY_MODES = ("policy", "d19_rules")

# Backtest strategy kind → the structure kind whose ExitPolicy applies.
STRUCTURE_KIND: dict[str, StructureKind] = {
    StrategyKind.LONG_CALL: StructureKind.LONG_CALL,
    StrategyKind.LONG_PUT: StructureKind.LONG_PUT,
    StrategyKind.BULL_CALL: StructureKind.VERTICAL_DEBIT,
    StrategyKind.BEAR_PUT: StructureKind.VERTICAL_DEBIT,
    StrategyKind.BULL_PUT: StructureKind.VERTICAL_CREDIT,
    StrategyKind.BEAR_CALL: StructureKind.VERTICAL_CREDIT,
    StrategyKind.IRON_CONDOR: StructureKind.IRON_CONDOR,
}


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
    exit_reason: str = ExitReason.EXPIRY.value
    exit_date: dt.date | None = None  # None = settled at expiry

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


def _mid_legs(o: _Open) -> list[Leg]:
    return [
        Leg(
            occ_symbol=format_occ(
                o.underlying,
                o.expiration,
                OptionKind.CALL if lg.right == "call" else OptionKind.PUT,
                Decimal(str(lg.strike)),
            ),
            side=LegIntent.LONG if lg.side > 0 else LegIntent.SHORT,
            premium=Decimal(str(round(lg.mid, 6))),
        )
        for lg in o.legs
    ]


def _rules_for(o: _Open, policies: ExitConfig) -> ResolvedRules:
    structure = analyze(_mid_legs(o), as_of=o.entry_date)
    return resolve_rules(structure, policies.policy_for(STRUCTURE_KIND[o.kind]))


def close_early(
    o: _Open,
    *,
    day: dt.date,
    marks: Mapping[str, tuple[float, float]],
    spot: float,
    reason: ExitReason,
    cost: CostModel,
) -> Trade:
    """Close *o* on *day* at *marks* (``symbol → (mid, spread)``): every leg pays slippage
    and commission."""
    entry_net = sum(lg.side * lg.fill for lg in o.legs)
    entry_net_mid = sum(lg.side * lg.mid for lg in o.legs)
    value_mid = sum(lg.side * marks[lg.symbol][0] for lg in o.legs)
    value = sum(
        lg.side * cost.fill(marks[lg.symbol][0], marks[lg.symbol][1], -lg.side) for lg in o.legs
    )
    close_fees = cost.fees(len(o.legs))
    fees = o.open_fees + close_fees
    return Trade(
        underlying=o.underlying,
        spec=o.spec,
        kind=o.kind,
        entry_date=o.entry_date,
        expiration=o.expiration,
        dte=o.dte,
        spot_entry=o.spot_entry,
        spot_exit=spot,
        legs=o.legs,
        entry_net=entry_net,
        entry_net_mid=entry_net_mid,
        fees=fees,
        pnl=(value - entry_net) * MULT - fees,
        pnl_mid=(value_mid - entry_net_mid) * MULT,
        max_loss=o.max_loss + close_fees,
        exit_reason=reason.value,
        exit_date=day,
    )


def _day_marks(
    chain: pd.DataFrame, symbols: Sequence[str]
) -> dict[str, tuple[float, float]] | None:
    rows = chain[chain["symbol"].isin(symbols)]
    marks = {str(r.symbol): (float(r.mid), float(r.spread)) for r in rows.itertuples()}
    return marks if len(marks) == len(set(symbols)) else None


def _managed_exit(
    o: _Open,
    rules: ResolvedRules,
    chains: Mapping[dt.date, pd.DataFrame],
    days: Sequence[dt.date],
    closes: pd.Series,
    cost: CostModel,
) -> Trade | None:
    """First session after entry (before expiry) where a rule fires, closed there."""
    symbols = [lg.symbol for lg in o.legs]
    for day in days:
        if day <= o.entry_date:
            continue
        if day >= o.expiration:
            break
        marks = _day_marks(chains[day], symbols)
        if marks is None:
            continue
        pnl = sum(lg.side * marks[lg.symbol][0] for lg in o.legs) - rules.entry_net
        reason = check_rules(rules, pnl=pnl, dte=(o.expiration - day).days)
        if reason is not None:
            return close_early(
                o, day=day, marks=marks, spot=float(closes[day]), reason=reason, cost=cost
            )
    return None


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
    exit_policy: ExitPolicyMode = "hold_to_expiry",
    policies: ExitConfig | None = None,
) -> list[Trade]:
    """Open every *spec* on every entry session (default: every chain session).

    ``exit_policy="hold_to_expiry"`` settles at expiry; ``"policy"`` applies the
    per-kind :class:`arc.exits.ExitPolicy` from *policies* (default:
    ``config/exits.yaml``) on each later session's marks (see module doc).
    """
    if exit_policy not in ("hold_to_expiry", *_POLICY_MODES):
        msg = f"unknown exit_policy {exit_policy!r}"
        raise ValueError(msg)
    if exit_policy in _POLICY_MODES and policies is None:
        from arc.exits.policy import load_exit_config

        policies = load_exit_config()
    all_days = sorted(chains)
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
                t = None
                if policies is not None and exit_policy in _POLICY_MODES:
                    rules = _rules_for(o, policies)
                    t = _managed_exit(o, rules, chains, all_days, closes, cost)
                if t is None:
                    t = settle(o, exit_spot, cost)
                trades.append(t.model_copy(update={"trend": str(trend[day]), "vol": str(vol[day])}))
    log.info(
        "backtest.run",
        underlying=underlying,
        sessions=len(days),
        trades=len(trades),
        still_open=skipped_open,
        exit_policy=exit_policy,
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
        "exit_reason",
        "exit_date",
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
