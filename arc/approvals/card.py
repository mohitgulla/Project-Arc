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
]

ACTION_APPROVE = "arc_approve"
ACTION_REJECT = "arc_reject"

_MULT = Decimal(100)
_TEXT_MAX = 2900  # Slack caps a section's text at 3000 chars


@dataclass(frozen=True)
class CardView:
    """What gets posted: plain fallback text (notifications) + Block Kit blocks."""

    text: str
    blocks: list[dict[str, Any]]


def _esc(text: str) -> str:
    """Escape Slack mrkdwn control characters (persona text is untrusted)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int = _TEXT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _money(v: Decimal | None) -> str:
    if v is None:
        return "unbounded"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _num(v: Decimal) -> str:
    return f"{v.normalize():f}"


def _structure_name(p: Proposal) -> str:
    kind = p.structure.kind
    return kind.value.replace("_", " ") if kind else "structure"


def ticker_of(p: Proposal) -> str:
    """Underlying root of the proposal's first leg."""
    return parse_occ(p.structure.legs[0].occ_symbol).root


def _legs(p: Proposal) -> str:
    lines = []
    for leg in p.structure.legs:
        occ = parse_occ(leg.occ_symbol)
        prem = f" @ {leg.premium:.2f}" if leg.premium is not None else ""
        lines.append(
            f"`{leg.side.value.upper():<5}` {leg.ratio}x {occ.root} {occ.expiration:%Y-%m-%d} "
            f"{_num(occ.strike)}{occ.kind.value[0].upper()}{prem}"
        )
    return "\n".join(lines)


def _net(p: Proposal) -> str:
    net = p.structure.net_debit_credit
    side = "credit" if net < 0 else "debit"
    per_contract = abs(net) * _MULT
    limit = p.limit_price if p.limit_price is not None else net
    return (
        f"{side} {abs(net):.2f}/sh (${per_contract:,.0f}/contract)\n"
        f"limit {'credit' if limit < 0 else 'debit'} {abs(limit):.2f}"
    )


def _gain_loss(p: Proposal) -> str:
    n = Decimal(p.sizing.contracts)
    st = p.structure
    gain_total = None if st.max_gain is None else st.max_gain * n
    loss_total = None if st.max_loss is None else st.max_loss * n
    return (
        f"gain {_money(st.max_gain)} / loss {_money(st.max_loss)} per contract\n"
        f"x{p.sizing.contracts}: gain {_money(gain_total)} / loss {_money(loss_total)}"
    )


def _breakevens(p: Proposal) -> str:
    bes = p.structure.breakevens
    return ", ".join(f"{b:.2f}" for b in bes) if bes else "n/a"


def _greeks(p: Proposal) -> str:
    """Position Greeks for the whole order: Δ/Γ in shares, ν in $/vol-point, Θ in $/day."""
    g = p.structure.greeks
    n = p.sizing.contracts
    return (
        f"Δ {g.delta * n:+.1f}  Γ {g.gamma * n:+.2f}  ν {g.vega * n / 100:+.1f}  "
        f"Θ {g.theta * n:+.1f}\n(x{n}; Δ/Γ shares, ν $/vol pt, Θ $/day)"
    )


def _quant(p: Proposal) -> str:
    q = p.quant
    return f"PoP {q.pop:.0%}  EV {_money(q.ev)}/contract  cost {q.cost_bps:.0f} bps"


def _sizing(p: Proposal) -> str:
    s = p.sizing
    return f"{s.contracts} contract(s), max loss {_money(s.notional)} ({s.pct_equity:.2%} equity)"


def _gate(decision: GateDecision | None) -> str:
    if decision is None:
        return ":grey_question: no gate decision"
    if decision.passed:
        token = "token issued" if decision.token else "no token (not executable)"
        return f":white_check_mark: PASS ({token})"
    return ":no_entry: FAIL — " + "; ".join(decision.violations)


def _body(p: Proposal, decision: GateDecision | None, proposal_hash: str) -> list[dict[str, Any]]:
    ticker = ticker_of(p)
    exp = parse_occ(p.structure.legs[0].occ_symbol).expiration
    header = f"[Quant] Proposal: {ticker} {_structure_name(p)} {exp:%b %d} ({p.structure.dte} DTE)"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Legs*\n{_legs(p)}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Net*\n{_net(p)}"},
                {"type": "mrkdwn", "text": f"*Max gain / loss*\n{_gain_loss(p)}"},
                {"type": "mrkdwn", "text": f"*Breakevens*\n{_breakevens(p)}"},
                {"type": "mrkdwn", "text": f"*Net Greeks*\n{_greeks(p)}"},
                {"type": "mrkdwn", "text": f"*PoP / EV*\n{_quant(p)}"},
                {"type": "mrkdwn", "text": f"*Sizing*\n{_sizing(p)}"},
                {"type": "mrkdwn", "text": f"*Gate*\n{_clip(_gate(decision), 1900)}"},
            ],
        },
    ]
    thesis = _esc(p.thesis.strip())
    if thesis:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*Thesis* {thesis}")}}
        )
    risk = _esc(p.risk_narrative.strip())
    if risk:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"[Risk] {risk}")}}
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"proposal `{proposal_hash[:12]}`"}],
        }
    )
    return blocks


def _fallback(p: Proposal, decision: GateDecision | None) -> str:
    verdict = "gate PASS" if decision is not None and decision.passed else "gate FAIL"
    return (
        f"[Quant] Proposal: {ticker_of(p)} {_structure_name(p)} x{p.sizing.contracts} ({verdict})"
    )


def render_card(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    actionable: bool,
    note: str = "",
) -> CardView:
    """The card as first posted.

    ``actionable`` adds the Approve / Reject buttons and the TTL line; an
    informational card (gate failed, no token) gets ``note`` instead.
    """
    blocks = _body(proposal, decision, proposal_hash)
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
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(note)}]})
    return CardView(text=_fallback(proposal, decision), blocks=blocks)


def render_resolved(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    outcome: str,
    at: _dt.datetime,
) -> CardView:
    """The card after it resolved: no buttons, one outcome line (``outcome`` is mrkdwn)."""
    blocks = _body(proposal, decision, proposal_hash)
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{outcome} · {at.astimezone(ET):%H:%M} ET"}],
        }
    )
    return CardView(text=f"{_fallback(proposal, decision)} — {outcome}", blocks=blocks)
