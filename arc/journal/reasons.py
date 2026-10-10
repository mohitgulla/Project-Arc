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
    "REASON_LABELS",
    "gate_reason",
    "reason_label",
]


class JournalPersona(StrEnum):
    """Who made the decision (a persona, a deterministic component, or the owner)."""

    SCALP = "scalp"
    SCOUT = "scout"  # D54: the slow-feed Scout (E13.7); pre-D54 "scout" rows read as SCALP
    RESEARCH = "research"
    QUANT = "quant"
    RISK = "risk"
    BROKER = "broker"  # D56 (E13.2): order ladders + reconcile (deterministic)
    OPS = "ops"  # D56 (E13.2): the weekly scorecard
    # Read-only legacy members: pre-E13.2 rows read as BROKER/QUANT/OPS through
    # arc.journal.legacy; nothing writes these any more.
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
    REALLOCATE = "reallocate"
    RECONCILE = "reconcile"
    EXPERIMENT = "experiment"  # E10.1 (D44): forward A/B experiment lifecycle


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
    # candidate (what Research was offered)
    SCALP_CANDIDATE = "scalp_candidate"
    # E13.10 (D56): the options tape's P/C direction matched the candidate's stance
    # (personas.scalp_options_tape on): one more corroborating source, code-counted
    TAPE_CORROBORATED = "tape_corroborated"
    # candidate: the Scalp's universe check (D28; arc.universe.guard)
    UNIVERSE_NOT_IN_UNIVERSE = "universe:not_in_universe"
    UNIVERSE_UNKNOWN_SYMBOL = "universe:unknown_symbol"
    UNIVERSE_ILLIQUID = "universe:illiquid"
    UNIVERSE_NEW_TICKER_CAP = "universe:over_new_ticker_cap"
    # D56 (E13.4): an idea for a name in no tier (mentioned only) / below its tier's floor
    UNIVERSE_NOT_IN_TIER = "universe:not_in_tier"
    UNIVERSE_BELOW_TIER_FLOOR = "universe:below_tier_floor"
    # D51 (E12.1): the tiered universe resolver cut a name past the active-list cap
    UNIVERSE_OVER_ACTIVE_CAP = "universe:over_active_cap"
    # D51 (E12.3): the daily trending tier (universe.trending job)
    UNIVERSE_TRENDING_ADMITTED = "universe:trending_admitted"
    UNIVERSE_TRENDING_SCREEN_FAIL = "universe:trending_screen_fail"
    UNIVERSE_TRENDING_SINGLE_INPUT = "universe:trending_single_input"
    # D58 (E13.19): a leveraged / inverse fund dropped from the trending tier
    UNIVERSE_TRENDING_LEVERAGED = "universe:trending_leveraged"
    # D64 (E14.7): a discovery / trending name kept from the previous run (48 h carry-over)
    UNIVERSE_CARRIED_OVER = "universe:carried_over"
    # E13.7 (D56): the daily Scout (name reserved by arc.journal.legacy's docstring)
    SCOUT_CANDIDATE = "scout_feed_candidate"  # a Scout ticker call written as a candidate
    SCOUT_DISCOVERY = "scout_discovery"  # added to the discovery tier
    SCOUT_DISCOVERY_SCREENED_OUT = "scout_discovery_screened_out"  # failed a discovery rule
    # shortlist (Research)
    SHORTLISTED = "shortlisted"
    NOT_RANKED = "not_ranked"
    NOT_A_CANDIDATE = "not_a_candidate"
    DUPLICATE = "duplicate"
    INVALID_FIELD = "invalid_field"
    OVER_LIMIT = "over_limit"  # pre-E5.7 rows only: Research no longer has a cap
    RESEARCH_EXCLUDED = "research_excluded"  # E5.7: excluded with a stated reason
    MARKET_READ = "market_read"
    NO_CANDIDATES = "no_candidates"
    # E13.8 (D56/D53/D54): the deterministic idea-pool cuts (before the Research call)
    POOL_SCOUT_ONLY_CAP = "over_scout_only_cap"  # past funnel.research.max_scout_only_ideas
    POOL_OVER_PROMPT_BUDGET = "over_prompt_budget"  # cut to fit research_prompt_max_chars
    # structure (Quant + scanner)
    CHOSEN_FROM_MENU = "chosen_from_menu"
    MENU_NOT_CHOSEN = "menu_not_chosen"
    NOT_IN_MENU = "not_in_menu"
    NOT_SHORTLISTED = "not_shortlisted"
    NO_CHAIN = "no_chain"
    QUANT_OMITTED = "quant_omitted"  # pre-E5.7 rows only; now NOT_STRUCTURED
    QUANT_SKIPPED = "quant_skipped"  # E5.7: Quant skipped a budgeted ticker, with a reason
    NOT_STRUCTURED = "not_structured"  # E5.7: no structure and no reason from Quant
    OVER_BUDGET = "over_budget"  # E5.7: ranked beyond pipeline_max_shortlist
    # account profile (D25): the profile maps the stance to no structure
    PROFILE_NO_NEUTRAL = "profile:no_neutral_structure"
    PROFILE_NO_STRUCTURE = "profile:no_structure"
    # risk review
    RISK_ASSESSED = "risk_assessed"
    RISK_DECLINED = "risk_declined"
    UNKNOWN_STRUCTURE = "unknown_structure"
    NOT_ASSESSED = "not_assessed"
    # E13.9 (D56): Quant <-> Risk open path (personas.quant_risk_loop: on)
    RISK_REVISE = "risk_revise"  # Risk asked Quant for one revision
    RISK_REJECT = "risk_reject"  # Risk rejected the structure; never proposed
    QUANT_REVISED = "quant_revised"  # quant.revise re-chose the structure
    QUANT_KEPT = "quant_kept"  # quant.revise kept the structure unchanged
    # propose (deterministic)
    PROPOSED = "proposed"
    ALREADY_PROPOSED = "already_proposed"
    NO_STRUCTURE = "no_structure"
    NO_RISK_REVIEW = "no_risk_review"
    NO_CANDIDATE_ID = "no_candidate_id"
    REPRICE_FAILED = "reprice_failed"
    NET_EV_FLOOR = "net_ev_floor"  # E6.4a: managed Net EV <= ranking.filters floor
    # daily options order budget (E6.5, D32)
    BUDGET_RESTRICTIVE = "budget_restrictive"
    ORDER_BUDGET_EXHAUSTED = "order_budget_exhausted"
    # E5.9 (D33): portfolio-aware Research, idea dedupe, explicit no-trade
    DROP_CONCENTRATION = "drop_concentration"  # adds_concentration over a flagged threshold
    DROP_AT_CAP = "drop_at_cap"  # underlying already at the per-underlying max-loss cap
    # E16.3 (D76/D78): the deterministic anti-chase entry filter (personas.anti_chase)
    STRETCHED_ENTRY = "stretched_entry"  # directional long premium on a stretched move
    TECHNICALS_MISSING = "technicals_missing"  # filter on, no technicals: idea kept (noted)
    VWAP_MISSING = "vwap_missing"  # anti_chase.vwap on, intraday fetch failed: daily rule only
    DEDUPE_EXECUTED = "dedupe_executed"  # same idea is open / closed within the cooldown
    DEDUPE_PROPOSED = "dedupe_proposed"  # same idea proposed within the cooldown
    DEDUPE_REJECTED = "dedupe_rejected"  # same idea rejected by the owner within the cooldown
    DEDUPE_OVERRIDE = "dedupe_override"  # re-admitted: spot moved or the regime changed
    RESEARCH_NO_TRADE = "research_no_trade"  # Research chose an empty shortlist
    MARKET_UNCLEAR = "market_unclear"  # VIX / term structure / regime guard: no new opens
    MARKET_DATA_MISSING = "market_data_missing"  # no VIX reading: fail closed for new opens
    PORTFOLIO_VIEW = "portfolio_view"  # Research's read of the open book (noted)
    THESIS_CHECK = "thesis_check"  # Research's check of an open position's thesis
    # E5.8 (D31): the 5-min loop found the same inputs as the last full run
    LOOP_NO_CHANGE = "loop_no_change"
    # sizing (D18)
    SIZING_OK = "sizing:ok"
    SIZING_CAPPED = "sizing:capped"
    SIZING_CAP_ZERO = "sizing:cap_zero"
    SIZING_BUDGET_EXHAUSTED = "sizing:budget_exhausted"
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
    GATE_BETA_DELTA_CAP = "gate:portfolio_beta_delta_cap"  # D62
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
    GATE_MISSING_GREEKS = "gate:missing_greeks"
    GATE_MISSING_SPOT = "gate:missing_spot"
    GATE_ACCOUNT_KIND = "gate:account_profile_kind"
    GATE_ACCOUNT_NET_DEBIT = "gate:account_profile_net_debit"
    GATE_ACCOUNT_SHORT_LEG = "gate:account_profile_short_leg"
    GATE_ACCOUNT_CASH = "gate:account_profile_settled_cash"
    GATE_ORDER_BUDGET = "gate:order_budget"
    GATE_DAY_TRADES = "gate:account_profile_day_trades"
    GATE_LIVE_SIZE_CAP = "gate:live_size_cap"  # D70 (E11.3)
    # approval (E6.1)
    OWNER_APPROVE = "owner_approve"
    OWNER_REJECT = "owner_reject"
    AUTO_APPROVE = "auto_approve"
    AUTO_APPROVE_GATED = "auto_approve_gated"  # E7.5a: scorecard gate held D34 back
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
    ORDER_BUDGET_STOP = "order:budget_exhausted"
    ORDER_STALE_BAND = "order:stale_band"  # D34: re-priced mid left the gate-approved band
    ORDER_SUBMIT_FAILED = "order:submit_failed"  # D71: submit errored; broker has no such order
    # exit (E6.2: E2.4 policy on open structures)
    EXIT_TAKE_PROFIT = "exit:take_profit"
    EXIT_STOP = "exit:stop"
    EXIT_PROFIT_LOCK = "exit:profit_lock"  # E18.1 (D78): trailing take profit
    EXIT_DTE = "exit:dte"
    EXIT_EXPIRY = "exit:expiry"
    EXIT_NOT_PROPOSED = "exit:not_proposed"
    EXIT_QUOTE_UNUSABLE = "exit:quote_unusable"  # E6.2a: close legs' quotes failed the check
    EXIT_DNE = "exit:dne"  # E11.4 (D73): do-not-exercise instruction (sent or refused)
    EXIT_EXPIRY_GUARD = "exit:expiry_guard"  # E11.4: not flat by DTE 1 / expiry-day cutoff
    EXIT_CLOSED = "exit:closed"
    # E6.4 position manager (D19): review signals beyond E6.2's fired rules
    EXIT_TIME_ADJUSTED = "exit:time_adjusted_target"
    EXIT_EV_FLOOR = "exit:remaining_ev_floor"
    EXIT_REALLOCATE = "exit:reallocate"
    # E13.17 (D56): Research-managed exits (personas.exit_path shadow | research)
    EXIT_WATCH_HOLD = "exit:watch_hold"  # Research: keep holding (noted)
    EXIT_WATCH_REVIEW = "exit:watch_review"  # Research: ask Quant for an exit case
    EXIT_WATCH_MISSING = "exit:watch_missing"  # no watch item for an open position: hold
    EXIT_CASE_BUILT = "exit:case_built"  # Quant judged an exit case (hold | close)
    EXIT_CASE_SKIPPED = "exit:case_skipped"  # mandatory_pending | exit_pending | no_trigger
    EXIT_FILL_DAY_HOLD = "exit:fill_day_hold"  # E18.2 (D78): opened today, thesis not broken
    # E13.18 (D56): Risk exit review + the close path (personas.exit_path research)
    EXIT_RESEARCH_REVIEW = "exit:research_review"  # close on a Research review (Risk close)
    EXIT_HOLD_REVIEWED = "exit:hold_reviewed"  # Risk said hold: journaled no-action
    EXIT_HOLD_LIMIT = "exit:hold_limit_reached"  # too many consecutive holds: close
    EXIT_REVIEW_UNAVAILABLE = "exit:review_unavailable"  # Risk unavailable: D23 fallback
    # reallocate (E6.4: close-to-reallocate swaps; one row per scored pair / swap step)
    REALLOC_SUGGESTED = "realloc:suggested"
    REALLOC_EDGE_BELOW_MIN = "realloc:edge_below_min"
    REALLOC_POP_BELOW_OPEN = "realloc:pop_below_open"
    REALLOC_FREES_NOTHING = "realloc:frees_nothing"
    REALLOC_NO_OPEN_NUMBERS = "realloc:no_open_numbers"
    REALLOC_CHURN_TICKER = "realloc:churn_ticker"
    REALLOC_CHURN_DAY = "realloc:churn_day"
    REALLOC_ALREADY_PAIRED = "realloc:already_paired"
    REALLOC_APPROVED = "realloc:risk_approved"
    REALLOC_VETOED = "realloc:risk_vetoed"
    REALLOC_OPEN_PROPOSED = "realloc:open_proposed"
    REALLOC_CANCELLED = "realloc:cancelled"
    # reconcile (E6.3: post-market broker vs local)
    RECONCILE_CLEAN = "reconcile:clean"
    RECONCILE_MISMATCH = "reconcile:mismatch"
    RECONCILE_RESOLVED = "reconcile:resolved"
    RECONCILE_EXPIRED = "reconcile:expired"
    RECONCILE_WASH_SALE = "reconcile:wash_sale"
    RECONCILE_LOT_PRICE = "reconcile:lot_price"
    RECONCILE_TEST_FILL = "reconcile:test_fill"
    # E6.2g: a stored fill whose sign disagreed with its signed band, repaired by
    # `arc journal repair-fill-signs`; the payload carries the realised P&L delta
    RECONCILE_FILL_SIGN = "reconcile:fill_sign_corrected"
    # E11.4 (D73): a leg the broker assigned / exercised, classified and booked
    RECONCILE_ASSIGNMENT = "reconcile:assignment"
    RECONCILE_EXERCISE = "reconcile:exercise"
    # experiments (E10.1, D44): pre-registration and lifecycle of a forward A/B test
    EXPERIMENT_DRAFTED = "experiment:drafted"
    EXPERIMENT_REGISTERED = "experiment:registered"
    EXPERIMENT_QUEUED = "experiment:queued"  # its area already has a registered/running one
    EXPERIMENT_STARTED = "experiment:started"
    EXPERIMENT_AA_OVERRIDE = "experiment:aa_override"  # owner started an ab with no A/A sigma
    EXPERIMENT_STOPPED = "experiment:stopped"
    EXPERIMENT_PROMOTED = "experiment:promoted"
    EXPERIMENT_REJECTED = "experiment:rejected"
    EXPERIMENT_EVALUATED = "experiment:evaluated"  # E10.3 daily evaluation (report stored)
    EXPERIMENT_INVALID = "experiment:invalid"  # E10.3 an A/A "won": the harness is broken


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
    # Stored value kept (decision_reviews CHECK); D56 removed the Auditor persona, so
    # this marks an automated (LLM) review.
    AUDITOR = "auditor"
    OWNER = "owner"


# Plain-language label per reason code, for owner-facing views (the tower's decision
# trail, E8.7b). One entry per ReasonCode; a test pins full coverage.
REASON_LABELS: dict[ReasonCode, str] = {
    ReasonCode.SCALP_CANDIDATE: "Scalp raised this idea",
    ReasonCode.TAPE_CORROBORATED: "Options tape agreed with the stance",
    ReasonCode.UNIVERSE_NOT_IN_UNIVERSE: "Not in the tradable universe",
    ReasonCode.UNIVERSE_UNKNOWN_SYMBOL: "Unknown symbol",
    ReasonCode.UNIVERSE_ILLIQUID: "Options too illiquid",
    ReasonCode.UNIVERSE_NEW_TICKER_CAP: "Over the new-ticker cap",
    ReasonCode.UNIVERSE_NOT_IN_TIER: "Outside the universe tiers (mentioned only)",
    ReasonCode.UNIVERSE_BELOW_TIER_FLOOR: "Below its tier's confidence floor",
    ReasonCode.UNIVERSE_OVER_ACTIVE_CAP: "Over the active-list cap",
    ReasonCode.UNIVERSE_TRENDING_ADMITTED: "Added to the trending tier",
    ReasonCode.UNIVERSE_TRENDING_SCREEN_FAIL: "Trending, but options too illiquid",
    ReasonCode.UNIVERSE_TRENDING_SINGLE_INPUT: "Trending on one input only",
    ReasonCode.UNIVERSE_TRENDING_LEVERAGED: "Trending, but a leveraged fund",
    ReasonCode.UNIVERSE_CARRIED_OVER: "Kept from the previous run (48 h carry-over)",
    ReasonCode.SCOUT_CANDIDATE: "Scout raised this idea",
    ReasonCode.SCOUT_DISCOVERY: "Added to the discovery tier by the Scout",
    ReasonCode.SCOUT_DISCOVERY_SCREENED_OUT: "Scout pick kept out of the discovery tier",
    ReasonCode.SHORTLISTED: "Shortlisted by Research",
    ReasonCode.NOT_RANKED: "Not ranked by Research",
    ReasonCode.NOT_A_CANDIDATE: "Not one of the Scalp's candidates",
    ReasonCode.DUPLICATE: "Duplicate entry",
    ReasonCode.INVALID_FIELD: "Invalid field in the persona output",
    ReasonCode.OVER_LIMIT: "Over the shortlist limit",
    ReasonCode.RESEARCH_EXCLUDED: "Excluded by Research",
    ReasonCode.MARKET_READ: "Research's market read",
    ReasonCode.NO_CANDIDATES: "No candidates to review",
    ReasonCode.POOL_SCOUT_ONLY_CAP: "Over the Scout-only idea cap",
    ReasonCode.POOL_OVER_PROMPT_BUDGET: "Cut to fit the Research prompt budget",
    ReasonCode.CHOSEN_FROM_MENU: "Quant picked this structure from the menu",
    ReasonCode.MENU_NOT_CHOSEN: "Menu option not chosen",
    ReasonCode.NOT_IN_MENU: "Structure not in the scanner's menu",
    ReasonCode.NOT_SHORTLISTED: "Not on the shortlist",
    ReasonCode.NO_CHAIN: "No option chain available",
    ReasonCode.QUANT_OMITTED: "Quant left it out",
    ReasonCode.QUANT_SKIPPED: "Quant skipped it, with a reason",
    ReasonCode.NOT_STRUCTURED: "No structure from Quant",
    ReasonCode.OVER_BUDGET: "Ranked beyond the structuring budget",
    ReasonCode.PROFILE_NO_NEUTRAL: "Account profile has no neutral structure",
    ReasonCode.PROFILE_NO_STRUCTURE: "Account profile allows no structure for this view",
    ReasonCode.RISK_ASSESSED: "Risk reviewed it",
    ReasonCode.RISK_DECLINED: "Risk declined it",
    ReasonCode.UNKNOWN_STRUCTURE: "Risk reviewed an unknown structure",
    ReasonCode.NOT_ASSESSED: "Risk did not review it",
    ReasonCode.RISK_REVISE: "Risk asked Quant to revise it",
    ReasonCode.RISK_REJECT: "Risk rejected it",
    ReasonCode.QUANT_REVISED: "Quant revised it for Risk",
    ReasonCode.QUANT_KEPT: "Quant kept it unchanged after Risk's ask",
    ReasonCode.PROPOSED: "Proposed",
    ReasonCode.ALREADY_PROPOSED: "Already proposed this run",
    ReasonCode.NO_STRUCTURE: "No structure to propose",
    ReasonCode.NO_RISK_REVIEW: "No risk review to propose on",
    ReasonCode.NO_CANDIDATE_ID: "No candidate record",
    ReasonCode.REPRICE_FAILED: "Could not re-price at fresh quotes",
    ReasonCode.NET_EV_FLOOR: "Managed Net EV after costs at or below the floor",
    ReasonCode.BUDGET_RESTRICTIVE: "Daily order budget is in its restrictive tier",
    ReasonCode.ORDER_BUDGET_EXHAUSTED: "Daily order budget used up",
    ReasonCode.DROP_CONCENTRATION: "Dropped: adds concentration",
    ReasonCode.DROP_AT_CAP: "Dropped: underlying already at its max-loss cap",
    ReasonCode.STRETCHED_ENTRY: "Skipped: move already stretched",
    ReasonCode.TECHNICALS_MISSING: "Anti-chase check skipped: no technicals",
    ReasonCode.VWAP_MISSING: "VWAP check skipped: no intraday bars",
    ReasonCode.DEDUPE_EXECUTED: "Same idea already traded recently",
    ReasonCode.DEDUPE_PROPOSED: "Same idea already proposed recently",
    ReasonCode.DEDUPE_REJECTED: "Same idea rejected by the owner recently",
    ReasonCode.DEDUPE_OVERRIDE: "Re-admitted: spot or regime moved",
    ReasonCode.RESEARCH_NO_TRADE: "Research chose no trade",
    ReasonCode.MARKET_UNCLEAR: "Market unclear: no new opens",
    ReasonCode.MARKET_DATA_MISSING: "Market data missing: no new opens",
    ReasonCode.PORTFOLIO_VIEW: "Research's read of the open book",
    ReasonCode.THESIS_CHECK: "Research checked an open position's thesis",
    ReasonCode.LOOP_NO_CHANGE: "Loop skipped: inputs unchanged",
    ReasonCode.SIZING_OK: "Sized at Risk's suggestion",
    ReasonCode.SIZING_CAPPED: "Size capped at 5% of equity",
    ReasonCode.SIZING_CAP_ZERO: "Cap allows zero contracts",
    ReasonCode.SIZING_BUDGET_EXHAUSTED: "No risk budget left",
    ReasonCode.SIZING_RISK_ZERO: "Risk suggested zero contracts",
    ReasonCode.SIZING_UNBOUNDED: "Unbounded loss: cannot size",
    ReasonCode.SIZING_INVALID_INPUT: "Invalid sizing input",
    ReasonCode.GATE_PASS: "Gate passed",
    ReasonCode.GATE_STRUCTURE_INVALID: "Gate: invalid structure",
    ReasonCode.GATE_PER_UNDERLYING: "Gate: over the per-underlying limit",
    ReasonCode.GATE_DAILY_LOSS: "Gate: daily loss halt",
    ReasonCode.GATE_HALTED: "Gate: trading halted",
    ReasonCode.GATE_SPREAD: "Gate: bid-ask spread too wide",
    ReasonCode.GATE_LIMIT_OUTSIDE_NBBO: "Gate: limit outside the market",
    ReasonCode.GATE_TICK: "Gate: limit not on a valid tick",
    ReasonCode.GATE_WASH_SALE: "Gate: wash-sale risk",
    ReasonCode.GATE_DELTA_CAP: "Gate: over the portfolio dollar-delta cap",
    ReasonCode.GATE_BETA_DELTA_CAP: "Gate: over the beta-weighted dollar-delta cap",
    ReasonCode.GATE_VEGA_CAP: "Gate: over the portfolio vega cap",
    ReasonCode.GATE_STRUCTURE_NOT_ALLOWED: "Gate: structure not allowed",
    ReasonCode.GATE_DTE_WINDOW: "Gate: expiry outside the DTE window",
    ReasonCode.GATE_EARNINGS_BLACKOUT: "Gate: earnings blackout",
    ReasonCode.GATE_MAX_POSITIONS: "Gate: too many open positions",
    ReasonCode.GATE_APPROVAL_TTL: "Gate: approval window expired",
    ReasonCode.GATE_STALE_DATA: "Gate: stale market data",
    ReasonCode.GATE_NO_MAX_GAIN: "Gate: limit leaves no max gain",
    ReasonCode.GATE_RULE_ERROR: "Gate: rule error",
    ReasonCode.GATE_PRICE_BAND: "Gate: price band invalid",
    ReasonCode.GATE_CLOSE_MISMATCH: "Gate: close does not match the open position",
    ReasonCode.GATE_MISSING_GREEKS: "Gate: Greeks missing",
    ReasonCode.GATE_MISSING_SPOT: "Gate: underlying spot missing",
    ReasonCode.GATE_ACCOUNT_KIND: "Gate: structure not allowed by the account profile",
    ReasonCode.GATE_ACCOUNT_NET_DEBIT: "Gate: account profile needs a net debit",
    ReasonCode.GATE_ACCOUNT_SHORT_LEG: "Gate: account profile forbids this short leg",
    ReasonCode.GATE_ACCOUNT_CASH: "Gate: not enough settled cash",
    ReasonCode.GATE_ORDER_BUDGET: "Gate: daily order budget",
    ReasonCode.GATE_DAY_TRADES: "Gate: day-trade limit of the account profile",
    ReasonCode.GATE_LIVE_SIZE_CAP: "Gate: live size cap (live gate not met)",
    ReasonCode.OWNER_APPROVE: "Owner approved",
    ReasonCode.OWNER_REJECT: "Owner rejected",
    ReasonCode.AUTO_APPROVE: "Auto-approved",
    ReasonCode.AUTO_APPROVE_GATED: "Auto-approve held back by the scorecard gate",
    ReasonCode.TTL_EXPIRED: "Approval window expired",
    ReasonCode.NOT_ACTIONABLE_GATE_FAIL: "Not actionable: gate failed",
    ReasonCode.NOT_ACTIONABLE_NO_TOKEN: "Not actionable: no gate token",
    ReasonCode.NOT_ACTIONABLE_NO_GATE: "Not actionable: no gate decision",
    ReasonCode.NOT_ACTIONABLE_EXPIRED: "Not actionable: expired before posting",
    ReasonCode.ORDER_STEP: "Order placed at the next price step",
    ReasonCode.ORDER_FILLED: "Order filled",
    ReasonCode.ORDER_PARTIAL: "Order partially filled",
    ReasonCode.ORDER_TIMEOUT: "Order cancelled after the ladder timed out",
    ReasonCode.ORDER_REFUSED: "Order refused before sending",
    ReasonCode.ORDER_REJECTED: "Broker rejected the order",
    ReasonCode.ORDER_UNCONFIRMED: "Cancel not confirmed by the broker",
    ReasonCode.ORDER_BUDGET_STOP: "Order stopped: daily budget used up",
    ReasonCode.ORDER_STALE_BAND: "Order stopped: price left the approved band",
    ReasonCode.ORDER_SUBMIT_FAILED: "Order not taken by the broker (submit failed)",
    ReasonCode.EXIT_TAKE_PROFIT: "Exit: profit target hit",
    ReasonCode.EXIT_STOP: "Exit: stop hit",
    ReasonCode.EXIT_PROFIT_LOCK: "Closed: profit lock",
    ReasonCode.EXIT_DTE: "Exit: days-to-expiry limit",
    ReasonCode.EXIT_EXPIRY: "Exit: expiry",
    ReasonCode.EXIT_NOT_PROPOSED: "Exit not proposed",
    ReasonCode.EXIT_QUOTE_UNUSABLE: "Exit held: close quotes unusable",
    ReasonCode.EXIT_DNE: "Do-not-exercise instruction at expiry",
    ReasonCode.EXIT_EXPIRY_GUARD: "Expiry guard: not flat before expiry",
    ReasonCode.EXIT_CLOSED: "Position closed",
    ReasonCode.EXIT_TIME_ADJUSTED: "Exit: time-adjusted profit target",
    ReasonCode.EXIT_EV_FLOOR: "Exit: remaining EV below the floor",
    ReasonCode.EXIT_REALLOCATE: "Exit: close to reallocate",
    ReasonCode.EXIT_WATCH_HOLD: "Exit watch: Research holds",
    ReasonCode.EXIT_WATCH_REVIEW: "Exit watch: Research asks for a review",
    ReasonCode.EXIT_WATCH_MISSING: "Exit watch: no Research item (hold)",
    ReasonCode.EXIT_CASE_BUILT: "Exit case judged by Quant",
    ReasonCode.EXIT_CASE_SKIPPED: "Exit case skipped",
    ReasonCode.EXIT_FILL_DAY_HOLD: "Held: opened today, thesis not broken",
    ReasonCode.EXIT_RESEARCH_REVIEW: "Exit: Research review (Risk close)",
    ReasonCode.EXIT_HOLD_REVIEWED: "Exit: Risk holds (reviewed)",
    ReasonCode.EXIT_HOLD_LIMIT: "Exit: hold limit reached (close)",
    ReasonCode.EXIT_REVIEW_UNAVAILABLE: "Exit: Risk review unavailable (policy fallback)",
    ReasonCode.REALLOC_SUGGESTED: "Swap suggested",
    ReasonCode.REALLOC_EDGE_BELOW_MIN: "Swap edge below the minimum",
    ReasonCode.REALLOC_POP_BELOW_OPEN: "Swap PoP below the open position's",
    ReasonCode.REALLOC_FREES_NOTHING: "Swap frees no buying power",
    ReasonCode.REALLOC_NO_OPEN_NUMBERS: "Swap: no numbers for the new trade",
    ReasonCode.REALLOC_CHURN_TICKER: "Swap blocked: ticker churn",
    ReasonCode.REALLOC_CHURN_DAY: "Swap blocked: daily churn limit",
    ReasonCode.REALLOC_ALREADY_PAIRED: "Swap: already paired",
    ReasonCode.REALLOC_APPROVED: "Risk approved the swap",
    ReasonCode.REALLOC_VETOED: "Risk vetoed the swap",
    ReasonCode.REALLOC_OPEN_PROPOSED: "Swap's new trade proposed",
    ReasonCode.REALLOC_CANCELLED: "Swap cancelled",
    ReasonCode.RECONCILE_CLEAN: "Reconcile clean",
    ReasonCode.RECONCILE_MISMATCH: "Reconcile mismatch",
    ReasonCode.RECONCILE_RESOLVED: "Reconcile mismatch resolved",
    ReasonCode.RECONCILE_EXPIRED: "Expired at reconcile",
    ReasonCode.RECONCILE_WASH_SALE: "Wash sale flagged at reconcile",
    ReasonCode.RECONCILE_LOT_PRICE: "Tax-lot price corrected at reconcile",
    ReasonCode.RECONCILE_FILL_SIGN: "Fill sign corrected (realised P&L restated)",
    ReasonCode.RECONCILE_ASSIGNMENT: "Short leg assigned at the broker (booked)",
    ReasonCode.RECONCILE_EXERCISE: "Long leg exercised at the broker (booked)",
    ReasonCode.RECONCILE_TEST_FILL: "Integration-test fill seen at reconcile",
    ReasonCode.EXPERIMENT_DRAFTED: "Experiment drafted",
    ReasonCode.EXPERIMENT_REGISTERED: "Experiment pre-registered (spec locked)",
    ReasonCode.EXPERIMENT_QUEUED: "Experiment queued behind another in its area",
    ReasonCode.EXPERIMENT_STARTED: "Experiment started",
    ReasonCode.EXPERIMENT_AA_OVERRIDE: "A/B started without an A/A (owner override)",
    ReasonCode.EXPERIMENT_STOPPED: "Experiment stopped",
    ReasonCode.EXPERIMENT_PROMOTED: "Experiment's treatment promoted",
    ReasonCode.EXPERIMENT_REJECTED: "Experiment's treatment rejected",
    ReasonCode.EXPERIMENT_EVALUATED: "Experiment evaluated",
    ReasonCode.EXPERIMENT_INVALID: "A/A experiment invalid (arms differ)",
}


def reason_label(code: str) -> str:
    """Plain-language label for a stored ``reason_code`` (unknown codes: humanised).

    D54: a pre-rename stored code (``scout_candidate``) reads as its Scalp label.
    """
    from arc.journal.legacy import reason_code as _current  # noqa: PLC0415 - leaf helper

    try:
        return REASON_LABELS[ReasonCode(_current(code))]
    except (ValueError, KeyError):
        text = code.replace("_", " ").replace(":", ": ")
        return text[:1].upper() + text[1:]
