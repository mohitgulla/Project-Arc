"""Block Kit proposal card for the #arc-investor day thread (E6.1, v2 in E6.1a).

Pure rendering: a :class:`~arc.models.Proposal`, its gate verdict and the stored
:class:`~arc.approvals.trail.DecisionTrail` (which carries the proposal's
:class:`~arc.journal.analytics.ProposalAnalytics`) in; a fallback text and Block
Kit blocks out. No I/O, and no number is recomputed from a live quote: every
figure comes from the proposal, the gate decision or the stored analytics.

Layout (shared grammar, :mod:`arc.slack.blocks`; D22/D23):

    title → summary line → legs (plain lines) → fact grid (one fact per line) →
    Net EV (hold to expiry and managed exit) → exit plan → cost & liquidity →
    underlying & moneyness → vol stats → gate violations → market line →
    persona reasoning → audit footer

An actionable card carries **Approve / Reject** buttons (``arc_approve`` /
``arc_reject``) whose ``value`` is the proposal hash; the ``arc-approvals``
Hermes plugin routes the click to ``arc approve decide``. A resolved card is the
same body without buttons plus one context line with the outcome.

Credit and debit structures (D25 ``cash_debit``) render through the same code:
"credit"/"debit" wording follows the sign of the net price, and exit targets say
whether the close is a debit or a credit.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from arc.approvals.trail import DecisionTrail
from arc.gate.token import BandToken, TokenError, parse_any
from arc.models import StructureKind
from arc.slack import blocks as B
from arc.slack.blocks import CardView
from arc.slack.personas import Persona, persona_label
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt

    from arc.exits.model import EvCosts, ExitModelResult, TriggerLevels
    from arc.gate.band import PriceBand
    from arc.journal.analytics import LegAnalytics, ProposalAnalytics
    from arc.models import GateDecision, Proposal

__all__ = [
    "ACTION_APPROVE",
    "ACTION_REJECT",
    "NET_EV_DEFINITION",
    "CardView",
    "render_card",
    "render_resolved",
    "strategy_name",
    "title",
]

ACTION_APPROVE = "arc_approve"
ACTION_REJECT = "arc_reject"

NET_EV_DEFINITION = (
    "Net EV = expected value after deducting execution costs, exchange/regulatory fees, "
    "slippage and bid-ask spread."
)

_MULT = Decimal(100)
NA = "n/a"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _money(v: Decimal | float | None, *, unbounded: str = "unbounded") -> str:
    if v is None:
        return unbounded
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _smoney(v: float) -> str:
    """Signed money: ``+$1.20`` / ``-$3.40``."""
    return f"{'-' if v < 0 else '+'}${abs(v):,.2f}"


def _num(v: Decimal) -> str:
    return f"{v.normalize():f}"


def _pct(v: object, digits: int = 0) -> str | None:
    return f"{float(v):.{digits}%}" if isinstance(v, int | float) else None


def _spct(v: float | None, digits: int = 1) -> str:
    return NA if v is None else f"{v:+.{digits}%}"


def _sig(v: float | None) -> str:
    """Absolute σ distance, e.g. ``0.9σ``."""
    return NA if v is None else f"{abs(v):.1f}σ"


def _size(v: float | None) -> str:
    return NA if v is None else f"{v:,.0f}"


def _int(v: int | None) -> str:
    return NA if v is None else f"{v:,}"


_STRATEGY_NAMES = {
    StructureKind.LONG_CALL: "Long Call",
    StructureKind.LONG_PUT: "Long Put",
    StructureKind.VERTICAL_DEBIT: "{side} Debit Spread",
    StructureKind.VERTICAL_CREDIT: "{side} Credit Spread",
    StructureKind.IRON_CONDOR: "Iron Condor",
}


def strategy_name(p: Proposal) -> str:
    """Title Case display name (D22), e.g. ``Iron Condor`` or ``Put Credit Spread``."""
    name = _STRATEGY_NAMES.get(p.structure.kind) if p.structure.kind else None
    if name is None:
        return "Custom"
    side = parse_occ(p.structure.legs[0].occ_symbol).kind.name.capitalize()
    return name.format(side=side)


def title(p: Proposal, kind: str = "open") -> str:
    """One consistent title: ``🤺 [Quant] Proposal: SPY • Oct 30 (35 DTE) • Iron Condor``.

    An exit (``kind='close'``, E6.2) reads ``🤺 [Quant] Exit: SPY • Oct 30 (21 DTE) • Close``.
    """
    exp = parse_occ(p.structure.legs[0].occ_symbol).expiration
    q = persona_label(Persona.QUANT)
    head = f"{q} Exit" if kind == "close" else f"{q} Proposal"
    what = "Close position" if kind == "close" else strategy_name(p)
    return f"{head}: {ticker_of(p)} • {exp:%b %d} ({p.structure.dte} DTE) • {what}"


def ticker_of(p: Proposal) -> str:
    """Underlying root of the proposal's first leg."""
    return parse_occ(p.structure.legs[0].occ_symbol).root


def _net_word(v: Decimal | float) -> str:
    return "credit" if v < 0 else "debit"


def band_of(decision: GateDecision | None) -> PriceBand | None:
    """The D24 price band signed into an ``arc2`` token (None for arc1 / no token)."""
    if decision is None or not decision.token:
        return None
    try:
        t = parse_any(decision.token)
    except TokenError:
        return None
    return t.band if isinstance(t, BandToken) else None


def limit_text(limit: Decimal, band: PriceBand | None) -> str:
    """``Limit credit 1.65, worst 1.58 after 3 steps`` (the band one approval authorises)."""
    text = f"Limit {_net_word(limit)} {abs(limit):.2f}"
    if band is not None and band.max_steps and band.hi != band.lo:
        steps = f"{band.max_steps} step{'s' if band.max_steps != 1 else ''}"
        text += f", worst {_net_word(band.hi)} {abs(band.hi):.2f} after {steps}"
    return text


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _legs(p: Proposal, a: ProposalAnalytics | None) -> str:
    """``Long 1x 740P @ 3.74 · 3.9% OTM · Δ -0.14``; expiry only when legs differ."""
    occs = [parse_occ(leg.occ_symbol) for leg in p.structure.legs]
    multi_exp = len({o.expiration for o in occs}) > 1
    by_sym = {la.occ_symbol: la for la in a.legs} if a else {}
    rows = []
    for leg, occ in zip(p.structure.legs, occs, strict=True):
        parts = [f"{leg.side.value.capitalize()} {leg.ratio}x"]
        if multi_exp:
            parts.append(f"{occ.expiration:%b %d}")
        parts.append(f"{_num(occ.strike)}{occ.kind.name[0]}")
        head = " ".join(parts)
        if leg.premium is not None:
            head += f" @ {leg.premium:.2f}"
        facts = [head]
        la = by_sym.get(occ.format())
        if la is not None and la.moneyness_pct is not None:
            facts.append(f"{abs(la.moneyness_pct):.1%} {'OTM' if la.otm else 'ITM'}")
        if la is not None and la.delta is not None:
            facts.append(f"Δ {la.delta * (1 if leg.side.value == 'long' else -1):+.2f}")
        rows.append(" · ".join(facts))
    return "\n".join(rows)


def _entry(p: Proposal, decision: GateDecision | None = None) -> str:
    net = p.structure.net_debit_credit
    limit = p.limit_price if p.limit_price is not None else net
    return (
        f"{_net_word(net).capitalize()} {abs(net):.2f}/sh\n"
        f"${abs(net) * _MULT:,.0f} per contract\n" + limit_text(limit, band_of(decision))
    )


def _risk_reward(p: Proposal) -> str:
    """Risk/Reward = max loss / max gain, e.g. ``2.02 : 1``."""
    st = p.structure
    if st.max_gain is None or st.max_loss is None or st.max_gain <= 0:
        return NA
    return f"{st.max_loss / st.max_gain:.2f} : 1"


def _payoff(p: Proposal) -> str:
    st = p.structure
    return (
        f"Max gain {_money(st.max_gain)}\nMax loss {_money(st.max_loss)}\n"
        f"Risk/Reward {_risk_reward(p)}"
    )


def _position(p: Proposal) -> str:
    n = Decimal(p.sizing.contracts)
    st = p.structure
    gain = None if st.max_gain is None else st.max_gain * n
    loss = None if st.max_loss is None else st.max_loss * n
    s = p.sizing
    return (
        f"{s.contracts} contract(s)\n{s.pct_equity:.2%} of equity\n"
        f"Max gain {_money(gain)}\nMax loss {_money(loss)}"
    )


def _breakevens(p: Proposal, a: ProposalAnalytics | None) -> str:
    if a is not None and a.breakevens:
        return "\n".join(f"BE {b.price:.2f} ({b.pct:+.1%}, {_sig(b.sigma)})" for b in a.breakevens)
    bes = p.structure.breakevens
    return "\n".join(f"BE {b:.2f}" for b in bes) if bes else NA


def _edge(p: Proposal) -> str:
    q = p.quant
    return f"PoP {q.pop:.0%}\nQuant EV {_money(q.ev)} / contract\nCost {q.cost_bps:.0f} bps"


def _greeks(p: Proposal) -> str:
    """Position Greeks for the whole order: Δ/Γ in shares, vega $/vol pt, theta $/day."""
    g = p.structure.greeks
    n = p.sizing.contracts
    return (
        f"Δ Delta {g.delta * n:+.1f} sh\nΓ Gamma {g.gamma * n:+.2f} sh\n"
        f"ν Vega {_smoney(g.vega * n / 100)} / vol pt\nΘ Theta {_smoney(g.theta * n)} / day"
    )


def _liquidity(p: Proposal) -> str | None:
    liq = p.structure.liquidity
    if not (liq.spread_pct or liq.open_interest or liq.volume):
        return None
    return (
        f"Widest spread {liq.spread_pct:.1%}\nMin open interest {liq.open_interest:,}\n"
        f"Min volume {liq.volume:,}"
    )


def _gate_short(decision: GateDecision | None) -> str:
    if decision is None:
        return ":grey_question: No gate decision"
    if decision.passed:
        return (
            ":white_check_mark: Gate PASS"
            if decision.token
            else ":white_check_mark: Gate PASS (no token)"
        )
    return f":no_entry: Gate FAIL ({len(decision.violations)})"


def _gate(decision: GateDecision | None) -> str:
    if decision is None:
        return ":grey_question: No gate decision"
    if decision.passed:
        token = "Token issued" if decision.token else "No token (not executable)"
        return f":white_check_mark: PASS\n{token}"
    return ":no_entry: FAIL\nSee violations"


# -- Net EV ------------------------------------------------------------------


def _ev_lines(gross: float, net: float, c: EvCosts | None, n: int) -> str:
    """Gross → minus each cost → Net EV, per contract and × contracts."""
    lines = [f"Gross (model) EV {_money(gross)}"]
    if c is not None:
        lines += [
            f"− Spread & slippage in {_money(c.entry_slippage)}",
            f"− Spread & slippage out {_money(c.exit_slippage)}",
            f"− Commission {_money(c.commission)}",
            f"− Regulatory fees {_money(c.regulatory_fees)}",
        ]
    lines.append(f"Net EV {_money(net)} / contract\nNet EV {_money(net * n)} x{n}")
    return "\n".join(lines)


def _net_ev_section(p: Proposal, em: ExitModelResult | None) -> list[dict[str, Any] | None]:
    if em is None:
        return [B.summary(f"{NET_EV_DEFINITION} Exit model n/a for this proposal.")]
    n = p.sizing.contracts
    s, m = em.static, em.managed
    pairs = [
        ("Net EV · hold to expiry", _ev_lines(s.gross_ev, s.net_ev, s.costs, n)),
        ("Net EV · managed exit", _ev_lines(m.gross_ev, m.net_ev, m.costs, n)),
    ]
    return [*B.facts(pairs), B.summary(NET_EV_DEFINITION)]


# -- Exit plan ---------------------------------------------------------------


def _trigger(t: TriggerLevels | None) -> str | None:
    if t is None:
        return None
    return f"{_money(t.close_price)} {t.close_side} to close"


def _policy_line(p: Proposal, em: ExitModelResult) -> str:
    pol = em.policy
    credit = em.entry_net < 0
    parts: list[str] = []
    tp_pct = pol.take_profit_pct_of_max_gain if credit else pol.take_profit_pct_of_debit
    if tp_pct is not None:
        base = "of max gain" if credit else "of debit"
        tp = f"Take profit {tp_pct:.0%} {base}"
        if (close := _trigger(em.take_profit)) is not None:
            tp += f" ({close})"
        parts.append(tp)
    if pol.stop is None:
        parts.append("No stop")
    else:
        what = {
            "credit_multiple": f"{pol.stop.value:g}x credit",
            "pct_max_loss": f"{pol.stop.value:.0%} of max loss",
            "pct_debit": f"{pol.stop.value:.0%} of debit",
        }[pol.stop.basis.value]
        stop = f"Stop at {what}"
        if (close := _trigger(em.stop)) is not None:
            stop += f" ({close})"
        if pol.stop_eod_only:
            stop += ", end-of-day marks"
        parts.append(stop)
    if pol.close_at_dte is not None:
        parts.append(f"Close at {pol.close_at_dte} DTE")
    return " · ".join(parts)


def _exit_plan(p: Proposal, em: ExitModelResult | None) -> list[dict[str, Any] | None]:
    if em is None:
        return []
    m, s = em.managed, em.static
    body = "\n".join(
        [
            _policy_line(p, em),
            f"Managed: PoP {m.pop:.0%} · Net EV {_money(m.net_ev)} / contract",
            f"Hold to expiry: PoP {s.pop:.0%} · Net EV {_money(s.net_ev)} / contract",
            f"Take profit {m.p_take_profit:.0%} · Stop {m.p_stop:.0%} · "
            f"DTE exit {m.p_dte_exit:.0%} · Expiry {m.p_expiry:.0%}",
            f"Expected days held {m.expected_days_held:.1f}",
            f"Model: {em.n_paths:,} paths, marks at IV {em.iv_used:.1%}, "
            f"paths at {em.path_vol:.1%} ({em.path_vol_source.replace('_', ' ')})",
        ]
    )
    return [{"type": "section", "text": {"type": "mrkdwn", "text": B.clip(f"*Exit plan*\n{body}")}}]


# -- Cost & liquidity --------------------------------------------------------


def _leg_label(la: LegAnalytics) -> str:
    return f"{la.side.capitalize()} {la.ratio}x {la.strike:g}{la.kind[0].upper()}"


def _leg_cost(la: LegAnalytics) -> str:
    q = (
        f"Bid {la.bid:.2f} · Ask {la.ask:.2f} · Mid {la.mid:.2f}"
        if la.bid is not None and la.ask is not None and la.mid is not None
        else "Quote n/a"
    )
    spread = (
        f"Spread {_money(la.spread)} ({la.spread_pct:.1%} of mid)"
        if la.spread is not None and la.spread_pct is not None
        else "Spread n/a"
    )
    return "\n".join(
        [
            q,
            spread,
            f"Size {_size(la.bid_size)} x {_size(la.ask_size)}",
            f"OI {_int(la.open_interest)} · Vol {_int(la.volume)}",
            f"Slippage {_money(la.entry_slippage)} · Fees {_money(la.entry_fees.total)}",
        ]
    )


def _cost_total(p: Proposal, a: ProposalAnalytics, em: ExitModelResult | None) -> str:
    f = a.entry_fees
    n = p.sizing.contracts
    lines = [
        f"Entry slippage {_money(a.entry_slippage)}",
        f"Entry commission {_money(f.commission)}",
        f"Entry regulatory fees {_money(f.regulatory)}",
        f"(ORF {_money(f.orf)} · OCC {_money(f.occ)} · CAT {_money(f.cat)} · "
        f"TAF {_money(f.taf)} · SEC {_money(f.sec)})",
    ]
    total = a.entry_slippage + f.total
    if em is not None and em.managed.costs is not None:
        c = em.managed.costs
        exit_fees = c.commission + c.regulatory_fees - f.total
        lines += [
            f"Expected exit slippage {_money(c.exit_slippage)}",
            f"Expected exit fees {_money(exit_fees)}",
        ]
        total = c.total
    lines += [f"Round trip {_money(total)} / contract", f"Round trip {_money(total * n)} x{n}"]
    return "\n".join(lines)


def _costs(p: Proposal, a: ProposalAnalytics | None) -> list[dict[str, Any] | None]:
    if a is None:
        return []
    em = a.exit_model
    cm = a.cost_model
    pairs = [(_leg_label(la), _leg_cost(la)) for la in a.legs]
    pairs.append(("Total (per contract)", _cost_total(p, a, em)))
    note = (
        f"Fill model: mid ± {cm.slippage_frac:g} × spread per leg · "
        f"Commission {_money(cm.commission_per_contract)} / contract · "
        "Depth: top of book only (Alpaca)"
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Cost & liquidity*"}},
        *B.facts(pairs),
        B.summary(note),
    ]


# -- Underlying & vol --------------------------------------------------------


def _underlying(p: Proposal, a: ProposalAnalytics) -> str:
    when = f" at {a.spot_as_of.astimezone(ET):%H:%M} ET" if a.spot_as_of else ""
    lines = [f"Spot {a.spot:,.2f}{when}", f"Day change {_spct(a.day_change_pct, 2)}"]
    if a.expected_move is not None:
        lines.append(
            f"1σ move to expiry ±{a.expected_move:,.2f} "
            f"({a.spot - a.expected_move:,.2f} – {a.spot + a.expected_move:,.2f})"
        )
    else:
        lines.append(f"1σ move to expiry {NA}")
    return "\n".join(lines)


def _moneyness(a: ProposalAnalytics) -> str:
    rows = [
        f"{la.side.capitalize()} {la.strike:g}{la.kind[0].upper()}: "
        f"{_spct(la.moneyness_pct)}, {_sig(la.sigma_distance)}"
        for la in a.legs
    ]
    rows += [f"BE {b.price:.2f}: {b.pct:+.1%}, {_sig(b.sigma)}" for b in a.breakevens]
    return "\n".join(rows)


def _vol(a: ProposalAnalytics) -> str:
    v = a.vol
    ratio = NA if v.iv_hv20 is None else f"{v.iv_hv20:.2f}"
    return "\n".join(
        [
            f"ATM IV {_pct(v.atm_iv, 1) or NA}",
            f"IV rank {_pct(v.iv_rank) or NA}",
            f"IV percentile {_pct(v.iv_percentile) or NA}",
            f"HV20 {_pct(v.hv20, 1) or NA}",
            f"HV60 {_pct(v.hv60, 1) or NA}",
            f"IV/HV20 {ratio}",
        ]
    )


def _market_facts(p: Proposal, a: ProposalAnalytics | None) -> list[dict[str, Any] | None]:
    if a is None:
        return []
    return list(
        B.facts(
            [
                ("Underlying", _underlying(p, a)),
                ("Moneyness (% / σ from spot)", _moneyness(a)),
                ("Vol stats", _vol(a)),
            ]
        )
    )


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


def _summary(p: Proposal, decision: GateDecision | None, a: ProposalAnalytics | None) -> str:
    net = p.structure.net_debit_credit
    em = a.exit_model if a else None
    parts = [
        f"{_net_word(net).capitalize()} {abs(net):.2f}",
        f"Max gain {_money(p.structure.max_gain)}",
        f"Max loss {_money(p.structure.max_loss)}",
        f"PoP {p.quant.pop:.0%}",
    ]
    if em is not None:
        parts.append(
            f"Net EV {_money(em.managed.net_ev)} managed / {_money(em.static.net_ev)} hold"
        )
    else:
        parts.append(f"Net EV {NA}")
    parts.append(f"x{p.sizing.contracts}")
    if a is not None and a.account_profile:
        parts.append(f"Account {a.account_profile}")
    parts.append(_gate_short(decision))
    return " · ".join(parts)


def _body(
    p: Proposal,
    decision: GateDecision | None,
    proposal_hash: str,
    trail: DecisionTrail | None = None,
    kind: str = "open",
) -> list[dict[str, Any]]:
    t = trail or DecisionTrail()
    a = t.analytics
    em = a.exit_model if a else None
    blocks: list[dict[str, Any] | None] = [
        B.header(title(p, kind)),
        B.summary(_summary(p, decision, a)),
        B.divider(),
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Legs*\n{_legs(p, a)}"}},
    ]
    pairs = [
        ("Entry", _entry(p, decision)),
        ("Payoff (per contract)", _payoff(p)),
        (f"Position (x{p.sizing.contracts})", _position(p)),
        ("Breakevens", _breakevens(p, a)),
        ("Edge", _edge(p)),
        ("Net Greeks (position)", _greeks(p)),
        ("Gate", _gate(decision)),
    ]
    liq = _liquidity(p)
    if liq:
        pairs.append(("Liquidity", liq))
    blocks.extend(B.facts(pairs))
    blocks.append(B.divider())
    blocks.extend(_net_ev_section(p, em))
    blocks.extend(_exit_plan(p, em))
    blocks.extend(_costs(p, a))
    blocks.extend(_market_facts(p, a))
    if decision is not None and not decision.passed:
        blocks.append(B.bullets("Gate violations", decision.violations))
    blocks.append(B.divider())
    blocks.extend(_why(p, t))
    blocks.append(B.footer(proposal=proposal_hash[:12], chain=t.chain_run_id))
    return [b for b in blocks if b is not None]


def _market_line(t: DecisionTrail) -> str:
    parts: list[str] = []
    f = t.features or {}
    regime = (f.get("regime") or {}).get("current")
    if regime:
        parts.append(f"Regime *{regime}*")
    if t.analytics is None:  # the vol stats grid covers these when analytics exist
        vol = f.get("vol") or {}
        if (iv := _pct(vol.get("iv"))) is not None:
            parts.append(f"IV {iv}")
        if (ivr := _pct(vol.get("iv_rank"))) is not None:
            parts.append(f"IV rank {ivr}")
        if (hv := _pct(vol.get("hv20"))) is not None:
            parts.append(f"HV20 {hv}")
    if t.market_regime:
        parts.append(f"Research market read *{B.esc(t.market_regime)}*")
    return " · ".join(parts)


def _why(p: Proposal, t: DecisionTrail) -> list[dict[str, Any] | None]:
    """Per-persona reasoning, each block attributed to the persona that wrote it."""
    out: list[dict[str, Any] | None] = []
    market = _market_line(t)
    if market:
        out.append(B.summary(f":bar_chart: {market}"))

    d = t.research or {}
    meta = []
    if d.get("rank"):
        meta.append(f"rank {d['rank']} of {t.shortlist_size}")
    if d.get("stance"):
        meta.append(str(d["stance"]))
    if (c := _pct(d.get("confidence"))) is not None:
        meta.append(f"confidence {c}")
    thesis = B.section_text(
        [
            ("Thesis", B.esc(p.thesis)),
            ("Regime", B.esc(str(d.get("regime_context") or ""))),
        ],
        escape=False,
    )
    out.append(
        B.persona_section(
            Persona.RESEARCH,
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
    rv = t.revision or {}
    if rv.get("rationale"):  # E13.9: Quant re-chose the structure for Risk
        out.append(B.persona_section(Persona.QUANT, "Revised for Risk", str(rv["rationale"])))

    r = t.risk or {}
    head = []
    if r.get("risk_rating"):
        head.append(f"Rating *{B.esc(str(r['risk_rating']))}*")
    if r.get("sizing_suggestion") is not None:
        n = p.sizing.contracts
        sug = int(r["sizing_suggestion"])
        head.append(f"Suggested {sug} → sized {n}" + (" (5% equity cap)" if n < sug else ""))
    if r.get("concentration_warning"):
        head.append(":warning: Concentration")
    req = r.get("revise_request")
    if r.get("verdict") == "revise" and isinstance(req, dict):  # E13.9
        head.append(f"Asked Quant to revise ({B.esc(str(req.get('reason', '')))})")
    lines = [" · ".join(head)] if head else []
    calendar_greeks = B.section_text(
        [
            ("Calendar", B.esc(str(r.get("calendar_concerns") or ""))),
            ("Greek budget", B.esc(str(r.get("greek_budget_impact") or ""))),
            ("Risks", B.esc(p.risk_narrative)),
        ],
        escape=False,
    )
    if calendar_greeks:
        lines.append(calendar_greeks)
    out.append(B.persona_section(Persona.RISK, "Review", "\n".join(lines), escape=False))
    return out


def _fallback(p: Proposal, decision: GateDecision | None, kind: str = "open") -> str:
    verdict = "gate PASS" if decision is not None and decision.passed else "gate FAIL"
    return f"{title(p, kind)} • x{p.sizing.contracts} • {verdict}"


def _cap(blocks: list[dict[str, Any]], tail: int) -> list[dict[str, Any]]:
    """Keep the message under Slack's 50-block cap (drops reasoning blocks before the footer)."""
    over = len(blocks) + tail - B.MAX_BLOCKS
    if over <= 0:
        return blocks
    return blocks[: len(blocks) - 1 - over] + blocks[-1:]


def render_card(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    actionable: bool,
    note: str = "",
    trail: DecisionTrail | None = None,
    kind: str = "open",
) -> CardView:
    """The card as first posted.

    ``actionable`` adds the Approve / Reject buttons and the TTL line; an
    informational card (gate failed, no token) gets ``note`` instead. An actionable
    card with a ``note`` (E7.5a: auto-approve held back by the scorecard gate) shows
    it under the buttons.
    """
    tail = 3 if actionable and note else 2
    blocks = _cap(_body(proposal, decision, proposal_hash, trail, kind), tail)
    if actionable:
        expires = proposal.expires_at.astimezone(ET)
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":hourglass_flowing_sand: Expires {expires:%H:%M} ET. "
                        "No decision by then = *rejected*.",
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
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": B.esc(note)}]})
    return CardView(text=_fallback(proposal, decision, kind), blocks=blocks)


def render_resolved(
    proposal: Proposal,
    decision: GateDecision | None,
    *,
    proposal_hash: str,
    outcome: str,
    at: _dt.datetime,
    trail: DecisionTrail | None = None,
    kind: str = "open",
) -> CardView:
    """The card after it resolved: no buttons, one outcome line (``outcome`` is mrkdwn)."""
    blocks = _cap(_body(proposal, decision, proposal_hash, trail, kind), 1)
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{outcome} · {at.astimezone(ET):%H:%M} ET"}],
        }
    )
    return CardView(text=f"{_fallback(proposal, decision, kind)} — {outcome}", blocks=blocks)
