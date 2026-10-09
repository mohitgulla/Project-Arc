"""D65: punchy headlines for #arc-investor: one per loop root, one per day recap.

A headline is at most two short sentences, written like a news headline: the
main thing that happened (a fill, a close, or why nothing traded), then the
thesis or the market read behind it. It is deliberately not exhaustive; the
thread under the root keeps every detail.

Deterministic: the facts come from what a chain journaled (``decisions`` /
``proposals`` / ``executions``) and the thesis/market sentences are lifted from
the personas' stored text. No LLM call, so a later fill or approval just
re-renders it with the root.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "BLOCKER_LABELS",
    "ChainFacts",
    "chain_facts",
    "day_recap",
    "first_sentence",
    "loop_headline",
    "names",
]

# Why a ranked name was not opened (first match wins: the later the stage, the more
# final the reason). Keys are short labels the recap counts by.
_OPEN_BLOCKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gate:",), "gate"),
    (("sizing:risk_zero", "sizing:cap_zero", "sizing:budget_exhausted"), "sizing"),
    (("order_budget_exhausted",), "budget"),
    (("net_ev_floor",), "ev"),
    (("risk_reject", "risk_declined"), "risk"),
    (("dedupe_executed", "dedupe_proposed", "dedupe_rejected"), "dedupe"),
    (("drop_concentration", "drop_at_cap"), "concentration"),
    (("reprice_failed",), "reprice"),
    (("no_structure", "quant_skipped", "no_chain", "not_structured", "quant_omitted"), "structure"),
)
#: Plain words per blocker, for the recap's "Held back by" line.
BLOCKER_LABELS = {
    "gate": "risk gate",
    "sizing": "sized to zero",
    "budget": "order budget",
    "ev": "Net EV floor",
    "risk": "Risk veto",
    "dedupe": "recently traded",
    "concentration": "concentration",
    "reprice": "re-price failed",
    "structure": "no workable structure",
    "stopped": "loop cut short",
    "other": "not proposed",
}
# Exit reasons as the tail of "MRVL closed …".
_EXIT_WHY = {
    "ev_exhausted": "with the edge used up",
    "ev_remaining": "to free capital for better edge",
    "concentration": "to cut concentration",
    "thesis_broken": "as the thesis broke",
    "costs_exceed_gain": "before costs ate the gain",
    "exit:stop": "on its stop",
    "exit:take_profit": "at the profit target",
    "exit:dte": "ahead of expiry",
    "exit:expiry": "at expiry",
    "exit:hold_limit_reached": "after too many holds",
}
_FIT = {
    "hedges": ", hedging the book",
    "diversifies": ", diversifying the book",
    "adds_concentration": ", adding to a crowded sector",
}
_STRUCT_NAMES = {
    "long_call": "long call",
    "long_put": "long put",
    "vertical_debit": "{side} debit spread",
    "vertical_credit": "{side} credit spread",
    "iron_condor": "iron condor",
}
# A thesis sentence that only restates the structure ("Bearish put debit vertical on
# Oracle.") is skipped in favour of the next one, which carries the why.
_STRUCTURE_ONLY = re.compile(
    r"^(bull(ish)?|bear(ish)?|neutral)\b.{0,60}\b(vertical|spread|call|put|condor)\b[^.]{0,40}\.?$",
    re.IGNORECASE,
)


@dataclass
class Trade:
    ticker: str
    kind: str  # open | close
    structure: str
    status: str  # filled | working | pending | missed | rejected | expired | proposed
    qty: int | None = None
    price: str = ""


@dataclass
class ChainFacts:
    """What one loop chain did, reduced to what a headline needs."""

    ideas: int = 0
    ranked: list[str] = field(default_factory=list)
    stance: dict[str, str] = field(default_factory=dict)
    fit: dict[str, str] = field(default_factory=dict)
    thesis: dict[str, str] = field(default_factory=dict)
    opens: list[Trade] = field(default_factory=list)
    closes: list[Trade] = field(default_factory=list)
    blocked: dict[str, list[str]] = field(default_factory=dict)  # blocker -> tickers
    exit_why: dict[str, str] = field(default_factory=dict)  # ticker -> reason key
    exit_note: dict[str, str] = field(default_factory=dict)  # ticker -> Risk's reason
    exit_stuck: list[str] = field(default_factory=list)  # close wanted, quotes unusable
    no_trade: str = ""  # Research's session-level no-trade reason code
    market: str = ""  # Research's market read (first sentence)
    has_research: bool = False


def names(items: list[str], limit: int = 3) -> str:
    """``A``, ``A and B``, ``A, B and C``, ``A, B, C +2``."""
    if len(items) > limit:
        return ", ".join(items[:limit]) + f" +{len(items) - limit}"
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def first_sentence(text: str, *, skip_structure: bool = False) -> str:
    """The first sentence of persona prose, minus a ``label:`` lead-in."""
    body = text.strip()
    head, sep, rest = body.partition(": ")
    if sep and len(head) <= 24 and " " not in head.strip():
        body = rest
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", body) if p.strip()]
    if skip_structure and len(parts) > 1 and _STRUCTURE_ONLY.match(parts[0]):
        parts = parts[1:]
    return parts[0] if parts else ""


def _struct(structure_json: str | None) -> str:
    try:
        st = json.loads(structure_json or "{}")
        name = _STRUCT_NAMES.get(str(st.get("kind") or ""), "")
        if "{side}" in name:
            occ = str(st["legs"][0]["occ_symbol"])
            name = name.format(side="put" if occ[-9:-8] == "P" else "call")
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return name


def _price(raw: object) -> str:
    try:
        return f"{abs(float(str(raw))):.2f}"
    except (TypeError, ValueError):
        return ""


def _status(p: sqlite3.Row) -> str:
    if p["execution"] in ("filled", "partially_filled"):
        return "filled"
    if p["execution"] == "cancelled":
        return "missed"
    if p["approval"] == "pending":
        return "pending"
    if p["approval"] == "approved":
        return "working"
    if p["approval"] in ("rejected", "expired"):
        return str(p["approval"])
    return "proposed"


def chain_facts(conn: sqlite3.Connection, chain_run_id: str) -> ChainFacts:
    """Read one chain's journal into :class:`ChainFacts` (read-only)."""
    rows = conn.execute(
        """SELECT d.persona, d.stage, d.choice, d.reason_code, d.subject, d.reason_text,
                  d.payload
           FROM decisions d LEFT JOIN routine_runs r ON r.run_id = d.run_id
           WHERE d.chain_run_id = ? OR r.chain_run_id = ? ORDER BY d.rowid""",
        (chain_run_id, chain_run_id),
    ).fetchall()
    props = conn.execute(
        """SELECT p.ticker, p.kind, p.structure_json, a.status AS approval,
                  e.status AS execution, e.filled_qty, e.fill_price, e.contracts
           FROM proposals p
           JOIN routine_runs r ON r.run_id = p.run_id
           LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash
           LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash
           WHERE r.chain_run_id = ? ORDER BY p.rowid""",
        (chain_run_id,),
    ).fetchall()
    ran = {
        str(r["job"])
        for r in conn.execute(
            "SELECT job FROM routine_runs WHERE chain_run_id = ? AND status = 'ok'",
            (chain_run_id,),
        )
    }
    f = ChainFacts(has_research="research" in ran)
    tickers: dict[str, str] = {}

    def ticker(subject: str) -> str:
        if subject not in tickers:
            row = conn.execute(
                "SELECT ticker FROM open_structures WHERE id = ?", (subject,)
            ).fetchone()
            tickers[subject] = str(row["ticker"]) if row else subject
        return tickers[subject]

    ideas: set[str] = set()
    for r in rows:
        code, subject = str(r["reason_code"]), str(r["subject"])
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        if r["stage"] == "candidate" and r["choice"] == "selected" and r["persona"] != "system":
            ideas.add(subject)
        elif r["stage"] == "shortlist" and r["choice"] == "selected":
            if subject not in f.ranked:
                f.ranked.append(subject)
            f.stance[subject] = str(payload.get("stance") or "")
            f.fit[subject] = str(payload.get("portfolio_fit") or "")
            f.thesis[subject] = str(payload.get("thesis") or r["reason_text"] or "")
        elif r["stage"] == "shortlist" and r["choice"] == "no_trade":
            f.no_trade = f.no_trade or code
        elif code == "market_read":
            f.market = first_sentence(str(r["reason_text"] or ""))
        elif code == "exit:research_review" and r["persona"] == "risk":
            verdict = payload.get("verdict") or {}
            if verdict.get("verdict") == "close":
                t = ticker(subject)
                f.exit_why[t] = str(verdict.get("reason_code") or "")
                f.exit_note[t] = first_sentence(str(verdict.get("reason") or ""))
        elif code in _EXIT_WHY and r["stage"] == "exit":
            f.exit_why.setdefault(ticker(subject), code)
        elif code == "exit:quote_unusable":
            t = ticker(subject)
            if t not in f.exit_stuck:
                f.exit_stuck.append(t)
    f.ideas = len(ideas)
    for p in props:
        trade = Trade(
            ticker=str(p["ticker"]),
            kind="close" if p["kind"] == "close" else "open",
            structure=_struct(p["structure_json"]),
            status=_status(p),
            qty=p["filled_qty"] or p["contracts"],
            price=_price(p["fill_price"]),
        )
        (f.closes if trade.kind == "close" else f.opens).append(trade)
    acted = {t.ticker for t in f.opens}
    stopped = f.has_research and "quant.open" not in ran
    for t in f.ranked:
        if t in acted:
            continue
        codes = [
            str(r["reason_code"]) for r in rows if r["subject"] == t and r["choice"] != "selected"
        ]
        key = next(
            (
                label
                for prefixes, label in _OPEN_BLOCKERS
                if any(c.startswith(pre) for c in codes for pre in prefixes)
            ),
            "stopped" if stopped else "other",
        )
        f.blocked.setdefault(key, []).append(t)
    closing = {t.ticker for t in f.closes}
    f.exit_stuck = [t for t in f.exit_stuck if t not in closing]
    return f


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------


def _bias(f: ChainFacts, t: str) -> str:
    return {"bullish": "bull", "bearish": "bear"}.get(f.stance.get(t, ""), "")


def _open_sentence(f: ChainFacts) -> str:
    filled = [t for t in f.opens if t.status == "filled"]
    if filled:
        t = filled[0]
        what = " ".join(x for x in (t.ticker, _bias(f, t.ticker), "bet") if x)
        size = f"{t.structure} x{t.qty}" if t.structure else f"x{t.qty}"
        at = f" at {t.price}" if t.price else ""
        more = f" (+{len(filled) - 1} more)" if len(filled) > 1 else ""
        return f"{what} is on: {size} filled{at}{_FIT.get(f.fit.get(t.ticker, ''), '')}{more}."
    for status, words in (
        ("pending", "awaits your approval"),
        ("working", "is working toward a fill"),
        ("missed", "missed: no fill inside the price band"),
        ("rejected", "was rejected"),
        ("expired", "expired unapproved"),
    ):
        hit = [t for t in f.opens if t.status == status]
        if hit:
            t = hit[0]
            return f"{t.ticker} {t.structure or 'trade'} {words}.".replace("  ", " ")
    return ""


def _close_sentence(f: ChainFacts) -> str:
    sold = [t for t in f.closes if t.status == "filled"]
    if sold:
        why = _EXIT_WHY.get(f.exit_why.get(sold[0].ticker, ""), "")
        return f"Closed {names([t.ticker for t in sold])}" + (f" {why}" if why else "") + "."
    for status, words in (
        ("pending", "close awaits your approval"),
        ("working", "close is working"),
        ("missed", "close missed: no fill inside the band"),
    ):
        hit = [t.ticker for t in f.closes if t.status == status]
        if hit:
            return f"{names(hit)} {words}."
    return ""


def _stuck_sentence(f: ChainFacts) -> str:
    if not f.exit_stuck:
        return ""
    t = f.exit_stuck[0]
    why = _EXIT_WHY.get(f.exit_why.get(t, ""), "")
    lead = f"Want out of {names(f.exit_stuck)}" + (f" {why}" if why else "")
    return f"{lead}, but quotes are too wide to close."


_NO_TRADE = {
    "ev": "No edge, no trade: {n} fell short of the Net EV floor.",
    "risk": "Risk said no to {n}.",
    "structure": "No workable structure for {n}.",
    "gate": "The risk gate blocked {n}.",
    "sizing": "{n} sized to zero.",
    "budget": "Order budget is spent; {n} must wait.",
    "dedupe": "{n} already traded recently; nothing new.",
    "concentration": "{n} dropped: the book is already crowded there.",
    "reprice": "{n} could not be re-priced at fresh quotes.",
    "stopped": "Shortlisted {n}, but the loop stopped after Research.",
    "other": "{n} shortlisted, nothing proposed.",
}


def _no_trade_sentence(f: ChainFacts) -> str:
    if not f.ranked:
        why = f.no_trade.replace("_", " ")
        lead = f"Research passed on all {f.ideas} ideas" if f.ideas else "Research passed"
        return lead + (f" ({why})." if why else ".")
    main = max(f.blocked.items(), key=lambda kv: len(kv[1]), default=None)
    if main is None:
        return ""
    key, tickers = main
    out = _NO_TRADE.get(key, _NO_TRADE["other"]).format(n=names(tickers))
    return out[0].upper() + out[1:]


def _thesis(f: ChainFacts, ticker: str) -> str:
    return first_sentence(f.thesis.get(ticker, ""), skip_structure=True)


def loop_headline(conn: sqlite3.Connection, chain_run_id: str) -> list[str]:
    """≤2 headline sentences for *chain_run_id*: the main action, then its why.

    Order: an open fill (or its pending/working/missed state), a close, else why
    nothing traded. The second sentence is a second action when there is one,
    otherwise the thesis behind the first (Research's for an open, Risk's for a
    close), a close stuck on wide quotes, or Research's market read.
    """
    f = chain_facts(conn, chain_run_id)
    opened = _open_sentence(f)
    closed = _close_sentence(f)
    lines: list[str] = []
    if opened and closed:
        return [opened, closed]
    if opened:
        t = f.opens[0].ticker
        lines = [opened, _thesis(f, t)]
    elif closed:
        sold = [t.ticker for t in f.closes]
        lines = [closed, f.exit_note.get(sold[0], "") if sold else ""]
        if not lines[1] and f.has_research:
            lines[1] = _no_trade_sentence(f)
    elif f.has_research:
        lines = [_no_trade_sentence(f), _stuck_sentence(f) or f.market]
    else:
        lines = [_stuck_sentence(f)]
    return [ln for ln in lines if ln][:2]


# ---------------------------------------------------------------------------
# D65: the end-of-day recap (posted after the reconcile, broadcast to the channel)
# ---------------------------------------------------------------------------


def _utc_bounds(day: _dt.date) -> tuple[str, str]:
    """``[start, end)`` of the ET *day* as UTC ``…Z`` strings (the store's format)."""
    from arc.utils.calendar import ET

    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=ET).astimezone(_dt.UTC)
    end = start + _dt.timedelta(days=1)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


def _money(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def day_recap(
    conn: sqlite3.Connection,
    day: _dt.date,
    *,
    day_pnl: float | None = None,
    equity_start: float | None = None,
) -> list[str]:
    """≤2 headline sentences for ET *day*: the result, then what drove or held it back.

    Line 1: the day's P&L with the trade count and the biggest closed P&L.
    Line 2: the most common reason ranked names didn't open, over the day's full loops,
    plus how many slots were skipped as unchanged.
    """
    lo, hi = _utc_bounds(day)
    fills = conn.execute(
        """SELECT p.kind, COUNT(*) AS n FROM executions e
           JOIN proposals p ON p.proposal_hash = e.proposal_hash
           WHERE e.status IN ('filled', 'partially_filled')
             AND e.started_at >= ? AND e.started_at < ? GROUP BY p.kind""",
        (lo, hi),
    ).fetchall()
    opened = sum(int(r["n"]) for r in fills if r["kind"] != "close")
    closed = sum(int(r["n"]) for r in fills if r["kind"] == "close")
    realized: list[tuple[str, float]] = []
    for r in conn.execute(
        """SELECT subject, payload FROM decisions
           WHERE reason_code = 'exit:closed' AND at >= ? AND at < ?""",
        (lo, hi),
    ):
        try:
            realized.append((str(r["subject"]), float(json.loads(r["payload"])["realized_pnl"])))
        except (ValueError, KeyError, TypeError):
            continue
    runs = conn.execute(
        """SELECT chain_run_id, summary FROM routine_runs
           WHERE job = 'research' AND reason = 'schedule' AND status = 'ok'
             AND scheduled_for >= ? AND scheduled_for < ? AND chain_run_id IS NOT NULL""",
        (lo, hi),
    ).fetchall()
    skips = sum(str(r["summary"] or "").startswith("no_change") for r in runs)
    blockers: dict[str, int] = {}
    ranked = 0
    for r in runs:
        if str(r["summary"] or "").startswith("no_change"):
            continue
        f = chain_facts(conn, str(r["chain_run_id"]))
        ranked += len(f.ranked)
        for key, tickers in f.blocked.items():
            blockers[key] = blockers.get(key, 0) + len(tickers)

    trades = []
    if opened:
        trades.append(f"{opened} open{'s' if opened != 1 else ''}")
    if closed:
        trades.append(f"{closed} close{'s' if closed != 1 else ''}")
    on = f" on {' and '.join(trades)}" if trades else ", no trades"
    if day_pnl is None:
        first = f"Day done{on}."
    else:
        pct = f" ({day_pnl / equity_start:+.1%})" if equity_start else ""
        mood = "Green day" if day_pnl > 0 else "Red day" if day_pnl < 0 else "Flat day"
        first = f"{mood}: {_money(day_pnl)}{pct}{on}."
    if realized:
        worst = min(realized, key=lambda kv: kv[1])
        best = max(realized, key=lambda kv: kv[1])
        pick = worst if abs(worst[1]) >= abs(best[1]) else best
        verb = "worst close" if pick[1] < 0 else "best close"
        first = first[:-1] + f"; {verb} {pick[0]} {_money(pick[1])}."
    lines = [first]
    if blockers:
        key, n = max(blockers.items(), key=lambda kv: kv[1])
        second = f"Biggest blocker: {BLOCKER_LABELS.get(key, key)}, {n} of {ranked} ranked picks"
        second += f"; {skips} quiet slot{'s' if skips != 1 else ''} skipped." if skips else "."
        lines.append(second)
    elif runs:
        lines.append(
            f"{len(runs)} loops ran"
            + (f", {skips} quiet slot{'s' if skips != 1 else ''} skipped." if skips else ".")
        )
    return lines
