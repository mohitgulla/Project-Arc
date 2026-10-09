"""State of the 5-min trading loop (D31 / D36): digests, thread ids, timeouts.

Everything lives in ``routine_state`` (no migration): the loop's last input
digest and last full-run time (the change-aware skip), the ``ts`` of each
loop's #arc-investor root (``loop_thread:<chain_run_id>``), the per-chain
step-duration summary the ``[Routines]`` reply renders, and the once-a-day
timeout notice. Pure bookkeeping; nothing here calls an LLM or the broker.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from arc.routines.manifest import digest as _digest
from arc.routines.runs import RoutineStateRepo
from arc.slack.loop import LoopRoot, slot_stamp

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

log = structlog.get_logger(__name__)

_LAST_DIGEST = "loop:last_digest"
_LAST_FULL = "loop:last_full_run"
_THREAD = "loop_thread:{chain}"
_ROOT = "loop_root:{chain}"
_SUMMARY = "loop_summary:{chain}"
_TIMEOUT_DAY = "loop:timeout_alerted"
_PROPOSAL_CHAIN = "loop_proposal:{proposal_hash}"


class LoopInputs(BaseModel):
    """What Research's input digest is made of (D31 change-aware skip).

    Every field is deterministic and already rounded: candidate ids with their
    context versions, the regime entries, the portfolio view with P&L in
    ``pnl_bucket_pct``-of-equity buckets, pending orders, the order-budget tier
    and the dedupe-suppressed set. Two loops with the same digest would ask the
    Research the same question.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[str]  # "<entry id>@<version>" sorted
    regimes: list[str]  # "<subject>@<version>" sorted
    positions: list[str]  # "<structure id>:<qty>" sorted
    pnl_bucket: int  # day P&L in buckets of pnl_bucket_pct of equity (signed)
    pending_orders: int
    budget_tier: str
    suppressed: list[str]  # dedupe-suppressed idea keys, sorted
    briefs: list[str] = []  # E4.6: active channel_brief "<subject>@<entry id>", sorted
    # E4.8a: Finnhub facts "<kind>:<ticker>@<as_of>", sorted; [] with the flag off.
    facts: list[str] = []

    def payload(self) -> dict[str, object]:
        """The recorded/digested form; without ``facts`` when empty (flag off), so the
        payload and digest are the pre-E4.8a ones."""
        data = self.model_dump(mode="json")
        if not data["facts"]:
            data.pop("facts")
        return data

    def digest(self) -> str:
        return _digest(self.payload())


def pnl_bucket(day_pnl: float | None, equity: float | None, bucket_pct: float) -> int:
    """Signed bucket index of *day_pnl* in steps of ``bucket_pct``% of *equity*.

    Unknown P&L or a non-positive equity gives bucket 0, so a loop without
    account data does not thrash the digest.
    """
    if day_pnl is None or not equity or equity <= 0 or bucket_pct <= 0:
        return 0
    step = equity * bucket_pct / 100.0
    return int(day_pnl // step) if step > 0 else 0


class LoopState:
    """``routine_state`` accessors for the loop (one instance per dispatcher)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._state = RoutineStateRepo(conn)

    # -- change-aware skip ----------------------------------------------------

    def last_digest(self) -> str | None:
        return self._state.get(_LAST_DIGEST)

    def last_full_run(self) -> _dt.datetime | None:
        return self._state.get_time(_LAST_FULL)

    def should_skip(self, new_digest: str, now: _dt.datetime, max_idle: _dt.timedelta) -> bool:
        """True when the inputs match the last loop and a full run is not yet due."""
        if self.last_digest() != new_digest:
            return False
        last = self.last_full_run()
        return last is not None and now - last < max_idle

    def record_digest(self, new_digest: str, now: _dt.datetime, *, full_run: bool) -> None:
        self._state.set(_LAST_DIGEST, new_digest, now=now)
        if full_run:
            self._state.set_time(_LAST_FULL, now)

    # -- Slack root per loop (D36) -------------------------------------------

    def thread_ts(self, chain_run_id: str) -> str | None:
        return self._state.get(_THREAD.format(chain=chain_run_id))

    def set_thread_ts(self, chain_run_id: str, ts: str, *, now: _dt.datetime | None = None) -> None:
        self._state.set(_THREAD.format(chain=chain_run_id), ts, now=now)

    def root(self, chain_run_id: str) -> dict[str, Any] | None:
        """The stored fields of a loop's root line (rendered again on updates)."""
        raw = self._state.get(_ROOT.format(chain=chain_run_id))
        return json.loads(raw) if raw else None

    def set_root(self, chain_run_id: str, fields: dict[str, Any]) -> None:
        self._state.set(_ROOT.format(chain=chain_run_id), json.dumps(fields, default=str))

    def chain_for_proposal(self, proposal_hash: str) -> str | None:
        return self._state.get(_PROPOSAL_CHAIN.format(proposal_hash=proposal_hash))

    def link_proposal(self, proposal_hash: str, chain_run_id: str) -> None:
        self._state.set(_PROPOSAL_CHAIN.format(proposal_hash=proposal_hash), chain_run_id)

    # -- per-chain summary ([Routines] reply) --------------------------------

    def chain_summary(self, chain_run_id: str) -> dict[str, Any] | None:
        raw = self._state.get(_SUMMARY.format(chain=chain_run_id))
        return json.loads(raw) if raw else None

    def set_chain_summary(self, chain_run_id: str, summary: dict[str, Any]) -> None:
        self._state.set(_SUMMARY.format(chain=chain_run_id), json.dumps(summary, default=str))

    # -- once-a-day timeout notice --------------------------------------------

    def first_timeout_today(self, day: _dt.date) -> bool:
        """True (and remembers it) the first time a loop times out on *day*."""
        if self._state.get(_TIMEOUT_DAY) == day.isoformat():
            return False
        self._state.set(_TIMEOUT_DAY, day.isoformat())
        return True


# ---------------------------------------------------------------------------
# D36: the root line per loop, computed from the audit DB
# ---------------------------------------------------------------------------


def loop_root_from_db(
    conn: sqlite3.Connection,
    chain_run_id: str,
    slot: _dt.datetime,
    *,
    no_change: bool = False,
    timeout: bool = False,
    skipped: str | None = None,
    fallback: LoopRoot | None = None,
) -> LoopRoot:
    """Build the :class:`LoopRoot` for *chain_run_id* from what the chain wrote.

    *fallback* fills equity / day P&L / the order budget when the chain recorded
    none (D38: the position manager writes no ``portfolio_context``).

    Deterministic and re-runnable: an approval, a fill or an expiry later just
    recomputes it. Equity / day P&L come from the chain's ``portfolio_context``
    entry, the order budget from the root run's manifest, the action lists from
    the chain's proposals joined to their approval request and execution.
    """

    equity = day_pnl = None
    row = conn.execute(
        """SELECT payload FROM context_entries
           WHERE kind = 'portfolio_context' AND chain_run_id = ?
           ORDER BY rowid DESC LIMIT 1""",
        (chain_run_id,),
    ).fetchone()
    if row is not None:
        acct = json.loads(row["payload"]).get("account") or {}
        equity, day_pnl = acct.get("equity"), acct.get("day_pnl")
    used = limit = None
    row = conn.execute(
        """SELECT m.payload FROM run_manifests m
           JOIN routine_runs r ON r.run_id = m.run_id
           WHERE r.chain_run_id = ? AND r.step_index = 0
           ORDER BY m.rowid DESC LIMIT 1""",
        (chain_run_id,),
    ).fetchone()
    if row is not None:
        budget = json.loads(row["payload"]).get("order_budget") or {}
        used, limit = budget.get("used"), budget.get("limit")
    buys: list[str] = []
    sells: list[str] = []
    pending: list[str] = []
    working: list[str] = []
    rows = conn.execute(
        """SELECT p.ticker, p.kind, a.status AS approval, e.status AS execution
           FROM proposals p
           JOIN routine_runs r ON r.run_id = p.run_id
           LEFT JOIN approval_requests a ON a.proposal_hash = p.proposal_hash
           LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash
           WHERE r.chain_run_id = ? ORDER BY p.rowid""",
        (chain_run_id,),
    ).fetchall()
    for r in rows:
        ticker, kind = str(r["ticker"]), str(r["kind"])
        execution, approval = r["execution"], r["approval"]
        if execution in ("filled", "partially_filled"):
            (sells if kind == "close" else buys).append(ticker)
        elif approval == "pending":
            pending.append(ticker)
        elif approval == "approved" and execution in (None, "working", "unconfirmed"):
            working.append(ticker)
    if fallback is not None:
        if equity is None:
            equity, day_pnl = fallback.equity, fallback.day_pnl
        if used is None:
            used, limit = fallback.orders_used, fallback.orders_limit
    headline: list[str] = []
    if not (no_change or skipped):
        try:
            headline = loop_headline(conn, chain_run_id)
        except Exception as exc:  # noqa: BLE001 - the headline is presentation; never fail the root
            log.warning("routines.loop_headline_failed", chain_run_id=chain_run_id, err=str(exc))
    return LoopRoot(
        slot=slot,
        equity=equity,
        day_pnl=day_pnl,
        orders_used=used,
        orders_limit=limit,
        buys=_dedupe(buys),
        sells=_dedupe(sells),
        pending=_dedupe(pending),
        working=_dedupe(working),
        no_change=no_change,
        timeout=timeout,
        skipped=skipped,
        headline=headline,
    )


# ---------------------------------------------------------------------------
# D65: the loop headline (≤2 bold-italic lines under the root)
# ---------------------------------------------------------------------------

# Why a ranked name was not opened, from the chain's journal (first match wins,
# in this order: the later the stage, the more final the reason).
_OPEN_BLOCKERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gate:",), "blocked by the gate"),
    (("sizing:risk_zero", "sizing:cap_zero", "sizing:budget_exhausted"), "sized to 0"),
    (("order_budget_exhausted",), "order budget used up"),
    (("net_ev_floor",), "below the Net EV floor"),
    (("risk_reject", "risk_declined"), "rejected by Risk"),
    (("dedupe_executed", "dedupe_proposed", "dedupe_rejected"), "already traded recently"),
    (("drop_concentration", "drop_at_cap"), "dropped for concentration"),
    (("reprice_failed",), "could not re-price"),
    (
        ("no_structure", "quant_skipped", "no_chain", "not_structured", "quant_omitted"),
        "no viable structure",
    ),
)
_EXIT_REASONS = {
    "ev_exhausted": "EV exhausted",
    "ev_remaining": "EV left elsewhere",
    "concentration": "concentration",
    "thesis_broken": "thesis broken",
    "costs_exceed_gain": "costs exceed the gain",
    "exit:stop": "stop",
    "exit:take_profit": "take profit",
    "exit:dte": "DTE exit",
    "exit:expiry": "expiry",
    "exit:hold_limit_reached": "hold limit",
}
_STRUCT_NAMES = {
    "long_call": "Long Call",
    "long_put": "Long Put",
    "vertical_debit": "{side} Debit Spread",
    "vertical_credit": "{side} Credit Spread",
    "iron_condor": "Iron Condor",
}


def _names(items: list[str], limit: int = 4) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" +{len(items) - limit}" if len(items) > limit else "")


def _struct_label(structure_json: str | None) -> str:
    try:
        st = json.loads(structure_json or "{}")
        name = _STRUCT_NAMES.get(str(st.get("kind") or ""), "")
        if "{side}" in name:
            occ = str(st["legs"][0]["occ_symbol"])
            side = "Put" if occ[-9:-8] == "P" else "Call"
            name = name.format(side=side)
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return name


def _price(raw: object) -> str:
    try:
        return f"{abs(float(str(raw))):.2f}"
    except (TypeError, ValueError):
        return "?"


def loop_headline(conn: sqlite3.Connection, chain_run_id: str) -> list[str]:
    """D65: at most two plain lines that say what *chain_run_id* did and why.

    Line 1 is the opens funnel (``21 ideas → 4 ranked → no open: ORCL, GOOGL below
    the Net EV floor; PLTR no viable structure``, or the fill when one opened).
    Line 2 is the exits (``Exits: sold MRVL x1 @ 7.20 (concentration), realized
    -$410; 3 held``), only when the loop reviewed open positions. Deterministic:
    read from the chain's ``decisions`` / ``proposals`` / ``executions`` rows, no
    LLM, so a fill or an approval later just re-renders it.
    """
    rows = conn.execute(
        """SELECT d.persona, d.stage, d.choice, d.reason_code, d.subject, d.payload
           FROM decisions d LEFT JOIN routine_runs r ON r.run_id = d.run_id
           WHERE d.chain_run_id = ? OR r.chain_run_id = ? ORDER BY d.rowid""",
        (chain_run_id, chain_run_id),
    ).fetchall()
    if not rows:
        return []
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
    opens = [p for p in props if p["kind"] != "close"]
    lines: list[str] = []
    if "research" in ran or opens:  # the position manager (D38) has no opens funnel
        lines.append(_opens_line(rows, opens, stopped="quant.open" not in ran))
    exits = _exits_line(conn, rows, [p for p in props if p["kind"] == "close"])
    if exits:
        lines.append(exits)
    return [ln for ln in lines if ln]


def _opens_line(rows: list[sqlite3.Row], opens: list[sqlite3.Row], *, stopped: bool) -> str:
    ideas = {
        r["subject"]
        for r in rows
        if r["stage"] == "candidate" and r["choice"] == "selected" and r["persona"] != "system"
    }
    ranked = _dedupe(
        [str(r["subject"]) for r in rows if r["stage"] == "shortlist" and r["choice"] == "selected"]
    )
    head = f"{len(ideas)} ideas → {len(ranked)} ranked" if ideas else f"{len(ranked)} ranked"
    if not ranked:
        why = next(
            (
                str(r["reason_code"]).replace("_", " ")
                for r in rows
                if r["stage"] == "shortlist" and r["choice"] == "no_trade"
            ),
            "",
        )
        return f"{head}: Research opened nothing" + (f" ({why})" if why else "") + "."
    done: list[str] = []
    for p in opens:
        what = " ".join(x for x in (p["ticker"], _struct_label(p["structure_json"])) if x)
        if p["execution"] in ("filled", "partially_filled"):
            done.append(
                f"bought {what} x{p['filled_qty'] or p['contracts']} @ {_price(p['fill_price'])}"
            )
        elif p["execution"] == "cancelled":
            done.append(f"{what} not filled (ladder cancelled)")
        elif p["approval"] == "pending":
            done.append(f"{what} awaiting approval")
        elif p["approval"] == "approved":
            done.append(f"working {what}")
        elif p["approval"] in ("rejected", "expired"):
            done.append(f"{what} {p['approval']}")
        else:
            done.append(f"proposed {what}")
    acted = {str(p["ticker"]) for p in opens}
    by_reason: dict[str, list[str]] = {}
    for t in ranked:
        if t in acted:
            continue
        codes = [
            str(r["reason_code"]) for r in rows if r["subject"] == t and r["choice"] != "selected"
        ]
        reason = next(
            (
                label
                for prefixes, label in _OPEN_BLOCKERS
                if any(c.startswith(pre) for c in codes for pre in prefixes)
            ),
            "not structured (loop stopped after Research)" if stopped else "not proposed",
        )
        by_reason.setdefault(reason, []).append(t)
    skipped = "; ".join(f"{_names(ts)} {reason}" for reason, ts in by_reason.items())
    if done:
        return f"{head} → " + "; ".join(done) + (f". Skipped: {skipped}." if skipped else ".")
    return f"{head} ({_names(ranked)}) → no open: {skipped}."


def _exits_line(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], closes: list[sqlite3.Row]
) -> str:
    watched = _dedupe(
        [str(r["subject"]) for r in rows if str(r["reason_code"]).startswith("exit:watch_")]
    )
    mandatory = [r for r in rows if r["reason_code"] in _EXIT_REASONS and r["stage"] == "exit"]
    if not watched and not mandatory and not closes:
        return ""
    tickers: dict[str, str] = {}

    def ticker(subject: str) -> str:
        """An open-structure id (``os-…``) as its ticker; a ticker stays as it is."""
        if subject not in tickers:
            row = conn.execute(
                "SELECT ticker FROM open_structures WHERE id = ?", (subject,)
            ).fetchone()
            tickers[subject] = str(row["ticker"]) if row else subject
        return tickers[subject]

    why: dict[str, str] = {}
    for r in rows:
        code = str(r["reason_code"])
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        if code == "exit:research_review" and r["persona"] == "risk":
            verdict = payload.get("verdict") or {}
            if verdict.get("verdict") == "close":
                why[ticker(str(r["subject"]))] = _EXIT_REASONS.get(
                    str(verdict.get("reason_code")), "Risk close"
                )
        elif code in _EXIT_REASONS and r["stage"] == "exit":
            why.setdefault(ticker(str(r["subject"])), _EXIT_REASONS[code])
    realized: dict[str, float] = {}
    for r in rows:
        if r["reason_code"] == "exit:closed":
            with contextlib.suppress(ValueError, KeyError, TypeError):
                realized[str(r["subject"])] = float(
                    json.loads(r["payload"] or "{}")["realized_pnl"]
                )
    blocked = {str(r["subject"]) for r in rows if r["reason_code"] == "exit:quote_unusable"}
    parts: list[str] = []
    for p in closes:
        t = str(p["ticker"])
        tag = f" ({why[t]})" if t in why else ""
        if p["execution"] in ("filled", "partially_filled"):
            part = f"sold {t} x{p['filled_qty'] or p['contracts']} @ {_price(p['fill_price'])}{tag}"
            if t in realized:
                pnl = realized[t]
                part += f", realized {'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}"
            parts.append(part)
        elif p["execution"] == "cancelled":
            parts.append(f"{t} close{tag} not filled (ladder cancelled)")
        elif p["approval"] == "pending":
            parts.append(f"close {t} awaiting approval{tag}")
        else:
            parts.append(f"closing {t}{tag}")
    acted = {str(p["ticker"]) for p in closes}
    for t, reason in why.items():
        if t in acted:
            continue
        if t in blocked:
            parts.append(f"{t} close ({reason}) blocked: quotes too wide")
        else:
            parts.append(f"Risk wants {t} closed ({reason})")
    closing = acted | set(why)
    held = [ticker(s) for s in watched if ticker(s) not in closing]
    if held:
        parts.append(f"{len(held)} held")
    return "Exits: " + "; ".join(parts) + "." if parts else ""


def latest_account_facts(conn: sqlite3.Connection, slot: _dt.datetime) -> LoopRoot:
    """Equity, day P&L and order budget as last recorded (any chain / run).

    D38: the position manager's root shows the same facts as the trading loop's,
    taken from the newest ``portfolio_context`` entry and run manifest.
    """
    equity = day_pnl = None
    row = conn.execute(
        """SELECT payload FROM context_entries WHERE kind = 'portfolio_context'
           ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    if row is not None:
        acct = json.loads(row["payload"]).get("account") or {}
        equity, day_pnl = acct.get("equity"), acct.get("day_pnl")
    used = limit = None
    row = conn.execute(
        """SELECT json_extract(payload, '$.order_budget') AS ob FROM run_manifests
           WHERE json_extract(payload, '$.order_budget') IS NOT NULL
           ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    if row is not None and row["ob"]:
        budget = json.loads(row["ob"])
        used, limit = budget.get("used"), budget.get("limit")
    return LoopRoot(slot=slot, equity=equity, day_pnl=day_pnl, orders_used=used, orders_limit=limit)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in items if not (x in seen or seen.add(x))]


class RootEditor(Protocol):
    """Whoever can edit an #arc-investor root line (a Notifier, a card poster)."""

    def update_root(self, ts: str, text: str) -> None: ...


def refresh_loop_root(
    conn: sqlite3.Connection, editor: RootEditor, chain_run_id: str | None
) -> str | None:
    """Recompute and edit the root line of *chain_run_id* (approval, fill, expiry).

    No-op when the chain has no root (day-thread layout, dry runs, a proposal
    from a manual ``arc propose``). Returns the new text.
    """
    if not chain_run_id:
        return None
    state = LoopState(conn)
    ts = state.thread_ts(chain_run_id)
    stored = state.root(chain_run_id)
    if ts is None or stored is None:
        return None

    prev = LoopRoot.model_validate(stored)
    root = loop_root_from_db(
        conn,
        chain_run_id,
        prev.slot,
        no_change=prev.no_change,
        timeout=prev.timeout,
        skipped=prev.skipped,
        fallback=prev,  # D38: an action chain keeps the facts its root opened with
    )
    text = root.text()
    if text == prev.text():
        return text
    state.set_root(chain_run_id, root.model_dump(mode="json"))
    try:
        editor.update_root(ts, text)
    except Exception as exc:  # noqa: BLE001 - the decision/fill is committed; the edit is best-effort
        log.warning("routines.loop_root_update_failed", chain_run_id=chain_run_id, error=str(exc))
    else:
        log.info("routines.loop_root_updated", chain_run_id=chain_run_id, text=text)
    return text


__all__ = [
    "LoopInputs",
    "LoopRoot",
    "LoopState",
    "RootEditor",
    "latest_account_facts",
    "loop_headline",
    "loop_root_from_db",
    "pnl_bucket",
    "refresh_loop_root",
    "slot_stamp",
]
