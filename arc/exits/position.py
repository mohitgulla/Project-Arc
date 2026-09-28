"""Live exit evaluation for open positions (E6.2 base exits, E6.4 profit-taking/reallocation).

:func:`evaluate_position` answers two questions for one open position, using the same
:class:`~arc.exits.policy.ExitPolicy` as the proposal card and the backtester:

1. Which rule fires **now** (stop → take profit → DTE exit), given current mid marks.
2. What is the **remaining** managed EV if held under the policy, versus closing now
   (``remaining_net_ev``, $ per unit, after exit costs on both sides). E6.4's
   close-to-reallocate compares this with a new candidate's managed net EV.

Pure and deterministic; the caller supplies marks (no broker or network access here).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields
import math

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.backtest.costs import CostModel
from arc.exits.model import MULT, close_values, sim_legs, simulate
from arc.exits.policy import (
    ExitModelConfig,
    ExitPolicy,
    ExitReason,
    check_rules,
    resolve_rules,
)
from arc.models import LegIntent, Structure  # noqa: TC001 - pydantic fields
from arc.structures import parse_occ
from arc.utils.calendar import dte_calendar

__all__ = ["OpenPosition", "PositionExitState", "PositionMarks", "evaluate_position"]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class OpenPosition(BaseModel):
    """An open position: the structure as opened plus the actual entry price."""

    model_config = _FORBID

    structure: Structure = Field(..., description="Legs as opened (premium = entry mid)")
    entry_net: float | None = Field(
        None, description="Per-share fill price, + debit / − credit; default = structure mid"
    )
    contracts: int = Field(1, ge=1)


class PositionMarks(BaseModel):
    """Current market marks for the position's legs."""

    model_config = _FORBID

    as_of: dt.date
    leg_mids: dict[str, float] = Field(..., description="Mid per share by OCC symbol")
    spot: float | None = Field(None, gt=0.0, description="Underlying price (for remaining EV)")
    iv: float | None = Field(None, gt=0.0, description="ATM IV (for remaining EV)")
    r: float = 0.04
    leg_spreads: dict[str, float] = Field(default_factory=dict, description="ask − bid per leg")
    realized_vol: float | None = Field(
        None, gt=0.0, description="Realised-vol forecast the remaining paths move at (else IV)"
    )
    end_of_day: bool = Field(
        True, description="End-of-day marks; intraday marks never trigger an EOD-only stop"
    )

    @model_validator(mode="after")
    def _finite(self) -> PositionMarks:
        if any(not math.isfinite(v) or v < 0 for v in self.leg_mids.values()):
            msg = "leg mids must be finite and >= 0"
            raise ValueError(msg)
        return self


class PositionExitState(BaseModel):
    """Which rule has fired and what holding is still worth. Money: $ per unit unless noted."""

    model_config = _FORBID

    as_of: dt.date
    dte: int
    fired: ExitReason | None = Field(None, description="None = keep holding")
    entry_net: float = Field(..., description="Per share, + debit / − credit")
    current_value: float = Field(..., description="Per share at mid (long +, short −)")
    close_price: float = Field(..., description="Per share to close at mid (abs value)")
    close_side: str = Field(..., description="'debit' (pay to close) or 'credit' (receive)")
    pnl_per_share: float
    pnl: float = Field(..., description="$ per unit at mid, before costs")
    pnl_total: float = Field(..., description="$ for all contracts at mid, before costs")
    pct_of_max_gain: float | None = None
    take_profit_pct: float | None = Field(None, description="TP percentage in force at this DTE")
    take_profit_pnl: float | None = Field(None, description="Per-share P&L that takes profit")
    stop_pnl: float | None = Field(None, description="Per-share P&L that stops out")
    close_now_net: float = Field(..., description="$ per unit realised by closing now, after costs")
    remaining_gross_ev: float | None = Field(
        None, description="E[further P&L at mid] if held under the policy"
    )
    remaining_net_ev: float | None = Field(
        None, description="E[net P&L if held] − net P&L of closing now (after exit costs)"
    )
    remaining_days_held: float | None = None
    n_paths: int | None = None
    seed: int | None = None


def _mark(position: OpenPosition, marks: PositionMarks) -> float:
    quoted = {parse_occ(k).format(): v for k, v in marks.leg_mids.items()}
    v = 0.0
    for leg in position.structure.legs:
        key = parse_occ(leg.occ_symbol).format()
        if key not in quoted:
            msg = f"no mark for leg {key}"
            raise LookupError(msg)
        sign = 1 if leg.side == LegIntent.LONG else -1
        v += sign * leg.ratio * quoted[key]
    return v


def evaluate_position(
    position: OpenPosition,
    marks: PositionMarks,
    policy: ExitPolicy,
    *,
    cost: CostModel | None = None,
    cfg: ExitModelConfig | None = None,
) -> PositionExitState:
    """Evaluate *position* against *policy* at *marks* (see module doc)."""
    cost = cost or CostModel()
    st = position.structure
    rules = resolve_rules(st, policy, entry_net=position.entry_net)
    expiry = parse_occ(st.legs[0].occ_symbol).expiration
    dte = dte_calendar(marks.as_of, expiry)
    value = _mark(position, marks)
    pnl = value - rules.entry_net
    fired = (
        ExitReason.EXPIRY
        if dte <= 0
        else check_rules(rules, pnl=pnl, dte=dte, eod=marks.end_of_day)
    )

    # closing now: every leg at mid ∓ x·spread, commission on every contract
    legs = sim_legs(st, cost, marks.leg_spreads or None)
    quoted = {parse_occ(k).format(): v for k, v in marks.leg_mids.items()}
    proceeds = 0.0
    for lg, leg in zip(legs, st.legs, strict=True):
        mid = quoted[parse_occ(leg.occ_symbol).format()]
        proceeds += lg.sign * lg.ratio * max(mid - lg.sign * cost.slippage_frac * lg.spread, 0.0)
    close_fees = cost.fees(sum(lg.ratio for lg in legs))
    close_now_net = (proceeds - value) * MULT - close_fees  # cost of closing vs mid, ≤ 0

    gross_ev = net_ev = days = None
    n_paths = seed = None
    if dte >= 1 and marks.spot is not None and marks.iv is not None:
        cfg = cfg or ExitModelConfig()
        out = simulate(
            legs,
            rules,
            spot=marks.spot,
            iv=marks.iv,
            r=marks.r,
            dte=dte,
            cost=cost,
            cfg=cfg,
            path_vol=marks.realized_vol if cfg.path_vol == "realized_forecast" else None,
        )
        # Model-consistent "now" value so the EV compares like with like.
        v0, p0, f0 = close_values(
            legs, np.array([marks.spot]), dte / 365.0, marks.r, marks.iv, cost
        )
        gross_ev = round(float(np.mean(out.exit_value_mid) - v0[0]) * MULT, 2)
        held_net = float(np.mean(out.exit_proceeds * MULT - out.exit_fees))
        now_net = float(p0[0] * MULT - f0[0])
        net_ev = round(held_net - now_net, 2)
        days = round(float(np.mean(out.exit_day)), 2)
        n_paths, seed = cfg.n_paths, cfg.seed

    tp = rules.tp_pnl(max(dte, 0))
    return PositionExitState(
        as_of=marks.as_of,
        dte=dte,
        fired=fired,
        entry_net=rules.entry_net,
        current_value=round(value, 4),
        close_price=round(abs(value), 4),
        close_side="debit" if value < 0 else "credit",
        pnl_per_share=round(pnl, 4),
        pnl=round(pnl * MULT, 2),
        pnl_total=round(pnl * MULT * position.contracts, 2),
        pct_of_max_gain=(None if not rules.max_gain else round(pnl / rules.max_gain, 4)),
        take_profit_pct=rules.tp_pct(max(dte, 0)),
        take_profit_pnl=None if tp is None else round(tp, 4),
        stop_pnl=None if rules.stop_pnl is None else round(rules.stop_pnl, 4),
        close_now_net=round(close_now_net, 2),
        remaining_gross_ev=gross_ev,
        remaining_net_ev=net_ev,
        remaining_days_held=days,
        n_paths=n_paths,
        seed=seed,
    )
