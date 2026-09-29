"""Streamlit control tower (E8.3): a read-only view of the audit store.

Run it with ``arc tower`` (binds the Tailscale interface on :4174). The page has
no inputs that change state: no approve, halt, resume or config buttons. Those
stay in Slack, where they are authenticated and audited. The DB is opened
``mode=ro`` (:func:`arc.tower.data.connect_ro`), so a bug here cannot write.

Environment (set by ``arc tower``):

- ``ARC_TOWER_DB``: audit store path (default: ``ArcSettings.db_path`` / data/arc.db)
- ``ARC_TOWER_REFRESH``: seconds between re-reads (default 30; 0 = manual)
- ``ARC_TOWER_LOOKBACK_DAYS``: proposal / violation window (default 7)
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import streamlit as st

from arc.tower.data import TowerSnapshot, connect_ro, load_snapshot
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

__all__ = ["main", "render"]

STALE_AFTER = _dt.timedelta(minutes=45)  # monitor runs every 30 min in session


def _db_path() -> Path:
    env = os.environ.get("ARC_TOWER_DB")
    if env:
        return Path(env)
    from arc.config import get_settings
    from arc.store.db import DEFAULT_DB_PATH

    return get_settings().db_path or DEFAULT_DB_PATH


def _caps() -> tuple[float, float]:
    from arc.config import get_settings

    s = get_settings()
    return s.portfolio_delta_cap, s.portfolio_vega_cap_pct


def _money(v: Decimal | float | None, *, signed: bool = False) -> str:
    if v is None:
        return "n/a"
    return f"${float(v):+,.2f}" if signed else f"${float(v):,.2f}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2%}"


def _ts(v: _dt.datetime | None) -> str:
    return "never" if v is None else f"{v:%a %m-%d %H:%M %Z}"


def _age(v: _dt.datetime | None, now: _dt.datetime) -> str:
    if v is None:
        return ""
    mins = int((now - v).total_seconds() // 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 48:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def _frame(rows: Sequence[dict[str, Any]], empty: str) -> None:
    if rows:
        st.dataframe(pd.DataFrame(list(rows)), hide_index=True, use_container_width=True)
    else:
        st.caption(empty)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _status(snap: TowerSnapshot) -> None:
    active = [h for h in snap.halts if h.active]
    if active:
        reasons = "; ".join(f"{h.actor}: {h.reason}" for h in active)
        st.error(f"HALTED ({len(active)} active): {reasons}")
    else:
        st.success("Trading enabled: no active halt")
    ops = snap.ops
    parts = [
        f"tick {ops.tick_status or 'n/a'} {_age(ops.tick_at, snap.as_of)}".strip(),
        f"health {ops.health_status or 'n/a'} {_age(ops.health_at, snap.as_of)}".strip(),
        f"{len(ops.open_alerts)} open ops alert(s)",
    ]
    st.caption(" · ".join(parts))
    for a in ops.open_alerts:
        st.warning(f"{a['kind']}: {a['message']} (since {a['opened']})")


def _pnl(snap: TowerSnapshot) -> None:
    st.subheader("P&L")
    p = snap.pnl
    c1, c2, c3, c4 = st.columns(4)
    intraday = p.intraday_equity is not None
    c1.metric(
        "Equity",
        _money(p.intraday_equity if intraday else p.equity),
        help="Latest monitor mark" if intraday else "Last reconcile",
    )
    c2.metric("Day P&L", _money(p.intraday_day_pnl if intraday else p.day_pnl, signed=True))
    perf = p.performance
    c3.metric("MTD", _money(perf.mtd_pnl if perf else None, signed=True), _pct_delta(perf, "mtd"))
    c4.metric("YTD", _money(perf.ytd_pnl if perf else None, signed=True), _pct_delta(perf, "ytd"))
    c5, c6, c7 = st.columns(3)
    c5.metric("Realized (reconcile day)", _money(p.realized, signed=True))
    c6.metric("Unrealized (options)", _money(p.unrealized, signed=True))
    clean = {True: "clean", False: "MISMATCH", None: "n/a"}[p.reconcile_clean]
    c7.metric("Last reconcile", clean, help=_ts(p.reconciled_at))
    st.caption(
        f"Intraday mark {_ts(p.intraday_at)} · reconciled {_ts(p.reconciled_at)}"
        + (f" for {p.reconciled_day}" if p.reconciled_day else "")
    )
    if len(p.equity_series) >= 2:
        df = pd.DataFrame([{"day": d, "equity": float(e)} for d, e in p.equity_series]).set_index(
            "day"
        )
        st.line_chart(df, height=180)


def _pct_delta(perf: Any, key: str) -> str | None:
    if perf is None:
        return None
    v = getattr(perf, f"{key}_pct")
    return None if v is None else f"{v:+.2%}"


def _greeks(snap: TowerSnapshot) -> None:
    st.subheader("Greeks (net portfolio)")
    g = snap.greeks
    if g.at is None:
        st.caption("No monitor run recorded yet (the intraday monitor writes these).")
        return
    if not g.valued:
        st.warning(f"Positions could not be valued at {_ts(g.at)}: Greeks unavailable.")
        return
    if snap.as_of - g.at > STALE_AFTER:
        st.warning(f"Greeks are stale: last monitor run {_ts(g.at)} ({_age(g.at, snap.as_of)}).")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Δ (share-eq)", f"{g.delta:+.1f}" if g.delta is not None else "n/a",
              help=f"gate cap ±{g.delta_cap:,.1f}" if g.delta_cap else None)  # fmt: skip
    c2.metric("Γ", f"{g.gamma:+.3f}" if g.gamma is not None else "n/a")
    c3.metric("ν ($/vol-pt)", _money(g.vega_usd, signed=True),
              help=f"gate cap ±{_money(g.vega_cap_usd)}" if g.vega_cap_usd else None)  # fmt: skip
    c4.metric("Θ ($/day)", f"{g.theta:+.2f}" if g.theta is not None else "n/a")
    c5.metric("Max loss", _money(g.max_loss))
    if g.delta is not None and g.delta_cap:
        st.progress(min(abs(g.delta) / g.delta_cap, 1.0), text="|Δ| vs gate cap")
    if g.vega_usd is not None and g.vega_cap_usd:
        st.progress(min(abs(g.vega_usd) / g.vega_cap_usd, 1.0), text="|ν| vs gate cap")
    st.caption(f"{g.positions or 0} position(s) · as of {_ts(g.at)}")


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


def _positions(snap: TowerSnapshot) -> None:
    st.subheader("Positions")
    rows = [
        {
            "ticker": s.ticker,
            "structure": s.kind or "",
            "contracts": s.contracts,
            "entry (per sh)": float(s.entry_net),
            "expiry": s.expiration,
            "unrealized $": None if s.unrealized_pl is None else float(s.unrealized_pl),
            "held at broker": {True: "yes", False: "NO", None: "n/a"}[s.held],
            "exit": s.exit_reason or ("pending" if s.exit_pending else ""),
            "opened": _ts(s.opened_at),
            "id": s.id,
        }
        for s in snap.structures
    ]
    _frame(rows, "No open structures.")
    with st.expander(f"Broker legs ({len(snap.legs)}), from the latest monitor run"):
        _frame(
            [
                {
                    "symbol": leg.symbol,
                    "qty": float(leg.qty),
                    "side": leg.side,
                    "class": leg.asset_class,
                    "avg entry": None
                    if leg.avg_entry_price is None
                    else float(leg.avg_entry_price),
                    "market value": None if leg.market_value is None else float(leg.market_value),
                    "unrealized $": None if leg.unrealized_pl is None else float(leg.unrealized_pl),
                }
                for leg in snap.legs
            ],
            "No broker positions recorded.",
        )


def _proposals(snap: TowerSnapshot) -> None:
    st.subheader("Proposals")
    rows = [
        {
            "created": _ts(p.created_at),
            "ticker": p.ticker or "",
            "kind": p.kind,
            "structure": p.structure_kind or "",
            "contracts": p.contracts,
            "mid (per sh)": None if p.limit is None else float(p.limit),
            "EV $": None if p.ev is None else float(p.ev),
            "PoP": None if p.pop is None else round(p.pop, 3),
            "gate": {True: "PASS", False: "FAIL", None: "n/a"}[p.gate_passed],
            "approval": p.approval or "",
            "execution": p.execution or "",
            "fill": None if p.fill_price is None else float(p.fill_price),
            "hash": p.proposal_hash[:12],
        }
        for p in snap.proposals
    ]
    _frame(rows, "No proposals in the window.")


def _halts(snap: TowerSnapshot) -> None:
    st.subheader("Halts")
    _frame(
        [
            {
                "active": "ACTIVE" if h.active else "",
                "kind": h.kind,
                "raised": _ts(h.at),
                "by": h.actor,
                "reason": h.reason,
                "cleared": _ts(h.cleared_at) if h.cleared_at else "",
                "cleared by": h.cleared_by or "",
            }
            for h in snap.halts
        ],
        "No halts recorded.",
    )


def _violations(snap: TowerSnapshot) -> None:
    st.subheader("Gate violations")
    if snap.violation_counts:
        st.bar_chart(
            pd.DataFrame(
                {"count": list(snap.violation_counts.values())},
                index=list(snap.violation_counts.keys()),
            ),
            height=180,
        )
    _frame(
        [
            {
                "decided": _ts(v.decided_at),
                "ticker": v.ticker or "",
                "kind": v.kind,
                "rule": v.code,
                "detail": v.detail,
                "hash": v.proposal_hash[:12],
            }
            for v in snap.violations
        ],
        "No gate violations in the window.",
    )


def render(snap: TowerSnapshot) -> None:
    """Draw every section for *snap* (no inputs, nothing written)."""
    st.title("Arc control tower")
    st.caption(f"Read-only · paper · as of {_ts(snap.as_of)} · {snap.db_path}")
    _status(snap)
    _pnl(snap)
    _greeks(snap)
    _positions(snap)
    _proposals(snap)
    _halts(snap)
    _violations(snap)


def _load() -> TowerSnapshot | None:
    path = _db_path()
    try:
        conn = connect_ro(path)
    except FileNotFoundError as exc:
        st.error(str(exc))
        return None
    try:
        delta_cap, vega_cap = _caps()
        return load_snapshot(
            conn,
            now=now_et(),
            db_path=str(path),
            lookback_days=int(os.environ.get("ARC_TOWER_LOOKBACK_DAYS", "7")),
            delta_cap=delta_cap,
            vega_cap_pct=vega_cap,
        )
    finally:
        conn.close()


def main() -> None:
    st.set_page_config(page_title="Arc control tower", layout="wide")
    refresh = int(os.environ.get("ARC_TOWER_REFRESH", "30"))

    @st.fragment(run_every=refresh if refresh > 0 else None)
    def _page() -> None:
        snap = _load()
        if snap is not None:
            render(snap)

    _page()


if __name__ == "__main__":
    main()
