"""Deterministic position review (E6.4 §1, D19). Pure: no I/O, no LLM, no broker.

For one open position (the structure as opened + its fill) and current marks,
:func:`review_position` calls E2.4's :func:`~arc.exits.evaluate_position` (the
same ``config/exits.yaml`` policy the proposal card and the backtester use; no
thresholds live here) and adds what the position manager needs:

- ``pnl``, ``pct_of_max_gain`` (credit: captured / initial credit),
  ``pct_of_max_loss``, ``pct_of_debit`` (debit), DTE, theta/day,
- ``remaining_ev``: E[net P&L if held under the policy] − net P&L of closing now,
  after half-spread slippage and fees on both paths (exit model at current IV),
- ``remaining_ev_per_bp``: that per $ of buying power the position holds,
- ``remaining_pop``: P(holding nets more than closing now),
- exit signals, in precedence order: stop → profit target / time-adjusted target
  → DTE exit → expiry, plus ``remaining_ev_floor`` (``positions:`` in exits.yaml).

Works for credit and debit structures alike (D25: paper defaults to cash_debit).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from arc.exits.policy import ExitConfig, ExitReason, resolve_rules
from arc.exits.position import OpenPosition, PositionMarks, evaluate_position
from arc.models import Structure  # noqa: TC001 - pydantic field

__all__ = ["ExitSignal", "PositionReview", "SignalKind", "review_position"]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class SignalKind(StrEnum):
    """Why the position manager suggests closing (journal ``exit:<kind>``)."""

    STOP = "stop"
    PROFIT_TARGET = "profit_target"
    TIME_ADJUSTED_TARGET = "time_adjusted_target"
    DTE_EXIT = "dte_exit"
    EXPIRY = "expiry"
    REMAINING_EV_FLOOR = "remaining_ev_floor"


class ExitSignal(BaseModel):
    model_config = _FORBID

    kind: SignalKind
    detail: str = Field(..., description="Plain-language why, used in the Investor's post")


class PositionReview(BaseModel):
    """One open position, reviewed. Money: $ per structure unit unless noted.

    Written to the context store as kind ``position_review`` (subject = structure id).
    """

    model_config = ConfigDict(extra="forbid")

    structure_id: str
    ticker: str
    kind: str | None
    credit: bool = Field(..., description="Opened for a net credit")
    contracts: int = Field(..., ge=1)
    as_of: dt.date
    dte: int
    entry_net: float = Field(..., description="Per share, + debit / − credit")
    current_value: float = Field(..., description="Per share at mid (long +, short −)")
    pnl: float = Field(..., description="$ per unit at mid, before costs")
    pnl_total: float = Field(..., description="$ for all contracts at mid, before costs")
    pct_of_max_gain: float | None = Field(None, description="P&L / max gain")
    pct_of_max_loss: float | None = Field(None, description="Loss / max loss (0 when in profit)")
    pct_of_debit: float | None = Field(None, description="Debit structures: P&L / debit paid")
    take_profit_pct: float | None = Field(None, description="TP percentage in force at this DTE")
    theta_per_day: float | None = Field(None, description="$ per unit per calendar day")
    buying_power: float | None = Field(None, description="$ per unit held by the position")
    close_now_net: float = Field(..., description="$ per unit: closing now vs mid, after costs")
    remaining_ev: float | None = Field(
        None, description="E[net if held under the policy] − net of closing now, $ per unit"
    )
    remaining_ev_per_bp: float | None = Field(None, description="remaining_ev / buying_power")
    remaining_pop: float | None = Field(None, ge=0.0, le=1.0)
    remaining_days_held: float | None = None
    signals: list[ExitSignal] = Field(default_factory=list)
    exit_pending: bool = Field(False, description="An exit proposal is already pending/working")
    structure: Structure

    @property
    def signal(self) -> ExitSignal | None:
        """The first (highest-precedence) signal, or ``None`` = keep holding."""
        return self.signals[0] if self.signals else None


_FIRED = {
    ExitReason.STOP: SignalKind.STOP,
    ExitReason.TAKE_PROFIT: SignalKind.PROFIT_TARGET,
    ExitReason.DTE_EXIT: SignalKind.DTE_EXIT,
    ExitReason.EXPIRY: SignalKind.EXPIRY,
}


def _money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 10 else f"${x:,.2f}"


def review_position(
    *,
    structure_id: str,
    ticker: str,
    position: OpenPosition,
    marks: PositionMarks,
    exits: ExitConfig,
    theta_per_day: float | None = None,
    exit_pending: bool = False,
) -> PositionReview:
    """Review one open position (see module doc). Raises ``LookupError`` on a missing mark."""
    st = position.structure
    policy = exits.policy_for(st.kind)
    state = evaluate_position(position, marks, policy, cfg=exits.model)
    rules = resolve_rules(st, policy, entry_net=position.entry_net)
    per = state.pnl_per_share
    pct_gain = state.pct_of_max_gain
    pct_loss = None if not rules.max_loss else round(max(-per, 0.0) / rules.max_loss, 4)
    pct_debit = None if rules.credit or rules.entry_net <= 0 else round(per / rules.entry_net, 4)
    bp_raw = st.buying_power if st.buying_power is not None else st.max_loss
    bp = None if bp_raw is None or bp_raw <= 0 else float(bp_raw)
    rem = state.remaining_net_ev
    rem_bp = None if rem is None or bp is None else round(rem / bp, 6)

    progress = (
        f"{pct_gain:+.0%} of max gain" if rules.credit and pct_gain is not None
        else f"{pct_debit:+.0%} of debit" if pct_debit is not None
        else f"P&L {_money(state.pnl)}"
    )  # fmt: skip
    ev_text = (
        f", remaining EV {_money(rem)} on {_money(bp)} BP"
        if rem is not None and bp is not None
        else ""
    )
    head = f"{progress} at {state.dte} DTE{ev_text}"

    signals: list[ExitSignal] = []
    if state.fired is not None:
        kind = _FIRED[state.fired]
        base = (
            policy.take_profit_pct_of_max_gain if rules.credit else policy.take_profit_pct_of_debit
        )
        if kind is SignalKind.PROFIT_TARGET and state.take_profit_pct != base:
            kind = SignalKind.TIME_ADJUSTED_TARGET
        detail = {
            SignalKind.STOP: f"stop: {head}",
            SignalKind.PROFIT_TARGET: (
                f"profit target {state.take_profit_pct:.0%} reached: {head}"
            ),
            SignalKind.TIME_ADJUSTED_TARGET: (
                f"time-adjusted target {state.take_profit_pct:.0%} at <= "
                f"{state.dte} DTE reached: {head}"
            ),
            SignalKind.DTE_EXIT: f"DTE exit (close at <= {policy.close_at_dte} DTE): {head}",
            SignalKind.EXPIRY: f"expiry: {head}",
        }[kind]
        signals.append(ExitSignal(kind=kind, detail=detail))
    floor = exits.positions.floor_for(st.kind)
    if floor is not None and rem_bp is not None and rem_bp < floor:
        signals.append(
            ExitSignal(
                kind=SignalKind.REMAINING_EV_FLOOR,
                detail=f"remaining EV per $ BP {rem_bp:+.4f} < floor {floor:+.4f}: {head}",
            )
        )

    return PositionReview(
        structure_id=structure_id,
        ticker=ticker,
        kind=None if st.kind is None else st.kind.value,
        credit=rules.credit,
        contracts=position.contracts,
        as_of=state.as_of,
        dte=state.dte,
        entry_net=state.entry_net,
        current_value=state.current_value,
        pnl=state.pnl,
        pnl_total=state.pnl_total,
        pct_of_max_gain=pct_gain,
        pct_of_max_loss=pct_loss,
        pct_of_debit=pct_debit,
        take_profit_pct=state.take_profit_pct,
        theta_per_day=None if theta_per_day is None else round(theta_per_day, 2),
        buying_power=bp,
        close_now_net=state.close_now_net,
        remaining_ev=rem,
        remaining_ev_per_bp=rem_bp,
        remaining_pop=state.remaining_pop,
        remaining_days_held=state.remaining_days_held,
        signals=signals,
        exit_pending=exit_pending,
        structure=st,
    )
