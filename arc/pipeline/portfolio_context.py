"""Portfolio context for Research (E5.9, D33): deterministic, no LLM.

:func:`build_portfolio_context` turns the account, the open structures and the
market marks into one typed :class:`PortfolioContext`, written to the context
store as kind ``portfolio_context`` (subject ``session``, TTL 10 min, supersede
latest). :func:`render_portfolio_context` is the bounded prompt block the
Research reads. With an empty book ``PortfolioContext.empty`` is true and the
rendered block is one line, so Research prompt is today's prompt.

P&L, remaining EV and exit signals come from E6.4's ``position_review`` when a
fresh one exists in the snapshot; otherwise the same evaluator
(:func:`arc.positions.evaluate.review_position`) is run on fresh marks. Nothing
here is a gate input.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import ConfigDict

from arc.models import Greeks, Stance, Structure
from arc.positions.evaluate import PositionReview
from arc.positions.portfolio import (
    GreekUsage,
    PortfolioAccount,
    PortfolioAggregates,
    PortfolioContext,
    PortfolioFlag,
    PortfolioPosition,
    PortfolioThesis,
    bucket_display,
    expiry_bucket,
)
from arc.structures import parse_occ, structure_stance
from arc.utils.calendar import ET, dte_calendar

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from arc.broker.base import AccountInfo
    from arc.config import ArcSettings
    from arc.context.store import ContextSnapshot
    from arc.gate.inputs import Portfolio
    from arc.pipeline.env import PipelineEnv
    from arc.routines.config import ResearchDiversificationSettings

__all__ = [
    "DEFAULT_SECTORS_PATH",
    "PORTFOLIO_SUBJECT",
    "PortfolioAggregates",
    "PortfolioContext",
    "PortfolioFlag",
    "PortfolioPosition",
    "build_portfolio_context",
    "empty_portfolio_line",
    "load_industries",
    "load_sectors",
    "render_portfolio_context",
]

log = structlog.get_logger(__name__)

_FORBID = ConfigDict(extra="forbid")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SECTORS_PATH = REPO_ROOT / "config" / "sectors.yaml"
PORTFOLIO_SUBJECT = "session"
UNKNOWN_SECTOR = "unknown"

# ---------------------------------------------------------------------------
# Sectors
# ---------------------------------------------------------------------------


def load_sectors(path: Path | str | None = None) -> dict[str, str]:
    """``ticker -> sector`` from ``config/sectors.yaml`` (unknown names map to ``unknown``)."""
    p = Path(path) if path is not None else DEFAULT_SECTORS_PATH
    data = yaml.safe_load(p.read_text()) or {}
    out: dict[str, str] = {}
    for sector, tickers in (data.get("sectors") or {}).items():
        for t in tickers or []:
            out[str(t).upper()] = str(sector)
    return out


def load_industries(path: Path | str | None = None) -> dict[str, str]:
    """``ticker -> industry`` from ``config/sectors.yaml`` ``industries:`` (D51, E12.1).

    Unknown names are absent (callers map them to ``unknown``). A ticker listed under
    two industries is a config error.
    """
    p = Path(path) if path is not None else DEFAULT_SECTORS_PATH
    data = yaml.safe_load(p.read_text()) or {}
    out: dict[str, str] = {}
    for industry, tickers in (data.get("industries") or {}).items():
        for t in tickers or []:
            sym = str(t).upper()
            if sym in out:
                msg = f"sectors.yaml: {sym} is in industries {out[sym]!r} and {industry!r}"
                raise ValueError(msg)
            out[sym] = str(industry)
    return out


def _sector(ticker: str, sectors: Mapping[str, str]) -> str:
    return sectors.get(ticker.upper(), UNKNOWN_SECTOR)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _f(x: object) -> float:
    return float(Decimal(str(x)))


def _thesis(conn: sqlite3.Connection, row: Mapping[str, Any]) -> PortfolioThesis:
    prop = conn.execute(
        "SELECT thesis FROM proposals WHERE proposal_hash = ?", (row["open_proposal_hash"],)
    ).fetchone()
    cand = conn.execute(
        "SELECT stance, catalyst_type, catalyst_date, confidence FROM candidates WHERE id = ?",
        (row["candidate_id"],),
    ).fetchone()
    return PortfolioThesis(
        research=str(prop["thesis"]) if prop else "",
        scalp_catalyst=str(cand["catalyst_type"]) if cand else None,
        scalp_catalyst_date=cand["catalyst_date"] if cand else None,
        scalp_stance=Stance(cand["stance"]) if cand else None,
        scalp_confidence=float(cand["confidence"]) if cand else None,
    )


def _fresh_review(
    snapshot: ContextSnapshot | None, structure_id: str, now: _dt.datetime, max_age: _dt.timedelta
) -> PositionReview | None:
    if snapshot is None:
        return None
    entry = snapshot.latest("position_review", structure_id)
    if entry is None or now - entry.valid_from > max_age:
        return None
    try:
        return PositionReview.model_validate(entry.payload)
    except ValueError:
        return None


def _computed_review(
    env: PipelineEnv, settings: ArcSettings, row: Mapping[str, Any], st: Structure, today: _dt.date
) -> PositionReview | None:
    """Run E6.4's evaluator on fresh marks (the same code path as positions.evaluate)."""
    from arc.control.effective import cost_model, exit_config
    from arc.execution.exits import price_close
    from arc.exits.position import OpenPosition, PositionMarks
    from arc.positions.evaluate import review_position

    try:
        priced = price_close(env.market, st, as_of=today, r=settings.scanner_risk_free_rate)
        mids = {k: float(c.mid) for k, c in priced.contracts.items() if c.mid is not None}
        return review_position(
            structure_id=str(row["id"]),
            ticker=str(row["ticker"]),
            position=OpenPosition(
                structure=st, entry_net=float(row["entry_net"]), contracts=int(row["contracts"])
            ),
            marks=PositionMarks(
                as_of=today,
                leg_mids=mids,
                leg_spreads=priced.leg_spreads(),
                spot=priced.spot,
                iv=priced.atm_iv,
                r=settings.scanner_risk_free_rate,
                end_of_day=False,
            ),
            exits=exit_config(settings),
            cost=cost_model(settings),
            exit_pending=bool(row.get("exit_proposal_hash")),
        )
    except (LookupError, ValueError) as exc:
        log.warning("portfolio_context.review_failed", structure_id=row["id"], error=str(exc))
        return None


def _scale(g: Greeks, n: int) -> Greeks:
    return Greeks(
        delta=g.delta * n,
        gamma=g.gamma * n,
        vega=g.vega * n,
        theta=g.theta * n,
        rho=g.rho * n,
        vanna=g.vanna * n,
    )


def _shares(totals: Mapping[str, float], total: float) -> dict[str, float]:
    if total <= 0:
        return {k: 0.0 for k in totals}
    return {k: round(v / total, 4) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


def build_portfolio_context(
    conn: sqlite3.Connection,
    env: PipelineEnv,
    settings: ArcSettings,
    *,
    info: AccountInfo,
    now: _dt.datetime,
    halted: bool,
    budget_tier: str,
    portfolio: Portfolio | None = None,
    snapshot: ContextSnapshot | None = None,
    sectors: Mapping[str, str] | None = None,
    review_max_age: _dt.timedelta = _dt.timedelta(minutes=30),
    diversification: ResearchDiversificationSettings | None = None,
) -> PortfolioContext:
    """Assemble Research's portfolio view from the audit DB and the market.

    *portfolio* is the gate ``Portfolio`` (:func:`arc.pipeline.market.build_portfolio`)
    when the caller could value the book; its net Greeks are used. Otherwise Greeks
    are the sum of each structure's as-opened Greeks (``greeks_source=as_opened``).

    E12.5: *diversification* in ``relaxed`` mode flags sector / stance / expiry
    concentration at its relaxed thresholds (never below the strict settings).
    """
    from arc.pipeline.market import account_baseline
    from arc.reconcile.baseline import day_pnl
    from arc.store.execution import OpenStructureRepo

    sectors = sectors if sectors is not None else load_sectors()
    today = now.astimezone(ET).date()
    equity = _f(info.equity)
    rows = OpenStructureRepo(conn).list_open()
    # E5.9b (D43): one start-of-day equity (Arc's prior close) for the loop root,
    # the digest bucket, the gate and the tower.
    baseline = account_baseline(conn, info, now)
    pnl = day_pnl(info.equity, baseline)
    account = PortfolioAccount(
        equity=equity,
        day_pnl=float(pnl) if pnl is not None else None,
        prev_close=float(baseline.value) if baseline is not None else None,
        prev_close_source=baseline.source if baseline is not None else None,
        cash=_f(info.cash),
        buying_power=_f(info.buying_power),
        halted=halted,
        order_budget_tier=budget_tier,
    )
    sector_max, stance_max, expiry_max = (
        settings.portfolio_sector_max_pct,
        settings.portfolio_stance_max_pct,
        settings.portfolio_expiry_max_pct,
    )
    if diversification is not None:
        sector_max, stance_max, expiry_max = diversification.thresholds(
            sector_max, stance_max, expiry_max
        )
    thresholds = {
        "sector_max_pct": sector_max,
        "stance_max_pct": stance_max,
        "expiry_max_pct": expiry_max,
        "greek_near_cap_pct": settings.portfolio_greek_near_cap_pct,
        "max_alloc_pct": settings.max_alloc_pct,
    }
    if not rows:
        return PortfolioContext(as_of=now, empty=True, account=account, thresholds=thresholds)

    positions: list[PortfolioPosition] = []
    warnings: list[str] = []
    opened_greeks = Greeks()
    open_pnl = 0.0
    for row in rows:
        st = Structure.model_validate_json(row["structure_json"])
        n = int(row["contracts"])
        dte = dte_calendar(today, min(parse_occ(leg.occ_symbol).expiration for leg in st.legs))
        review = _fresh_review(snapshot, str(row["id"]), now, review_max_age)
        source: Literal["position_review", "computed", "none"] = "position_review"
        if review is None:
            review = _computed_review(env, settings, row, st, today)
            source = "computed" if review is not None else "none"
        if review is None:
            warnings.append(f"{row['ticker']} {row['id']}: no marks (P&L unknown)")
        thesis = _thesis(conn, row)
        stance = thesis.scalp_stance or structure_stance(st)
        max_loss_total = float(st.max_loss or 0) * n
        g = _scale(st.greeks, n)
        opened_greeks = Greeks(
            delta=opened_greeks.delta + g.delta,
            gamma=opened_greeks.gamma + g.gamma,
            vega=opened_greeks.vega + g.vega,
            theta=opened_greeks.theta + g.theta,
        )
        if review is not None:
            open_pnl += review.pnl_total
        positions.append(
            PortfolioPosition(
                structure_id=str(row["id"]),
                ticker=str(row["ticker"]),
                sector=_sector(str(row["ticker"]), sectors),
                kind=st.kind.value if st.kind else None,
                stance=stance,
                dte=dte,
                expiry_bucket=expiry_bucket(dte),
                contracts=n,
                entry_net=float(row["entry_net"]),
                mark_pnl_per_share=review.pnl if review else None,
                mark_pnl_total=review.pnl_total if review else None,
                pct_of_max_gain=review.pct_of_max_gain if review else None,
                pct_of_max_loss=review.pct_of_max_loss if review else None,
                max_loss_total=round(max_loss_total, 2),
                max_loss_pct_equity=round(max_loss_total / equity, 4) if equity > 0 else 0.0,
                remaining_ev=review.remaining_ev if review else None,
                greeks=g,
                thesis=thesis,
                exit_pending=bool(row.get("exit_proposal_hash"))
                or bool(review and review.exit_pending),
                signals=[s.kind.value for s in review.signals] if review else [],
                review_source=source,
                opened_at=str(row["opened_at"]),
            )
        )
    account = account.model_copy(update={"open_pnl_total": round(open_pnl, 2)})

    total = sum(p.max_loss_total for p in positions)
    by_under: dict[str, float] = {}
    by_sector: dict[str, float] = {}
    by_stance: dict[str, float] = {}
    by_bucket: dict[str, float] = {}
    for p in positions:
        by_under[p.ticker] = by_under.get(p.ticker, 0.0) + p.max_loss_total
        by_sector[p.sector] = by_sector.get(p.sector, 0.0) + p.max_loss_total
        by_stance[p.stance.value] = by_stance.get(p.stance.value, 0.0) + p.max_loss_total
        by_bucket[p.expiry_bucket] = by_bucket.get(p.expiry_bucket, 0.0) + p.max_loss_total
    under_sh, sector_sh = _shares(by_under, total), _shares(by_sector, total)
    stance_sh, bucket_sh = _shares(by_stance, total), _shares(by_bucket, total)
    hhi = round(sum(s * s for s in under_sh.values()), 4)

    net = portfolio.greeks if portfolio is not None else opened_greeks
    delta_cap = settings.portfolio_delta_cap * equity / 100.0
    vega_cap = settings.portfolio_vega_cap_pct * equity
    vega_usd = net.vega / 100.0
    delta = GreekUsage(
        net=round(net.delta, 4),
        cap=round(delta_cap, 4),
        pct_used=round(abs(net.delta) / delta_cap, 4) if delta_cap > 0 else None,
    )
    vega = GreekUsage(
        net=round(vega_usd, 4),
        cap=round(vega_cap, 4),
        pct_used=round(abs(vega_usd) / vega_cap, 4) if vega_cap > 0 else None,
    )

    flags: list[PortfolioFlag] = []
    f_sectors = [s for s, sh in sector_sh.items() if sh > sector_max]
    f_stances = [s for s, sh in stance_sh.items() if s != Stance.NEUTRAL.value and sh > stance_max]
    f_buckets = [b for b, sh in bucket_sh.items() if sh > expiry_max]
    if f_sectors and len(positions) > 1:
        flags.append("over_concentrated_sector")
    if f_stances and len(positions) > 1:
        flags.append("stance_skew")
    if f_buckets and len(positions) > 1:
        flags.append("expiry_cluster")
    near = settings.portfolio_greek_near_cap_pct
    if delta.pct_used is not None and delta.pct_used >= near:
        flags.append("delta_near_cap")
    if vega.pct_used is not None and vega.pct_used >= near:
        flags.append("vega_near_cap")
    cap_usd = settings.max_alloc_pct * equity
    at_cap = sorted(t for t, v in by_under.items() if cap_usd > 0 and v >= cap_usd * 0.999)

    aggregates = PortfolioAggregates(
        total_max_loss=round(total, 2),
        by_underlying=under_sh,
        by_sector=sector_sh,
        by_stance=stance_sh,
        by_expiry_bucket=bucket_sh,
        hhi_underlying=hhi,
        delta=delta,
        vega=vega,
        gamma=round(net.gamma, 4),
        theta=round(net.theta, 4),
        greeks_source="market" if portfolio is not None else "as_opened",
        positions=len(positions),
        max_positions=settings.max_open_positions,
        flags=flags,
        flagged_sectors=f_sectors if len(positions) > 1 else [],
        flagged_stances=f_stances if len(positions) > 1 else [],
        flagged_expiry_buckets=f_buckets if len(positions) > 1 else [],
        at_cap_underlyings=at_cap,
    )
    positions.sort(key=lambda p: (-p.max_loss_total, p.ticker, p.structure_id))
    return PortfolioContext(
        as_of=now,
        empty=False,
        account=account,
        positions=positions,
        aggregates=aggregates,
        thresholds=thresholds,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Rendering (prompt block, bounded)
# ---------------------------------------------------------------------------


def empty_portfolio_line(info: AccountInfo, settings: ArcSettings) -> str:
    """The one portfolio line an empty book puts in Research prompt."""
    return (
        f"Portfolio: no open positions. Equity ${_f(info.equity):,.2f}. Max "
        f"{settings.max_open_positions} positions, {settings.max_alloc_pct:.0%} of equity max "
        "loss per underlying."
    )


def _money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:+,.0f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _mix(shares: Mapping[str, float]) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in shares.items()) or "-"


# E3.4a: week-based bucket labels in the prompt (arc.positions.portfolio.BUCKET_DISPLAY).
_bucket = bucket_display


def expiry_cluster_text(ag: PortfolioAggregates) -> str:
    """The ``expiry_cluster`` flag line: the concentration, never a DTE range.

    The configured window itself is stated once, in the prompt's entry-window
    section (:meth:`arc.personas.entry_window.EntryTerms.research_line`).
    """
    parts = [
        f"{ag.by_expiry_bucket.get(b, 0.0):.0%} of open max loss expires in one bucket "
        f"({_bucket(b)})"
        for b in ag.flagged_expiry_buckets
    ]
    return (
        "expiry_cluster: "
        + "; ".join(parts)
        + ". Spread expiries within the configured entry window; the buckets measure "
        "concentration and are not an entry rule."
    )


def render_portfolio_context(
    pc: PortfolioContext, settings: ArcSettings, *, max_positions: int | None = None
) -> str:
    """Research's prompt block: the account line, top-N positions, aggregates."""
    a = pc.account
    if pc.empty or pc.aggregates is None:
        return (
            f"Portfolio: no open positions. Equity ${a.equity:,.2f}. Max "
            f"{settings.max_open_positions} positions, {settings.max_alloc_pct:.0%} of equity "
            "max loss per underlying."
        )
    ag = pc.aggregates
    limit = max_positions if max_positions is not None else settings.portfolio_context_max_positions
    day = f"{a.day_pnl:+,.0f}" if a.day_pnl is not None else "n/a"
    lines = [
        f"Equity ${a.equity:,.2f} (day P&L {day}, open P&L {a.open_pnl_total:+,.0f}). "
        f"Cash ${a.cash:,.0f}, buying power ${a.buying_power:,.0f}. "
        f"{ag.positions} of {ag.max_positions} positions open; "
        f"{settings.max_alloc_pct:.0%} of equity max loss per underlying."
        + (" HALTED." if a.halted else "")
        + (
            f" Order budget tier: {a.order_budget_tier}." if a.order_budget_tier != "normal" else ""
        ),
        "",
        f"Open positions (largest max loss first; {min(limit, len(pc.positions))} of "
        f"{len(pc.positions)} shown):",
    ]
    for p in pc.positions[:limit]:
        th = p.thesis
        thesis = th.research.strip() or "(no thesis on record)"
        cat = f"{th.scalp_catalyst}" + (
            f" {th.scalp_catalyst_date}" if th.scalp_catalyst_date else ""
        )
        lines.append(
            f"- {p.structure_id} {p.ticker} {p.kind or 'structure'} {p.stance.value} "
            f"x{p.contracts} {p.dte} DTE (expiry bucket {_bucket(p.expiry_bucket)}); "
            f"sector {p.sector}; "
            f"entry {p.entry_net:+.2f}; mark P&L {_money(p.mark_pnl_total)} "
            f"({_pct(p.pct_of_max_gain)} of max gain, {_pct(p.pct_of_max_loss)} of max loss); "
            f"max loss ${p.max_loss_total:,.0f} ({p.max_loss_pct_equity:.1%} of equity); "
            f"Δ {p.greeks.delta:+.1f} ν {p.greeks.vega:+.1f} Θ {p.greeks.theta:+.1f}"
            + (
                f"; remaining EV {_money(p.remaining_ev)}/unit"
                if p.remaining_ev is not None
                else ""
            )
            + ("; exit pending" if p.exit_pending else "")
            + (f"; signals: {', '.join(p.signals)}" if p.signals else "")
            + f"\n  Thesis: {thesis}"
            + (
                f" [Scalp: {th.scalp_stance.value if th.scalp_stance else '?'} {cat}]"
                if th.scalp_catalyst
                else ""
            )
        )
    d, v = ag.delta, ag.vega
    lines += [
        "",
        f"Allocation of open max loss (${ag.total_max_loss:,.0f}): by underlying "
        f"{_mix(ag.by_underlying)}; by sector {_mix(ag.by_sector)}; by stance "
        f"{_mix(ag.by_stance)}; by expiry bucket "
        f"{_mix({_bucket(k): v for k, v in ag.by_expiry_bucket.items()})}. "
        f"HHI {ag.hhi_underlying:.2f}.",
        f"Net Greeks ({ag.greeks_source}): Δ {d.net:+.1f} of cap {d.cap:.1f} "
        f"({_pct(d.pct_used)} used); ν ${v.net:+,.0f}/vol-pt of cap ${v.cap:,.0f} "
        f"({_pct(v.pct_used)} used); Γ {ag.gamma:+.2f}; Θ {ag.theta:+.1f}.",
        "Flags: "
        + (", ".join(ag.flags) if ag.flags else "none")
        + (f" (sectors {', '.join(ag.flagged_sectors)})" if ag.flagged_sectors else "")
        + (f" (stances {', '.join(ag.flagged_stances)})" if ag.flagged_stances else "")
        + (
            f". At per-underlying cap: {', '.join(ag.at_cap_underlyings)}"
            if ag.at_cap_underlyings
            else ""
        )
        + ".",
    ]
    if ag.flagged_expiry_buckets:
        lines.append(expiry_cluster_text(ag))
    if pc.warnings:
        lines.append("Warnings: " + "; ".join(pc.warnings))
    return "\n".join(lines)
