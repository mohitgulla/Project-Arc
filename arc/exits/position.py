"""Live exit evaluation for open positions (E6.2 base exits, E6.4 profit-taking/reallocation).

:func:`evaluate_position` answers two questions for one open position, using the same
:class:`~arc.exits.policy.ExitPolicy` as the proposal card and the backtester:

1. Which rule fires **now** (stop → profit lock → take profit → DTE exit), given
   current mid marks and, for the E18.1 profit lock, the peak P&L of the stored marks.
2. What is the **remaining** managed EV if held under the policy, versus closing now
   at the market marks (``remaining_net_ev``, $ per unit, after exit costs on both
   sides). E6.4's close-to-reallocate compares this with a new candidate's managed
   net EV. E6.4a: it is on the same basis as the entry model, so at unchanged marks
   ``remaining_net_ev = managed.net_ev + entry_costs - close_now_net``.

Pure and deterministic; the caller supplies marks (no broker or network access here).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields
import math

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.backtest.costs import CostModel, load_cost_model
from arc.exits.model import MULT, sim_legs, simulate
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
    peak_pnl_per_share: float | None = Field(
        None, description="E18.1: peak per-share P&L since entry (stored marks + now)"
    )
    lock_arm_pnl: float | None = Field(None, description="Per-share peak that arms the lock")
    lock_floor_pnl: float | None = Field(None, description="Per-share P&L an armed lock closes at")
    lock_armed: bool | None = Field(None, description="Profit lock armed; None = no lock")
    remaining_pop: float | None = Field(
        None, ge=0.0, le=1.0, description="P(holding under the policy nets more than closing now)"
    )
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
    peak_pnl: float | None = None,
) -> PositionExitState:
    """Evaluate *position* against *policy* at *marks* (see module doc).

    *peak_pnl* (E18.1) is the peak per-share P&L over the position's stored marks
    since entry (:func:`arc.exits.policy.peak_pnl`); ``None`` = no stored mark, so the
    profit lock is not armed (a peak is never made up from the current mark alone).
    """
    cost = cost or load_cost_model()
    st = position.structure
    rules = resolve_rules(st, policy, entry_net=position.entry_net)
    expiry = parse_occ(st.legs[0].occ_symbol).expiration
    dte = dte_calendar(marks.as_of, expiry)
    value = _mark(position, marks)
    pnl = value - rules.entry_net
    peak = None if peak_pnl is None else max(float(peak_pnl), pnl)
    fired = (
        ExitReason.EXPIRY
        if dte <= 0
        else check_rules(rules, pnl=pnl, dte=dte, eod=marks.end_of_day, peak_pnl=peak)
    )
    arm = rules.lock_arm_pnl

    # closing now: every leg at mid ∓ x·spread, commission on every contract
    legs = sim_legs(st, cost, marks.leg_spreads or None)
    quoted = {parse_occ(k).format(): v for k, v in marks.leg_mids.items()}
    proceeds = 0.0
    close_fees = 0.0
    for lg, leg in zip(legs, st.legs, strict=True):
        mid = quoted[parse_occ(leg.occ_symbol).format()]
        fill = max(mid - lg.sign * cost.slippage_frac * lg.spread, 0.0)
        proceeds += lg.sign * lg.ratio * fill
        close_fees += cost.trade_fees(lg.ratio, -lg.sign, fill)  # closing a long sells
    close_now_net = (proceeds - value) * MULT - close_fees  # cost of closing vs mid, ≤ 0

    gross_ev = net_ev = days = rem_pop = None
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
            peak_pnl=peak,
        )
        # E6.4a parity: "now" is the close at the MARKET marks (the fill closing now
        # would get), the same basis the entry model charges the open against
        # (``model_exits``: model paths vs the market entry fill). Comparing the
        # paths with the flat-IV model value instead dropped the model-vs-market
        # mark gap from the remaining EV but not from the entry EV, so a fresh
        # position read as -EV minutes after a +EV open (Analyst A-1). Exact
        # identity at unchanged marks (same paths):
        #   remaining_net_ev = managed.net_ev + entry_costs - close_now_net
        gross_ev = round(float(np.mean(out.exit_value_mid) - value) * MULT, 2)
        held_paths = out.exit_proceeds * MULT - out.exit_fees
        held_net = float(np.mean(held_paths))
        now_net = proceeds * MULT - close_fees
        net_ev = round(held_net - now_net, 2)
        rem_pop = round(float(np.mean(held_paths > now_net)), 4)
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
        peak_pnl_per_share=None if peak is None else round(peak, 4),
        lock_arm_pnl=None if arm is None else round(arm, 4),
        lock_floor_pnl=None if rules.lock_floor_pnl is None else round(rules.lock_floor_pnl, 4),
        lock_armed=None if arm is None else (peak is not None and peak >= arm),
        remaining_pop=rem_pop,
        n_paths=n_paths,
        seed=seed,
    )
