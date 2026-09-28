"""Stable decision vocabulary for the journal (E7.4): the ONE enum module.

Every :class:`~arc.journal.models.DecisionRecord` carries a ``reason_code``
from :class:`ReasonCode`. Free text (``reason_text``) adds detail but never
replaces the code, so decisions can be counted and compared across runs.

Gate codes are ``gate:<RuleCode>`` for every rule in :mod:`arc.gate.rules`; a
test pins that mapping so a new gate rule cannot ship without its code.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Choice",
    "JournalPersona",
    "ReasonCode",
    "ReviewLabel",
    "Reviewer",
    "RootCause",
    "Stage",
    "gate_reason",
]


class JournalPersona(StrEnum):
    """Who made the decision (a persona, a deterministic component, or the owner)."""

    SCOUT = "scout"
    DIRECTOR = "director"
    QUANT = "quant"
    RISK = "risk"
    INVESTOR = "investor"
    AUDITOR = "auditor"
    GATE = "gate"
    SIZING = "sizing"
    OWNER = "owner"
    SYSTEM = "system"


class Stage(StrEnum):
    """Pipeline stage, in the order ``arc journal show`` prints them."""

    CANDIDATE = "candidate"
    SHORTLIST = "shortlist"
    STRUCTURE = "structure"
    RISK_REVIEW = "risk_review"
    PROPOSE = "propose"
    SIZING = "sizing"
    GATE = "gate"
    APPROVAL = "approval"
    ORDER = "order"
    EXIT = "exit"
    RECONCILE = "reconcile"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class Choice(StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"
    NO_TRADE = "no_trade"
    ASSESSED = "assessed"
    SIZED = "sized"
    PASSED = "passed"
    FAILED = "failed"
    APPROVED = "approved"
    EXPIRED = "expired"
    NOTED = "noted"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"


class ReasonCode(StrEnum):
    # candidate (what the Director was offered)
    SCOUT_CANDIDATE = "scout_candidate"
    # shortlist (Director)
    SHORTLISTED = "shortlisted"
    NOT_RANKED = "not_ranked"
    NOT_A_CANDIDATE = "not_a_candidate"
    DUPLICATE = "duplicate"
    INVALID_FIELD = "invalid_field"
    OVER_LIMIT = "over_limit"
    MARKET_READ = "market_read"
    NO_CANDIDATES = "no_candidates"
    # structure (Quant + scanner)
    CHOSEN_FROM_MENU = "chosen_from_menu"
    MENU_NOT_CHOSEN = "menu_not_chosen"
    NOT_IN_MENU = "not_in_menu"
    NOT_SHORTLISTED = "not_shortlisted"
    NO_CHAIN = "no_chain"
    QUANT_OMITTED = "quant_omitted"
    # risk review
    RISK_ASSESSED = "risk_assessed"
    RISK_DECLINED = "risk_declined"
    UNKNOWN_STRUCTURE = "unknown_structure"
    NOT_ASSESSED = "not_assessed"
    # propose (deterministic)
    PROPOSED = "proposed"
    ALREADY_PROPOSED = "already_proposed"
    NO_STRUCTURE = "no_structure"
    NO_RISK_REVIEW = "no_risk_review"
    NO_CANDIDATE_ID = "no_candidate_id"
    REPRICE_FAILED = "reprice_failed"
    # sizing (D18)
    SIZING_OK = "sizing:ok"
    SIZING_CAPPED = "sizing:capped"
    SIZING_CAP_ZERO = "sizing:cap_zero"
    SIZING_RISK_ZERO = "sizing:risk_zero"
    SIZING_UNBOUNDED = "sizing:unbounded"
    SIZING_INVALID_INPUT = "sizing:invalid_input"
    # gate: one code per arc.gate.rules.RuleCode
    GATE_PASS = "gate:pass"
    GATE_STRUCTURE_INVALID = "gate:structure_invalid"
    GATE_PER_UNDERLYING = "gate:per_underlying_limit"
    GATE_DAILY_LOSS = "gate:daily_loss_halt"
    GATE_HALTED = "gate:halted"
    GATE_SPREAD = "gate:spread_too_wide"
    GATE_LIMIT_OUTSIDE_NBBO = "gate:limit_outside_nbbo"
    GATE_TICK = "gate:limit_off_tick"
    GATE_WASH_SALE = "gate:wash_sale"
    GATE_DELTA_CAP = "gate:portfolio_delta_cap"
    GATE_VEGA_CAP = "gate:portfolio_vega_cap"
    GATE_STRUCTURE_NOT_ALLOWED = "gate:structure_not_allowed"
    GATE_DTE_WINDOW = "gate:dte_window"
    GATE_EARNINGS_BLACKOUT = "gate:earnings_blackout"
    GATE_MAX_POSITIONS = "gate:max_open_positions"
    GATE_APPROVAL_TTL = "gate:approval_ttl"
    GATE_STALE_DATA = "gate:stale_data"
    GATE_NO_MAX_GAIN = "gate:limit_no_max_gain"
    GATE_RULE_ERROR = "gate:rule_error"
    GATE_PRICE_BAND = "gate:price_band"
    GATE_CLOSE_MISMATCH = "gate:close_mismatch"
    # approval (E6.1)
    OWNER_APPROVE = "owner_approve"
    OWNER_REJECT = "owner_reject"
    AUTO_APPROVE = "auto_approve"
    TTL_EXPIRED = "ttl_expired"
    NOT_ACTIONABLE_GATE_FAIL = "not_actionable:gate_fail"
    NOT_ACTIONABLE_NO_TOKEN = "not_actionable:no_token"
    NOT_ACTIONABLE_NO_GATE = "not_actionable:no_gate_decision"
    NOT_ACTIONABLE_EXPIRED = "not_actionable:expired_before_post"
    # order (E6.2: the D24 price-band ladder)
    ORDER_STEP = "order:step"
    ORDER_FILLED = "order:filled"
    ORDER_PARTIAL = "order:partially_filled"
    ORDER_TIMEOUT = "order:timeout_cancelled"
    ORDER_REFUSED = "order:refused"
    ORDER_REJECTED = "order:broker_rejected"
    ORDER_UNCONFIRMED = "order:cancel_unconfirmed"
    # exit (E6.2: E2.4 policy on open structures)
    EXIT_TAKE_PROFIT = "exit:take_profit"
    EXIT_STOP = "exit:stop"
    EXIT_DTE = "exit:dte"
    EXIT_EXPIRY = "exit:expiry"
    EXIT_NOT_PROPOSED = "exit:not_proposed"
    EXIT_CLOSED = "exit:closed"


def gate_reason(violation: str) -> ReasonCode:
    """``gate:<code>`` for a stored violation string (``"<code>: <detail>"``).

    An unrecognised code maps to ``gate:rule_error`` rather than being dropped.
    """
    code = violation.split(":", 1)[0].strip()
    try:
        return ReasonCode(f"gate:{code}")
    except ValueError:
        return ReasonCode.GATE_RULE_ERROR


class ReviewLabel(StrEnum):
    """Decision quality and outcome, judged separately: a good decision can lose money."""

    GOOD_DECISION_GOOD_OUTCOME = "good_decision_good_outcome"
    GOOD_DECISION_BAD_OUTCOME = "good_decision_bad_outcome"
    BAD_DECISION_GOOD_OUTCOME = "bad_decision_good_outcome"
    BAD_DECISION_BAD_OUTCOME = "bad_decision_bad_outcome"


class RootCause(StrEnum):
    THESIS_WRONG = "thesis_wrong"
    REGIME_MISREAD = "regime_misread"
    STRUCTURE_CHOICE = "structure_choice"
    STRIKE_SELECTION = "strike_selection"
    SIZING = "sizing"
    TIMING = "timing"
    EXECUTION_SLIPPAGE = "execution_slippage"
    DATA_QUALITY = "data_quality"
    EXIT_MANAGEMENT = "exit_management"
    GATE_GAP = "gate_gap"
    UNKNOWN = "unknown"


class Reviewer(StrEnum):
    AUDITOR = "auditor"
    OWNER = "owner"
