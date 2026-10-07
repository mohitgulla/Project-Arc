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
from collections import Counter
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

# Re-exported: Performance moved to arc.models (E8.7) so the read-only tower can use it
# without importing the Slack/persona stack.
from arc.models import Performance
from arc.slack import blocks as B
from arc.slack.blocks import Block, CardView
from arc.slack.personas import Persona, persona_label

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.context.kinds import ScoutReadPayload
    from arc.context.store import ContextEntry
    from arc.ingest.scalp import ScalpMention
    from arc.ingest.sources import CategoryMix
    from arc.models import Candidate
    from arc.personas.schemas import (
        BrokerPlan,
        ExitWatchItem,
        QuantLeg,
        QuantOutput,
        QuantStructureOut,
        ReconcileOutput,
        ResearchOutput,
        RiskAssessment,
        RiskExitVerdict,
        RiskOutput,
    )
    from arc.positions.exit_case import ExitCase
    from arc.sizing import SizingResult

__all__ = [
    "ExecutionResult",
    "Performance",
    "research_card",
    "quant_card",
    "exits_mandatory_summary",
    "quant_exit_card",
    "regime_name",
    "risk_card",
    "risk_exit_card",
    "scalp_card",
    "scalp_context_card",
    "scout_card",
    "structure_name",
]

_MULT = 100  # option contract multiplier
# E13.13 (D56): emoji persona labels on every card title (``🧠 [Research] Ranked: …``).
_SCOUT = persona_label(Persona.SCOUT)
_SCALP = persona_label(Persona.SCALP)
_RESEARCH = persona_label(Persona.RESEARCH)
_QUANT = persona_label(Persona.QUANT)
_RISK = persona_label(Persona.RISK)
_BROKER = persona_label(Persona.BROKER)
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
    "not_in_tier": "outside the universe (mentioned only)",
    "excluded": "excluded by Research",
    "over_budget": "ranked, not structured (budget)",
    "skipped": "skipped by Quant",
    "not_structured": "no structure, no reason",
    "below_threshold": "below confidence threshold",
    "no_grounded_source": "no grounded source",
    "not_a_candidate": "not a Scalp candidate",
    "over_scout_only_cap": "over the Scout-only cap",
    "over_prompt_budget": "cut for the prompt budget",
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
    mentions: Sequence[ScalpMention | str] = (),
    tape_line: str = "",
) -> CardView:
    """``⚡ [Scalp] Scan: 12 Sources → 3 Candidates``; one evidence line per candidate.

    No source links (owner, E5.5 review): the row carries the Scalp's one-line
    rationale and a source count; the URLs stay in the audit store. D28: non-seed
    tickers admitted by the liquidity screen are tagged ``new``; universe rejects
    (``illiquid`` etc.) are grouped by reason under Rejected with the failed checks.
    D30 (E4.5): a *Source mix* fact (docs read per source, over-budget counts) and
    the story count; each candidate shows how many distinct sources back it.
    D55: *filtered* (source label -> docs a feed's title filter closed since the last
    Scalp, never read) is one ``Filtered`` line under the source mix.
    D56 (E13.4): *mentions* (ideas for names in no tier, ``not_in_tier``) are listed
    under *Outside the universe*, never as candidates, and not repeated under Rejected.
    E13.10: *tape_line* (``Options tape: VIX 17.6 · 9D/30D 0.94 · n tickers``, flag
    on only) sits under the source mix; mentions render ``Outside the universe (n):
    X (bullish), Y (bearish)``.
    """
    title = f"{_SCALP} Scan: {_plural(docs, 'Source')} → {_plural(len(candidates), 'Candidate')}"
    if mentions:  # D56: listed once, under Outside the universe
        rejected = {k: n for k, n in rejected.items() if k != "not_in_tier"}
        rejected_items = {k: v for k, v in (rejected_items or {}).items() if k != "not_in_tier"}
    n_rej = sum(rejected.values())
    new = set(new_tickers)
    blocks = _head(
        title,
        f"*{accepted}* accepted this run",
        (f"{stories} stor{'y' if stories == 1 else 'ies'}" if stories is not None else ""),
        f"{len(new)} new (screened)" if new else "",
        f"{n_rej} rejected",
        f"{len(mentions)} outside the universe" if mentions else "",
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
    if tape_line:
        blocks.append(B.summary(B.esc(tape_line)))
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
    if mentions:
        named = [
            B.esc(m) if isinstance(m, str) else f"{B.esc(m.ticker)} ({m.stance.value})"
            for m in mentions
        ]
        blocks.append(B.divider())
        blocks.append(  # E13.13: ``*Outside the universe (n):* X (bullish), Y (bearish)``
            B.sections(
                [(f"Outside the universe ({len(mentions)})", ", ".join(named))], escape=False
            )
        )
        blocks.append(B.summary("mentioned only, never admitted (D56)"))
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


def scout_card(
    *,
    read: ScoutReadPayload,
    max_discovery: int,
    min_discovery_alert: int,
    candidates: int,
    dropped_calls: Mapping[str, str] | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``🔭 [Scout] Daily read: Discovery n/20`` (E13.7; E13.13 bold sections).

    *read* is the run's :class:`~arc.context.kinds.ScoutReadPayload`: the sections
    verbatim (``*Regime:*`` · ``*Options sentiment:*`` · ``*Themes:*`` ·
    ``*Discovery (n/20):*`` · ``*Risks:*``, no second summary), a ``Discovery: n/N``
    fact, the code-counted inputs and a
    one-line under-fill notice below ``funnel.scout.min_discovery_alert``.
    """
    fill = int(read.discovery_fill)
    title = f"{_SCOUT} Daily read: Discovery {fill}/{max_discovery}"
    inp = read.inputs
    briefs = (
        f"briefs macro {inp.youtube_macro.present}/{inp.youtube_macro.configured} · "
        f"micro {inp.youtube_micro.present}/{inp.youtube_micro.configured}"
    )
    blocks = _head(
        title,
        f"*Discovery: {fill}/{max_discovery}*",
        f"{_plural(len(read.ticker_calls), 'ticker call')}",
        f"{_plural(candidates, 'candidate')}",
        briefs,
    )
    if fill < min_discovery_alert:
        blocks.append(
            _section(
                "Under-filled",
                [f":warning: discovery {fill} < {min_discovery_alert} (coverage:scout)"],
            )
        )
    # E13.13: the scout_read sections verbatim, bold labels in the fixed note order.
    blocks.append(
        B.sections(
            [
                ("Regime", read.regime),
                ("Options sentiment", read.options_sentiment),
                ("Themes", " · ".join(read.themes)),
                (
                    f"Discovery ({fill}/{max_discovery})",
                    ", ".join(read.discovery) or "none",
                ),
                ("Risks", " · ".join(read.risks)),
            ]
        )
    )
    calls = [
        f"*{B.esc(c.ticker)}* {c.stance.value} · {_pct(c.confidence)} · {c.horizon} · "
        f"{B.esc(', '.join(o.removeprefix('youtube:') for o in c.origins))}"
        for c in read.ticker_calls[:_MAX_SCALP_ROWS]
    ]
    if calls:
        blocks.append(_section("Ticker calls", [B.clip("\n".join(calls))]))
    out = [f"{B.esc(t)}: {B.esc(r)}" for t, r in (read.screened_out or {}).items()]
    out += [f"{B.esc(t)}: {B.esc(r)}" for t, r in (dropped_calls or {}).items()]
    if out:
        blocks.append(_section("Screened out", [B.clip("\n".join(out))]))
    missing = [
        *(f"{m} (macro)" for m in inp.youtube_macro.missing),
        *(f"{m} (micro)" for m in inp.youtube_micro.missing),
        *(k for k in ("options_daily", "vx_curve", "vol_term") if getattr(inp, k) is None),
    ]
    if missing:
        blocks.append(B.summary(B.clip("No fresh input: " + ", ".join(B.esc(m) for m in missing))))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def scalp_context_card(
    entries: Sequence[ContextEntry],
    *,
    chain_run_id: str | None = None,
) -> CardView:
    """D36 thread item 1: the Scalp candidates Research read this loop.

    ``⚡ [Scalp] Context: 3 Candidates • run 2026-09-28 09:30ET``: the run time is
    the newest candidate entry's ``valid_from`` (the Scalp run that wrote it),
    one section per candidate (E5.5b layout), and the footer links the Scalp
    ``run`` that produced the newest entry plus the loop ``chain``. Built from
    the stored context entries only: no LLM, no re-scan.
    """
    from arc.slack.loop import slot_stamp

    cands = sorted(entries, key=lambda e: -float(e.payload.get("confidence") or 0.0))
    newest = max(entries, key=lambda e: e.valid_from) if entries else None
    when = f" • run {slot_stamp(newest.valid_from)}" if newest else ""
    title = f"{_SCALP} Context: {_plural(len(cands), 'Candidate')}{when}"
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
    exits: Sequence[ExitWatchItem] | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``🧠 [Research] Ranked: 12 / 30 • Market Risk ON``; *Opens* then *Exits* (E13.13).

    E5.7: Research ranks every candidate it would trade. The first ``budget``
    (the Quant/Risk budget, ``pipeline_max_shortlist``) get full sections under
    ``*Opens:*``; the rest are listed under "Ranked, not structured".
    ``funnel`` is ``(ticker, key, reason)`` for candidates Research excluded or left
    unranked. E13.13 (D56): excluded tickers are not listed on the card (they stay in
    the journal and the Tower trail); unranked ones are counted under Dropped.
    ``dropped`` is ``(ticker, reason_key)`` for invalid shortlist entries.
    ``evidence`` is ticker → a pre-escaped one-line summary of the upstream Scalp
    data (stance, catalyst, confidence, sources) shown under the thesis.
    ``exits`` is the stored exit watchlist (E13.17): one ``*Exits:*`` line per item,
    ``Exits: none open`` for an empty list, no section at all for ``None``
    (``personas.exit_path: deterministic``).
    """
    regime = regime_name(out.market_regime)
    ranked = sorted(out.shortlist, key=lambda i: i.rank)
    inside = ranked if budget is None else ranked[:budget]
    beyond = [] if budget is None else ranked[budget:]
    unranked = [(t, k) for t, k, _ in funnel if k != "excluded"]
    # E5.9 (D33): explicit no-trade, market guard, portfolio view and suppressed ideas.
    guard = getattr(out, "market_guard", None)
    no_trade = getattr(out, "no_trade_reason", None)
    pview = getattr(out, "portfolio_view", None)
    checks = list(getattr(out, "thesis_checks", []) or [])
    suppressed = list(getattr(out, "suppressed", []) or [])
    pool = getattr(out, "pool_counts", None)  # E13.8: None under the control
    pool_line = (
        f"Pool: {pool.get('scalp', 0) + pool.get('scout', 0) + pool.get('both', 0)} "
        f"(scalp {pool.get('scalp', 0)} · scout {pool.get('scout', 0)} · "
        f"both {pool.get('both', 0)})"
        if pool
        else ""
    )
    if guard is not None and not guard.opens_allowed:
        title = f"{_RESEARCH} No trade: market unclear • Market {regime}"
    elif not ranked and no_trade:
        title = f"{_RESEARCH} No trade: {_title_case(no_trade)} • Market {regime}"
    else:
        title = f"{_RESEARCH} Ranked: {len(ranked)} / {candidates} • Market {regime}"
    reviews = sum(w.action == "review" for w in exits or ())
    blocks = _head(
        title,
        f"*{len(ranked)}* ranked",
        pool_line,
        f"{len(inside)} to Quant (budget {budget})" if beyond else "",
        f"{len(dropped) + len(unranked)} dropped" if dropped or unranked else "",
        f"{len(suppressed)} suppressed (dedupe)" if suppressed else "",
        f"Portfolio {B.esc(_title_case(pview.verdict))}" if pview is not None else "",
        f"{len(exits)} watched · {reviews} review" if exits else "",
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
    # -- Opens ------------------------------------------------------------------
    if ranked:
        opens = [f"{len(inside)} ranked within budget"]
        if beyond:
            opens.append(f"{len(beyond)} beyond the budget of {budget}")
    else:
        opens = [
            f"nothing worth trading today ({_title_case(no_trade)})"
            if no_trade
            else "nothing worth trading today"
        ]
    if pool_line:
        opens.append(pool_line)
    blocks.append(B.divider())
    blocks.append(B.sections([("Opens", " · ".join(opens))], escape=False))
    for item in inside:
        fit = getattr(item, "portfolio_fit", None)
        meta = (
            f"Rank {item.rank} · {B.esc(item.stance.strip().capitalize())} · "
            f"{_pct(item.confidence)} confidence · "
            f"{B.esc(_title_case(item.suggested_structure_type))}"
            + (f" · Fit: {B.esc(_title_case(fit))}" if fit else "")
        )
        body = B.section_text(
            [
                ("Thesis", B.esc(item.thesis)),
                ("Regime", B.esc(item.regime_context)),
                ("Evidence", (evidence or {}).get(item.ticker, "")),
                ("Research evidence", " · ".join(B.esc(e) for e in item.evidence)),
            ],
            escape=False,
        )
        lines = [f"*{B.esc(item.ticker)}*", meta, *([body] if body else [])]
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": B.clip("\n".join(lines))}}
        )
    if beyond:
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
    # -- Exits (E13.17 watchlist; absent under exit_path deterministic) ------------
    if exits is not None:
        blocks.append(B.divider())
        blocks.append(_exits_section(exits))
    if pview is not None:
        blocks.append(B.divider())
        rows = [B.esc(pview.notes.strip())] if pview.notes.strip() else []
        if exits is None:  # E13.17: the watchlist replaces the thesis-check rows
            rows += [
                f"• {B.esc(c.structure_id)}: {B.esc(_title_case(c.status))}"
                + (f" · {B.esc(_clip_line(c.reason))}" if c.reason.strip() else "")
                for c in checks
            ]
        blocks.append(
            B.sections(
                [(f"Portfolio ({_title_case(pview.verdict)})", "\n".join(rows) or "-")],
                escape=False,
            )
        )
    if suppressed:
        blocks.append(B.divider())
        blocks.append(
            _section(
                f"Suppressed by dedupe ({len(suppressed)})",
                [f"• {B.esc(_clip_line(x))}" for x in suppressed],
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


_MAX_EXIT_LINES = 12


def _exits_section(exits: Sequence[ExitWatchItem]) -> Block:
    """``*Exits:*`` one line per watch item (≤ 12): ticker · action · status · evidence."""
    if not exits:
        return {"type": "section", "text": {"type": "mrkdwn", "text": "*Exits:* none open"}}
    order = sorted(exits, key=lambda w: (w.action != "review", w.ticker, w.structure_id))
    lines = []
    for w in order[:_MAX_EXIT_LINES]:
        first = w.evidence[0] if w.evidence else w.reason
        line = f"• *{B.esc(w.ticker)}* · {w.action} · {w.thesis_status}"
        if first.strip():
            line += f" · {B.esc(_clip_line(first))}"
        lines.append(line)
    if len(order) > _MAX_EXIT_LINES:
        lines.append(f"+{len(order) - _MAX_EXIT_LINES} more")
    text = f"*Exits ({len(exits)}):*\n" + "\n".join(lines)
    return {"type": "section", "text": {"type": "mrkdwn", "text": B.clip(text)}}


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
    revision: bool = False,
    kept: Sequence[str] = (),
) -> CardView:
    """``🤺 [Quant] Structures: SPY Iron Condor • PoP 62% • EV -$21.78`` + legs per structure.

    E13.9: ``revision=True`` is the ``quant.revise`` card (header ``[Quant (revised)]``);
    ``kept`` lists the revise-requested tickers Quant kept unchanged.

    E5.7: every budgeted ticker is accounted for: a structure, a skip with its reason
    (``out.skipped``), "no structure, no reason" (``not_structured``) or no chain.
    ``over_budget`` = ranked by Research beyond the Quant/Risk budget.
    """
    label = persona_label(Persona.QUANT, suffix=" (revised)") if revision else _QUANT
    if out.structures:
        best = out.structures[0]
        more = f" +{len(out.structures) - 1} more" if len(out.structures) > 1 else ""
        title = (
            f"{label} Structures: {best.ticker} {structure_name(best)}{more} • "
            f"PoP {_pct(best.pop)} • EV {_money(best.ev_per_contract)}"
        )
    else:
        title = f"{label} Structures: none chosen"
    n_drop = sum((dropped or {}).values()) + len(no_chain) + len(not_structured)
    skipped = [s for s in out.skipped if s.ticker not in set(no_chain)]
    blocks = _head(
        title,
        f"*{len(out.structures)}* chosen",
        f"{len(skipped)} skipped" if skipped else "",
        f"{n_drop} dropped" if n_drop else "",
        f"{len(over_budget)} over budget" if over_budget else "",
        f"{len(kept)} kept" if kept else "",
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
    if kept:
        blocks.append(_section("Kept unchanged", [", ".join(B.esc(t) for t in kept)]))
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
    verdicts: bool = False,
) -> CardView:
    """``🛡️ [Risk] Review: SPY Moderate • 14 Contracts``; one section per assessment.

    E13.9: ``verdicts=True`` (``personas.quant_risk_loop: on``) adds a verdict chip per
    assessment (Accept / Revise: <reason> / Reject) and a count line in the header.

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
        title = f"{_RISK} Review: {a0.ticker} {rating}{more} • {size_of(a0)}"
    else:
        title = f"{_RISK} Review: nothing assessed"
    warn = sum(a.concentration_warning for a in out.assessments)
    tally = Counter(_verdict(a) for a in out.assessments) if verdicts else Counter()
    blocks = _head(
        title,
        f"*{len(out.assessments)}* assessed",
        " · ".join(f"{n} {v}" for v, n in sorted(tally.items())) if tally else "",
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
                    *([("Verdict", _verdict_chip(a))] if verdicts else []),
                ]
            )
        )
        body = B.section_text(  # E13.13: bold section labels
            [
                ("Greek budget", a.greek_budget_impact),
                ("Calendar", a.calendar_concerns),
                ("Risks", a.narrative),
            ]
        )
        blocks.append(
            B.persona_section(Persona.RISK, f"{B.esc(a.ticker)} review", body, escape=False)
        )
    blocks.append(B.divider())
    blocks.append(B.persona_section(Persona.RISK, "Portfolio", out.portfolio_summary))
    blocks.append(B.persona_section(Persona.RISK, "Advisory", out.advisory_notes))
    items = [*dropped_items, *((s, "not_assessed") for s in not_assessed)]
    counts = dict(dropped or {})
    blocks.append(_section("Dropped / missing", _drops(counts, items)))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def _verdict(a: RiskAssessment) -> str:
    return str(getattr(a, "verdict", "accept"))


# ---------------------------------------------------------------------------
# Quant exit cases (E13.17 / D56; E13.13 styles it)
# ---------------------------------------------------------------------------

_TRIGGER_LABEL = {
    "research_review": "Research review",
    "profit_target": "Profit target",
    "time_adjusted_target": "Time-adjusted target",
    "remaining_ev_floor": "Remaining EV floor",
    "reallocate": "Reallocate",
}


_MAX_EXIT_CASES = 8


def _exit_case_line(c: ExitCase) -> str:
    """``*CRWD* `os-1` · Profit target · EV hold +$12 / managed +$30 · close now +$85``.

    Numbers come from ``ExitCase.facts`` only (E13.13).
    """
    f = c.facts
    trig = ", ".join(_TRIGGER_LABEL.get(t.kind, t.kind) for t in c.triggers)
    managed = (
        f" / managed {_money(f.remaining_ev_managed, signed=True)}"
        if f.remaining_ev_managed is not None
        else ""
    )
    ev = (
        f"EV hold {_money(f.remaining_ev_hold, signed=True)}{managed}"
        if f.remaining_ev_hold is not None
        else f"EV hold n/a{managed}"
    )
    return (
        f"*{B.esc(c.ticker)}* `{B.esc(c.structure_id)}` · {B.esc(trig)} · {ev} · "
        f"close now {_money(f.close_now_net, signed=True)}"
    )


def quant_exit_card(
    cases: Sequence[ExitCase],
    *,
    shadow: bool = True,
    skipped: Mapping[str, int] | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``🤺 [Quant] Exit cases: 2 judged • 1 close``; one section per case (≤ 8).

    E13.13: ticker · triggers · remaining EV hold/managed · close-now net, then
    ``*Recommendation:*`` and Quant's rationale. Under ``shadow`` the summary says
    nothing is proposed.
    """
    closes = sum(c.recommendation == "close" for c in cases)
    title = f"{_QUANT} Exit cases: {len(cases)} judged • {closes} close"
    skip_txt = " · ".join(
        f"{n} skipped ({k.replace('_', ' ')})" for k, n in sorted((skipped or {}).items())
    )
    blocks = _head(title, "Shadow: journaled only, nothing proposed" if shadow else "", skip_txt)
    for c in cases[:_MAX_EXIT_CASES]:
        body = B.section_text(
            [
                ("Recommendation", c.recommendation.capitalize()),
                ("Thesis", c.facts.thesis_status or ""),
                ("Rationale", _clip_line(c.rationale, 400)),
            ]
        )
        blocks.append(B.divider())
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": B.clip(f"{_exit_case_line(c)}\n{body}")},
            }
        )
    if len(cases) > _MAX_EXIT_CASES:
        rest = ", ".join(B.esc(c.ticker) for c in cases[_MAX_EXIT_CASES:])
        blocks.append(B.summary(B.clip(f"+{len(cases) - _MAX_EXIT_CASES} more: {rest}")))
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def risk_exit_card(
    cases: Sequence[ExitCase],
    verdicts: Mapping[str, RiskExitVerdict],
    *,
    unavailable: bool = False,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    """``🛡️ [Risk] Exit review: 2 reviewed • 1 close``; one section per case (E13.13).

    ticker · Quant's call → Risk's verdict chip · reason code; the reason underneath.
    """
    closes = sum(v.verdict == "close" for v in verdicts.values())
    title = f"{_RISK} Exit review: {len(cases)} reviewed • {closes} close"
    sub = "Risk unavailable: every case held (policy fallback applies)" if unavailable else ""
    blocks = _head(title, sub, "")
    shown = 0
    for c in cases:
        v = verdicts.get(c.structure_id)
        if v is None:
            continue
        if shown == _MAX_EXIT_CASES:
            break
        shown += 1
        line = (
            f"*{B.esc(c.ticker)}* `{B.esc(c.structure_id)}` · Quant {c.recommendation} → "
            f"*{v.verdict.capitalize()}* · `{B.esc(v.reason_code)}`"
        )
        body = B.section_text([("Reason", _clip_line(v.reason, 240))])
        blocks.append(B.divider())
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": B.clip(f"{line}\n{body}")}}
        )
    return _finish(title, blocks, run_id=run_id, chain=chain_run_id)


def exits_mandatory_summary(closes: Sequence[tuple[str, str, str | None]]) -> str:
    """E13.13: the ``exits.mandatory`` one-liner, ``CRWD · stop · proposal ab12cd34``.

    *closes* is ``(ticker, signal, proposal hash or None)``; ``None`` = not proposed.
    """
    if not closes:
        return "no mandatory exit signals (stop / DTE exit / expiry)"
    parts = [
        f"{t} · {sig.replace('_', ' ')} · " + (f"proposal {h[:8]}" if h else "not proposed")
        for t, sig, h in closes
    ]
    return "mandatory exits: " + "; ".join(parts)


def _verdict_chip(a: RiskAssessment) -> str:
    """E13.9: ``Accept`` / ``Revise: width — <instruction>`` / ``Reject``."""
    v = _verdict(a)
    req = getattr(a, "revise_request", None)
    if v == "revise" and req is not None:
        why = _title_case(str(req.reason))
        return f"*Revise*: {B.esc(why)} — {B.esc(_clip_line(req.instruction))}"
    return f"*{v.capitalize()}*"


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
    """``🏦 [Broker] Order: SPY Iron Condor • x3 • Limit -1.25`` (+ fill/cancel outcome)."""
    title = (
        f"{_BROKER} Order: {plan.ticker} {_title_case(plan.structure_type)} • "
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
    """``🏦 [Broker] Reconcile: Sep 28 • P&L +$312 (+0.3%)``; anomalies live in the body.

    E8.2a: *ops_line* (``Slots: research 71/75, … · missed 6 (list in tower Ops)``)
    is the day's routine slot coverage, shown in an ``Ops`` section.
    """
    perf = performance or Performance(day_pnl=out.daily_pnl)
    title = (
        f"{_BROKER} Reconcile: {_day(out.journal_date)} • P&L {_pnl(perf.day_pnl, perf.day_pct)}"
    )
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
