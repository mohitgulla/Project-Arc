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

# Re-exported: Performance moved to arc.models (E8.7) so the read-only tower can use it
# without importing the Slack/persona stack.
from arc.models import Performance
from arc.slack import blocks as B
from arc.slack.blocks import Block, CardView
from arc.slack.personas import Persona

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.context.store import ContextEntry
    from arc.ingest.sources import CategoryMix
    from arc.models import Candidate
    from arc.personas.schemas import (
        BrokerPlan,
        QuantLeg,
        QuantOutput,
        QuantStructureOut,
        ReconcileOutput,
        ResearchOutput,
        RiskAssessment,
        RiskOutput,
    )
    from arc.sizing import SizingResult

__all__ = [
    "ExecutionResult",
    "Performance",
    "research_card",
    "quant_card",
    "regime_name",
    "risk_card",
    "scalp_card",
    "scalp_context_card",
    "structure_name",
]

_MULT = 100  # option contract multiplier
# Scalp rows are two blocks each (divider + section); 20 keeps a card with the
# head, a "+N more" line, the Rejected list, folded Session notes and the footer
# under Slack's 50-block cap.
_MAX_SCALP_ROWS = 20

# Human text for stable drop/reject reason keys (arc.ingest.scalp, arc.pipeline.steps).
_REASONS = {
    "schema": "invalid reply",
    "not_in_universe": "not in universe",
    "unknown_symbol": "unknown symbol",
    "illiquid": "failed liquidity screen",
    "over_new_ticker_cap": "over new-ticker cap",
    "excluded": "excluded by Research",
    "over_budget": "ranked, not structured (budget)",
    "skipped": "skipped by Quant",
    "not_structured": "no structure, no reason",
    "below_threshold": "below confidence threshold",
    "no_grounded_source": "no grounded source",
    "not_a_candidate": "not a Scalp candidate",
    "duplicate": "duplicate",
    "invalid_field": "invalid stance/structure",
    "over_limit": "over shortlist limit",
    "not_in_menu": "not in the scanner menu",
    "not_shortlisted": "not shortlisted",
    "unknown_structure": "structure Quant did not propose",
    "not_picked": "not ranked or excluded by Research",
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
# Scalp
# ---------------------------------------------------------------------------


def source_mix_line(mix: Sequence[tuple[str, int, int]]) -> str:
    """``WSJ 12 · CNBC 12 · EDGAR 24 (157 over budget)`` (D30)."""
    parts = []
    for label, read, over in mix:
        part = f"{B.esc(label)} {read}"
        if over:
            part += f" ({over} over budget)"
        parts.append(part)
    return " · ".join(parts)


def category_mix_lines(mix: Sequence[CategoryMix]) -> list[str]:
    """D47: one line per category, its share first, then its sources.

    ``*Market news* 33% · 12 read: WSJ 4 · CNBC 4 (3 over budget) · Nasdaq 4 (2 stale)``
    """
    out: list[str] = []
    for c in mix:
        parts = []
        for label, read, over, stale in c.sources:
            notes = [f"{over} over budget"] if over else []
            if stale:
                notes.append(f"{stale} stale")
            parts.append(f"{B.esc(label)} {read}" + (f" ({', '.join(notes)})" if notes else ""))
        out.append(
            f"*{B.esc(c.label)}* {round(c.share * 100)}% · {c.picked} read: " + " · ".join(parts)
        )
    return out


def scalp_card(
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
    reject_details: Mapping[str, str] | None = None,
    new_tickers: Sequence[str] = (),
    source_mix: Sequence[tuple[str, int, int]] = (),
    stories: int | None = None,
    category_mix: Sequence[CategoryMix] = (),
    filtered: Mapping[str, int] | None = None,
) -> CardView:
    """``[Scalp] Scan: 12 Sources → 3 Candidates``; one evidence line per candidate.

    No source links (owner, E5.5 review): the row carries the Scalp's one-line
    rationale and a source count; the URLs stay in the audit store. D28: non-seed
    tickers admitted by the liquidity screen are tagged ``new``; universe rejects
    (``illiquid`` etc.) are grouped by reason under Rejected with the failed checks.
    D30 (E4.5): a *Source mix* fact (docs read per source, over-budget counts) and
    the story count; each candidate shows how many distinct sources back it.
    D55: *filtered* (source label -> docs a feed's title filter closed since the last
    Scalp, never read) is one ``Filtered`` line under the source mix.
    """
    title = f"[Scalp] Scan: {_plural(docs, 'Source')} → {_plural(len(candidates), 'Candidate')}"
    n_rej = sum(rejected.values())
    new = set(new_tickers)
    blocks = _head(
        title,
        f"*{accepted}* accepted this run",
        (f"{stories} stor{'y' if stories == 1 else 'ies'}" if stories is not None else ""),
        f"{len(new)} new (screened)" if new else "",
        f"{n_rej} rejected",
        f":warning: {failed_batches} failed batch{'es' if failed_batches != 1 else ''}"
        if failed_batches
        else "",
    )
    # D30: which sources this run read (and what waited), before the candidate rows.
    # D47: grouped by category (share, then sources with read / over budget / stale).
    if category_mix:
        blocks.append(_section("Source mix", category_mix_lines(category_mix)))
    elif source_mix:
        blocks.append(_section("Source mix", [source_mix_line(source_mix)]))
    if filtered and sum(filtered.values()):
        line = " · ".join(f"{B.esc(k)} {n}" for k, n in filtered.items() if n)
        blocks.append(_section("Filtered (title filter, not read)", [line]))
    # E5.5b: one section per candidate with dividers (like Research's ranked
    # list), so each row folds on its own. Lines start at column 0: no indent.
    ranked = sorted(candidates, key=lambda c: -c.confidence)
    for c in ranked[:_MAX_SCALP_ROWS]:
        when = f" {c.catalyst_date:%b %d}" if c.catalyst_date else ""
        facts = (
            f"{c.stance.value} · {c.catalyst_type.value}{when} · {_pct(c.confidence)} confidence"
        )
        if c.corroboration is not None:
            facts += f" · {_plural(c.corroboration, 'source')}"
        tag = " · new, passed liquidity screen" if c.ticker in new else ""
        why = (rationales or {}).get(c.ticker, "").strip()
        lines = [f"*{B.esc(c.ticker)}*", facts + tag]
        if why:
            lines.append(B.esc(why))
        blocks.append(B.divider())
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": B.clip("\n".join(lines))}}
        )
    if len(ranked) > _MAX_SCALP_ROWS:
        rest = ", ".join(B.esc(c.ticker) for c in ranked[_MAX_SCALP_ROWS:])
        blocks.append(B.summary(B.clip(f"+{len(ranked) - _MAX_SCALP_ROWS} more: {rest}")))
    if not ranked:
        blocks.append(B.divider())
        blocks.append(_section("Candidates", ["none"]))
    items = [(t, reason) for reason, ts in (rejected_items or {}).items() for t in ts]
    rej_rows = _drops(rejected, items)
    # E5.7 failed-check details; E5.5b: column 0, no indent.
    for ticker, detail in (reject_details or {}).items():
        rej_rows.append(f"{B.esc(ticker)}: {B.esc(detail)}")
    rejected_block = _section("Rejected", rej_rows)
    if rejected_block is not None:
        blocks.append(B.divider())
        blocks.append(rejected_block)
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def scalp_context_card(
    entries: Sequence[ContextEntry],
    *,
    chain_run_id: str | None = None,
) -> CardView:
    """D36 thread item 1: the Scalp candidates Research read this loop.

    ``[Scalp] Context: 3 Candidates • run 2026-09-28 09:30ET``: the run time is
    the newest candidate entry's ``valid_from`` (the Scalp run that wrote it),
    one section per candidate (E5.5b layout), and the footer links the Scalp
    ``run`` that produced the newest entry plus the loop ``chain``. Built from
    the stored context entries only: no LLM, no re-scan.
    """
    from arc.slack.loop import slot_stamp

    cands = sorted(entries, key=lambda e: -float(e.payload.get("confidence") or 0.0))
    newest = max(entries, key=lambda e: e.valid_from) if entries else None
    when = f" • run {slot_stamp(newest.valid_from)}" if newest else ""
    title = f"[Scalp] Context: {_plural(len(cands), 'Candidate')}{when}"
    runs = sorted({e.run_id for e in entries if e.run_id})
    blocks = _head(
        title,
        "what Research read this loop",
        f"{_plural(len(runs), 'Scalp run')}" if len(runs) > 1 else "",
    )
    for e in cands[:_MAX_SCALP_ROWS]:
        p = e.payload
        facts = f"{p.get('stance', '?')} · {p.get('catalyst_type', '?')}"
        raw_date = p.get("catalyst_date")
        if raw_date:
            facts += f" {str(raw_date)[:10]}"
        conf = p.get("confidence")
        if conf is not None:
            facts += f" · {_pct(float(conf))} confidence"
        corr = p.get("corroboration")
        if corr is not None:
            facts += f" · {_plural(int(corr), 'source')}"
        facts += f" · as of {slot_stamp(e.valid_from)}"
        blocks.append(B.divider())
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": B.clip(f"*{B.esc(e.subject)}*\n{B.esc(facts)}")},
            }
        )
    if len(cands) > _MAX_SCALP_ROWS:
        rest = ", ".join(B.esc(e.subject) for e in cands[_MAX_SCALP_ROWS:])
        blocks.append(B.summary(B.clip(f"+{len(cands) - _MAX_SCALP_ROWS} more: {rest}")))
    if not cands:
        blocks.append(B.divider())
        blocks.append(_section("Candidates", ["none"]))
    return _finish(title, blocks, run_id=newest.run_id if newest else None, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


_REGIME_NAMES = {"risk_on": "Risk ON", "risk_off": "Risk OFF", "unknown": "Unknown"}


def regime_name(raw: str) -> str:
    """Regime display text: ``risk_on`` → ``Risk ON``, ``range_bound`` → ``Range Bound``."""
    key = raw.strip().lower().replace("-", "_").replace(" ", "_") or "unknown"
    return _REGIME_NAMES.get(key, _title_case(key))


def research_card(
    out: ResearchOutput,
    *,
    candidates: int,
    dropped: Sequence[tuple[str, str]] = (),
    funnel: Sequence[tuple[str, str, str]] = (),
    budget: int | None = None,
    evidence: Mapping[str, str] | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Research] Ranked: 12 / 30 • Market Risk ON``; a section per budgeted pick.

    E5.7: Research ranks every candidate it would trade. The first ``budget``
    (the Quant/Risk budget, ``pipeline_max_shortlist``) get full sections; the rest
    are listed under "Ranked, not structured". ``funnel`` is ``(ticker, key, reason)``
    for candidates Research excluded (with its reason) or left unranked.
    ``dropped`` is ``(ticker, reason_key)`` for invalid shortlist entries.
    ``evidence`` is ticker → a pre-escaped one-line summary of the upstream Scalp
    data (stance, catalyst, confidence, sources) shown under the thesis.
    """
    regime = regime_name(out.market_regime)
    ranked = sorted(out.shortlist, key=lambda i: i.rank)
    inside = ranked if budget is None else ranked[:budget]
    beyond = [] if budget is None else ranked[budget:]
    excluded = [(t, r) for t, k, r in funnel if k == "excluded"]
    unranked = [(t, k) for t, k, _ in funnel if k != "excluded"]
    # E5.9 (D33): explicit no-trade, market guard, portfolio view and suppressed ideas.
    guard = getattr(out, "market_guard", None)
    no_trade = getattr(out, "no_trade_reason", None)
    pview = getattr(out, "portfolio_view", None)
    checks = list(getattr(out, "thesis_checks", []) or [])
    suppressed = list(getattr(out, "suppressed", []) or [])
    if guard is not None and not guard.opens_allowed:
        title = f"[Research] No trade: market unclear • Market {regime}"
    elif not ranked and no_trade:
        title = f"[Research] No trade: {_title_case(no_trade)} • Market {regime}"
    else:
        title = f"[Research] Ranked: {len(ranked)} / {candidates} • Market {regime}"
    blocks = _head(
        title,
        f"*{len(ranked)}* ranked",
        f"{len(inside)} to Quant (budget {budget})" if beyond else "",
        f"{len(excluded)} excluded" if excluded else "",
        f"{len(dropped) + len(unranked)} dropped" if dropped or unranked else "",
        f"{len(suppressed)} suppressed (dedupe)" if suppressed else "",
        f"Portfolio {B.esc(_title_case(pview.verdict))}" if pview is not None else "",
    )
    if guard is not None:
        vix = f"VIX {guard.vix.value:.1f}" if guard.vix else "VIX n/a"
        reg = (
            f"SPY regime {B.esc(guard.regime)}"
            + (f" (stickiness {guard.regime_stickiness:.2f})" if guard.regime_stickiness else "")
            if guard.regime
            else ""
        )
        state = "clear" if guard.opens_allowed else "no new opens"
        blocks.append(
            _section(
                f"Market guard: {state}",
                [x for x in (vix, reg, *[B.esc(r) for r in guard.reasons]) if x],
            )
        )
    for item in inside:
        blocks.append(B.divider())
        fit = getattr(item, "portfolio_fit", None)
        meta = (
            f"Rank {item.rank} · {B.esc(item.stance.strip().capitalize())} · "
            f"{_pct(item.confidence)} confidence · "
            f"{B.esc(_title_case(item.suggested_structure_type))}"
            + (f" · Fit: {B.esc(_title_case(fit))}" if fit else "")
        )
        lines = [f"*{B.esc(item.ticker)}*", meta]
        if item.thesis.strip():
            lines.append(f"Thesis: {B.esc(item.thesis.strip())}")
        if item.regime_context.strip():
            lines.append(f"Regime: {B.esc(item.regime_context.strip())}")
        ev = (evidence or {}).get(item.ticker, "")
        if ev:
            lines.append(f"Evidence: {ev}")
        if item.evidence:
            lines.append("Research evidence: " + " · ".join(B.esc(e) for e in item.evidence))
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": B.clip("\n".join(lines))}}
        )
    if not ranked:
        why = (
            f"nothing worth trading today ({_title_case(no_trade)})"
            if no_trade
            else ("nothing worth trading today")
        )
        blocks.append(_section("Ranked", [why]))
    if pview is not None:
        blocks.append(B.divider())
        rows = [B.esc(pview.notes.strip())] if pview.notes.strip() else []
        rows += [
            f"• {B.esc(c.structure_id)}: {B.esc(_title_case(c.status))}"
            + (f" · {B.esc(_clip_line(c.reason))}" if c.reason.strip() else "")
            for c in checks
        ]
        blocks.append(_section(f"Portfolio: {_title_case(pview.verdict)}", rows or ["-"]))
    if suppressed:
        blocks.append(B.divider())
        blocks.append(
            _section(
                f"Suppressed by dedupe ({len(suppressed)})",
                [f"• {B.esc(_clip_line(x))}" for x in suppressed],
            )
        )
    if beyond:
        blocks.append(B.divider())
        blocks.append(
            _section(
                f"Ranked, not structured ({len(beyond)}, over the budget of {budget})",
                [
                    f"#{i.rank} *{B.esc(i.ticker)}* · {B.esc(i.stance.strip().capitalize())} · "
                    f"{_pct(i.confidence)}"
                    + (f" · {B.esc(_clip_line(i.thesis))}" if i.thesis.strip() else "")
                    for i in beyond
                ],
            )
        )
    if excluded:
        blocks.append(B.divider())
        blocks.append(
            _section(
                f"Excluded ({len(excluded)})",
                [f"• *{B.esc(t)}*: {B.esc(_clip_line(r))}" for t, r in excluded],
            )
        )
    items = [*dropped, *unranked]
    counts: dict[str, int] = {}
    for _, reason in items:
        counts[reason] = counts.get(reason, 0) + 1
    if items:
        blocks.append(B.divider())
    blocks.append(_section("Dropped", _drops(counts, items)))
    blocks.append(B.persona_section(Persona.RESEARCH, "Session notes", out.session_notes))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def _clip_line(text: str, limit: int = 140) -> str:
    """One line of at most *limit* chars (list rows in the funnel sections)."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


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
    not_structured: Sequence[str] = (),
    over_budget: Sequence[str] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78`` + legs per structure.

    E5.7: every budgeted ticker is accounted for: a structure, a skip with its reason
    (``out.skipped``), "no structure, no reason" (``not_structured``) or no chain.
    ``over_budget`` = ranked by Research beyond the Quant/Risk budget.
    """
    if out.structures:
        best = out.structures[0]
        more = f" +{len(out.structures) - 1} more" if len(out.structures) > 1 else ""
        title = (
            f"[Quant] Structures: {best.ticker} {structure_name(best)}{more} • "
            f"PoP {_pct(best.pop)} • EV {_money(best.ev_per_contract)}"
        )
    else:
        title = "[Quant] Structures: none chosen"
    n_drop = sum((dropped or {}).values()) + len(no_chain) + len(not_structured)
    skipped = [s for s in out.skipped if s.ticker not in set(no_chain)]
    blocks = _head(
        title,
        f"*{len(out.structures)}* chosen",
        f"{len(skipped)} skipped" if skipped else "",
        f"{n_drop} dropped" if n_drop else "",
        f"{len(over_budget)} over budget" if over_budget else "",
    )
    for s in out.structures:
        net = s.net_debit_credit
        word = "credit" if net < 0 else "debit"
        blocks.append(B.divider())
        blocks.append(
            _section(
                f"{B.esc(s.ticker)} {structure_name(s)} · {_expiry(s)}",
                [*_legs_lines(s.legs), ""],  # trailing blank line before the metrics
            )
        )
        g = s.greeks
        # E5.5b: every metric category ends with a blank line, so categories
        # don't run together in the grid or when Slack stacks fields on mobile.
        blocks.extend(
            B.facts(
                [
                    (label, value + "\n")
                    for label, value in [
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
                            f"Δ Delta {g.delta:+.1f} sh\nΓ Gamma {g.gamma:+.2f} sh\n"
                            f"ν Vega {_money(g.vega / 100, signed=True)} / vol pt\n"
                            f"Θ Theta {_money(g.theta, signed=True)} / day",
                        ),
                        ("Confidence", _pct(s.confidence)),
                    ]
                ]
            )
        )
        blocks.append(B.persona_section(Persona.QUANT, "Rationale", s.rationale))
    if skipped:
        blocks.append(
            _section(
                f"Skipped ({len(skipped)})",
                [f"• *{B.esc(s.ticker)}*: {B.esc(_clip_line(s.reason))}" for s in skipped],
            )
        )
    items = [
        *dropped_items,
        *((t, "no_chain") for t in no_chain),
        *((t, "not_structured") for t in not_structured),
        *((t, "over_budget") for t in over_budget),
    ]
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
    max_gain: Mapping[tuple[str, str], float | None] | None = None,
    cap_pct: float | None = None,
    dropped: Mapping[str, int] | None = None,
    dropped_items: Sequence[tuple[str, str]] = (),
    not_assessed: Sequence[str] = (),
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Risk] Review: SPY Moderate • 14 Contracts``; one section per assessment.

    ``sized`` is the deterministic D18 result per ``(ticker, structure_type)``
    (``min(suggestion, floor(cap × equity / max loss))``), so the card shows the size
    the proposal will actually carry next to Risk's advisory suggestion.
    """
    sized = sized or {}

    def size_of(a: RiskAssessment) -> str:
        r = sized.get((a.ticker, a.structure_type))
        if r is None:
            return f"Suggests {a.sizing_suggestion}"
        return _plural(r.contracts, "Contract") if r.trade else "No Trade"

    if out.assessments:
        a0 = out.assessments[0]
        more = f" +{len(out.assessments) - 1} more" if len(out.assessments) > 1 else ""
        rating = _title_case(a0.risk_rating)
        title = f"[Risk] Review: {a0.ticker} {rating}{more} • {size_of(a0)}"
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
            gain = (max_gain or {}).get((a.ticker, a.structure_type))
            gain_txt = _money(None if gain is None else gain * r.contracts)
            loss_txt = (
                f"Max gain {gain_txt}\nMax loss {_money(float(r.max_loss_total))}\n"
                f"{r.pct_equity:.2%} of equity at risk"
            )
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
                    ("Payoff (sized)" if r is not None and r.trade else "Max loss", loss_txt),
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
# Broker orders (E6.2 / E6.4; D56 E13.2)
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Outcome of one Broker order (E6.2). Prices are per share, debit > 0, credit < 0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["filled", "partially_filled", "cancelled", "rejected", "expired", "unconfirmed"]
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
    "unconfirmed": ":warning:",
}


def broker_card(
    plan: BrokerPlan,
    result: ExecutionResult | None = None,
    *,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``[Broker] Order: SPY Iron Condor • x3 • Limit -1.25`` (+ fill/cancel outcome)."""
    title = (
        f"[Broker] Order: {plan.ticker} {_title_case(plan.structure_type)} • "
        f"x{plan.contracts} • Limit {plan.initial_limit_price:+.2f}"
    )
    if result is not None:
        title += f" • {_title_case(result.status)}"
    attempts = 1 + len(plan.improvement_steps)
    blocks = _head(
        title,
        f"{plan.order_type.capitalize()} order",
        f"{attempts} attempts max",
        f"timeout {plan.timeout_seconds}s",
    )
    lines = [f"1. Start {plan.initial_limit_price:+.2f} (mid)"]
    for s in plan.improvement_steps:
        lines.append(
            f"{s.step_number + 1}. Step {s.step_number} {s.price:+.2f} (wait {s.wait_seconds}s)"
        )
    blocks.append(B.divider())
    blocks.append(_section("Order plan", lines))
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
    blocks.append(B.persona_section(Persona.BROKER, "Notes", plan.notes))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


# ---------------------------------------------------------------------------
# Broker reconcile (D56 E13.2)
# ---------------------------------------------------------------------------


def _day(raw: str) -> str:
    try:
        return f"{_dt.date.fromisoformat(raw[:10]):%b %d}"
    except ValueError:
        return raw[:20]


def _pnl(v: float | None, pct: float | None) -> str:
    if v is None:
        return "n/a"
    return _money0(v) + (f" ({pct:+.1%})" if pct is not None else "")


def reconcile_card(
    out: ReconcileOutput,
    *,
    performance: Performance | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
    ops_line: str | None = None,
) -> CardView:
    """``[Broker] Reconcile: Sep 28 • P&L +$312 (+0.3%)``; anomalies live in the body.

    E8.2a: *ops_line* (``Slots: research 71/75, … · missed 6 (list in tower Ops)``)
    is the day's routine slot coverage, shown in an ``Ops`` section.
    """
    perf = performance or Performance(day_pnl=out.daily_pnl)
    title = f"[Broker] Reconcile: {_day(out.journal_date)} • P&L {_pnl(perf.day_pnl, perf.day_pct)}"
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
    if ops_line:
        blocks.append(_section("Ops", [B.esc(ops_line)]))
    blocks.append(B.persona_section(Persona.BROKER, "Journal", out.journal_narrative))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)
