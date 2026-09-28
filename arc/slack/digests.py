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
        RiskAssessment,
        RiskOutput,
    )
    from arc.sizing import SizingResult

__all__ = [
    "ExecutionResult",
    "Performance",
    "auditor_card",
    "director_card",
    "investor_card",
    "quant_card",
    "regime_name",
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
    rationales: Mapping[str, str] | None = None,
    failed_batches: int = 0,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Scout] Scan: 12 sources → 3 candidates``; one evidence line per candidate.

    No source links (owner, E5.5 review): the row carries the Scout's one-line
    rationale and a source count; the URLs stay in the audit store.
    """
    title = f"[Scout] Scan: {_plural(docs, 'source')} → {_plural(len(candidates), 'candidate')}"
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
    for c in sorted(candidates, key=lambda c: -c.confidence):
        when = f" {c.catalyst_date:%b %d}" if c.catalyst_date else ""
        facts = (
            f"{c.stance.value} · {c.catalyst_type.value}{when} · {_pct(c.confidence)} confidence"
            f" · {_plural(len(c.sources), 'source')}"
        )
        why = (rationales or {}).get(c.ticker, "").strip()
        rows.append(f"• *{B.esc(c.ticker)}* {facts}" + (f"\n   {B.esc(why)}" if why else ""))
    blocks.append(B.divider())
    blocks.append(_section("Candidates", rows) or _section("Candidates", ["none"]))
    items = [(t, reason) for reason, ts in (rejected_items or {}).items() for t in ts]
    blocks.append(_section("Rejected", _drops(rejected, items)))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Director
# ---------------------------------------------------------------------------


_REGIME_NAMES = {"risk_on": "Risk ON", "risk_off": "Risk OFF", "unknown": "Unknown"}


def regime_name(raw: str) -> str:
    """Regime display text: ``risk_on`` → ``Risk ON``, ``range_bound`` → ``Range Bound``."""
    key = raw.strip().lower().replace("-", "_").replace(" ", "_") or "unknown"
    return _REGIME_NAMES.get(key, _title_case(key))


def director_card(
    out: DirectorOutput,
    *,
    candidates: int,
    dropped: Sequence[tuple[str, str]] = (),
    evidence: Mapping[str, str] | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Director] Ranked: 2 / 5 • Market Risk ON``; one section per ranked pick.

    ``dropped`` is ``(ticker, reason_key)`` for every candidate not kept.
    ``evidence`` is ticker → a pre-escaped one-line summary of the upstream Scout
    data (stance, catalyst, confidence, sources) shown under the thesis.
    """
    regime = regime_name(out.market_regime)
    title = f"[Director] Ranked: {len(out.shortlist)} / {candidates} • Market {regime}"
    blocks = _head(
        title,
        f"*{len(out.shortlist)}* ranked",
        f"{len(dropped)} dropped" if dropped else "",
    )
    for item in sorted(out.shortlist, key=lambda i: i.rank):
        blocks.append(B.divider())
        meta = (
            f"Rank {item.rank} · {B.esc(item.stance.strip().capitalize())} · "
            f"{_pct(item.confidence)} confidence · "
            f"{B.esc(_title_case(item.suggested_structure_type))}"
        )
        lines = [f"*{B.esc(item.ticker)}*", meta]
        if item.thesis.strip():
            lines.append(f"Thesis: {B.esc(item.thesis.strip())}")
        if item.regime_context.strip():
            lines.append(f"Regime: {B.esc(item.regime_context.strip())}")
        ev = (evidence or {}).get(item.ticker, "")
        if ev:
            lines.append(f"Evidence: {ev}")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": B.clip("\n".join(lines))}}
        )
    if not out.shortlist:
        blocks.append(_section("Ranked", ["nothing worth trading today"]))
    counts: dict[str, int] = {}
    for _, reason in dropped:
        counts[reason] = counts.get(reason, 0) + 1
    if dropped:
        blocks.append(B.divider())
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


def _legs_lines(legs: Sequence[QuantLeg]) -> list[str]:
    """Plain mrkdwn legs (D22: no code block): ``Short 1x 745P``; expiry only if legs differ."""
    multi_exp = len({leg.expiry for leg in legs}) > 1
    rows = []
    for leg in legs:
        kind = "P" if leg.option_type.lower().startswith("p") else "C"
        exp = ""
        if multi_exp:
            try:
                exp = f" {_dt.date.fromisoformat(leg.expiry):%b %d}"
            except ValueError:
                exp = f" {leg.expiry[:10]}"
        rows.append(
            B.esc(f"{leg.side.strip().capitalize()} {leg.ratio}x {leg.strike:g}{kind}{exp}")
        )
    return rows


def _risk_reward(s: QuantStructureOut) -> str:
    """Risk/Reward = max loss / max gain (owner convention)."""
    if s.max_gain is None or s.max_loss is None or not s.max_gain:
        return "n/a"
    return f"{s.max_loss / s.max_gain:.2f} : 1"


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
    blocks = _head(
        title,
        f"*{len(out.structures)}* chosen",
        f"{n_drop} dropped" if n_drop else "",
        "per contract, before sizing (Risk sizes; the proposal card shows the sized position)",
    )
    for s in out.structures:
        net = s.net_debit_credit
        word = "credit" if net < 0 else "debit"
        blocks.append(B.divider())
        blocks.append(
            _section(f"{B.esc(s.ticker)} {structure_name(s)} · {_expiry(s)}", _legs_lines(s.legs))
        )
        g = s.greeks
        blocks.extend(
            B.facts(
                [
                    (
                        "Entry (1 contract)",
                        f"{word.capitalize()} {abs(net):.2f}/sh\n"
                        f"${abs(net) * _MULT:,.0f} per contract",
                    ),
                    (
                        "Payoff",
                        f"Max gain {_money(s.max_gain)}\nMax loss {_money(s.max_loss)}\n"
                        f"Risk/Reward {_risk_reward(s)}",
                    ),
                    (
                        "Edge (hold to expiry)",
                        f"PoP {_pct(s.pop)}\nEV {_money(s.ev_per_contract)} per contract\n"
                        f"Cost {s.cost_bps:.0f} bps round trip",
                    ),
                    ("Breakevens", "\n".join(f"{b:.2f}" for b in s.breakevens) or "n/a"),
                    (
                        "Greeks (1 contract)",
                        f"Delta {g.delta:+.1f} sh\nGamma {g.gamma:+.2f} sh\n"
                        f"Vega {_money(g.vega / 100, signed=True)} / vol pt\n"
                        f"Theta {_money(g.theta, signed=True)} / day",
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
    sized: Mapping[tuple[str, str], SizingResult] | None = None,
    cap_pct: float | None = None,
    dropped: Mapping[str, int] | None = None,
    dropped_items: Sequence[tuple[str, str]] = (),
    not_assessed: Sequence[str] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Risk] Review: SPY moderate • 14 contracts``; one section per assessment.

    ``sized`` is the deterministic D18 result per ``(ticker, structure_type)``
    (``min(suggestion, floor(cap × equity / max loss))``), so the card shows the size
    the proposal will actually carry next to Risk's advisory suggestion.
    """
    sized = sized or {}

    def size_of(a: RiskAssessment) -> str:
        r = sized.get((a.ticker, a.structure_type))
        if r is None:
            return f"suggests {a.sizing_suggestion}"
        return _plural(r.contracts, "contract") if r.trade else "no trade"

    if out.assessments:
        a0 = out.assessments[0]
        more = f" +{len(out.assessments) - 1} more" if len(out.assessments) > 1 else ""
        title = f"[Risk] Review: {a0.ticker} {a0.risk_rating}{more} • {size_of(a0)}"
    else:
        title = "[Risk] Review: nothing assessed"
    warn = sum(a.concentration_warning for a in out.assessments)
    blocks = _head(
        title,
        f"*{len(out.assessments)}* assessed",
        f":warning: {warn} concentration" if warn else "",
        f"{len(not_assessed)} not assessed" if not_assessed else "",
    )
    cap = f"{cap_pct:.0%} cap" if cap_pct is not None else "equity cap"
    for a in out.assessments:
        blocks.append(B.divider())
        r = sized.get((a.ticker, a.structure_type))
        if r is None:
            size_txt = f"Suggested {a.sizing_suggestion} (advisory)"
            loss_txt = f"{a.max_loss_pct_equity:.1%} of equity (Risk estimate)"
        elif r.trade:
            capped = f" (capped by {cap})" if r.contracts < r.suggestion else ""
            size_txt = f"Suggested {r.suggestion}\nSized {r.contracts}{capped}"
            loss_txt = f"{_money(float(r.max_loss_total))}\n{r.pct_equity:.2%} of equity"
        else:
            size_txt = f"Suggested {r.suggestion}\nNo trade: {B.esc(r.reason or '')}"
            loss_txt = "n/a"
        blocks.extend(
            B.facts(
                [
                    (
                        f"{B.esc(a.ticker)} {_title_case(a.structure_type)}",
                        f"Rating *{B.esc(a.risk_rating.strip().capitalize())}*",
                    ),
                    ("Size", size_txt),
                    ("Max loss (sized)" if r is not None else "Max loss", loss_txt),
                    (
                        "Concentration",
                        ":warning: over limit" if a.concentration_warning else "OK",
                    ),
                ]
            )
        )
        lines = [
            f"Greek budget: {B.esc(a.greek_budget_impact.strip())}",
            f"Calendar: {B.esc(a.calendar_concerns.strip())}",
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
    attempts = 1 + len(plan.improvement_steps)
    blocks = _head(
        title,
        f"{plan.order_type.capitalize()} order",
        f"{attempts} attempts max",
        f"timeout {plan.timeout_seconds}s",
    )
    lines = [f"1. {plan.initial_limit_price:+.2f} at mid"]
    prev_wait = plan.improvement_steps[0].wait_seconds if plan.improvement_steps else None
    for s in plan.improvement_steps:
        lines.append(f"{s.step_number + 1}. {s.price:+.2f}")
    waits = {s.wait_seconds for s in plan.improvement_steps}
    wait = f"{prev_wait}s" if len(waits) == 1 and prev_wait is not None else "per step"
    rule = (
        f"Each attempt waits {wait}; if unfilled it is cancelled and the cancel is confirmed "
        "before the next limit is sent, so only one order is ever working. "
        "Unfilled after the last attempt: cancel and stop."
    )
    blocks.append(B.divider())
    blocks.append(_section("Order plan (same strikes, limit price steps)", [*lines, rule]))
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
        pairs.append(
            (
                "Filled on attempt",
                f"{result.steps_used + 1} of {attempts}"
                if result.filled_qty
                else f"none of {attempts}",
            )
        )
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


class Performance(BaseModel):
    """Account performance for the Auditor digest (E6.3 pnl_snapshots). $ and fractions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    day_pnl: float
    day_pct: float | None = None
    mtd_pnl: float | None = None
    mtd_pct: float | None = None
    ytd_pnl: float | None = None
    ytd_pct: float | None = None
    equity: float | None = None


def _pnl(v: float | None, pct: float | None) -> str:
    if v is None:
        return "n/a"
    return _money0(v) + (f" ({pct:+.1%})" if pct is not None else "")


def auditor_card(
    out: AuditorOutput,
    *,
    performance: Performance | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Auditor] Journal: Sep 28 • P&L +$312 (+0.3%)``; anomalies live in the body."""
    perf = performance or Performance(day_pnl=out.daily_pnl)
    title = f"[Auditor] Journal: {_day(out.journal_date)} • P&L {_pnl(perf.day_pnl, perf.day_pct)}"
    recon = out.reconciliation_status.strip() or "pending"
    icon = ":white_check_mark:" if recon == "clean" else ":warning:"
    n_anom = len(out.anomalies)
    blocks = _head(
        title,
        f"Reconciliation {icon} *{B.esc(recon.replace('_', ' '))}*",
        f"equity {_money0(perf.equity).lstrip('+')}" if perf.equity is not None else "",
    )
    blocks.extend(
        B.facts(
            [
                (
                    "Performance",
                    f"Day {_pnl(perf.day_pnl, perf.day_pct)}\n"
                    f"MTD {_pnl(perf.mtd_pnl, perf.mtd_pct)}\n"
                    f"YTD {_pnl(perf.ytd_pnl, perf.ytd_pct)}",
                ),
                (
                    "Positions",
                    f"Open {out.open_positions}\nClosed today {out.closed_today}\n"
                    f"Fills reviewed {out.fills_reviewed}",
                ),
            ]
        )
    )
    anomalies = [
        f"• *{B.esc(a.severity.capitalize())}* · {B.esc(_title_case(a.category).capitalize())}: "
        f"{B.esc(a.description)}"
        + (f" (orders {B.esc(', '.join(a.affected_orders))})" if a.affected_orders else "")
        for a in out.anomalies
    ]
    blocks.append(_section(f"Anomalies ({n_anom})", anomalies))
    lessons = [
        f"• *{B.esc(x.topic)}*: {B.esc(x.observation)}\n   → {B.esc(x.recommendation)}"
        for x in out.lessons
    ]
    blocks.append(_section("Lessons", lessons))
    blocks.append(B.persona_section(Persona.AUDITOR, "Journal", out.journal_narrative))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)
