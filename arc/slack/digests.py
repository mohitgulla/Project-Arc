"""Persona digest cards (E5.5, D22): one Block Kit layout for every persona post.

Each renderer turns one persona's run output into a :class:`~arc.slack.blocks.CardView`
using the shared grammar in :mod:`arc.slack.blocks` (title → summary → facts →
persona-attributed reasoning → audit footer). Titles follow
``[Persona] <What>: <subject> • <fact> • <fact>``.

Pure functions: models in, blocks out. No I/O, no clock, no DB. Every piece of
persona text is escaped (:func:`~arc.slack.blocks.esc`) and clipped under
Slack's limits (3000 chars per section, 2000 per field, 10 fields per section,
50 blocks per message). The footer carries the ``run`` and ``chain`` ids so a
post links back to ``routine_runs`` and the decision journal (E7.4).

The heartbeat posts ``CardView.blocks``; the notification fallback text stays
the old one-line summary (:meth:`arc.routines.heartbeat.Heartbeats.summary`).
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from arc.slack import blocks as B
from arc.slack.blocks import Block, CardView
from arc.slack.personas import Persona

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.models import Candidate
    from arc.personas.schemas import (
        AuditorOutput,
        DirectorOutput,
        InvestorPlan,
        QuantLeg,
        QuantOutput,
        QuantStructureOut,
        RiskOutput,
    )

__all__ = [
    "ExecutionResult",
    "auditor_card",
    "director_card",
    "investor_card",
    "quant_card",
    "risk_card",
    "scout_card",
    "structure_name",
]

_MULT = 100  # option contract multiplier

# Human text for stable drop/reject reason keys (arc.ingest.scout, arc.pipeline.steps).
_REASONS = {
    "schema": "invalid reply",
    "not_in_universe": "not in universe",
    "below_threshold": "below confidence threshold",
    "no_grounded_source": "no grounded source",
    "not_a_candidate": "not a Scout candidate",
    "duplicate": "duplicate",
    "invalid_field": "invalid stance/structure",
    "over_limit": "over shortlist limit",
    "not_in_menu": "not in the scanner menu",
    "not_shortlisted": "not shortlisted",
    "unknown_structure": "structure Quant did not propose",
    "not_picked": "not picked by Director",
    "no_chain": "no tradable chain",
    "not_assessed": "not assessed by Risk",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _reason(key: str) -> str:
    return _REASONS.get(key, key.replace("_", " "))


def _money(v: float | None, *, signed: bool = False) -> str:
    if v is None:
        return "unbounded"
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${abs(v):,.2f}"


def _money0(v: float) -> str:
    sign = "-" if v < 0 else "+"
    return f"{sign}${abs(v):,.0f}"


def _pct(v: float) -> str:
    return f"{v:.0%}"


def _title_case(raw: str) -> str:
    return " ".join(w.capitalize() for w in raw.replace("_", " ").split()) or "Custom"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _link(url: str) -> str:
    """A source as a Slack link labelled by host; non-http text is escaped verbatim."""
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return B.esc(url)
    target = B.esc(url.strip()).replace("|", "%7C")
    host = parts.netloc.removeprefix("www.")
    return f"<{target}|{B.esc(host)}>"


def _section(title: str, lines: Sequence[str]) -> Block | None:
    """A titled section of pre-escaped mrkdwn lines, clipped to Slack's limit."""
    if not lines:
        return None
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": B.clip(f"*{title}*\n" + "\n".join(lines))},
    }


def _drops(counts: Mapping[str, int], items: Sequence[tuple[str, str]] = ()) -> list[str]:
    """Rejects grouped by reason: ``• not in universe (2): AAPL, TSLA``."""
    by_reason: dict[str, list[str]] = {}
    for subject, reason in items:
        by_reason.setdefault(reason, []).append(subject)
    keys = list(dict.fromkeys([*counts, *by_reason]))
    out = []
    for key in keys:
        names = by_reason.get(key, [])
        n = max(counts.get(key, 0), len(names))
        tail = f": {B.esc(', '.join(names))}" if names else ""
        out.append(f"• {_reason(key)} ({n}){tail}")
    return out


def _finish(
    title: str, blocks: list[Block | None], *, run_id: str | None, chain: str | None
) -> CardView:
    """Drop empty blocks, cap at Slack's 50, and end with the audit footer."""
    kept = [b for b in blocks if b is not None][: B.MAX_BLOCKS - 1]
    kept.append(B.footer(run=run_id, chain=chain))
    # The header is plain_text (Slack never parses mentions there); ``text`` is
    # mrkdwn wherever it is used as a fallback, so it is escaped.
    return CardView(text=B.esc(title[: B.HEADER_MAX]), blocks=kept)


def _head(title: str, *summary: str) -> list[Block | None]:
    return [B.header(title), B.summary(*summary) if any(summary) else None]


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------


def scout_card(
    *,
    docs: int,
    accepted: int,
    candidates: Sequence[Candidate],
    rejected: Mapping[str, int],
    rejected_items: Mapping[str, Sequence[str]] | None = None,
    failed_batches: int = 0,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Scout] Scan: 12 docs → 3 candidates`` with one row per candidate."""
    title = f"[Scout] Scan: {_plural(docs, 'doc')} → {_plural(len(candidates), 'candidate')}"
    n_rej = sum(rejected.values())
    blocks = _head(
        title,
        f"*{accepted}* accepted this run",
        f"{n_rej} rejected",
        f":warning: {failed_batches} failed batch{'es' if failed_batches != 1 else ''}"
        if failed_batches
        else "",
    )
    rows = []
    for c in candidates:
        when = f" {c.catalyst_date:%b %d}" if c.catalyst_date else ""
        links = " ".join(_link(s) for s in c.sources[:3])
        more = f" +{len(c.sources) - 3}" if len(c.sources) > 3 else ""
        rows.append(
            f"• *{B.esc(c.ticker)}* {c.stance.value} · {c.catalyst_type.value}{when} · "
            f"conf {_pct(c.confidence)} · {links}{more}"
        )
    blocks.append(B.divider())
    blocks.append(_section("Candidates today", rows) or _section("Candidates today", ["none"]))
    items = [(t, reason) for reason, ts in (rejected_items or {}).items() for t in ts]
    blocks.append(_section("Rejected", _drops(rejected, items)))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Director
# ---------------------------------------------------------------------------


def director_card(
    out: DirectorOutput,
    *,
    candidates: int,
    dropped: Sequence[tuple[str, str]] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Director] Shortlist: 2 of 5 • market risk_on``; one section per pick.

    ``dropped`` is ``(ticker, reason_key)`` for every candidate not kept.
    """
    regime = out.market_regime.strip() or "unknown"
    title = f"[Director] Shortlist: {len(out.shortlist)} of {candidates} • market {regime}"
    blocks = _head(
        title,
        f"*{len(out.shortlist)}* picked",
        f"{len(dropped)} dropped",
        f"market *{B.esc(regime)}*",
    )
    blocks.append(B.divider())
    for item in out.shortlist:
        meta = (
            f"rank {item.rank} · {B.esc(item.stance)} · confidence {_pct(item.confidence)} · "
            f"{B.esc(_title_case(item.suggested_structure_type))}"
        )
        body = f"{meta}\n{B.esc(item.thesis.strip())}"
        if item.regime_context.strip():
            body += f"\n_Regime:_ {B.esc(item.regime_context.strip())}"
        blocks.append(B.persona_section(Persona.DIRECTOR, B.esc(item.ticker), body, escape=False))
    if not out.shortlist:
        blocks.append(_section("Shortlist", ["nothing worth trading today"]))
    counts: dict[str, int] = {}
    for _, reason in dropped:
        counts[reason] = counts.get(reason, 0) + 1
    blocks.append(_section("Dropped", _drops(counts, dropped)))
    blocks.append(B.persona_section(Persona.DIRECTOR, "Session notes", out.session_notes))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Quant
# ---------------------------------------------------------------------------


def structure_name(s: QuantStructureOut) -> str:
    """Display name, e.g. ``Iron Condor``, ``Put Credit Spread``, ``Long Call``."""
    stype = s.structure_type.strip().lower()
    if stype == "vertical_spread" and s.legs:
        side = "Put" if s.legs[0].option_type.lower().startswith("p") else "Call"
        return f"{side} {'Credit' if s.net_debit_credit < 0 else 'Debit'} Spread"
    if stype in {"iron_condor", "long_call", "long_put"}:
        return _title_case(stype)
    return "Custom"


def _legs_table(legs: Sequence[QuantLeg]) -> str:
    multi_exp = len({leg.expiry for leg in legs}) > 1
    rows = []
    for leg in legs:
        kind = "P" if leg.option_type.lower().startswith("p") else "C"
        exp = ""
        if multi_exp:
            try:
                exp = f"{_dt.date.fromisoformat(leg.expiry):%b %d}  "
            except ValueError:
                exp = f"{leg.expiry[:10]}  "
        rows.append(f"{leg.side.upper():<5}  {leg.ratio}x  {exp}{f'{leg.strike:g}{kind}':>8}")
    return "```" + B.esc("\n".join(rows)) + "```"


def _expiry(s: QuantStructureOut) -> str:
    try:
        exp = _dt.date.fromisoformat(s.legs[0].expiry)
    except (ValueError, IndexError):
        return f"{s.dte} DTE"
    return f"{exp:%b %d} ({s.dte} DTE)"


def quant_card(
    out: QuantOutput,
    *,
    dropped: Mapping[str, int] | None = None,
    dropped_items: Sequence[tuple[str, str]] = (),
    no_chain: Sequence[str] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78`` + legs per structure."""
    if out.structures:
        best = out.structures[0]
        more = f" +{len(out.structures) - 1} more" if len(out.structures) > 1 else ""
        title = (
            f"[Quant] Structures: {best.ticker} {structure_name(best)}{more} • "
            f"PoP {_pct(best.pop)} • EV {_money(best.ev_per_contract)}"
        )
    else:
        title = "[Quant] Structures: none chosen"
    n_drop = sum((dropped or {}).values()) + len(no_chain)
    blocks = _head(title, f"*{len(out.structures)}* chosen", f"{n_drop} dropped")
    for s in out.structures:
        net = s.net_debit_credit
        word = "credit" if net < 0 else "debit"
        blocks.append(B.divider())
        blocks.append(
            _section(f"{B.esc(s.ticker)} {structure_name(s)} · {_expiry(s)}", [_legs_table(s.legs)])
        )
        blocks.extend(
            B.facts(
                [
                    ("Net", f"{word.capitalize()} {abs(net):.2f}/sh (${abs(net) * _MULT:,.0f})"),
                    ("Payoff", f"Max gain {_money(s.max_gain)}\nMax loss {_money(s.max_loss)}"),
                    (
                        "Edge",
                        f"PoP {_pct(s.pop)}\nEV {_money(s.ev_per_contract)}/contract\n"
                        f"Cost {s.cost_bps:.0f} bps",
                    ),
                    (
                        "Breakevens",
                        " / ".join(f"{b:.2f}" for b in s.breakevens) or "n/a",
                    ),
                    (
                        "Greeks (1 lot)",
                        f"Δ {s.greeks.delta:+.1f} · Γ {s.greeks.gamma:+.2f}\n"
                        f"ν {s.greeks.vega / 100:+.2f} $/vol pt · Θ {s.greeks.theta:+.2f} $/day",
                    ),
                    ("Confidence", _pct(s.confidence)),
                ]
            )
        )
        blocks.append(B.persona_section(Persona.QUANT, "Rationale", s.rationale))
    items = [*dropped_items, *((t, "no_chain") for t in no_chain)]
    counts = dict(dropped or {})
    if no_chain:
        counts["no_chain"] = len(no_chain)
    blocks.append(_section("Dropped", _drops(counts, items)))
    blocks.append(B.persona_section(Persona.QUANT, "Analysis", out.analysis_notes))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


def risk_card(
    out: RiskOutput,
    *,
    dropped: Mapping[str, int] | None = None,
    dropped_items: Sequence[tuple[str, str]] = (),
    not_assessed: Sequence[str] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Risk] Review: SPY moderate • suggests 20``; one section per assessment."""
    if out.assessments:
        a0 = out.assessments[0]
        more = f" +{len(out.assessments) - 1} more" if len(out.assessments) > 1 else ""
        title = (
            f"[Risk] Review: {a0.ticker} {a0.risk_rating}{more} • suggests {a0.sizing_suggestion}"
        )
    else:
        title = "[Risk] Review: nothing assessed"
    warn = sum(a.concentration_warning for a in out.assessments)
    blocks = _head(
        title,
        f"*{len(out.assessments)}* assessed",
        f":warning: {warn} concentration" if warn else "",
        f"{len(not_assessed)} not assessed" if not_assessed else "",
    )
    for a in out.assessments:
        blocks.append(B.divider())
        blocks.extend(
            B.facts(
                [
                    (
                        f"{B.esc(a.ticker)} {_title_case(a.structure_type)}",
                        f"Rating *{B.esc(a.risk_rating)}*",
                    ),
                    ("Suggested size", f"{a.sizing_suggestion} contract(s) (advisory)"),
                    ("Max loss", f"{a.max_loss_pct_equity:.1%} of equity"),
                    (
                        "Concentration",
                        ":warning: over limit" if a.concentration_warning else "ok",
                    ),
                ]
            )
        )
        lines = [
            f"• _Greek budget:_ {B.esc(a.greek_budget_impact.strip())}",
            f"• _Calendar:_ {B.esc(a.calendar_concerns.strip())}",
        ]
        if a.narrative.strip():
            lines.append(B.esc(a.narrative.strip()))
        blocks.append(
            B.persona_section(
                Persona.RISK, f"{B.esc(a.ticker)} review", "\n".join(lines), escape=False
            )
        )
    blocks.append(B.divider())
    blocks.append(B.persona_section(Persona.RISK, "Portfolio", out.portfolio_summary))
    blocks.append(B.persona_section(Persona.RISK, "Advisory", out.advisory_notes))
    items = [*dropped_items, *((s, "not_assessed") for s in not_assessed)]
    counts = dict(dropped or {})
    blocks.append(_section("Dropped / missing", _drops(counts, items)))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Investor (E6.2 / E6.4)
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Outcome of one Investor order (E6.2). Prices are per share, debit > 0, credit < 0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["filled", "partially_filled", "cancelled", "rejected", "expired"]
    filled_qty: int = Field(0, ge=0)
    fill_price: float | None = None
    mid_at_submit: float | None = None
    steps_used: int = Field(0, ge=0)
    detail: str = ""

    @property
    def slippage(self) -> float | None:
        """Per-share cost vs mid (positive = worse than mid, for debits and credits alike)."""
        if self.fill_price is None or self.mid_at_submit is None:
            return None
        return self.fill_price - self.mid_at_submit


_STATUS_ICON = {
    "filled": ":white_check_mark:",
    "partially_filled": ":large_orange_circle:",
    "cancelled": ":x:",
    "rejected": ":no_entry:",
    "expired": ":hourglass:",
}


def investor_card(
    plan: InvestorPlan,
    result: ExecutionResult | None = None,
    *,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Investor] Order: SPY Iron Condor • x3 • limit -1.25`` (+ fill/cancel outcome)."""
    title = (
        f"[Investor] Order: {plan.ticker} {_title_case(plan.structure_type)} • "
        f"x{plan.contracts} • limit {plan.initial_limit_price:+.2f}"
    )
    if result is not None:
        title += f" • {result.status.replace('_', ' ')}"
    blocks = _head(
        title,
        f"{plan.order_type} order",
        _plural(len(plan.improvement_steps), "improvement step"),
        f"timeout {plan.timeout_seconds}s",
    )
    steps = [
        f"• step {s.step_number}: {s.price:+.2f} (wait {s.wait_seconds}s)"
        for s in plan.improvement_steps
    ]
    blocks.append(B.divider())
    blocks.append(
        _section("Order plan", [f"• start {plan.initial_limit_price:+.2f} (mid)", *steps])
    )
    if result is not None:
        pairs = [
            ("Result", f"{_STATUS_ICON[result.status]} {result.status.replace('_', ' ')}"),
            ("Filled", f"{result.filled_qty} of {plan.contracts}"),
        ]
        if result.fill_price is not None:
            pairs.append(("Fill price", f"{result.fill_price:+.2f}"))
        slip = result.slippage
        if slip is not None:
            usd = slip * _MULT * max(result.filled_qty, 1)
            pairs.append(
                (
                    "Slippage vs mid",
                    f"{slip:+.2f}/sh ({_money(usd, signed=True)} total, + = cost)",
                )
            )
        pairs.append(("Steps used", f"{result.steps_used} of {len(plan.improvement_steps)}"))
        blocks.extend(B.facts(pairs))
        if result.detail.strip():
            blocks.append(B.summary(B.clip(B.esc(result.detail.strip()))))
    blocks.append(B.persona_section(Persona.INVESTOR, "Notes", plan.notes))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


def _day(raw: str) -> str:
    try:
        return f"{_dt.date.fromisoformat(raw[:10]):%b %d}"
    except ValueError:
        return raw[:20]


def auditor_card(
    out: AuditorOutput,
    *,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Auditor] Journal: Sep 28 • P&L +$312 • 1 anomaly``."""
    n_anom = len(out.anomalies)
    title = (
        f"[Auditor] Journal: {_day(out.journal_date)} • P&L {_money0(out.daily_pnl)} • "
        f"{n_anom} {'anomaly' if n_anom == 1 else 'anomalies'}"
    )
    recon = out.reconciliation_status.strip() or "pending"
    icon = ":white_check_mark:" if recon == "clean" else ":warning:"
    blocks = _head(title, f"reconciliation {icon} *{B.esc(recon)}*")
    blocks.extend(
        B.facts(
            [
                ("Daily P&L", _money(out.daily_pnl, signed=True)),
                ("Open positions", str(out.open_positions)),
                ("Closed today", str(out.closed_today)),
                ("Fills reviewed", str(out.fills_reviewed)),
            ]
        )
    )
    anomalies = [
        f"• *{B.esc(a.severity)}* {B.esc(a.category)}: {B.esc(a.description)}"
        + (f" (orders {B.esc(', '.join(a.affected_orders))})" if a.affected_orders else "")
        for a in out.anomalies
    ]
    blocks.append(_section("Anomalies", anomalies))
    lessons = [
        f"• *{B.esc(x.topic)}*: {B.esc(x.observation)} → _{B.esc(x.recommendation)}_"
        for x in out.lessons
    ]
    blocks.append(_section("Lessons", lessons))
    blocks.append(B.persona_section(Persona.AUDITOR, "Journal", out.journal_narrative))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)
