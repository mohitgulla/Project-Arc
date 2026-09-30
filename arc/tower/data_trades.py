"""Trades list + per-trade drill-down for control tower v2 (E8.7b, D35).

A *trade* is one ``proposals`` row (an open or a close) joined to everything keyed by
its ``proposal_hash``. Pure reads over the audit store (``mode=ro`` connection from
:func:`arc.tower.data.connect_ro`); nothing here imports the broker, market data, an
LLM or Slack (import-linter contract in ``pyproject.toml``).

List (``GET /api/trades``)
    One SQL pass (:data:`_BASE`) computes every column, the stage reached and the
    filter-wide summary (window aggregates), then pages. Filters, sort and pagination
    are server-side. Migration 017 adds the indexes the joins use.

Detail (``GET /api/trades/{hash}``)
    Every section of the card, each from its own table:

    ======================  ===============================================================
    Section                 Source
    ======================  ===============================================================
    header                  ``proposals`` + the list row (stage, realized P&L, net EV)
    payoff                  ``proposals.structure_json`` via :mod:`arc.structures`
    quant                   ``proposals.quant_json`` / ``sizing_json`` + the latest
                            ``market_contexts`` analytics (:class:`ProposalAnalytics`)
    decision trail          ``decisions`` for the hash and its chain (this ticker /
                            session rows) + ``persona_calls``
    gate                    ``gate_decisions`` (the token itself is never returned)
    approval                ``approval_requests`` + ``approvals``
    execution               ``executions``, ``orders`` + ``order_events``, ``fills``
    position & exits        ``open_structures``, close proposals for it, ``swaps``
    outcome & review        ``outcomes`` (latest), ``decision_reviews`` + citations
    market context          ``market_contexts``, the ``regime`` context entry of the
                            decisions' input snapshot, ``candidates``
    run manifest            ``run_manifests`` of the proposing run (D27)
    ======================  ===============================================================

Optional tables or rows that are missing render as empty sections, never errors.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arc.journal.analytics import ProposalAnalytics  # noqa: TC001 - pydantic field
from arc.journal.reasons import gate_reason, reason_label
from arc.tower.data import _dec, _has_table, _json, parse_ts
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "DATE_PRESETS",
    "MAX_PAGE_SIZE",
    "DatePreset",
    "SortKey",
    "Stage",
    "SORT_KEYS",
    "STAGES",
    "SearchMatch",
    "SearchResponse",
    "TradeDetail",
    "TradeFilterOptions",
    "TradeFilters",
    "TradeListResponse",
    "TradeRow",
    "TradeSummary",
    "date_range",
    "load_filter_options",
    "load_trade",
    "load_trades",
    "search",
]

_STRICT = ConfigDict(extra="forbid", frozen=True)

Stage = Literal[
    "proposed",
    "gate_pass",
    "gate_fail",
    "approved",
    "rejected",
    "expired",
    "filled",
    "cancelled",
    "open",
    "closed",
]
STAGES: tuple[str, ...] = (
    "proposed",
    "gate_pass",
    "gate_fail",
    "approved",
    "rejected",
    "expired",
    "filled",
    "cancelled",
    "open",
    "closed",
)
DatePreset = Literal["today", "7d", "30d", "mtd", "ytd", "all", "custom"]
DATE_PRESETS: tuple[str, ...] = ("today", "7d", "30d", "mtd", "ytd", "all", "custom")
SortKey = Literal[
    "time", "ticker", "contracts", "limit", "net_ev", "pop", "slippage_bps", "realized_pnl"
]
SORT_KEYS: tuple[str, ...] = (
    "time",
    "ticker",
    "contracts",
    "limit",
    "net_ev",
    "pop",
    "slippage_bps",
    "realized_pnl",
)
MAX_PAGE_SIZE = 200
SEARCH_LIMIT = 8

# ---------------------------------------------------------------------------
# The list query
# ---------------------------------------------------------------------------

# The market_contexts expressions must match migration 017's covering index exactly.
_MC = {
    "net_ev": "json_extract(payload, '$.analytics.exit_model.managed.net_ev')",
    "pop_managed": "json_extract(payload, '$.analytics.exit_model.managed.pop')",
    "pop_static": "json_extract(payload, '$.analytics.exit_model.static.pop')",
    "profile": "json_extract(payload, '$.analytics.account_profile')",
}

# Everything a list row shows, per proposal. Exactly one row per proposal: every join
# is to a unique key or to the latest row by a correlated subquery.
#
# Realized P&L of an open trade (no ``outcomes`` row) is booked per close tranche, the
# way the ladder books it (``arc/execution/ladder.py``): Σ -(entry + fill) x 100 x qty
# over the structure's filled close executions. ``OpenStructureRepo.reduce`` lowers
# ``contracts`` on a partial close and keeps only the final ``close_net``, so
# ``contracts x close_net`` is wrong after more than one tranche. Contracts closed
# outside an execution (reconcile expiry settlement) are the opened quantity minus the
# executed closes, at the structure's ``close_net``. See :func:`realized_from_tranches`.
_BASE = f"""
WITH {{stubs}}base AS (
  SELECT
    p.rowid AS rid,
    p.proposal_hash, p.ticker, p.kind, p.day, p.created_at, p.run_id, p.chain_run_id,
    p.swap_id, p.candidate_id,
    julianday(p.created_at) AS jd,
    json_extract(p.structure_json, '$.kind') AS structure_kind,
    json_extract(p.structure_json, '$.legs') AS legs_json,
    CAST(json_extract(p.structure_json, '$.net_debit_credit') AS REAL) AS limit_px,
    json_extract(p.sizing_json, '$.contracts') AS contracts,
    json_extract(p.quant_json, '$.pop') AS pop,
    g.passed AS gate_passed,
    json_array_length(g.violations_json) AS n_violations,
    json_extract(g.violations_json, '$[0]') AS first_violation,
    a.status AS approval,
    x.status AS execution,
    x.fill_price AS fill_price,
    x.filled_qty AS filled_qty,
    (SELECT {_MC["net_ev"]} FROM market_contexts
       WHERE proposal_hash = p.proposal_hash ORDER BY created_at DESC LIMIT 1) AS net_ev_unit,
    (SELECT {_MC["pop_managed"]} FROM market_contexts
       WHERE proposal_hash = p.proposal_hash ORDER BY created_at DESC LIMIT 1) AS pop_managed,
    (SELECT {_MC["pop_static"]} FROM market_contexts
       WHERE proposal_hash = p.proposal_hash ORDER BY created_at DESC LIMIT 1) AS pop_static,
    (SELECT {_MC["profile"]} FROM market_contexts
       WHERE proposal_hash = p.proposal_hash ORDER BY created_at DESC LIMIT 1) AS profile,
    s.id AS structure_id, s.status AS structure_status,
    s.entry_net AS s_entry, s.close_net AS s_close, s.contracts AS s_contracts,
    s2.id AS closes_structure_id,
    COALESCE(o.exit_reason, s.exit_reason, s2.exit_reason) AS exit_reason,
    o.realised_pnl AS outcome_pnl,
    -- realized P&L per close tranche (the rule above), one indexed lookup per structure
    CASE WHEN p.kind = 'open' AND s.id IS NOT NULL THEN (
      SELECT CASE WHEN COUNT(*) > 0 OR (s.status = 'closed' AND s.close_net IS NOT NULL) THEN
        COALESCE(-(CAST(s.entry_net AS REAL) * SUM(e.filled_qty)
                   + SUM(CAST(e.fill_price AS REAL) * e.filled_qty)) * 100.0, 0.0)
        + CASE WHEN s.status = 'closed' AND s.close_net IS NOT NULL THEN
            -(CAST(s.entry_net AS REAL) + CAST(s.close_net AS REAL)) * 100.0 * (
              CASE WHEN COALESCE(x.filled_qty, 0) > 0
                     THEN MAX(x.filled_qty - COALESCE(SUM(e.filled_qty), 0), 0)
                   WHEN COUNT(*) = 0 THEN s.contracts
                   ELSE 0 END)
          ELSE 0.0 END
      END
      FROM executions e
      WHERE e.structure_id = s.id AND e.kind = 'close' AND e.filled_qty > 0
        AND e.fill_price IS NOT NULL) END AS structure_pnl
  FROM proposals p
  LEFT JOIN gate_decisions g ON g.rowid = (
    SELECT rowid FROM gate_decisions WHERE proposal_hash = p.proposal_hash
    ORDER BY decided_at DESC, rowid DESC LIMIT 1)
  LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash
  LEFT JOIN executions x ON x.proposal_hash = p.proposal_hash
  LEFT JOIN open_structures s ON s.open_proposal_hash = p.proposal_hash
  LEFT JOIN open_structures s2 ON p.kind = 'close' AND s2.rowid = (
    SELECT rowid FROM open_structures WHERE exit_proposal_hash = p.proposal_hash LIMIT 1)
  LEFT JOIN outcomes o ON o.rowid = (
    SELECT rowid FROM outcomes WHERE proposal_hash = p.proposal_hash
    ORDER BY at DESC, rowid DESC LIMIT 1)
), trades AS (
  SELECT base.*,
    CASE
      WHEN kind = 'open' AND structure_status = 'closed' THEN 'closed'
      WHEN kind = 'open' AND structure_status = 'open' THEN 'open'
      WHEN execution IN ('filled', 'partially_filled') THEN 'filled'
      WHEN execution IN ('cancelled', 'rejected', 'unconfirmed') THEN 'cancelled'
      WHEN execution = 'working' THEN 'approved'
      WHEN approval = 'approved' THEN 'approved'
      WHEN approval = 'rejected' THEN 'rejected'
      WHEN approval = 'expired' THEN 'expired'
      WHEN gate_passed = 0 THEN 'gate_fail'
      WHEN gate_passed = 1 THEN 'gate_pass'
      ELSE 'proposed'
    END AS stage,
    net_ev_unit * COALESCE(contracts, 1) AS net_ev,
    CASE WHEN fill_price IS NOT NULL AND limit_px IS NOT NULL AND limit_px != 0
      THEN (CAST(fill_price AS REAL) - limit_px) / ABS(limit_px) * 10000.0 END AS slippage_bps,
    COALESCE(CAST(outcome_pnl AS REAL), structure_pnl)
      AS realized_pnl
  FROM base
)
"""

# Optional tables the list query joins: when one is missing (an older store), an empty
# CTE of the same name shadows it, so the page renders with empty columns, never a 500.
_STUBS: dict[str, str] = {
    "market_contexts": "SELECT NULL AS rowid, NULL AS proposal_hash, NULL AS created_at, "
    "NULL AS payload WHERE 0",
    "outcomes": "SELECT NULL AS rowid, NULL AS proposal_hash, NULL AS at, NULL AS exit_reason, "
    "NULL AS realised_pnl WHERE 0",
    "decisions": "SELECT NULL AS proposal_hash, NULL AS reason_code WHERE 0",
    "routine_runs": "SELECT NULL AS run_id, NULL AS chain_run_id WHERE 0",
}
# The list needs these (migrations 001-011); without them it is empty.
_REQUIRED = ("proposals", "gate_decisions", "approval_requests", "executions", "open_structures")


def _base(conn: sqlite3.Connection) -> str:
    stubs = "".join(
        f"{name} AS ({sql}), " for name, sql in _STUBS.items() if not _has_table(conn, name)
    )
    return _BASE.replace("{stubs}", stubs)


_SORT_SQL: dict[str, str] = {
    "time": "jd",
    "ticker": "ticker",
    "contracts": "contracts",
    "limit": "limit_px",
    "net_ev": "net_ev",
    "pop": "COALESCE(pop_managed, pop)",
    "slippage_bps": "slippage_bps",
    "realized_pnl": "realized_pnl",
}


# ---------------------------------------------------------------------------
# Models: filters, list
# ---------------------------------------------------------------------------


class TradeFilters(BaseModel):
    """Every list filter; all optional. Multi-value filters match any of their values."""

    model_config = _STRICT

    date: DatePreset = "all"
    date_from: _dt.date | None = Field(default=None, description="custom range start (ET day)")
    date_to: _dt.date | None = Field(default=None, description="custom range end, inclusive")
    ticker: list[str] = Field(default_factory=list)
    kind: list[Literal["open", "close"]] = Field(default_factory=list)
    structure: list[str] = Field(default_factory=list, description="structure kinds")
    stage: list[Stage] = Field(default_factory=list)
    exit_reason: list[str] = Field(default_factory=list)
    reason_code: list[str] = Field(default_factory=list, description="any decision's code")
    min_net_ev: float | None = Field(default=None, description="$ after costs, × contracts")
    min_pop: float | None = Field(
        default=None, ge=0.0, le=1.0, description="managed PoP, else Quant's"
    )
    account_profile: list[str] = Field(default_factory=list)
    q: str | None = Field(
        default=None, description="hash prefix, ticker, run id, chain id, structure id"
    )


class TradeRow(BaseModel):
    model_config = _STRICT

    proposal_hash: str
    created_at: _dt.datetime | None
    day: str | None
    ticker: str | None
    kind: Literal["open", "close"]
    structure_kind: str | None
    legs: list[str] = Field(description="OCC symbols")
    contracts: int | None
    limit: Decimal | None = Field(description="Per-share net at proposal (+ debit / − credit)")
    net_ev: float | None = Field(description="Managed net EV after costs × contracts, $")
    pop: float | None = Field(description="Quant's PoP")
    pop_managed: float | None = None
    pop_hold: float | None = Field(default=None, description="Hold-to-expiry PoP after costs")
    gate_passed: bool | None
    violations: int = 0
    first_violation: str | None = None
    approval: str | None
    execution: str | None
    fill_price: Decimal | None
    slippage_bps: float | None = Field(description="Fill vs the proposal net; + = worse")
    realized_pnl: float | None
    exit_reason: str | None
    stage: Stage
    account_profile: str | None = None
    run_id: str | None = None
    chain_run_id: str | None = None
    structure_id: str | None = Field(default=None, description="The structure this open created")
    closes_structure_id: str | None = Field(
        default=None, description="The structure this close exits"
    )
    swap_id: str | None = None


class TradeSummary(BaseModel):
    """Aggregates over every row matching the filter (not just the page)."""

    model_config = _STRICT

    count: int = 0
    filled: int = 0
    filled_pct: float | None = None
    realized_pnl: float | None = Field(default=None, description="Σ realized P&L")
    realized_count: int = 0
    avg_slippage_bps: float | None = None
    avg_net_ev: float | None = None
    net_ev_realized: float | None = Field(
        None, description="Σ modelled net EV over the trades that have a realized P&L"
    )


class TradeListResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    filters: TradeFilters
    date_from: _dt.date | None = Field(description="Resolved range start (ET)")
    date_to: _dt.date | None
    sort: SortKey
    dir: Literal["asc", "desc"]
    page: int
    size: int
    total: int
    items: list[TradeRow]
    summary: TradeSummary


class TradeFilterOptions(BaseModel):
    """Distinct values for the filter dropdowns."""

    model_config = _STRICT

    as_of: _dt.datetime
    tickers: list[str]
    kinds: list[str]
    structures: list[str]
    stages: list[str] = Field(default_factory=lambda: list(STAGES))
    exit_reasons: list[str]
    reason_codes: list[dict[str, str]] = Field(description="[{code, label}]")
    account_profiles: list[str]
    date_presets: list[str] = Field(default_factory=lambda: list(DATE_PRESETS))
    sort_keys: list[str] = Field(default_factory=lambda: list(SORT_KEYS))


class SearchMatch(BaseModel):
    model_config = _STRICT

    kind: Literal["ticker", "trade", "run", "chain", "structure"]
    id: str
    label: str
    route: str


class SearchResponse(BaseModel):
    model_config = _STRICT

    as_of: _dt.datetime
    q: str
    matches: list[SearchMatch]


# ---------------------------------------------------------------------------
# Models: detail
# ---------------------------------------------------------------------------


class LegInfo(BaseModel):
    model_config = _STRICT

    occ_symbol: str
    side: str
    ratio: int = 1
    premium: Decimal | None = None
    intent: str = ""


class TradeHeader(BaseModel):
    model_config = _STRICT

    row: TradeRow
    legs: list[LegInfo]
    thesis: str
    risk_narrative: str
    candidate_id: str
    expires_at: _dt.datetime | None
    opened_at: _dt.datetime | None = Field(default=None, description="Structure opened (fill)")
    closed_at: _dt.datetime | None = None
    dte: int | None = None
    lifecycle: str = Field(description="StatusStepper stage reached")
    lifecycle_failed: str | None = Field(
        default=None, description="StatusStepper stage that failed"
    )


class PayoffPoint(BaseModel):
    model_config = _STRICT

    spot: float
    pnl: float


class PayoffSection(BaseModel):
    """Payoff at expiry for the whole position (× contracts), from :mod:`arc.structures`."""

    model_config = _STRICT

    contracts: int
    points: list[PayoffPoint] = Field(default_factory=list)
    breakevens: list[float] = Field(default_factory=list)
    max_gain: float | None = Field(default=None, description="None = unbounded")
    max_loss: float | None = Field(default=None, description="Positive $; None = unbounded")
    entry_spot: float | None = Field(default=None, description="Underlying at proposal time")
    entry_spot_at: _dt.datetime | None = None
    latest_spot: float | None = Field(
        default=None, description="Latest close in the regime context"
    )
    latest_spot_at: _dt.date | None = None
    mark_pnl: float | None = Field(default=None, description="P&L at the latest broker mark, $")
    mark_at: _dt.datetime | None = None
    error: str | None = Field(default=None, description="Why no payoff could be computed")


class QuantSection(BaseModel):
    model_config = _STRICT

    pop: float | None = None
    ev: Decimal | None = Field(default=None, description="Quant's EV per contract (model inputs)")
    cost_bps: float | None = None
    contracts: int | None = None
    notional: Decimal | None = None
    pct_equity: float | None = None
    max_gain: Decimal | None = Field(default=None, description="Per unit, $")
    max_loss: Decimal | None = None
    buying_power: Decimal | None = None
    dte: int | None = None
    # Managed-exit vs hold-to-expiry headline, read straight from the stored payload so it
    # shows even when the full analytics block does not validate (older/partial rows).
    net_ev_managed: float | None = Field(default=None, description="$ per unit after costs")
    net_ev_hold: float | None = None
    pop_managed: float | None = None
    pop_hold: float | None = None
    analytics: ProposalAnalytics | None = None
    analytics_at: _dt.datetime | None = None
    analytics_error: str | None = None


class PersonaCallView(BaseModel):
    model_config = _STRICT

    id: str
    run_id: str
    persona: str
    model: str
    status: str
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    prompt_sha256: str
    prompt_text: str | None = None
    created_at: _dt.datetime | None = None


class DecisionItem(BaseModel):
    model_config = _STRICT

    id: str
    at: _dt.datetime | None
    persona: str
    stage: str
    subject: str
    choice: str
    reason_code: str
    reason_label: str
    reason_text: str
    confidence: float | None = None
    this_trade: bool = Field(description="False = a chain row for the same ticker/session")
    persona_call_id: str | None = None
    inputs_snapshot_id: str | None = None
    run_id: str | None = None


class DecisionTrail(BaseModel):
    model_config = _STRICT

    chain_run_id: str | None = None
    items: list[DecisionItem] = Field(default_factory=list)
    persona_calls: dict[str, PersonaCallView] = Field(default_factory=dict)


class ViolationView(BaseModel):
    model_config = _STRICT

    code: str
    detail: str
    reason_code: str
    label: str


class GateView(BaseModel):
    model_config = _STRICT

    id: str
    decided_at: _dt.datetime | None
    passed: bool
    violations: list[ViolationView]
    token_version: str | None = Field(
        default=None, description="arc1 | arc2; the token is never shown"
    )
    account_snapshot: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class ApprovalView(BaseModel):
    model_config = _STRICT

    status: str
    reason: str = ""
    channel: str | None = None
    thread_ts: str | None = None
    message_ts: str | None = None
    posted_at: _dt.datetime | None = None
    expires_at: _dt.datetime | None = None
    ttl_s: int | None = None
    decided_at: _dt.datetime | None = None
    decided_by: str | None = None
    limit_price: Decimal | None = Field(default=None, description="The approved per-share limit")
    permalink: str | None = Field(default=None, description="Slack permalink, when stored")


class OrderEventView(BaseModel):
    model_config = _STRICT

    from_state: str
    to_state: str
    actor: str
    detail: str
    at: _dt.datetime | None


class OrderView(BaseModel):
    model_config = _STRICT

    id: str
    client_order_id: str
    broker_order_id: str | None
    state: str
    created_at: _dt.datetime | None
    updated_at: _dt.datetime | None
    events: list[OrderEventView] = Field(default_factory=list)


class FillView(BaseModel):
    model_config = _STRICT

    id: str
    order_id: str
    qty: int
    price: Decimal | None
    filled_at: _dt.datetime | None


class ExecutionSection(BaseModel):
    model_config = _STRICT

    status: str | None = None
    kind: str | None = None
    token_version: str | None = None
    band_lo: Decimal | None = None
    band_hi: Decimal | None = None
    max_steps: int | None = None
    attempts: int | None = None
    steps_used: int | None = None
    contracts: int | None = None
    filled_qty: int | None = None
    fill_price: Decimal | None = None
    detail: str = ""
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    limit: Decimal | None = Field(default=None, description="Approved limit, else the proposal net")
    mid: Decimal | None = Field(default=None, description="Structure mid at proposal")
    slippage_vs_limit: Decimal | None = Field(default=None, description="$/share, + = worse")
    slippage_vs_mid: Decimal | None = None
    orders: list[OrderView] = Field(default_factory=list)
    fills: list[FillView] = Field(default_factory=list)


class ExitLink(BaseModel):
    model_config = _STRICT

    proposal_hash: str
    created_at: _dt.datetime | None
    stage: Stage
    exit_reason: str | None = None
    close_net: Decimal | None = None


class SwapView(BaseModel):
    model_config = _STRICT

    id: str
    status: str
    detail: str
    close_ticker: str
    close_proposal_hash: str | None
    open_ticker: str
    open_proposal_hash: str | None
    suggestion: dict[str, Any] = Field(default_factory=dict)
    created_at: _dt.datetime | None = None


class PositionSection(BaseModel):
    model_config = _STRICT

    structure_id: str | None = None
    status: str | None = None
    ticker: str | None = None
    contracts: int | None = None
    entry_net: Decimal | None = None
    opened_at: _dt.datetime | None = None
    closed_at: _dt.datetime | None = None
    close_net: Decimal | None = None
    exit_reason: str | None = None
    exit_pending: bool = False
    days_held: int | None = None
    realized_pnl: Decimal | None = None
    open_proposal_hash: str | None = None
    exits: list[ExitLink] = Field(default_factory=list)
    swaps: list[SwapView] = Field(default_factory=list)


class OutcomeView(BaseModel):
    model_config = _STRICT

    status: str
    contracts: int | None = None
    limit_price: Decimal | None = None
    entry_fill: Decimal | None = None
    slippage_usd: Decimal | None = None
    slippage_bps: float | None = None
    cost_bps: float | None = None
    exit_fill: Decimal | None = None
    realised_pnl: Decimal | None = None
    max_adverse_excursion: Decimal | None = None
    days_held: int | None = None
    exit_reason: str | None = None
    ev_total: Decimal | None = None
    pnl_vs_ev: Decimal | None = None
    hold_to_expiry_shadow_pnl: Decimal | None = None
    at: _dt.datetime | None = None


class ReviewView(BaseModel):
    model_config = _STRICT

    id: str
    label: str
    root_cause: str
    notes: str
    reviewer: str
    at: _dt.datetime | None
    decision_id: str | None = None
    cites: list[str] = Field(default_factory=list)


class OutcomeSection(BaseModel):
    model_config = _STRICT

    outcome: OutcomeView | None = None
    reviews: list[ReviewView] = Field(default_factory=list)


class LegQuoteView(BaseModel):
    model_config = _STRICT

    occ_symbol: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    iv: float | None = None
    quote_time: _dt.datetime | None = None


class RegimeView(BaseModel):
    model_config = _STRICT

    entry_id: str
    snapshot_id: str | None = None
    as_of: str | None = None
    current: str | None = None
    trailing_return: float | None = None
    stickiness: float | None = None
    expected_duration: float | None = None
    last_close: float | None = None
    iv: float | None = None
    iv_rank: float | None = None
    hv20: float | None = None


class CandidateView(BaseModel):
    model_config = _STRICT

    id: str
    ticker: str
    stance: str
    catalyst_type: str
    catalyst_date: str | None = None
    confidence: float
    sources: list[str] = Field(default_factory=list)
    corroboration: int | None = None
    created_at: _dt.datetime | None = None
    run_id: str | None = None


class MarketSection(BaseModel):
    model_config = _STRICT

    subject: str | None = None
    spot: float | None = None
    atm_iv: float | None = None
    ivr: float | None = None
    hv20: float | None = None
    regime_label: str | None = Field(default=None, description="Regime stored with the context")
    legs: list[LegQuoteView] = Field(default_factory=list)
    quotes_as_of: _dt.datetime | None = None
    at: _dt.datetime | None = None
    regime: RegimeView | None = None
    candidate: CandidateView | None = None


class ManifestView(BaseModel):
    model_config = _STRICT

    run_id: str
    attempt: int
    job: str
    chain_run_id: str | None = None
    status: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    config_hashes: dict[str, str] = Field(default_factory=dict)
    config_version: str | None = None
    models_requested: list[str] = Field(default_factory=list)
    models_served: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    route: str


class TradeDetail(BaseModel):
    """Everything about one trade, in one round trip."""

    model_config = _STRICT

    as_of: _dt.datetime
    header: TradeHeader
    payoff: PayoffSection
    quant: QuantSection
    decisions: DecisionTrail
    gate: list[GateView]
    approval: ApprovalView | None
    execution: ExecutionSection | None
    position: PositionSection | None
    outcome: OutcomeSection
    market: MarketSection
    manifest: ManifestView | None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def date_range(f: TradeFilters, today: _dt.date) -> tuple[_dt.date | None, _dt.date | None]:
    """The ET day range a date preset covers (inclusive); ``(None, None)`` = all time."""
    match f.date:
        case "today":
            return today, today
        case "7d":
            return today - _dt.timedelta(days=6), today
        case "30d":
            return today - _dt.timedelta(days=29), today
        case "mtd":
            return today.replace(day=1), today
        case "ytd":
            return today.replace(month=1, day=1), today
        case "custom":
            return f.date_from, f.date_to
        case _:
            return None, None


def _in(col: str, values: list[str], params: list[Any]) -> str:
    params.extend(values)
    return f"{col} IN ({','.join('?' * len(values))})"


def _where(f: TradeFilters, today: _dt.date) -> tuple[str, list[Any]]:
    """WHERE clause over the ``trades`` CTE."""
    clauses: list[str] = []
    params: list[Any] = []
    lo, hi = date_range(f, today)
    day = "COALESCE(day, substr(created_at, 1, 10))"
    if lo is not None:
        clauses.append(f"{day} >= ?")
        params.append(lo.isoformat())
    if hi is not None:
        clauses.append(f"{day} <= ?")
        params.append(hi.isoformat())
    if f.ticker:
        clauses.append(_in("ticker", [t.upper() for t in f.ticker], params))
    if f.kind:
        clauses.append(_in("kind", list(f.kind), params))
    if f.structure:
        clauses.append(_in("structure_kind", f.structure, params))
    if f.stage:
        clauses.append(_in("stage", list(f.stage), params))
    if f.exit_reason:
        clauses.append(_in("exit_reason", f.exit_reason, params))
    if f.account_profile:
        clauses.append(_in("profile", f.account_profile, params))
    if f.reason_code:
        sub = _in("d.reason_code", f.reason_code, params)
        clauses.append(
            f"EXISTS (SELECT 1 FROM decisions d WHERE d.proposal_hash = trades.proposal_hash "
            f"AND {sub})"
        )
    if f.min_net_ev is not None:
        clauses.append("net_ev >= ?")
        params.append(f.min_net_ev)
    if f.min_pop is not None:
        clauses.append("COALESCE(pop_managed, pop) >= ?")
        params.append(f.min_pop)
    q = (f.q or "").strip()
    if q:
        clauses.append(
            "(proposal_hash >= ? AND proposal_hash < ?"
            " OR ticker = ? OR run_id = ? OR chain_run_id = ?"
            " OR run_id IN (SELECT run_id FROM routine_runs WHERE chain_run_id = ?)"
            " OR structure_id = ? OR closes_structure_id = ? OR swap_id = ?)"
        )
        low = q.lower()
        params += [low, low + "\uffff", q.upper(), q, q, q, q, q, q]
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _legs(legs_json: str | None) -> list[str]:
    out: list[str] = []
    for leg in _json(legs_json, []) or []:
        if isinstance(leg, dict) and leg.get("occ_symbol"):
            out.append(str(leg["occ_symbol"]))
    return out


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row(r: sqlite3.Row) -> TradeRow:
    contracts = r["contracts"]
    return TradeRow(
        proposal_hash=r["proposal_hash"],
        created_at=parse_ts(r["created_at"]),
        day=r["day"],
        ticker=r["ticker"],
        kind=r["kind"] or "open",
        structure_kind=r["structure_kind"],
        legs=_legs(r["legs_json"]),
        contracts=int(contracts) if contracts is not None else None,
        limit=_dec(r["limit_px"]) if r["limit_px"] is None else _dec(repr(r["limit_px"])),
        net_ev=_f(r["net_ev"]),
        pop=_f(r["pop"]),
        pop_managed=_f(r["pop_managed"]),
        pop_hold=_f(r["pop_static"]),
        gate_passed=None if r["gate_passed"] is None else bool(r["gate_passed"]),
        violations=int(r["n_violations"] or 0),
        first_violation=r["first_violation"],
        approval=r["approval"],
        execution=r["execution"],
        fill_price=_dec(r["fill_price"]),
        slippage_bps=_f(r["slippage_bps"]),
        realized_pnl=_f(r["realized_pnl"]),
        exit_reason=r["exit_reason"],
        stage=r["stage"],
        account_profile=r["profile"],
        run_id=r["run_id"],
        chain_run_id=r["chain_run_id"],
        structure_id=r["structure_id"],
        closes_structure_id=r["closes_structure_id"],
        swap_id=r["swap_id"],
    )


def _ready(conn: sqlite3.Connection) -> bool:
    """The list query needs the execution-era tables (migration 011+)."""
    return all(_has_table(conn, t) for t in _REQUIRED)


def load_trades(
    conn: sqlite3.Connection,
    filters: TradeFilters,
    *,
    now: _dt.datetime,
    page: int = 1,
    size: int = 50,
    sort: SortKey = "time",
    direction: Literal["asc", "desc"] = "desc",
) -> TradeListResponse:
    """One page of trades matching *filters*, plus the filter-wide summary (SELECT only)."""
    now_et = now.astimezone(ET)
    today = now_et.date()
    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(1, page)
    lo, hi = date_range(filters, today)
    empty = TradeListResponse(
        as_of=now_et,
        filters=filters,
        date_from=lo,
        date_to=hi,
        sort=sort,
        dir=direction,
        page=page,
        size=size,
        total=0,
        items=[],
        summary=TradeSummary(),
    )
    if not _ready(conn):
        return empty
    where, params = _where(filters, today)
    order = _SORT_SQL[sort]
    # Three lean passes instead of one wide window query: the filter-wide summary, the
    # page's row ids (sort keys only), then the full rows for that page (<= size rows).
    # The summary reads a LIMIT -1 subquery so SQLite runs it as a co-routine and
    # evaluates each derived column (realized P&L, net EV) once per row rather than once
    # per aggregate that names it (~40% of the pass on 100k proposals).
    agg = conn.execute(
        f"""{_base(conn)}
        SELECT COUNT(*),
          SUM(CASE WHEN COALESCE(filled_qty, 0) > 0 THEN 1 ELSE 0 END),
          SUM(realized_pnl), COUNT(realized_pnl), AVG(slippage_bps), AVG(net_ev),
          SUM(CASE WHEN realized_pnl IS NOT NULL THEN net_ev END)
        FROM (SELECT filled_qty, realized_pnl, slippage_bps, net_ev
              FROM trades{where} LIMIT -1)""",  # noqa: S608 - clauses from a fixed whitelist
        params,
    ).fetchone()
    total = int(agg[0] or 0)
    if total == 0:
        return empty
    filled = int(agg[1] or 0)
    summary = TradeSummary(
        count=total,
        filled=filled,
        filled_pct=filled / total,
        realized_pnl=_f(agg[2]),
        realized_count=int(agg[3] or 0),
        avg_slippage_bps=_f(agg[4]),
        avg_net_ev=_f(agg[5]),
        net_ev_realized=_f(agg[6]),
    )
    ids = [
        r[0]
        for r in conn.execute(
            f"""{_base(conn)} SELECT rid FROM trades{where}
            ORDER BY {order} IS NULL, {order} {direction.upper()}, jd DESC, rid DESC
            LIMIT ? OFFSET ?""",  # noqa: S608 - column names from a fixed whitelist
            [*params, size, (page - 1) * size],
        )
    ]
    by_rid: dict[int, sqlite3.Row] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
            f"{_base(conn)} SELECT * FROM trades WHERE rid IN ({marks})",  # noqa: S608
            ids,
        ):
            by_rid[r["rid"]] = r
    items = [_row(by_rid[i]) for i in ids if i in by_rid]
    return empty.model_copy(update={"total": total, "items": items, "summary": summary})


def _trade_row(conn: sqlite3.Connection, proposal_hash: str) -> TradeRow | None:
    if not _ready(conn):
        return None
    row = conn.execute(
        f"{_base(conn)} SELECT * FROM trades WHERE proposal_hash = ?", (proposal_hash,)
    ).fetchone()
    return _row(row) if row is not None else None


def load_filter_options(conn: sqlite3.Connection, *, now: _dt.datetime) -> TradeFilterOptions:
    """Distinct values for the filter dropdowns (SELECT only)."""

    def col(sql: str) -> list[str]:
        return [str(r[0]) for r in conn.execute(sql).fetchall() if r[0] not in (None, "")]

    now_et = now.astimezone(ET)
    has_outcomes = _has_table(conn, "outcomes")
    if not _ready(conn):
        return TradeFilterOptions(
            as_of=now_et,
            tickers=[],
            kinds=[],
            structures=[],
            exit_reasons=[],
            reason_codes=[],
            account_profiles=[],
        )
    codes = (
        col("SELECT DISTINCT reason_code FROM decisions WHERE proposal_hash IS NOT NULL ORDER BY 1")
        if _has_table(conn, "decisions")
        else []
    )
    return TradeFilterOptions(
        as_of=now_et,
        tickers=col("SELECT DISTINCT ticker FROM proposals ORDER BY 1"),
        kinds=col("SELECT DISTINCT kind FROM proposals ORDER BY 1"),
        structures=col(
            "SELECT DISTINCT json_extract(structure_json, '$.kind') FROM proposals ORDER BY 1"
        ),
        exit_reasons=sorted(
            set(col("SELECT DISTINCT exit_reason FROM open_structures"))
            | (set(col("SELECT DISTINCT exit_reason FROM outcomes")) if has_outcomes else set())
        ),
        reason_codes=[{"code": c, "label": reason_label(c)} for c in codes],
        account_profiles=col(
            f"SELECT DISTINCT {_MC['profile']} FROM market_contexts ORDER BY 1"  # noqa: S608
        )
        if _has_table(conn, "market_contexts")
        else [],
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search(conn: sqlite3.Connection, q: str, *, now: _dt.datetime) -> SearchResponse:
    """Resolve a ticker / hash prefix / run id / chain id / structure id to routes."""
    now_et = now.astimezone(ET)
    text = q.strip()
    out: list[SearchMatch] = []
    if not text or not _ready(conn):
        return SearchResponse(as_of=now_et, q=q, matches=out)
    seen: set[tuple[str, str]] = set()

    def add(m: SearchMatch) -> None:
        if (m.kind, m.id) not in seen and len(out) < SEARCH_LIMIT:
            seen.add((m.kind, m.id))
            out.append(m)

    upper = text.upper()
    n = conn.execute("SELECT COUNT(*) FROM proposals WHERE ticker = ?", (upper,)).fetchone()[0]
    if n:
        add(
            SearchMatch(
                kind="ticker",
                id=upper,
                label=f"{upper} · {n} trade(s)",
                route=f"/trades?ticker={upper}",
            )
        )
    for r in conn.execute(
        "SELECT id, open_proposal_hash, ticker FROM open_structures WHERE id = ?", (text,)
    ):
        add(
            SearchMatch(
                kind="structure",
                id=r["id"],
                label=f"Structure {r['ticker']}",
                route=f"/trades/{r['open_proposal_hash']}",
            )
        )
    for key, label in (("run_id", "Run"), ("chain_run_id", "Chain")):
        hits = conn.execute(
            f"SELECT proposal_hash, ticker FROM proposals WHERE {key} = ? LIMIT 2",  # noqa: S608
            (text,),
        ).fetchall()
        if key == "chain_run_id" and _has_table(conn, "routine_runs"):
            hits += conn.execute(
                """SELECT p.proposal_hash, p.ticker FROM proposals p
                   JOIN routine_runs r ON r.run_id = p.run_id
                   WHERE r.chain_run_id = ? LIMIT 2""",
                (text,),
            ).fetchall()
        hashes = {h["proposal_hash"]: h["ticker"] for h in hits}
        if len(hashes) == 1:
            h, t = next(iter(hashes.items()))
            add(
                SearchMatch(
                    kind="trade", id=h, label=f"{label} {text[:18]} → {t}", route=f"/trades/{h}"
                )
            )
        elif hashes:
            add(
                SearchMatch(
                    kind="run" if key == "run_id" else "chain",
                    id=text,
                    label=f"{label} {text[:18]} · trades",
                    route=f"/trades?q={text}",
                )
            )
    if len(text) >= 4 and all(c in "0123456789abcdefABCDEF" for c in text):
        low = text.lower()
        for r in conn.execute(
            """SELECT proposal_hash, ticker, kind FROM proposals
               WHERE proposal_hash >= ? AND proposal_hash < ? ORDER BY proposal_hash LIMIT 5""",
            (low, low + "\uffff"),
        ):
            add(
                SearchMatch(
                    kind="trade",
                    id=r["proposal_hash"],
                    label=f"{r['ticker'] or '?'} {r['kind']} · {r['proposal_hash'][:10]}",
                    route=f"/trades/{r['proposal_hash']}",
                )
            )
    if not out and len(upper) <= 6 and upper.isalpha():
        like = conn.execute(
            "SELECT DISTINCT ticker FROM proposals WHERE ticker LIKE ? || '%' ORDER BY 1 LIMIT 5",
            (upper,),
        ).fetchall()
        for r in like:
            add(SearchMatch(kind="ticker", id=r[0], label=r[0], route=f"/trades?ticker={r[0]}"))
    return SearchResponse(as_of=now_et, q=q, matches=out)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

_LIFECYCLE_FAIL = {
    "gate_fail": ("gate", "gate"),
    "rejected": ("approval", "approval"),
    "expired": ("approval", "approval"),
    "cancelled": ("execution", "execution"),
}
_LIFECYCLE = {
    "proposed": "proposed",
    "gate_pass": "gate",
    "approved": "approval",
    "filled": "filled",
    "open": "open",
    "closed": "closed",
}


def _lifecycle(row: TradeRow, exit_pending: bool) -> tuple[str, str | None]:
    """StatusStepper position: proposed → gate → approval → execution → filled → open →
    exit → closed. A close proposal ends at ``filled``."""
    if row.stage in _LIFECYCLE_FAIL:
        reached, failed = _LIFECYCLE_FAIL[row.stage]
        return reached, failed
    if row.execution == "working":
        return "execution", None
    if row.stage == "open" and exit_pending:
        return "exit", None
    return _LIFECYCLE[row.stage], None


def _token_version(token: str | None) -> str | None:
    if not token:
        return None
    head = token.split(".", 1)[0]
    return head if head.startswith("arc") else "unknown"


def _header(p: sqlite3.Row, row: TradeRow, pos: PositionSection | None) -> TradeHeader:
    st = _json(p["structure_json"], {})
    legs = [
        LegInfo(
            occ_symbol=str(leg.get("occ_symbol", "")),
            side=str(leg.get("side", "")),
            ratio=int(leg.get("ratio", 1) or 1),
            premium=_dec(leg.get("premium")),
            intent=str(leg.get("intent", "")),
        )
        for leg in st.get("legs", [])
        if isinstance(leg, dict)
    ]
    reached, failed = _lifecycle(row, bool(pos and pos.exit_pending))
    own = pos is not None and pos.open_proposal_hash == row.proposal_hash
    return TradeHeader(
        row=row,
        legs=legs,
        thesis=p["thesis"] or "",
        risk_narrative=p["risk_narrative"] or "",
        candidate_id=p["candidate_id"],
        expires_at=parse_ts(p["expires_at"]),
        opened_at=pos.opened_at if pos and own else None,
        closed_at=pos.closed_at if pos and own else None,
        dte=st.get("dte"),
        lifecycle=reached,
        lifecycle_failed=failed,
    )


def _latest_mark_pnl(
    conn: sqlite3.Connection, legs: list[LegInfo], contracts: int, entry: Decimal | None
) -> tuple[float | None, _dt.datetime | None]:
    """P&L at the latest monitor mark: Σ side × ratio × mark − entry, × 100 × contracts."""
    if entry is None or not _has_table(conn, "heartbeats"):
        return None, None
    from arc.structures import parse_occ

    hb = conn.execute(
        "SELECT at, detail FROM heartbeats WHERE component = 'monitor' "
        "ORDER BY at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if hb is None:
        return None, None
    marks: dict[str, Decimal] = {}
    for leg in _json(hb["detail"], {}).get("legs", []):
        price = _dec(leg.get("current_price"))
        try:
            key = parse_occ(str(leg.get("symbol", ""))).format()
        except ValueError:
            continue
        if price is not None:
            marks[key] = price
    total = Decimal(0)
    for leg in legs:
        try:
            key = parse_occ(leg.occ_symbol).format()
        except ValueError:
            return None, None
        if key not in marks:
            return None, None
        sign = 1 if leg.side == "long" else -1
        total += sign * leg.ratio * marks[key]
    return float((total - entry) * 100 * contracts), parse_ts(hb["at"])


def _payoff(
    conn: sqlite3.Connection,
    p: sqlite3.Row,
    header: TradeHeader,
    market: MarketSection,
    pos: PositionSection | None,
) -> PayoffSection:
    from arc.models import Structure
    from arc.structures import breakevens, max_gain_loss, payoff_grid

    contracts = header.row.contracts or 1
    base = PayoffSection(
        contracts=contracts,
        entry_spot=market.spot,
        entry_spot_at=market.at,
        latest_spot=market.regime.last_close if market.regime else None,
        latest_spot_at=_dt.date.fromisoformat(market.regime.as_of)
        if market.regime and market.regime.as_of
        else None,
    )
    try:
        st = Structure.model_validate_json(p["structure_json"])
        grid = payoff_grid(st.legs)
        mg, ml = max_gain_loss(st.legs)
        bes = breakevens(st.legs)
    except (ValidationError, ValueError, TypeError, ArithmeticError) as exc:
        return base.model_copy(update={"error": f"{type(exc).__name__}: {exc}"})
    n = Decimal(contracts)
    points = [PayoffPoint(spot=float(s), pnl=float(v * n)) for s, v in grid]
    spots = [x for x in (market.spot, base.latest_spot) if x is not None]
    if spots and points:  # stretch the grid so the spot markers sit on the line
        lo, hi = points[0].spot, points[-1].spot
        extra = [s for s in spots if s < lo or s > hi]
        if extra:
            grid2 = sorted(
                {*(Decimal(str(x.spot)) for x in points), *(Decimal(str(s)) for s in extra)}
            )
            points = [
                PayoffPoint(spot=float(s), pnl=float(v * n)) for s, v in payoff_grid(st.legs, grid2)
            ]
    entry = pos.entry_net if pos and pos.open_proposal_hash == header.row.proposal_hash else None
    mark_pnl, mark_at = (None, None)
    if pos is not None and pos.status == "open" and entry is not None:
        mark_pnl, mark_at = _latest_mark_pnl(conn, header.legs, pos.contracts or contracts, entry)
    return base.model_copy(
        update={
            "points": points,
            "breakevens": [float(b) for b in bes],
            "max_gain": None if mg is None else float(mg * n),
            "max_loss": None if ml is None else float(ml * n),
            "mark_pnl": mark_pnl,
            "mark_at": mark_at,
        }
    )


def _latest_mc(conn: sqlite3.Connection, h: str) -> sqlite3.Row | None:
    if not _has_table(conn, "market_contexts"):
        return None
    return conn.execute(
        "SELECT payload, created_at FROM market_contexts WHERE proposal_hash = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (h,),
    ).fetchone()


def _quant(p: sqlite3.Row, mc: sqlite3.Row | None) -> QuantSection:
    quant = _json(p["quant_json"], {})
    sizing = _json(p["sizing_json"], {})
    st = _json(p["structure_json"], {})
    analytics: ProposalAnalytics | None = None
    error: str | None = None
    raw = _json(mc["payload"], {}).get("analytics") if mc is not None else None
    em = (raw or {}).get("exit_model") or {}
    managed, static = em.get("managed") or {}, em.get("static") or {}
    if raw:
        try:
            analytics = ProposalAnalytics.model_validate(raw)
        except ValidationError as exc:
            error = f"stored analytics do not validate: {exc.error_count()} error(s)"
    return QuantSection(
        pop=_f(quant.get("pop")),
        ev=_dec(quant.get("ev")),
        cost_bps=_f(quant.get("cost_bps")),
        contracts=sizing.get("contracts"),
        notional=_dec(sizing.get("notional")),
        pct_equity=_f(sizing.get("pct_equity")),
        max_gain=_dec(st.get("max_gain")),
        max_loss=_dec(st.get("max_loss")),
        buying_power=_dec(st.get("buying_power")),
        dte=st.get("dte"),
        net_ev_managed=_f(managed.get("net_ev")),
        net_ev_hold=_f(static.get("net_ev")),
        pop_managed=_f(managed.get("pop")),
        pop_hold=_f(static.get("pop")),
        analytics=analytics,
        analytics_at=parse_ts(mc["created_at"]) if mc is not None else None,
        analytics_error=error,
    )


def _chain_id(conn: sqlite3.Connection, p: sqlite3.Row) -> str | None:
    if p["chain_run_id"]:
        return str(p["chain_run_id"])
    if p["run_id"] and _has_table(conn, "routine_runs"):
        r = conn.execute(
            "SELECT chain_run_id FROM routine_runs WHERE run_id = ?", (p["run_id"],)
        ).fetchone()
        if r is not None and r[0]:
            return str(r[0])
    return None


def _decisions(conn: sqlite3.Connection, p: sqlite3.Row, chain: str | None) -> DecisionTrail:
    if not _has_table(conn, "decisions"):
        return DecisionTrail(chain_run_id=chain)
    h = p["proposal_hash"]
    rows = conn.execute(
        """SELECT *, rowid AS rid FROM decisions WHERE proposal_hash = ?
           UNION
           SELECT *, rowid AS rid FROM decisions
           WHERE ? IS NOT NULL AND chain_run_id = ? AND proposal_hash IS NULL
             AND subject IN (?, 'session', 'market')
           ORDER BY at, rid""",
        (h, chain, chain, p["ticker"] or ""),
    ).fetchall()
    items = [
        DecisionItem(
            id=r["id"],
            at=parse_ts(r["at"]),
            persona=r["persona"],
            stage=r["stage"],
            subject=r["subject"],
            choice=r["choice"],
            reason_code=r["reason_code"],
            reason_label=reason_label(r["reason_code"]),
            reason_text=r["reason_text"] or "",
            confidence=r["confidence"],
            this_trade=r["proposal_hash"] == h,
            persona_call_id=r["persona_call_id"],
            inputs_snapshot_id=r["inputs_snapshot_id"],
            run_id=r["run_id"],
        )
        for r in rows
    ]
    calls: dict[str, PersonaCallView] = {}
    ids = sorted({i.persona_call_id for i in items if i.persona_call_id})
    if ids and _has_table(conn, "persona_calls"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(persona_calls)")}
        for r in conn.execute(
            f"SELECT * FROM persona_calls WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
            ids,
        ):
            get = lambda k, r=r: r[k] if k in cols else None  # noqa: E731
            calls[r["id"]] = PersonaCallView(
                id=r["id"],
                run_id=r["run_id"],
                persona=r["persona"],
                model=r["model"],
                status=r["status"],
                error=r["error"],
                input_tokens=get("input_tokens"),
                output_tokens=get("output_tokens"),
                latency_ms=get("latency_ms"),
                cost_usd=get("cost_usd"),
                prompt_sha256=r["prompt_sha256"],
                prompt_text=get("prompt_text"),
                created_at=parse_ts(r["created_at"]),
            )
    return DecisionTrail(chain_run_id=chain, items=items, persona_calls=calls)


def _gate(conn: sqlite3.Connection, h: str) -> list[GateView]:
    out: list[GateView] = []
    for r in conn.execute(
        "SELECT * FROM gate_decisions WHERE proposal_hash = ? ORDER BY decided_at, rowid", (h,)
    ):
        violations = []
        for v in _json(r["violations_json"], []) or []:
            text = str(v)
            code, _, detail = text.partition(":")
            rc = gate_reason(text)
            violations.append(
                ViolationView(
                    code=code.strip(),
                    detail=detail.strip() or text,
                    reason_code=rc.value,
                    label=reason_label(rc.value),
                )
            )
        snap = _json(r["account_snapshot"], {})
        out.append(
            GateView(
                id=r["id"],
                decided_at=parse_ts(r["decided_at"]),
                passed=bool(r["passed"]),
                violations=violations,
                token_version=_token_version(r["token"]),
                account_snapshot=snap if isinstance(snap, dict) else {},
                run_id=r["run_id"],
            )
        )
    return out


def _approval(conn: sqlite3.Connection, h: str) -> ApprovalView | None:
    r = conn.execute("SELECT * FROM approval_requests WHERE proposal_hash = ?", (h,)).fetchone()
    legacy = (
        conn.execute("SELECT * FROM approvals WHERE proposal_hash = ?", (h,)).fetchone()
        if _has_table(conn, "approvals")
        else None
    )
    if r is None and legacy is None:
        return None
    if r is None:
        assert legacy is not None
        return ApprovalView(
            status=legacy["decision"],
            thread_ts=legacy["slack_ts"],
            decided_at=parse_ts(legacy["decided_at"]),
            decided_by=legacy["slack_user"],
        )
    posted, expires = parse_ts(r["created_at"]), parse_ts(r["expires_at"])
    proposal = _json(r["proposal_json"], {})
    return ApprovalView(
        status=r["status"],
        reason=r["reason"] or "",
        channel=r["channel"],
        thread_ts=r["thread_ts"],
        message_ts=r["message_ts"],
        posted_at=posted,
        expires_at=expires,
        ttl_s=int((expires - posted).total_seconds()) if posted and expires else None,
        decided_at=parse_ts(r["decided_at"]),
        decided_by=r["decided_by"],
        limit_price=_dec(proposal.get("limit_price")) if isinstance(proposal, dict) else None,
    )


def _execution(
    conn: sqlite3.Connection, h: str, row: TradeRow, approval: ApprovalView | None
) -> ExecutionSection | None:
    x = conn.execute("SELECT * FROM executions WHERE proposal_hash = ?", (h,)).fetchone()
    orders: list[OrderView] = []
    fills: list[FillView] = []
    has_events = _has_table(conn, "order_events")
    has_fills = _has_table(conn, "fills")
    order_rows = (
        conn.execute(
            "SELECT * FROM orders WHERE proposal_hash = ? ORDER BY created_at, rowid", (h,)
        ).fetchall()
        if _has_table(conn, "orders")
        else []
    )
    for o in order_rows:
        events = [
            OrderEventView(
                from_state=e["from_state"],
                to_state=e["to_state"],
                actor=e["actor"],
                detail=e["detail"],
                at=parse_ts(e["event_at"]),
            )
            for e in (
                conn.execute(
                    "SELECT * FROM order_events WHERE order_id = ? ORDER BY event_at, id",
                    (o["id"],),
                )
                if has_events
                else []
            )
        ]
        orders.append(
            OrderView(
                id=o["id"],
                client_order_id=o["client_order_id"],
                broker_order_id=o["broker_order_id"],
                state=o["state"],
                created_at=parse_ts(o["created_at"]),
                updated_at=parse_ts(o["updated_at"]),
                events=events,
            )
        )
        fills += [
            FillView(
                id=f["id"],
                order_id=f["order_id"],
                qty=int(f["qty"]),
                price=_dec(f["price"]),
                filled_at=parse_ts(f["filled_at"]),
            )
            for f in (
                conn.execute(
                    "SELECT * FROM fills WHERE order_id = ? ORDER BY filled_at, rowid", (o["id"],)
                )
                if has_fills
                else []
            )
        ]
    if x is None and not orders:
        return None
    mid = row.limit
    limit = approval.limit_price if approval and approval.limit_price is not None else mid
    fill = _dec(x["fill_price"]) if x is not None else None
    base = ExecutionSection(
        limit=limit,
        mid=mid,
        slippage_vs_limit=fill - limit if fill is not None and limit is not None else None,
        slippage_vs_mid=fill - mid if fill is not None and mid is not None else None,
        orders=orders,
        fills=fills,
    )
    if x is None:
        return base
    return base.model_copy(
        update={
            "status": x["status"],
            "kind": x["kind"],
            "token_version": x["token_version"],
            "band_lo": _dec(x["band_lo"]),
            "band_hi": _dec(x["band_hi"]),
            "max_steps": x["max_steps"],
            "attempts": x["attempts"],
            "steps_used": x["steps_used"],
            "contracts": x["contracts"],
            "filled_qty": x["filled_qty"],
            "fill_price": fill,
            "detail": x["detail"] or "",
            "started_at": parse_ts(x["started_at"]),
            "finished_at": parse_ts(x["finished_at"]),
        }
    )


def _structure_for(conn: sqlite3.Connection, h: str, kind: str) -> sqlite3.Row | None:
    """The open_structures row this trade opened (open) or exits (close)."""
    if kind == "open":
        return conn.execute(
            "SELECT * FROM open_structures WHERE open_proposal_hash = ?", (h,)
        ).fetchone()
    row = conn.execute(
        "SELECT * FROM open_structures WHERE exit_proposal_hash = ? LIMIT 1", (h,)
    ).fetchone()
    if row is not None:
        return row
    x = conn.execute(
        "SELECT structure_id FROM executions WHERE proposal_hash = ? AND structure_id IS NOT NULL",
        (h,),
    ).fetchone()
    if x is None and _has_table(conn, "swaps"):
        x = conn.execute(
            "SELECT close_structure_id FROM swaps WHERE close_proposal_hash = ? LIMIT 1", (h,)
        ).fetchone()
    if x is None:
        return None
    return conn.execute("SELECT * FROM open_structures WHERE id = ?", (x[0],)).fetchone()


def realized_from_tranches(conn: sqlite3.Connection, s: sqlite3.Row) -> Decimal | None:
    """Realized P&L of one open structure, booked per close tranche (the list's rule).

    Σ -(entry + fill) x 100 x qty over its filled close executions (how the ladder
    books each tranche), plus any contracts closed outside an execution (reconcile
    expiry settlement: the opened quantity minus executed closes, at ``close_net``).
    ``None`` when nothing has been closed yet; a partially closed, still-open
    structure shows the realized part of its closed tranches only.
    """
    entry = _dec(s["entry_net"])
    if entry is None:
        return None
    tranches = conn.execute(
        """SELECT filled_qty, fill_price FROM executions
           WHERE structure_id = ? AND kind = 'close' AND filled_qty > 0
             AND fill_price IS NOT NULL""",
        (s["id"],),
    ).fetchall()
    total, closed_qty, any_closed = Decimal(0), 0, False
    for qty, px in tranches:
        fill = _dec(px)
        if fill is None:
            continue
        total += -(entry + fill) * 100 * int(qty)
        closed_qty += int(qty)
        any_closed = True
    close = _dec(s["close_net"])
    if s["status"] == "closed" and close is not None:
        opened = conn.execute(
            """SELECT filled_qty FROM executions
               WHERE proposal_hash = ? AND kind = 'open' AND filled_qty > 0""",
            (s["open_proposal_hash"],),
        ).fetchone()
        if opened is not None:
            settled = max(int(opened[0]) - closed_qty, 0)
        else:
            settled = 0 if closed_qty else int(s["contracts"])
        if settled:
            total += -(entry + close) * 100 * settled
            any_closed = True
    return total if any_closed else None


def _position(conn: sqlite3.Connection, h: str, kind: str) -> PositionSection | None:
    s = _structure_for(conn, h, kind)
    swaps: list[SwapView] = []
    swap_rows: list[sqlite3.Row] = []
    if _has_table(conn, "swaps"):
        sid = s["id"] if s is not None else None
        swap_rows = conn.execute(
            """SELECT * FROM swaps WHERE close_proposal_hash = ? OR open_proposal_hash = ?
               OR (? IS NOT NULL AND close_structure_id = ?) ORDER BY created_at, rowid""",
            (h, h, sid, sid),
        ).fetchall()
        swaps = [
            SwapView(
                id=w["id"],
                status=w["status"],
                detail=w["detail"] or "",
                close_ticker=w["close_ticker"],
                close_proposal_hash=w["close_proposal_hash"],
                open_ticker=w["open_ticker"],
                open_proposal_hash=w["open_proposal_hash"],
                suggestion=_json(w["suggestion_json"], {}) or {},
                created_at=parse_ts(w["created_at"]),
            )
            for w in swap_rows
        ]
    if s is None:
        return PositionSection(swaps=swaps) if swaps else None
    # Every exit proposal for the structure: its pending/last exit, closes executed
    # against it, and swap closes.
    exit_hashes = {
        r[0]
        for r in conn.execute(
            "SELECT proposal_hash FROM executions WHERE structure_id = ? AND kind = 'close'",
            (s["id"],),
        )
    }
    if s["exit_proposal_hash"]:
        exit_hashes.add(s["exit_proposal_hash"])
    exit_hashes |= {
        w["close_proposal_hash"]
        for w in swap_rows
        if w["close_proposal_hash"] and w["close_structure_id"] == s["id"]
    }
    exits: list[ExitLink] = []
    for eh in sorted(exit_hashes):
        er = _trade_row(conn, eh)
        if er is not None:
            exits.append(
                ExitLink(
                    proposal_hash=eh,
                    created_at=er.created_at,
                    stage=er.stage,
                    exit_reason=er.exit_reason or s["exit_reason"],
                    close_net=er.fill_price,
                )
            )
    epoch = _dt.datetime.min.replace(tzinfo=_dt.UTC)
    exits.sort(key=lambda e: e.created_at or epoch)
    entry, close = _dec(s["entry_net"]), _dec(s["close_net"])
    opened, closed = parse_ts(s["opened_at"]), parse_ts(s["closed_at"])
    n = int(s["contracts"])
    realized = realized_from_tranches(conn, s) if entry is not None else None
    return PositionSection(
        structure_id=s["id"],
        status=s["status"],
        ticker=s["ticker"],
        contracts=n,
        entry_net=entry,
        opened_at=opened,
        closed_at=closed,
        close_net=close,
        exit_reason=s["exit_reason"],
        exit_pending=s["status"] == "open" and s["exit_proposal_hash"] is not None,
        days_held=(closed.date() - opened.date()).days if opened and closed else None,
        realized_pnl=realized,
        open_proposal_hash=s["open_proposal_hash"],
        exits=exits,
        swaps=swaps,
    )


def _outcome(conn: sqlite3.Connection, h: str) -> OutcomeSection:
    outcome: OutcomeView | None = None
    if _has_table(conn, "outcomes"):
        r = conn.execute(
            "SELECT * FROM outcomes WHERE proposal_hash = ? ORDER BY at DESC, rowid DESC LIMIT 1",
            (h,),
        ).fetchone()
        if r is not None:
            outcome = OutcomeView(
                status=r["status"],
                contracts=r["contracts"],
                limit_price=_dec(r["limit_price"]),
                entry_fill=_dec(r["entry_fill"]),
                slippage_usd=_dec(r["slippage_usd"]),
                slippage_bps=r["slippage_bps"],
                cost_bps=r["cost_bps"],
                exit_fill=_dec(r["exit_fill"]),
                realised_pnl=_dec(r["realised_pnl"]),
                max_adverse_excursion=_dec(r["max_adverse_excursion"]),
                days_held=r["days_held"],
                exit_reason=r["exit_reason"],
                ev_total=_dec(r["ev_total"]),
                pnl_vs_ev=_dec(r["pnl_vs_ev"]),
                hold_to_expiry_shadow_pnl=_dec(r["hold_to_expiry_shadow_pnl"]),
                at=parse_ts(r["at"]),
            )
    reviews: list[ReviewView] = []
    if _has_table(conn, "decision_reviews"):
        for r in conn.execute(
            """SELECT * FROM decision_reviews r
               WHERE (r.proposal_hash = ? OR r.decision_id IN
                      (SELECT id FROM decisions WHERE proposal_hash = ?))
                 AND NOT EXISTS (SELECT 1 FROM decision_reviews s WHERE s.supersedes_id = r.id)
               ORDER BY r.at, r.rowid""",
            (h, h),
        ).fetchall():
            cites = [
                c[0]
                for c in conn.execute(
                    "SELECT decision_id FROM decision_review_citations WHERE review_id = ? "
                    "ORDER BY rowid",
                    (r["id"],),
                )
            ]
            reviews.append(
                ReviewView(
                    id=r["id"],
                    label=r["label"],
                    root_cause=r["root_cause"],
                    notes=r["notes"] or "",
                    reviewer=r["reviewer"],
                    at=parse_ts(r["at"]),
                    decision_id=r["decision_id"],
                    cites=cites,
                )
            )
    return OutcomeSection(outcome=outcome, reviews=reviews)


def _regime(
    conn: sqlite3.Connection, ticker: str | None, snapshot_ids: list[str]
) -> RegimeView | None:
    """The ``regime`` entry for *ticker* in the first input snapshot that has one."""
    if not ticker or not _has_table(conn, "context_snapshots"):
        return None
    for sid in snapshot_ids:
        snap = conn.execute(
            "SELECT entry_ids FROM context_snapshots WHERE id = ?", (sid,)
        ).fetchone()
        ids = [str(i) for i in (_json(snap["entry_ids"], []) if snap else [])]
        if not ids:
            continue
        r = conn.execute(
            f"""SELECT id, payload, valid_from FROM context_entries
                WHERE id IN ({",".join("?" * len(ids))}) AND kind = 'regime' AND subject = ?
                ORDER BY valid_from DESC LIMIT 1""",  # noqa: S608
            [*ids, ticker],
        ).fetchone()
        if r is None:
            continue
        f = _json(r["payload"], {})
        reg = f.get("regime") or {}
        vol = f.get("vol") or {}
        return RegimeView(
            entry_id=r["id"],
            snapshot_id=sid,
            as_of=f.get("as_of"),
            current=reg.get("current"),
            trailing_return=_f(reg.get("trailing_return")),
            stickiness=_f(reg.get("stickiness")),
            expected_duration=_f(reg.get("expected_duration")),
            last_close=_f(f.get("last_close")),
            iv=_f(vol.get("iv")),
            iv_rank=_f(vol.get("iv_rank")),
            hv20=_f(vol.get("hv20")),
        )
    return None


def _market(
    conn: sqlite3.Connection, p: sqlite3.Row, mc: sqlite3.Row | None, trail: DecisionTrail
) -> MarketSection:
    payload = _json(mc["payload"], {}) if mc is not None else {}
    legs = []
    for q in payload.get("legs", []) or []:
        if isinstance(q, dict) and q.get("occ_symbol"):
            legs.append(
                LegQuoteView(
                    occ_symbol=str(q["occ_symbol"]),
                    bid=_f(q.get("bid")),
                    ask=_f(q.get("ask")),
                    mid=_f(q.get("mid")),
                    iv=_f(q.get("iv")),
                    quote_time=parse_ts(q.get("quote_time")),
                )
            )
    snaps = [i.inputs_snapshot_id for i in trail.items if i.inputs_snapshot_id]
    snaps = list(dict.fromkeys(reversed(snaps)))  # latest first, unique
    cand: CandidateView | None = None
    c = conn.execute("SELECT * FROM candidates WHERE id = ?", (p["candidate_id"],)).fetchone()
    if c is not None:
        cols = c.keys()
        sources = _json(c["sources"], [])
        cand = CandidateView(
            id=c["id"],
            ticker=c["ticker"],
            stance=c["stance"],
            catalyst_type=c["catalyst_type"],
            catalyst_date=c["catalyst_date"],
            confidence=float(c["confidence"]),
            sources=[str(s) for s in sources] if isinstance(sources, list) else [],
            corroboration=c["corroboration"] if "corroboration" in cols else None,
            created_at=parse_ts(c["created_at"]),
            run_id=c["run_id"],
        )
    spot = _f(payload.get("underlying_last"))
    if spot is None:
        spot = _f(p["spot"])
    return MarketSection(
        subject=payload.get("subject"),
        spot=spot,
        atm_iv=_f(payload.get("atm_iv")),
        ivr=_f(payload.get("ivr")),
        hv20=_f(payload.get("hv20")),
        regime_label=payload.get("regime") or p["regime"],
        legs=legs,
        quotes_as_of=parse_ts(payload.get("quotes_as_of")),
        at=parse_ts(payload.get("at")) or (parse_ts(mc["created_at"]) if mc is not None else None),
        regime=_regime(conn, p["ticker"], snaps),
        candidate=cand,
    )


def _manifest(conn: sqlite3.Connection, run_id: str | None) -> ManifestView | None:
    if not run_id or not _has_table(conn, "run_manifests"):
        return None
    r = conn.execute(
        "SELECT * FROM run_manifests WHERE run_id = ? ORDER BY attempt DESC LIMIT 1", (run_id,)
    ).fetchone()
    if r is None:
        return None
    m = _json(r["payload"], {})
    hashes = m.get("config_hashes") or {}
    return ManifestView(
        run_id=run_id,
        attempt=int(r["attempt"]),
        job=r["job"],
        chain_run_id=r["chain_run_id"],
        status=r["status"],
        git_sha=m.get("git_sha"),
        git_dirty=m.get("git_dirty"),
        config_hashes={str(k): str(v) for k, v in hashes.items()},
        config_version=None if m.get("config_version") is None else str(m["config_version"]),
        models_requested=[str(x) for x in m.get("models_requested") or []],
        models_served=[str(x) for x in m.get("models_served") or []],
        input_tokens=m.get("input_tokens"),
        output_tokens=m.get("output_tokens"),
        cost_usd=m.get("cost_usd"),
        started_at=parse_ts(m.get("started_at")),
        finished_at=parse_ts(m.get("finished_at")),
        route=f"/ops/runs/{run_id}",
    )


def load_trade(
    conn: sqlite3.Connection, proposal_hash: str, *, now: _dt.datetime
) -> TradeDetail | None:
    """Every section for one trade (SELECT only); ``None`` when the hash is unknown."""
    p = conn.execute("SELECT * FROM proposals WHERE proposal_hash = ?", (proposal_hash,)).fetchone()
    row = _trade_row(conn, proposal_hash) if p is not None else None
    if p is None or row is None:
        return None
    chain = _chain_id(conn, p)
    trail = _decisions(conn, p, chain)
    pos = _position(conn, proposal_hash, row.kind)
    header = _header(p, row, pos)
    mc = _latest_mc(conn, proposal_hash)
    market = _market(conn, p, mc, trail)
    approval = _approval(conn, proposal_hash)
    return TradeDetail(
        as_of=now.astimezone(ET),
        header=header,
        payoff=_payoff(conn, p, header, market, pos),
        quant=_quant(p, mc),
        decisions=trail,
        gate=_gate(conn, proposal_hash),
        approval=approval,
        execution=_execution(conn, proposal_hash, row, approval),
        position=pos,
        outcome=_outcome(conn, proposal_hash),
        market=market,
        manifest=_manifest(conn, p["run_id"]),
    )
