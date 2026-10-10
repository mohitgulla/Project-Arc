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
- exit signals, in precedence order: stop → profit lock (E18.1) → profit target /
  time-adjusted target → DTE exit → expiry, plus ``remaining_ev_floor``
  (``positions:`` in exits.yaml),
- the profit lock's state: the peak P&L since entry over the stored marks
  (``peak_pnl``, supplied by the caller) and whether the lock is armed.

Works for credit and debit structures alike (D25: paper defaults to cash_debit).
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic fields
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.costs import CostModel  # noqa: TC001 - runtime default arg type
from arc.exits.policy import ExitConfig, ExitReason, ResolvedRules, resolve_rules
from arc.exits.position import OpenPosition, PositionMarks, evaluate_position
from arc.models import Structure  # noqa: TC001 - pydantic field

__all__ = ["ExitSignal", "PositionReview", "SignalKind", "review_position"]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class SignalKind(StrEnum):
    """Why the position manager suggests closing (journal ``exit:<kind>``)."""

    STOP = "stop"
    PROFIT_LOCK = "profit_lock"  # E18.1 (D78): trailing take profit, mandatory
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
    # E6.4a observability: what the remaining-EV floor saw and why it did (not) fire.
    end_of_day: bool | None = Field(None, description="Marks were end-of-day marks")
    ev_floor: float | None = Field(None, description="Remaining-EV floor per $ BP in force")
    ev_floor_window: str | None = Field(
        None, description="'eod' (floor on EOD marks only) or 'intraday'; None = floor off"
    )
    ev_floor_live: bool | None = Field(
        None, description="The floor was evaluated on this review (window open)"
    )
    entry_managed_net_ev: float | None = Field(
        None, description="Proposal-time E2.4 managed Net EV, $ per unit"
    )
    entry_managed_net_ev_per_bp: float | None = None
    minutes_since_fill: float | None = Field(None, ge=0.0)
    # E18.1 (D78) profit lock: per-share P&L basis (same as current_value − entry_net)
    peak_pnl_per_share: float | None = Field(
        None, description="Peak per-share P&L since entry (stored marks + this one)"
    )
    pct_peak: float | None = Field(None, description="Peak P&L / take-profit basis")
    lock_armed: bool | None = Field(None, description="Profit lock armed; None = no lock")
    structure: Structure

    @property
    def signal(self) -> ExitSignal | None:
        """The first (highest-precedence) signal, or ``None`` = keep holding."""
        return self.signals[0] if self.signals else None


_FIRED = {
    ExitReason.STOP: SignalKind.STOP,
    ExitReason.PROFIT_LOCK: SignalKind.PROFIT_LOCK,
    ExitReason.TAKE_PROFIT: SignalKind.PROFIT_TARGET,
    ExitReason.DTE_EXIT: SignalKind.DTE_EXIT,
    ExitReason.EXPIRY: SignalKind.EXPIRY,
}


def _pct_basis(x: float | None, rules: ResolvedRules) -> str:
    """*x* (per share) as a share of the take-profit basis: ``+52% of debit``."""
    basis = rules.tp_basis
    if x is None or not basis:
        return "n/a"
    return f"{x / basis:+.0%} of {'max gain' if rules.credit else 'debit'}"


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
    cost: CostModel | None = None,
    entry_managed_net_ev: float | None = None,
    minutes_since_fill: float | None = None,
    peak_pnl: float | None = None,
) -> PositionReview:
    """Review one open position (see module doc). Raises ``LookupError`` on a missing mark.

    E6.4a: with ``positions.remaining_ev_floor_eod_only`` (default) the floor is only
    evaluated on end-of-day marks (``marks.end_of_day``), never intraday.
    *entry_managed_net_ev* (the open proposal's managed Net EV) and
    *minutes_since_fill* are recorded for the audit only; they never change a signal.
    *peak_pnl* (E18.1) is the peak per-share P&L of the position's stored marks
    (:func:`arc.positions.marks.stored_peak_pnl`); ``None`` leaves the lock unarmed.
    """
    st = position.structure
    policy = exits.policy_for(st.kind)
    state = evaluate_position(
        position, marks, policy, cost=cost, cfg=exits.model, peak_pnl=peak_pnl
    )
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
            SignalKind.PROFIT_LOCK: (
                f"profit lock: peak {_pct_basis(state.peak_pnl_per_share, rules)} fell to "
                f"{_pct_basis(per, rules)} (floor {_pct_basis(state.lock_floor_pnl, rules)}): "
                f"{head}"
            ),
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
    eod_only = exits.positions.remaining_ev_floor_eod_only
    window = None if floor is None else ("eod" if eod_only else "intraday")
    floor_live = floor is not None and (marks.end_of_day or not eod_only)
    if floor_live and floor is not None and rem_bp is not None and rem_bp < floor:
        when = "end-of-day marks" if marks.end_of_day else "intraday marks"
        signals.append(
            ExitSignal(
                kind=SignalKind.REMAINING_EV_FLOOR,
                detail=(
                    f"remaining EV per $ BP {rem_bp:+.4f} < floor {floor:+.4f} ({when}): {head}"
                ),
            )
        )
    entry_bp = (
        None if entry_managed_net_ev is None or bp is None else round(entry_managed_net_ev / bp, 6)
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
        end_of_day=marks.end_of_day,
        ev_floor=floor,
        ev_floor_window=window,
        ev_floor_live=None if floor is None else floor_live,
        entry_managed_net_ev=entry_managed_net_ev,
        entry_managed_net_ev_per_bp=entry_bp,
        minutes_since_fill=(
            None if minutes_since_fill is None else round(max(minutes_since_fill, 0.0), 1)
        ),
        peak_pnl_per_share=state.peak_pnl_per_share,
        pct_peak=(
            None
            if state.peak_pnl_per_share is None or not rules.tp_basis
            else round(state.peak_pnl_per_share / rules.tp_basis, 4)
        ),
        lock_armed=state.lock_armed,
        structure=st,
    )
