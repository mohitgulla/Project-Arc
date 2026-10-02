"""Retrospective views of the audit store (E9.3): ``explain`` and ``attribution``.

Read-only and deterministic (no LLM, no network, no broker). The Analyst's
pre-run script (E9.2), the owner's CLI and the future D26 ``!arc explain``
mirror all call these functions; each returns a typed, versioned document.

- :func:`explain` — one :class:`ExplainDoc` per proposal: the journal
  decisions, every persona call of its chain (prompt sha256 + reply), the gate
  verdict, the approval record, the order/fill timeline, the local position,
  the outcome row and any reviews. Works for proposals that never traded.
- :class:`CounterfactualReport` (built by :func:`arc.journal.report.counterfactual`,
  next to ``gaps`` whose rows it reuses) — each closed trade against holding it to expiry
  (D19 shadow) and against not trading ($0), plus every rejected / expired /
  gate-failed proposal and menu alternative shadow-priced from the E7.1 EOD
  history (the same rows ``arc journal gaps`` prints).
- :func:`attribution` — realised P&L, N, win rate, avg ``pnl_vs_ev`` and
  realised vs modelled entry slippage per bucket of ``kind`` / ``regime`` /
  ``persona_model``. Buckets below :data:`MIN_SAMPLE` trades carry
  ``low_sample: true``; the Analyst's MIN-SAMPLE gate reads that flag.

Secrets never leave the store through these views: the gate token (and the
``client_order_id``, which embeds it) are reduced to ``token_present``.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from arc.journal.models import (
    DecisionRecord,
    DecisionReview,
    MarketContext,
    OutcomeRecord,
    OutcomeStatus,
)
from arc.journal.scorecard import _closed_positions, _realised_by_structure, _slippage
from arc.journal.scorecard import _ts as _ts_any
from arc.journal.store import JournalStore
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence

__all__ = [
    "ATTRIBUTION_DIMENSIONS",
    "MIN_SAMPLE",
    "VIEWS_VERSION",
    "AttributionBucket",
    "AttributionReport",
    "CounterfactualReport",
    "ExplainDoc",
    "ExplainReport",
    "attribution",
    "explain",
]

VIEWS_VERSION = 1
#: Buckets with fewer closed trades than this are flagged ``low_sample``.
MIN_SAMPLE = 30
ATTRIBUTION_DIMENSIONS = ("kind", "regime", "persona_model", "ticker")
UNKNOWN = "unknown"

_FORBID = ConfigDict(extra="forbid", frozen=True)


def _ts(text: str | None) -> _dt.datetime | None:
    """Any stored timestamp (``to_db`` UTC text or ISO with offset) as ET."""
    ts = _ts_any(text)
    return None if ts is None else ts.astimezone(ET)


_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
_FOREVER = _dt.datetime(9999, 1, 1, tzinfo=_dt.UTC)


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class PersonaCallView(BaseModel):
    model_config = _FORBID

    id: str
    run_id: str
    persona: str
    model: str
    status: str
    error: str | None = None
    prompt_sha256: str
    raw_response: str | None = None
    dropped: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    created_at: _dt.datetime | None = None


class GateView(BaseModel):
    model_config = _FORBID

    id: str
    passed: bool
    violations: list[str] = Field(default_factory=list)
    token_present: bool = Field(..., description="A gate token was minted (value withheld)")
    account_snapshot: dict[str, Any] = Field(default_factory=dict)
    decided_at: _dt.datetime | None = None
    run_id: str | None = None


class ApprovalView(BaseModel):
    """The approval request (card) and the ``approvals`` decision row, if any."""

    model_config = _FORBID

    request_status: str | None = None
    reason: str = ""
    decided_by: str | None = None
    decided_at: _dt.datetime | None = None
    expires_at: _dt.datetime | None = None
    channel: str | None = None
    message_ts: str | None = None
    decision: str | None = Field(None, description="approvals.decision")
    approval_id: str | None = None
    approver: str | None = None


class TimelineEvent(BaseModel):
    """One order-lifecycle event: order created, state transition, or fill."""

    model_config = _FORBID

    at: _dt.datetime | None
    event: str = Field(..., description="order_created | transition | fill")
    order_id: str
    broker_order_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    actor: str | None = None
    detail: str = ""
    qty: int | None = None
    price: str | None = None


class ExplainDoc(BaseModel):
    """Everything the store knows about one proposal, in one document."""

    model_config = _FORBID

    proposal_hash: str
    ticker: str | None = None
    kind: str = "open"
    status: str = Field(..., description="Where the proposal ended up (derived)")
    chain_run_id: str | None = None
    run_id: str | None = None
    created_at: _dt.datetime | None = None
    regime: str | None = None
    structure: dict[str, Any] = Field(default_factory=dict)
    quant: dict[str, Any] = Field(default_factory=dict)
    sizing: dict[str, Any] = Field(default_factory=dict)
    thesis: str = ""
    decisions: list[DecisionRecord] = Field(default_factory=list)
    persona_calls: list[PersonaCallView] = Field(default_factory=list)
    market_context: MarketContext | None = None
    gate: GateView | None = None
    approval: ApprovalView | None = None
    execution: dict[str, Any] | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)
    position: dict[str, Any] | None = None
    outcome: OutcomeRecord | None = None
    reviews: list[DecisionReview] = Field(default_factory=list)


class ExplainReport(BaseModel):
    model_config = _FORBID

    version: int = VIEWS_VERSION
    ref: str
    chain_run_id: str | None = None
    proposals: list[ExplainDoc] = Field(default_factory=list)
    chain_decisions: list[DecisionRecord] = Field(
        default_factory=list,
        description="Decisions of the chain not tied to a proposal (no-trade, drops)",
    )


def _json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _dict_or_none(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _resolve(conn: sqlite3.Connection, ref: str) -> tuple[str | None, list[str]]:
    """``(chain_run_id, proposal_hashes)`` for a proposal hash/prefix, chain id or run id."""
    j = JournalStore(conn)
    if ref.startswith("run-"):
        row = conn.execute(
            "SELECT chain_run_id FROM routine_runs WHERE run_id = ?", (ref,)
        ).fetchone()
        if row is None:
            msg = f"no routine run {ref!r}"
            raise LookupError(msg)
        chain = row[0]
        if chain is None:
            hashes = [
                r[0]
                for r in conn.execute(
                    "SELECT proposal_hash FROM proposals WHERE run_id = ? ORDER BY created_at",
                    (ref,),
                )
            ]
            return None, hashes
        ref = chain
    if ref.startswith("chain-"):
        hashes = j.proposals_in_chain(ref)
        extra = [
            r[0]
            for r in conn.execute(
                "SELECT proposal_hash FROM proposals WHERE chain_run_id = ? ORDER BY created_at",
                (ref,),
            )
            if r[0] not in hashes
        ]
        return ref, hashes + extra
    return j.resolve(ref)


def _persona_calls(conn: sqlite3.Connection, chain: str | None) -> list[PersonaCallView]:
    if chain is None:
        return []
    out = []
    for c in JournalStore(conn).persona_calls(chain):
        out.append(
            PersonaCallView(
                id=c["id"],
                run_id=c["run_id"],
                persona=c["persona"],
                model=c["model"],
                status=c["status"],
                error=c.get("error"),
                prompt_sha256=c["prompt_sha256"],
                raw_response=c.get("raw_response"),
                dropped=_json(c.get("dropped"), {}),
                input_tokens=c.get("input_tokens"),
                output_tokens=c.get("output_tokens"),
                latency_ms=c.get("latency_ms"),
                cost_usd=c.get("cost_usd"),
                created_at=_ts(c.get("created_at")),
            )
        )
    return out


def _gate(conn: sqlite3.Connection, phash: str) -> GateView | None:
    row = conn.execute(
        """SELECT * FROM gate_decisions WHERE proposal_hash = ?
           ORDER BY decided_at DESC, rowid DESC LIMIT 1""",
        (phash,),
    ).fetchone()
    if row is None:
        return None
    return GateView(
        id=row["id"],
        passed=bool(row["passed"]),
        violations=[str(v) for v in _json(row["violations_json"], [])],
        token_present=bool(row["token"]),
        account_snapshot=_json(row["account_snapshot"], {}),
        decided_at=_ts(row["decided_at"]),
        run_id=row["run_id"],
    )


def _approval(conn: sqlite3.Connection, phash: str) -> ApprovalView | None:
    req = conn.execute(
        "SELECT * FROM approval_requests WHERE proposal_hash = ?", (phash,)
    ).fetchone()
    appr = conn.execute(
        "SELECT * FROM approvals WHERE proposal_hash = ? ORDER BY decided_at DESC LIMIT 1",
        (phash,),
    ).fetchone()
    if req is None and appr is None:
        return None
    return ApprovalView(
        request_status=req["status"] if req else None,
        reason=(req["reason"] if req else "") or "",
        decided_by=req["decided_by"] if req else (appr["slack_user"] if appr else None),
        decided_at=(
            _ts(req["decided_at"] if req else None) or _ts(appr["decided_at"] if appr else None)
        ),
        expires_at=_ts(req["expires_at"]) if req else None,
        channel=req["channel"] if req else None,
        message_ts=req["message_ts"] if req else None,
        decision=appr["decision"] if appr else None,
        approval_id=appr["id"] if appr else None,
        approver=appr["slack_user"] if appr else None,
    )


def _timeline(conn: sqlite3.Connection, phash: str) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []
    for o in conn.execute(
        "SELECT * FROM orders WHERE proposal_hash = ? ORDER BY created_at, rowid", (phash,)
    ).fetchall():
        events.append(
            TimelineEvent(
                at=_ts(o["created_at"]),
                event="order_created",
                order_id=o["id"],
                broker_order_id=o["broker_order_id"],
                to_state=o["state"],
            )
        )
        for e in conn.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (o["id"],)
        ):
            events.append(
                TimelineEvent(
                    at=_ts(e["event_at"]),
                    event="transition",
                    order_id=o["id"],
                    from_state=e["from_state"],
                    to_state=e["to_state"],
                    actor=e["actor"],
                    detail=e["detail"],
                )
            )
        for f in conn.execute(
            "SELECT * FROM fills WHERE order_id = ? ORDER BY filled_at, rowid", (o["id"],)
        ):
            events.append(
                TimelineEvent(
                    at=_ts(f["filled_at"]),
                    event="fill",
                    order_id=o["id"],
                    qty=f["qty"],
                    price=f["price"],
                )
            )
    return sorted(events, key=lambda e: e.at or _EPOCH)  # stable: store order within a tie


def _position(conn: sqlite3.Connection, phash: str) -> dict[str, Any] | None:
    return _dict_or_none(
        conn.execute(
            """SELECT * FROM open_structures
               WHERE open_proposal_hash = ? OR exit_proposal_hash = ?
               ORDER BY opened_at LIMIT 1""",
            (phash, phash),
        ).fetchone()
    )


def _status(doc: dict[str, Any]) -> str:
    """Where the proposal ended up, most advanced fact first."""
    outcome: OutcomeRecord | None = doc["outcome"]
    if outcome is not None:
        return str(outcome.status)
    pos = doc["position"]
    if pos is not None and doc["kind"] == "open":
        return "closed" if pos["status"] == "closed" else "open"
    ex = doc["execution"]
    if ex is not None:
        return f"execution_{ex['status']}"
    appr: ApprovalView | None = doc["approval"]
    if appr is not None:
        return appr.request_status or appr.decision or "proposed"
    gate: GateView | None = doc["gate"]
    if gate is not None and not gate.passed:
        return "gate_failed"
    return "proposed"


def _doc(conn: sqlite3.Connection, phash: str, calls: list[PersonaCallView]) -> ExplainDoc:
    j = JournalStore(conn)
    row = dict(conn.execute("SELECT * FROM proposals WHERE proposal_hash = ?", (phash,)).fetchone())
    chain = row.get("chain_run_id") or j.chain_for_proposal(phash)
    parts: dict[str, Any] = {
        "kind": row.get("kind") or "open",
        "outcome": j.outcome(phash),
        "position": _position(conn, phash),
        "execution": _dict_or_none(
            conn.execute("SELECT * FROM executions WHERE proposal_hash = ?", (phash,)).fetchone()
        ),
        "approval": _approval(conn, phash),
        "gate": _gate(conn, phash),
    }
    return ExplainDoc(
        proposal_hash=phash,
        ticker=row.get("ticker"),
        status=_status(parts),
        chain_run_id=chain,
        run_id=row.get("run_id"),
        created_at=_ts(row.get("created_at")),
        regime=row.get("regime"),
        structure=_json(row.get("structure_json"), {}),
        quant=_json(row.get("quant_json"), {}),
        sizing=_json(row.get("sizing_json"), {}),
        thesis=row.get("thesis") or "",
        decisions=j.decisions(proposal_hash=phash),
        persona_calls=calls,
        market_context=j.market_context(phash),
        timeline=_timeline(conn, phash),
        reviews=j.reviews(proposal_hash=phash),
        **parts,
    )


def explain(conn: sqlite3.Connection, ref: str) -> ExplainReport:
    """The explain document for a proposal hash (or prefix), a ``chain-…`` or ``run-…`` id."""
    chain, hashes = _resolve(conn, ref)
    j = JournalStore(conn)
    chain_calls = _persona_calls(conn, chain)
    docs = []
    for h in hashes:
        own_chain = conn.execute(
            "SELECT chain_run_id FROM proposals WHERE proposal_hash = ?", (h,)
        ).fetchone()[0] or j.chain_for_proposal(h)
        calls = chain_calls if own_chain == chain else _persona_calls(conn, own_chain)
        docs.append(_doc(conn, h, calls))
    loose = [d for d in j.decisions(chain_run_id=chain) if d.proposal_hash is None] if chain else []
    if chain is None and ref.startswith("run-"):  # a run outside a chain (reconcile, monitor)
        loose = [
            JournalStore._row(r)
            for r in conn.execute(
                "SELECT * FROM decisions WHERE run_id = ? AND proposal_hash IS NULL "
                "ORDER BY at, rowid",
                (ref,),
            )
        ]
    if not docs and not loose:
        msg = f"nothing in the journal for {ref!r}"
        raise LookupError(msg)
    return ExplainReport(ref=ref, chain_run_id=chain, proposals=docs, chain_decisions=loose)


def _gate_snapshot_lines(decisions: Sequence[Any]) -> list[str]:
    """E6.6a: what the E7.5a scorecard gate said at an auto-approval (gate on or off)."""
    out: list[str] = []
    for dec in decisions:
        p = dec.payload or {}
        if str(dec.reason_code) != "auto_approve" or "scorecard_gate" not in p:
            continue
        ev = p.get("realised_net_ev")
        out.append(
            f"  scorecard gate {p['scorecard_gate']}"
            + ("" if p.get("scorecard_gate_applies", True) else " (close: not gated)")
            + f" · closed {p.get('closed_trades')}/{p.get('min_closed_trades')}"
            + (f" · net EV ${ev:,.2f}/trade" if isinstance(ev, int | float) else "")
            + (f" · failing {', '.join(p['failing'])}" if p.get("failing") else "")
        )
    return out


def explain_lines(rep: ExplainReport) -> list[str]:
    """A short human summary; ``--json`` prints the full document."""
    out = [f"explain {rep.ref} · chain {rep.chain_run_id or 'n/a'}"]
    for d in rep.proposals:
        out.append(f"── proposal {d.proposal_hash[:12]} {d.ticker or ''} {d.kind} → {d.status}")
        for c in d.persona_calls:
            out.append(
                f"  [{c.persona.capitalize()}] {c.status} · model {c.model} · "
                f"prompt {c.prompt_sha256[:12]}"
            )
        if d.gate:
            verdict = "PASS" if d.gate.passed else "FAIL " + "; ".join(d.gate.violations)
            out.append(f"  gate {verdict}")
        if d.approval:
            a = d.approval
            out.append(
                f"  approval {a.request_status or a.decision} by {a.decided_by or 'n/a'}"
                + (f" · {a.reason}" if a.reason else "")
            )
        out.extend(_gate_snapshot_lines(d.decisions))
        for e in d.timeline:
            at = f"{e.at.astimezone(ET):%Y-%m-%d %H:%M:%S}" if e.at else "n/a"
            what = (
                f"fill {e.qty} @ {e.price}"
                if e.event == "fill"
                else f"{e.event} {e.from_state or ''}→{e.to_state or ''}"
            )
            out.append(f"  {at} {e.order_id[:10]} {what}")
        o = d.outcome
        if o is None:
            out.append("  outcome: none yet")
        else:
            out.append(
                f"  outcome {o.status} · P&L {o.realised_pnl} · vs EV {o.pnl_vs_ev} · "
                f"slippage {o.slippage_bps} bps · hold-to-expiry {o.hold_to_expiry_shadow_pnl}"
                f" · exit {o.exit_reason}"
            )
        out.extend(f"  review {r.label} · {r.root_cause}" for r in d.reviews)
    if rep.chain_decisions:
        out.append(f"── decisions without a proposal ({len(rep.chain_decisions)})")
        out.extend(
            f"  [{d.persona.value.capitalize()}] {d.subject} {d.choice} {d.reason_code}"
            for d in rep.chain_decisions
        )
    return out


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------


class AttributedTrade(BaseModel):
    """One closed trade and the dimensions it is bucketed by."""

    model_config = _FORBID

    proposal_hash: str
    ticker: str
    closed_at: _dt.datetime
    kind: str
    regime: str
    persona_model: str
    realised_pnl: float
    pnl_vs_ev: float | None = None
    slippage_realised_usd: float | None = None
    slippage_modelled_usd: float | None = None
    source: str = Field(..., description="position (open_structures) | outcome (outcomes row)")


class AttributionBucket(BaseModel):
    model_config = _FORBID

    key: dict[str, str]
    n: int
    realised_pnl: float
    win_rate: float
    avg_pnl_vs_ev: float | None = Field(None, description="Mean over trades with an EV")
    n_pnl_vs_ev: int = 0
    slippage_realised_usd: float | None = Field(
        None, description="Entry slippage summed over trades that have a modelled value"
    )
    slippage_modelled_usd: float | None = None
    n_slippage: int = 0
    low_sample: bool = Field(..., description=f"n < MIN_SAMPLE ({MIN_SAMPLE})")


class AttributionReport(BaseModel):
    model_config = _FORBID

    version: int = VIEWS_VERSION
    since: _dt.datetime | None = None
    until: _dt.datetime
    by: list[str]
    min_sample: int = MIN_SAMPLE
    trades: int = 0
    buckets: list[AttributionBucket] = Field(default_factory=list)
    rows: list[AttributedTrade] = Field(default_factory=list)


def _persona_models(conn: sqlite3.Connection, phash: str) -> str:
    j = JournalStore(conn)
    row = conn.execute(
        "SELECT chain_run_id FROM proposals WHERE proposal_hash = ?", (phash,)
    ).fetchone()
    chain = (row[0] if row else None) or j.chain_for_proposal(phash)
    if chain is None:
        return UNKNOWN
    pairs = sorted(
        {f"{c['persona']}={c['model']}" for c in j.persona_calls(chain) if c["status"] == "ok"}
    )
    return ",".join(pairs) or UNKNOWN


def _dims(conn: sqlite3.Connection, phash: str) -> tuple[str, str, str, dict[str, Any]]:
    """``(structure kind, regime, persona_model, proposal row)`` for a proposal."""
    row = conn.execute(
        """SELECT ticker, structure_json, quant_json, regime FROM proposals
           WHERE proposal_hash = ?""",
        (phash,),
    ).fetchone()
    prop = dict(row) if row else {}
    kind = _json(prop.get("structure_json"), {}).get("kind") or UNKNOWN
    regime = prop.get("regime")
    if not regime:
        mc = JournalStore(conn).market_context(phash)
        regime = mc.regime if mc is not None else None
    return str(kind), str(regime or UNKNOWN), _persona_models(conn, phash), prop


def _ev_total(prop: dict[str, Any], contracts: int) -> Decimal | None:
    ev = _json(prop.get("quant_json"), {}).get("ev")
    return None if ev is None else Decimal(str(ev)) * contracts


def _trades(
    conn: sqlite3.Connection, since: _dt.datetime | None, until: _dt.datetime
) -> list[AttributedTrade]:
    start = since or _EPOCH
    closed = _closed_positions(
        conn,
        start,
        until,
        _realised_by_structure(conn),
        None,
        today=until.astimezone(ET).date(),
    )
    slip = {r.proposal_hash: r for r in _slippage(conn, _EPOCH, _FOREVER).rows if r.kind == "open"}
    out: list[AttributedTrade] = []
    seen: set[str] = set()
    for c in closed:
        kind, regime, pm, prop = _dims(conn, c.open_proposal_hash)
        ev = _ev_total(prop, c.contracts)
        s = slip.get(c.open_proposal_hash)
        seen.add(c.open_proposal_hash)
        out.append(
            AttributedTrade(
                proposal_hash=c.open_proposal_hash,
                ticker=c.ticker,
                closed_at=c.closed_at,
                kind=c.kind or kind,
                regime=regime,
                persona_model=pm,
                realised_pnl=c.realised_pnl,
                pnl_vs_ev=None if ev is None else c.realised_pnl - float(ev),
                slippage_realised_usd=s.realised_usd if s else None,
                slippage_modelled_usd=s.expected_usd if s else None,
                source="position",
            )
        )
    # outcome rows (attribution.attribute) for trades with no local position record
    for o in JournalStore(conn).outcomes(since=since):
        if (
            o.proposal_hash in seen
            or o.status not in (OutcomeStatus.CLOSED, OutcomeStatus.EXPIRED_WORTHLESS)
            or o.realised_pnl is None
            or o.at >= until
        ):
            continue
        kind, regime, pm, prop = _dims(conn, o.proposal_hash)
        s = slip.get(o.proposal_hash)
        out.append(
            AttributedTrade(
                proposal_hash=o.proposal_hash,
                ticker=str(prop.get("ticker") or UNKNOWN),
                closed_at=o.at,
                kind=kind,
                regime=regime,
                persona_model=pm,
                realised_pnl=float(o.realised_pnl),
                pnl_vs_ev=None if o.pnl_vs_ev is None else float(o.pnl_vs_ev),
                slippage_realised_usd=(
                    s.realised_usd
                    if s
                    else (None if o.slippage_usd is None else float(o.slippage_usd))
                ),
                slippage_modelled_usd=s.expected_usd if s else None,
                source="outcome",
            )
        )
    return sorted(out, key=lambda t: (t.closed_at, t.proposal_hash))


def _bucket(key: dict[str, str], rows: list[AttributedTrade]) -> AttributionBucket:
    evs = [r.pnl_vs_ev for r in rows if r.pnl_vs_ev is not None]
    slips = [r for r in rows if r.slippage_modelled_usd is not None]
    n = len(rows)
    return AttributionBucket(
        key=key,
        n=n,
        realised_pnl=sum(r.realised_pnl for r in rows),
        win_rate=sum(1 for r in rows if r.realised_pnl > 0) / n,
        avg_pnl_vs_ev=sum(evs) / len(evs) if evs else None,
        n_pnl_vs_ev=len(evs),
        slippage_realised_usd=(
            sum(r.slippage_realised_usd or 0.0 for r in slips) if slips else None
        ),
        slippage_modelled_usd=(
            sum(r.slippage_modelled_usd or 0.0 for r in slips) if slips else None
        ),
        n_slippage=len(slips),
        low_sample=n < MIN_SAMPLE,
    )


def parse_by(text: str | Iterable[str]) -> list[str]:
    """Validate the ``--by`` dimensions (comma-separated or a list)."""
    parts = [p.strip() for p in (text.split(",") if isinstance(text, str) else text)]
    dims = [p for p in parts if p]
    bad = [p for p in dims if p not in ATTRIBUTION_DIMENSIONS]
    if bad or not dims:
        msg = f"--by must name one or more of {', '.join(ATTRIBUTION_DIMENSIONS)} (got {text!r})"
        raise ValueError(msg)
    return list(dict.fromkeys(dims))


def attribution(
    conn: sqlite3.Connection,
    *,
    since: _dt.datetime | None,
    until: _dt.datetime,
    by: Iterable[str] = ("kind", "regime", "persona_model"),
) -> AttributionReport:
    """Closed trades in ``[since, until)`` bucketed by the *by* dimensions."""
    dims = parse_by(by)
    rows = _trades(conn, since, until)
    groups: dict[tuple[str, ...], list[AttributedTrade]] = defaultdict(list)
    for r in rows:
        groups[tuple(str(getattr(r, d)) for d in dims)].append(r)
    buckets = [
        _bucket(dict(zip(dims, k, strict=True)), v)
        for k, v in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return AttributionReport(
        since=since, until=until, by=dims, trades=len(rows), buckets=buckets, rows=rows
    )


def attribution_lines(rep: AttributionReport) -> list[str]:
    head = f"attribution by {', '.join(rep.by)}"
    head += f" since {rep.since:%Y-%m-%d}" if rep.since else " (all time)"
    out = [head, f"closed trades: {rep.trades} · low_sample below n={rep.min_sample}"]
    if not rep.buckets:
        out.append("  none")
    for b in rep.buckets:
        key = " · ".join(f"{k}={v}" for k, v in b.key.items())
        ev = f"{b.avg_pnl_vs_ev:+,.2f}" if b.avg_pnl_vs_ev is not None else "n/a"
        slip = (
            f"{b.slippage_realised_usd:,.2f} vs modelled {b.slippage_modelled_usd:,.2f}"
            if b.slippage_realised_usd is not None and b.slippage_modelled_usd is not None
            else "n/a"
        )
        out.append(
            f"  {key}: n={b.n} P&L ${b.realised_pnl:,.2f} win {b.win_rate:.0%} "
            f"avg vs EV {ev} slippage {slip}" + ("  [low_sample]" if b.low_sample else "")
        )
    return out


# ---------------------------------------------------------------------------
# counterfactual
# ---------------------------------------------------------------------------


class ClosedCounterfactual(BaseModel):
    """A closed trade against holding it to expiry and against not trading at all."""

    model_config = _FORBID

    proposal_hash: str
    ticker: str
    closed_at: _dt.datetime
    exit_reason: str | None = None
    early: bool | None = Field(None, description="Closed before expiry (None = unknown)")
    realised_pnl: float
    hold_to_expiry_shadow_pnl: float | None = Field(
        None, description="D19 shadow; None = pending (legs not expired / no settlement)"
    )
    hold_minus_realised: float | None = Field(
        None, description="+ = holding to expiry would have done better"
    )
    no_trade_pnl: float = Field(0.0, description="The no-trade alternative is always $0")
    no_trade_minus_realised: float = Field(
        ..., description="+ = not trading would have been better"
    )
    source: str


class NotTradedCounterfactual(BaseModel):
    """A rejected / expired / gate-failed / not-actionable proposal, EOD-marked (E7.1)."""

    model_config = _FORBID

    proposal_hash: str
    ticker: str
    status: str
    structure: str
    contracts: int
    entry: float = Field(..., description="Per share, + debit / - credit (the proposal's limit)")
    shadow_pnl: float | None = Field(None, description="$ had it filled; None = no history")
    as_of: _dt.date | None = None


class AlternativeCounterfactual(BaseModel):
    """A Quant menu alternative against the chosen structure (per contract, EOD-marked)."""

    model_config = _FORBID

    ticker: str
    chosen: str
    alternative: str
    chosen_pnl: float | None = None
    alternative_pnl: float | None = None
    better_by: float | None = Field(None, description="+ = the alternative would have done better")
    as_of: _dt.date | None = None


class CounterfactualReport(BaseModel):
    model_config = _FORBID

    version: int = VIEWS_VERSION
    since: _dt.datetime | None = None
    until: _dt.datetime
    shadow_source: str = Field(..., description="What priced the not-traded shadows")
    closed: list[ClosedCounterfactual] = Field(default_factory=list)
    not_traded: list[NotTradedCounterfactual] = Field(default_factory=list)
    alternatives: list[AlternativeCounterfactual] = Field(default_factory=list)
    realised_total: float = 0.0
    hold_total: float | None = Field(None, description="Sum over trades with a known shadow")
    realised_on_hold_known: float | None = Field(
        None, description="Realised over the same trades as hold_total"
    )
    not_traded_shadow_total: float | None = None


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def counterfactual_lines(rep: CounterfactualReport) -> list[str]:
    head = "counterfactual"
    head += f" since {rep.since:%Y-%m-%d}" if rep.since else " (all time)"
    out = [head, f"── closed trades ({len(rep.closed)}): realised vs hold-to-expiry vs no trade"]
    if not rep.closed:
        out.append("  none")
    for c in rep.closed:
        hold = (
            f"${c.hold_to_expiry_shadow_pnl:,.2f}"
            if c.hold_to_expiry_shadow_pnl is not None
            else "pending"
        )
        out.append(
            f"  {c.ticker} {c.proposal_hash[:12]} realised ${c.realised_pnl:,.2f} · "
            f"hold {hold} · no trade $0.00 · exit {c.exit_reason or 'n/a'}"
        )
    out.append(f"── not traded ({len(rep.not_traded)}; shadow: {rep.shadow_source})")
    if not rep.not_traded:
        out.append("  none")
    for n in rep.not_traded:
        px = f"${n.shadow_pnl:,.2f} (as of {n.as_of})" if n.shadow_pnl is not None else "n/a"
        out.append(f"  {n.ticker} {n.structure} {n.status} {n.proposal_hash[:12]}: {px}")
    out.append(f"── menu alternatives ({len(rep.alternatives)})")
    for a in rep.alternatives:
        by = f"{a.better_by:+,.2f}/contract" if a.better_by is not None else "n/a"
        out.append(f"  {a.ticker} {a.alternative} vs chosen {a.chosen}: {by}")
    return out
