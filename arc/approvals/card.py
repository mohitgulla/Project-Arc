"""Block Kit proposal card for the #arc-investor day thread (E6.1).

Pure rendering: a :class:`~arc.models.Proposal` plus its gate verdict in, a
fallback text and Block Kit blocks out. No I/O, so every field is testable.

The card shows ticker, structure, legs, net credit/debit, max gain/loss,
breakevens, net Greeks, PoP/EV, the gate result and the sizing. An actionable
card carries **Approve / Reject** buttons (``arc_approve`` / ``arc_reject``)
whose ``value`` is the proposal hash; the ``arc-approvals`` Hermes plugin
routes the click to ``arc approve decide``. A resolved card is the same body
without buttons plus one context line with the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.approvals.trail import DecisionTrail
from arc.models import StructureKind
from arc.slack import blocks as B
from arc.slack.personas import Persona
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt

    from arc.models import GateDecision, Proposal

__all__ = [
    "ACTION_APPROVE",
    "ACTION_REJECT",
    "CardView",
    "render_card",
    "render_resolved",
    "strategy_name",
    "title",
]

ACTION_APPROVE = "arc_approve"
ACTION_REJECT = "arc_reject"

_MULT = Decimal(100)


@dataclass(frozen=True)
class CardView:
    """What gets posted: plain fallback text (notifications) + Block Kit blocks."""

    text: str
    blocks: list[dict[str, Any]]


def _money(v: Decimal | None) -> str:
    if v is None:
        return "unbounded"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _num(v: Decimal) -> str:
    return f"{v.normalize():f}"


_STRATEGY_NAMES = {
    StructureKind.LONG_CALL: "Long Call",
    StructureKind.LONG_PUT: "Long Put",
    StructureKind.VERTICAL_DEBIT: "{side} Debit Spread",
    StructureKind.VERTICAL_CREDIT: "{side} Credit Spread",
    StructureKind.IRON_CONDOR: "Iron Condor",
}


def strategy_name(p: Proposal) -> str:
    """Display name of the structure, e.g. ``Iron Condor`` or ``Put Credit Spread``."""
    name = _STRATEGY_NAMES.get(p.structure.kind) if p.structure.kind else None
    if name is None:
        return "Custom"
    side = parse_occ(p.structure.legs[0].occ_symbol).kind.name.capitalize()
    return name.format(side=side)


def title(p: Proposal) -> str:
    """One consistent title: ``[Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor``."""
    exp = parse_occ(p.structure.legs[0].occ_symbol).expiration
    return (
        f"[Quant] Proposal: {ticker_of(p)} • {exp:%b %d} ({p.structure.dte} DTE) • "
        f"{strategy_name(p)}"
    )


def ticker_of(p: Proposal) -> str:
    """Underlying root of the proposal's first leg."""
    return parse_occ(p.structure.legs[0].occ_symbol).root


def _legs(p: Proposal) -> str:
    """Legs as an aligned monospace table; the expiry column appears only if legs differ."""
    occs = [parse_occ(leg.occ_symbol) for leg in p.structure.legs]
    multi_exp = len({o.expiration for o in occs}) > 1
    rows = []
    for leg, occ in zip(p.structure.legs, occs, strict=True):
        strike = f"{_num(occ.strike)}{occ.kind.name[0]}"
        exp = f"{occ.expiration:%b %d}  " if multi_exp else ""
        prem = f"@ {leg.premium:.2f}" if leg.premium is not None else ""
        rows.append(f"{leg.side.value.upper():<5}  {leg.ratio}x  {exp}{strike:>8}  {prem}".rstrip())
    return "```" + "\n".join(rows) + "```"


def _net_word(v: Decimal) -> str:
    return "credit" if v < 0 else "debit"


def _entry(p: Proposal) -> str:
    net = p.structure.net_debit_credit
    limit = p.limit_price if p.limit_price is not None else net
    return (
        f"{_net_word(net).capitalize()} {abs(net):.2f}/sh (${abs(net) * _MULT:,.0f}/contract)\n"
        f"Limit {_net_word(limit)} {abs(limit):.2f}"
    )


def _payoff(p: Proposal) -> str:
    st = p.structure
    return (
        f"Max gain {_money(st.max_gain)}\nMax loss {_money(st.max_loss)}\n"
        f"Reward/risk {_reward_risk(p)}"
    )


def _reward_risk(p: Proposal) -> str:
    st = p.structure
    if st.max_gain is None or st.max_loss is None or st.max_loss == 0:
        return "n/a"
    return f"{st.max_gain / st.max_loss:.2f}"


def _position(p: Proposal) -> str:
    n = Decimal(p.sizing.contracts)
    st = p.structure
    gain = None if st.max_gain is None else st.max_gain * n
    loss = None if st.max_loss is None else st.max_loss * n
    s = p.sizing
    return (
        f"{s.contracts} contract(s) · {s.pct_equity:.2%} of equity\n"
        f"Max gain {_money(gain)}\nMax loss {_money(loss)}"
    )


def _breakevens(p: Proposal) -> str:
    bes = p.structure.breakevens
    return " / ".join(f"{b:.2f}" for b in bes) if bes else "n/a"


def _edge(p: Proposal) -> str:
    q = p.quant
    return f"PoP {q.pop:.0%}\nEV {_money(q.ev)}/contract\nCost {q.cost_bps:.0f} bps"


def _greeks(p: Proposal) -> str:
    """Position Greeks for the whole order: Δ/Γ in shares, ν in $/vol-point, Θ in $/day."""
    g = p.structure.greeks
    n = p.sizing.contracts
    return (
        f"Δ {g.delta * n:+.1f} · Γ {g.gamma * n:+.2f}\n"
        f"ν {g.vega * n / 100:+.1f} $/vol pt · Θ {g.theta * n:+.1f} $/day"
    )


def _liquidity(p: Proposal) -> str | None:
    liq = p.structure.liquidity
    if not (liq.spread_pct or liq.open_interest or liq.volume):
        return None
    return f"Spread {liq.spread_pct:.1%}\nOI {liq.open_interest:,} · Vol {liq.volume:,}"


def _gate_short(decision: GateDecision | None) -> str:
    if decision is None:
        return ":grey_question: no gate decision"
    if decision.passed:
        return (
            ":white_check_mark: Gate PASS"
            if decision.token
            else ":white_check_mark: Gate PASS (no token)"
        )
    return f":no_entry: Gate FAIL ({len(decision.violations)})"


def _gate(decision: GateDecision | None) -> str:
    if decision is None:
        return ":grey_question: no gate decision"
    if decision.passed:
        token = "token issued" if decision.token else "no token (not executable)"
        return f":white_check_mark: PASS ({token})"
    return ":no_entry: FAIL — see violations"


def _body(
    p: Proposal,
    decision: GateDecision | None,
    proposal_hash: str,
    trail: DecisionTrail | None = None,
) -> list[dict[str, Any]]:
    """Layout (shared grammar, :mod:`arc.slack.blocks`).

    title → one-line summary → legs → fact grid → gate violations →
    market context → persona reasoning (each attributed) → audit footer.
    """
    net = p.structure.net_debit_credit
    blocks: list[dict[str, Any] | None] = [
        B.header(title(p)),
        B.summary(
            f"*{_net_word(net).capitalize()} {abs(net):.2f}*",
            f"max gain {_money(p.structure.max_gain)}",
            f"max loss {_money(p.structure.max_loss)}",
            f"PoP {p.quant.pop:.0%}",
            f"EV {_money(p.quant.ev)}",
            f"x{p.sizing.contracts}",
            _gate_short(decision),
        ),
        B.divider(),
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Legs*\n{_legs(p)}"}},
    ]
    pairs = [
        ("Entry", _entry(p)),
        ("Payoff (per contract)", _payoff(p)),
        (f"Position (x{p.sizing.contracts})", _position(p)),
        ("Breakevens", _breakevens(p)),
        ("Edge", _edge(p)),
        ("Net Greeks (position)", _greeks(p)),
        ("Gate", _gate(decision)),
    ]
    liq = _liquidity(p)
    if liq:
        pairs.append(("Liquidity", liq))
    blocks.extend(B.facts(pairs))
    if decision is not None and not decision.passed:
        blocks.append(B.bullets("Gate violations", decision.violations))
    blocks.append(B.divider())
    blocks.extend(_why(p, trail or DecisionTrail()))
    blocks.append(
        B.footer(
            proposal=proposal_hash[:12],
            chain=(trail.chain_run_id if trail else None),
        )
    )
    return [b for b in blocks if b is not None]


def _pct(v: object) -> str | None:
    return f"{float(v):.0%}" if isinstance(v, int | float) else None


def _market_line(t: DecisionTrail) -> str:
    parts: list[str] = []
    f = t.features or {}
    regime = (f.get("regime") or {}).get("current")
    if regime:
        parts.append(f"Regime *{regime}*")
    vol = f.get("vol") or {}
    if (iv := _pct(vol.get("iv"))) is not None:
        parts.append(f"IV {iv}")
    if (ivr := _pct(vol.get("iv_rank"))) is not None:
        parts.append(f"IVR {ivr}")
    if (hv := _pct(vol.get("hv20"))) is not None:
        parts.append(f"HV20 {hv}")
    if t.market_regime:
        parts.append(f"Director market read *{B.esc(t.market_regime)}*")
    return " · ".join(parts)


def _why(p: Proposal, t: DecisionTrail) -> list[dict[str, Any] | None]:
    """Per-persona reasoning, each block attributed to the persona that wrote it."""
    out: list[dict[str, Any] | None] = []
    market = _market_line(t)
    if market:
        out.append(B.summary(f":bar_chart: {market}"))

    d = t.director or {}
    meta = []
    if d.get("rank"):
        meta.append(f"rank {d['rank']} of {t.shortlist_size}")
    if d.get("stance"):
        meta.append(str(d["stance"]))
    if (c := _pct(d.get("confidence"))) is not None:
        meta.append(f"confidence {c}")
    thesis = B.esc(p.thesis.strip())
    if d.get("regime_context"):
        thesis += f"\n_Regime:_ {B.esc(str(d['regime_context']))}"
    out.append(
        B.persona_section(
            Persona.DIRECTOR,
            "Thesis" + (f" ({', '.join(meta)})" if meta else ""),
            thesis,
            escape=False,
        )
    )

    q = t.quant or {}
    if q.get("rationale"):
        conf = _pct(q.get("confidence"))
        out.append(
            B.persona_section(
                Persona.QUANT,
                "Structure choice" + (f" (confidence {conf})" if conf else ""),
                str(q["rationale"]),
            )
        )

    r = t.risk or {}
    head = []
    if r.get("risk_rating"):
        head.append(f"rating *{B.esc(str(r['risk_rating']))}*")
    if r.get("sizing_suggestion") is not None:
        n = p.sizing.contracts
        sug = int(r["sizing_suggestion"])
        head.append(f"suggested {sug} → sized {n}" + (" (5% equity cap)" if n < sug else ""))
    if r.get("concentration_warning"):
        head.append(":warning: concentration")
    lines = [" · ".join(head)] if head else []
    for label, key in (("Calendar", "calendar_concerns"), ("Greek budget", "greek_budget_impact")):
        if r.get(key):
            lines.append(f"• _{label}:_ {B.esc(str(r[key]))}")
    narrative = B.esc(p.risk_narrative.strip())
    if narrative:
        lines.append(narrative)
    out.append(B.persona_section(Persona.RISK, "Review", "\n".join(lines), escape=False))
    return out


def _fallback(p: Proposal, decision: GateDecision | None) -> str:
    verdict = "gate PASS" if decision is not None and decision.passed else "gate FAIL"
    return f"{title(p)} • x{p.sizing.contracts} • {verdict}"


def render_card(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    actionable: bool,
    note: str = "",
    trail: DecisionTrail | None = None,
) -> CardView:
    """The card as first posted.

    ``actionable`` adds the Approve / Reject buttons and the TTL line; an
    informational card (gate failed, no token) gets ``note`` instead.
    """
    blocks = _body(proposal, decision, proposal_hash, trail)
    if actionable:
        expires = proposal.expires_at.astimezone(ET)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":hourglass_flowing_sand: Expires {expires:%H:%M} ET — "
                        "no decision by then = *rejected*.",
                    }
                ],
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"arc_approval_{proposal_hash[:16]}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                        "style": "primary",
                        "action_id": ACTION_APPROVE,
                        "value": proposal_hash,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                        "style": "danger",
                        "action_id": ACTION_REJECT,
                        "value": proposal_hash,
                    },
                ],
            }
        )
    elif note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": B.esc(note)}]})
    return CardView(text=_fallback(proposal, decision), blocks=blocks)


def render_resolved(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    outcome: str,
    at: _dt.datetime,
    trail: DecisionTrail | None = None,
) -> CardView:
    """The card after it resolved: no buttons, one outcome line (``outcome`` is mrkdwn)."""
    blocks = _body(proposal, decision, proposal_hash, trail)
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{outcome} · {at.astimezone(ET):%H:%M} ET"}],
        }
    )
    return CardView(text=f"{_fallback(proposal, decision)} — {outcome}", blocks=blocks)
