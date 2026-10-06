"""Exit cases (E13.17, D56): which open positions Quant judges, and with what numbers.

Pure, no I/O. Research's exit watchlist (``review``) and the deterministic position
review's *discretionary* signals each trigger an exit case; a close-to-reallocate
pairing against today's capacity rejections (:func:`arc.positions.reallocate.pair_swaps`,
wrapping the D19 ``score_swaps``) adds a ``reallocate`` trigger. Mandatory signals
(stop, DTE exit, expiry) never get a case: those exits stay deterministic (E13.18
closes them).

The case's numbers (:class:`ExitCaseFacts`) are code-built from ``position_review``
and the E13.17 :class:`~arc.positions.portfolio.PositionFacts`; Quant's LLM
contributes the hold / close judgement only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from arc.positions.evaluate import PositionReview, SignalKind
from arc.positions.portfolio import PositionFacts  # noqa: TC001 - pydantic field

if TYPE_CHECKING:
    from arc.exits.policy import ExitPolicy

__all__ = [
    "DISCRETIONARY_KINDS",
    "MANDATORY_KINDS",
    "ExitCase",
    "ExitCaseFacts",
    "ExitSwap",
    "ExitTrigger",
    "SkipReason",
    "ThesisStatus3",
    "TriggerKind",
    "WatchItemLike",
    "case_skip_reason",
    "stop_state",
    "triggers_for",
]

_FORBID = ConfigDict(extra="forbid")

#: E13.17 (D56): three-state thesis status (``arc.personas.schemas`` re-exports it;
#: defined here so ``arc.positions`` never imports the persona layer).
type ThesisStatus3 = Literal["intact", "weakened", "broken"]


class WatchItemLike(Protocol):
    """The fields :func:`triggers_for` reads from an ``ExitWatchItem``."""

    @property
    def action(self) -> str: ...
    @property
    def thesis_status(self) -> str: ...
    @property
    def reason(self) -> str: ...


#: Loss and time caps: always closed deterministically, never an exit case (D56).
MANDATORY_KINDS: frozenset[SignalKind] = frozenset(
    {SignalKind.STOP, SignalKind.DTE_EXIT, SignalKind.EXPIRY}
)
#: Gain / EV optimisation, where judgement adds value: each triggers an exit case.
DISCRETIONARY_KINDS: frozenset[SignalKind] = frozenset(
    {SignalKind.PROFIT_TARGET, SignalKind.TIME_ADJUSTED_TARGET, SignalKind.REMAINING_EV_FLOOR}
)

type TriggerKind = Literal[
    "research_review", "profit_target", "time_adjusted_target", "remaining_ev_floor", "reallocate"
]
type SkipReason = Literal["mandatory_pending", "exit_pending", "no_trigger"]
type StopState = Literal["armed_eod", "armed", "off"]


class ExitTrigger(BaseModel):
    """Why a position has an exit case."""

    model_config = _FORBID

    kind: TriggerKind
    detail: str = Field(..., max_length=200)


class ExitCaseFacts(BaseModel):
    """The case's numbers, code-built from ``position_review`` + ``PositionFacts``.

    Money is $ per structure unit unless noted (``pnl_total`` / ``buying_power_freed``
    are for all contracts).
    """

    model_config = _FORBID

    dte: int
    contracts: int
    credit: bool
    pnl_total: float
    pct_of_max_gain: float | None = None
    pct_of_max_loss: float | None = None
    pct_of_debit: float | None = None
    take_profit_pct: float | None = None
    stop_state: StopState
    theta_per_day: float | None = None
    buying_power: float | None = None
    close_now_net: float
    remaining_ev_hold: float | None = Field(
        None, description="PositionReview.remaining_ev (hold under the policy − close now)"
    )
    remaining_ev_managed: float | None = Field(
        None, description="PositionReview.entry_managed_net_ev (proposal-time managed Net EV)"
    )
    remaining_ev_per_bp: float | None = None
    remaining_pop: float | None = None
    iv_rank: float | None = None
    next_earnings: str | None = None
    ex_dividend: str | None = None
    buying_power_freed: float | None = Field(None, description="buying_power × contracts")
    thesis_status: ThesisStatus3 | None = None

    @classmethod
    def from_review(
        cls,
        review: PositionReview,
        facts: PositionFacts | None,
        *,
        policy: ExitPolicy | None = None,
        thesis_status: ThesisStatus3 | None = None,
    ) -> ExitCaseFacts:
        """Build the facts; *policy* sets ``stop_state`` (``off`` when unknown)."""
        bp = review.buying_power
        return cls(
            dte=review.dte,
            contracts=review.contracts,
            credit=review.credit,
            pnl_total=round(review.pnl_total, 2),
            pct_of_max_gain=review.pct_of_max_gain,
            pct_of_max_loss=review.pct_of_max_loss,
            pct_of_debit=review.pct_of_debit,
            take_profit_pct=review.take_profit_pct,
            stop_state=stop_state(policy),
            theta_per_day=review.theta_per_day,
            buying_power=bp,
            close_now_net=review.close_now_net,
            remaining_ev_hold=review.remaining_ev,
            remaining_ev_managed=review.entry_managed_net_ev,
            remaining_ev_per_bp=review.remaining_ev_per_bp,
            remaining_pop=review.remaining_pop,
            iv_rank=facts.iv_rank if facts else None,
            next_earnings=facts.next_earnings if facts else None,
            ex_dividend=facts.ex_dividend if facts else None,
            buying_power_freed=round(bp * review.contracts, 2) if bp is not None else None,
            thesis_status=thesis_status,
        )


class ExitSwap(BaseModel):
    """A D19 close-to-reallocate pairing (``SwapSuggestion`` fields, gate-free copy).

    ``arc.positions.reallocate.SwapSuggestion`` types ``rejected_for`` with the gate's
    ``CapacityRejection``; context payloads must not import ``arc.gate``, so the case
    stores the same numbers with the reason as its string value.
    """

    model_config = _FORBID

    close_structure_id: str
    close_ticker: str
    source_ref: str
    open_ticker: str
    rejected_for: str
    new_ev_per_bp: float
    open_remaining_ev_per_bp: float
    switching_cost_per_bp: float
    edge: float
    min_edge_required: float
    new_pop: float
    open_remaining_pop: float
    detail: str

    @classmethod
    def of(cls, suggestion: BaseModel) -> ExitSwap:
        """From a ``SwapSuggestion`` (enum values become their string values)."""
        return cls.model_validate(suggestion.model_dump(mode="json"))


class ExitCase(BaseModel):
    """One position Quant judges (kind ``exit_case``, subject = structure id)."""

    model_config = _FORBID

    structure_id: str
    ticker: str
    kind: str | None = None
    triggers: list[ExitTrigger] = Field(..., min_length=1)
    facts: ExitCaseFacts
    swap: ExitSwap | None = Field(
        None, description="Set when a capacity rejection pairs with this position"
    )
    options: list[Literal["hold", "close"]] = Field(default_factory=lambda: ["hold", "close"])
    recommendation: Literal["hold", "close"] = Field(
        ..., description="Quant's call (LLM), code-checked; a missing judgement = hold"
    )
    rationale: str = Field(..., max_length=400)
    review_id: str | None = Field(None, description="exit_watchlist entry id this answers")
    schema_version: int = 1


def stop_state(policy: ExitPolicy | None) -> StopState:
    """``armed_eod`` (stop on end-of-day marks only, D23 default), ``armed``, ``off``."""
    if policy is None or policy.stop is None:
        return "off"
    return "armed_eod" if policy.stop_eod_only else "armed"


def case_skip_reason(review: PositionReview, *, today_exit: bool = False) -> SkipReason | None:
    """Why *review* can never get an exit case (``None`` = it may)."""
    if any(s.kind in MANDATORY_KINDS for s in review.signals):
        return "mandatory_pending"
    if review.exit_pending or today_exit:
        return "exit_pending"
    return None


def _clip(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def triggers_for(
    review: PositionReview,
    watch_item: WatchItemLike | None,
    swap: ExitSwap | None = None,
) -> list[ExitTrigger]:
    """The exit-case triggers of one position, in a fixed order (pure).

    (a) the watchlist says ``review`` → ``research_review``; (b) each discretionary
    signal of the review → its kind; (c) a swap pairing → ``reallocate``. Mandatory
    signals are not triggers (see :func:`case_skip_reason`).
    """
    out: list[ExitTrigger] = []
    if watch_item is not None and watch_item.action == "review":
        detail = f"Research: thesis {watch_item.thesis_status}"
        if watch_item.reason:
            detail += f": {watch_item.reason}"
        out.append(ExitTrigger(kind="research_review", detail=_clip(detail)))
    for sig in review.signals:
        if sig.kind in DISCRETIONARY_KINDS:
            out.append(ExitTrigger(kind=sig.kind.value, detail=_clip(sig.detail)))  # type: ignore[arg-type]
    if swap is not None:
        out.append(ExitTrigger(kind="reallocate", detail=_clip(swap.detail)))
    return out
