"""E7.22 (D125): live <-> sim reconciliation by replay.

Reads the live audit store (and, with ``arm="all"``, the D69 experiment arm stores)
**read-only** and replays what live did through the backtest's own pieces, on the
ThetaData EOD option history (``HistoryView``), to calibrate the sim's three biggest
assumptions:

1. **Slippage.** Every filled execution (``executions`` x ``proposals``): the realised
   ``x = (fill - mid) / spread`` in the sim's own fill model (a buy pays
   ``mid + x * spread``; prices in the order's pay convention, so a positive x is a cost
   and a negative x price improvement). The NBBO is the quote the order was priced on:

   * opens: ``market_contexts.payload.legs[].bid/ask`` (the stored chain snapshot the
     proposal used, ``quotes_as_of`` = submit time);
   * a fill after a D101 re-gate: the ``order:band_moved`` decision's ``mid`` /
     ``far_touch``;
   * closes (no market context is stored): ``proposals.structure_json.legs[].premium``
     (the exit review's mid) and the D24 band ``executions.band_hi - band_lo``
     (``band_hi`` = ``execution_band_reach`` of the way to the far touch, so the combo
     spread is ``2 * (band_hi - band_lo) / reach``; tick snapping and the max-gain cap can
     only shrink it, so this x is an upper bound).

   The fill-time NBBO is not stored (only the submit-time snapshot). Fills are combo
   prices (one mleg order), so x is per structure; per-leg quotes are kept in the
   per-trade detail. The same fill against that session's ThetaData closing NBBO gives
   ``x_eod_equiv``, the x the sim (which fills on the EOD book) would need to reproduce
   the fill. Measured x per kind gets a seeded percentile-bootstrap CI and a proposed
   ``backtest.slippage_by_kind`` (report only).
2. **Candidate overlap.** Each live open proposal's day and ticker through the E7.19 sim
   menu (every sleeve of the day's account profile, ``config/ranking.yaml``): is live's
   structure in it (exact / nearest), its rank under every applicable ranker within the
   live stance's kinds, and the sim's own top pick (trend_v2 proxy stance, the day's
   live menu measure) vs live's pick.
3. **Exit timing.** Each closed live position re-opened on its entry session's EOD book
   and walked forward with the sim's own exit loop (:func:`arc.backtest.sim.exit_walk`)
   under the same policy (the entry DTE's sleeve block), EOD-only and with the intraday
   range proxy, vs live's exit day / reason / P&L.
4. **Stance gap.** Research's stance per (day, ticker) -- ``decisions.payload.stance`` of
   ``persona='research'``, ``stage='shortlist'`` rows, last of the day -- vs the sim's
   proxies ``trend_v2`` (the ticker's own label at that session) and ``market_v2``
   (SPY's), and the E27.2 ``m1_direction`` when a series is supplied. The same rows are
   emitted as the ``live_llm`` stance series for E7.20.

The account profile and menu measure come per day from the run manifests
(``run_manifests.payload.account_profile`` / ``git_sha``), never from today's default.
Every section splits All / Stocks / ETFs and pre/post the D128 margin switch (by
profile). Paper fills are friendlier than live (no queue, generous midpoint fills): the
report carries a ``paper_bias`` note. Deterministic: fixed bootstrap seed, injected
``now``; no network.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import subprocess
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.engine import MULT, STRUCTURE_KIND, open_trade, settle
from arc.backtest.ranking import (
    Candidate,
    _legs,
    build_menus,
    build_menus_by_kind,
    kind_costs,
    live_incumbent,
    profile_allows_credit,
    rankers_for,
    stance_kinds,
)
from arc.backtest.regime import label_trend_v2
from arc.backtest.sim import exit_walk, quote_marks, session_ranges, sleeve_windows
from arc.backtest.strategies import LegPick, StrategyKind, StrategySpec
from arc.exits.policy import resolve_rules
from arc.models import LegIntent, StructureKind
from arc.pricing.bs import OptionKind
from arc.scanner.rank import Ranker, passes_filters, rank
from arc.structures import analyze
from arc.structures.occ import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

__all__ = [
    "PAPER_BIAS",
    "ExitRow",
    "LiveFill",
    "LivePosition",
    "LiveProposal",
    "LiveStance",
    "OverlapRow",
    "ReconReport",
    "SlippageRow",
    "StanceRow",
    "XEstimate",
    "bootstrap_ci",
    "combo_nbbo",
    "estimate_x",
    "live_kind",
    "load_live",
    "match_structure",
    "realised_x",
    "render_markdown",
    "run_recon",
    "stance_rows",
    "write_outputs",
]

#: Stated in every report (owner rule, D125): paper fills are not live fills.
PAPER_BIAS = (
    "Paper fills are friendlier than live: Alpaca's paper engine fills a marketable or "
    "at-mid limit without queue position or partial fills, so the measured x is a lower "
    "bound on live x."
)
#: A CI half-width above this is "insufficient" (card: wider than +/-0.25 x).
MAX_CI_HALF = 0.25
BOOT_N = 2000
BOOT_SEED = 0
#: Profile whose first live day is the D128 margin switch (pre = every other profile).
SWITCH_PROFILE = "margin"
_TREND_STANCE = {"bull": "bullish", "bear": "bearish", "sideways": "neutral"}
_STANCE_LABEL = {v: k for k, v in _TREND_STANCE.items()}
#: The trend label whose stance menu holds a kind (debit/single kinds are directional).
_KIND_TREND = {
    "long_call": "bull",
    "bull_call": "bull",
    "bull_put": "bull",
    "long_put": "bear",
    "bear_put": "bear",
    "bear_call": "bear",
    "iron_condor": "sideways",
}

AssetClass = Literal["stock", "etf"]
_FORBID = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


class LegQuote(BaseModel):
    """One leg of a live order: OCC symbol, signed side, ratio and its quote."""

    model_config = _FORBID

    symbol: str
    side: int = Field(..., description="+1 long (buy to open / bought), -1 short")
    ratio: int = 1
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None


class LiveFill(BaseModel):
    """One filled live execution with the NBBO it was priced on."""

    model_config = _FORBID

    arm: str
    proposal_hash: str
    side: Literal["open", "close"]
    ticker: str
    kind: str = Field(..., description="StructureKind of the position (open's kind)")
    sleeve: str | None
    profile: str | None
    day: dt.date
    filled_at: dt.datetime
    fill: float = Field(..., description="Combo fill, pay convention (+ debit paid)")
    contracts: int
    steps_used: int
    legs: list[LegQuote]
    mid: float | None = Field(None, description="Combo mid at submit, pay convention")
    spread: float | None = Field(None, description="Combo NBBO width at submit (>= 0)")
    nbbo_source: str


class LiveProposal(BaseModel):
    """One live open proposal (whether or not it filled)."""

    model_config = _FORBID

    arm: str
    proposal_hash: str
    day: dt.date
    ticker: str
    kind: str
    sleeve: str | None
    profile: str | None
    menu_measure: str | None
    stance: str | None = Field(None, description="candidates.stance of its candidate")
    legs: list[LegQuote]


class LivePosition(BaseModel):
    """One closed live position (``open_structures`` status closed)."""

    model_config = _FORBID

    arm: str
    structure_id: str
    ticker: str
    kind: str
    sleeve: str | None
    profile: str | None
    regime: str | None
    legs: list[LegQuote]
    contracts: int
    entry_day: dt.date
    entry_fill: float
    exit_day: dt.date
    close_fill: float | None = Field(None, description="Pay convention (- = received)")
    exit_reason: str | None
    realised_pnl: float | None


class LiveStance(BaseModel):
    """Research's stance on one name on one day (the LLM stance)."""

    model_config = _FORBID

    arm: str
    day: dt.date
    ticker: str
    stance: str


class SlippageRow(BaseModel):
    model_config = _FORBID

    arm: str
    proposal_hash: str
    side: str
    ticker: str
    asset: AssetClass
    kind: str
    sleeve: str | None
    profile: str | None
    period: str
    tod: str
    step: int
    contracts: int
    fill: float
    mid: float | None
    spread: float | None
    nbbo_source: str
    x: float | None
    x_eod: float | None
    eod_note: str | None = None


class OverlapRow(BaseModel):
    model_config = _FORBID

    arm: str
    day: dt.date
    ticker: str
    asset: AssetClass
    profile: str | None
    period: str
    live_kind: str | None
    live_key: str
    menu_measure: str | None
    menu_size: int
    match: str = Field(..., description="exact | same_expiry | nearest | kind_absent | skip")
    expiry_diff_days: int | None = None
    strike_diff_pct: float | None = None
    ranks: dict[str, int | None] = Field(default_factory=dict)
    sim_top_key: str | None = None
    sim_top_kind: str | None = None
    sim_top_is_live: bool | None = None
    live_passes_filters: bool | None = Field(
        None, description="The matched candidate passes ranking.filters on the EOD book"
    )
    live_managed_net_ev: float | None = None
    note: str | None = None


class ExitRow(BaseModel):
    model_config = _FORBID

    arm: str
    structure_id: str
    ticker: str
    asset: AssetClass
    kind: str
    regime: str | None
    profile: str | None
    period: str
    contracts: int
    entry_day: dt.date
    live_exit_day: dt.date
    live_reason: str | None
    live_pnl: float | None
    entry_live: float
    entry_sim: float | None = None
    close_live: float | None = None
    close_sim_at_live_day: float | None = None
    sim_pnl_at_live_exit: float | None = Field(
        None, description="Sim fills (EOD book) on live's own entry and exit days, $ total"
    )
    pricing_gap: float | None = Field(None, description="live P&L - sim_pnl_at_live_exit")
    timing_gap: float | None = Field(None, description="sim_pnl_at_live_exit - sim P&L")
    sim_exit_day: dt.date | None = None
    sim_reason: str | None = None
    sim_pnl: float | None = None
    proxy_exit_day: dt.date | None = None
    proxy_reason: str | None = None
    proxy_pnl: float | None = None
    tracking_error: float | None = Field(None, description="live - sim (EOD) P&L, $ total")
    proxy_tracking_error: float | None = None
    note: str | None = None


class StanceRow(BaseModel):
    model_config = _FORBID

    arm: str
    day: dt.date
    ticker: str
    asset: AssetClass
    llm: str
    trend_v2: str
    market_v2: str
    m1_direction: str


class XEstimate(BaseModel):
    model_config = _FORBID

    n: int
    mean: float | None
    lo: float | None
    hi: float | None
    insufficient: bool


class ReconReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: dt.datetime
    git_sha: str
    stores: list[str]
    since: dt.date | None
    sim_slippage_x: float
    slippage: list[SlippageRow]
    overlap: list[OverlapRow]
    exits: list[ExitRow]
    stance: list[StanceRow]
    m1_note: str
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def combo_nbbo(legs: Sequence[LegQuote]) -> tuple[float, float] | None:
    """(mid, spread) of the combo in pay convention; ``None`` when a leg is unquoted.

    ``mid = sum(side * ratio * leg mid)`` and ``spread = sum(ratio * (ask - bid))``: the
    same per-leg sum as the sim's fill model (every leg pays ``x * its spread``).
    """
    mid = spread = 0.0
    for lg in legs:
        if lg.bid is None or lg.ask is None or lg.ask < lg.bid:
            return None
        m = lg.mid if lg.mid is not None else (lg.bid + lg.ask) / 2.0
        mid += lg.side * lg.ratio * m
        spread += lg.ratio * (lg.ask - lg.bid)
    return mid, spread


def realised_x(fill: float, mid: float | None, spread: float | None) -> float | None:
    """``(fill - mid) / spread`` (pay convention; + = cost); ``None`` without a spread."""
    if mid is None or spread is None or not spread > 0:
        return None
    return (fill - mid) / spread


def bootstrap_ci(
    values: Sequence[float], *, n_boot: int = BOOT_N, seed: int = BOOT_SEED, level: float = 0.9
) -> tuple[float, float] | None:
    """Seeded percentile-bootstrap CI of the mean; ``None`` below two values."""
    v = np.asarray([x for x in values if math.isfinite(x)], dtype=float)
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(v, size=(n_boot, len(v)), replace=True).mean(axis=1)
    a = (1.0 - level) / 2.0
    return float(np.quantile(means, a)), float(np.quantile(means, 1.0 - a))


def estimate_x(values: Sequence[float | None]) -> XEstimate:
    """Mean x with its bootstrap CI; insufficient below 2 values or CI half-width > 0.25."""
    v = [float(x) for x in values if x is not None and math.isfinite(x)]
    if not v:
        return XEstimate(n=0, mean=None, lo=None, hi=None, insufficient=True)
    ci = bootstrap_ci(v)
    mean = float(np.mean(v))
    if ci is None:
        return XEstimate(n=len(v), mean=round(mean, 4), lo=None, hi=None, insufficient=True)
    lo, hi = ci
    return XEstimate(
        n=len(v),
        mean=round(mean, 4),
        lo=round(lo, 4),
        hi=round(hi, 4),
        insufficient=(hi - lo) / 2.0 > MAX_CI_HALF,
    )


def tod_bucket(t: dt.datetime) -> str:
    """Time-of-day bucket of an ET timestamp: open (< 10:30), midday (< 14:30), late."""
    et = t.astimezone(ET).time()
    if et < dt.time(10, 30):
        return "open"
    if et < dt.time(14, 30):
        return "midday"
    return "late"


def period_of(profile: str | None) -> str:
    """``post_switch`` under the D128 margin profile, else ``pre_switch``."""
    return "post_switch" if profile == SWITCH_PROFILE else "pre_switch"


def live_kind(kind: str, legs: Sequence[LegQuote]) -> StrategyKind | None:
    """The backtest :class:`StrategyKind` of a live structure (``None`` = not in the menu)."""
    rights = {parse_occ(lg.symbol).kind for lg in legs}
    calls = rights == {OptionKind.CALL}
    puts = rights == {OptionKind.PUT}
    if kind == StructureKind.LONG_CALL:
        return StrategyKind.LONG_CALL
    if kind == StructureKind.LONG_PUT:
        return StrategyKind.LONG_PUT
    if kind == StructureKind.IRON_CONDOR:
        return StrategyKind.IRON_CONDOR
    if kind == StructureKind.VERTICAL_DEBIT:
        return StrategyKind.BULL_CALL if calls else StrategyKind.BEAR_PUT if puts else None
    if kind == StructureKind.VERTICAL_CREDIT:
        return StrategyKind.BEAR_CALL if calls else StrategyKind.BULL_PUT if puts else None
    return None


def leg_key(kind: StrategyKind | str, legs: Sequence[tuple[str, int]]) -> str:
    """The sim's candidate key shape: ``kind|+1SYM,-1SYM`` (legs sorted by symbol)."""
    body = ",".join(f"{s:+d}{sym}" for sym, s in sorted(legs))
    return f"{kind}|{body}"


def match_structure(
    live_legs: Sequence[LegQuote],
    kind: StrategyKind | None,
    menu: Sequence[Candidate],
    *,
    spot: float,
) -> tuple[str, Candidate | None, int | None, float | None]:
    """Live's structure in a sim *menu*: ``(match, candidate, expiry diff, strike diff)``.

    ``exact`` = same kind and the same (symbol, side) set; else the same-kind candidate
    nearest by (|expiry diff| days, sum |strike diff| / spot) -- ``same_expiry`` when the
    expiry matches, ``nearest`` otherwise; ``kind_absent`` when no candidate has the kind.
    """
    if kind is None:
        return "kind_absent", None, None, None
    want = {(lg.symbol, lg.side) for lg in live_legs}
    same = [c for c in menu if c.kind == str(kind)]
    if not same:
        return "kind_absent", None, None, None
    for c in same:
        if {(p.symbol, p.side) for p in c.picks} == want:
            return "exact", c, 0, 0.0
    occ = [parse_occ(lg.symbol) for lg in live_legs]
    exp = min(o.expiration for o in occ)
    strikes = sorted(float(o.strike) for o in occ)

    def dist(c: Candidate) -> tuple[int, float]:
        ce = min(p.expiration for p in c.picks)
        cs = sorted(p.strike for p in c.picks)
        if len(cs) != len(strikes):
            return abs((ce - exp).days), math.inf
        return abs((ce - exp).days), sum(abs(a - b) for a, b in zip(cs, strikes, strict=True))

    best = min(same, key=lambda c: (dist(c), c.inputs.key))
    dd, ds = dist(best)
    pct = ds / spot if spot > 0 and math.isfinite(ds) else None
    return ("same_expiry" if dd == 0 else "nearest"), best, dd, pct


# ---------------------------------------------------------------------------
# Live store (read-only)
# ---------------------------------------------------------------------------


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _ts(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _et_day(s: str) -> dt.date:
    return _ts(s).astimezone(ET).date()


def _legs_of(structure: Mapping[str, Any]) -> list[LegQuote]:
    out = []
    for lg in structure.get("legs") or []:
        side = 1 if str(lg.get("side")) == LegIntent.LONG.value else -1
        prem = lg.get("premium")
        out.append(
            LegQuote(
                symbol=str(lg["occ_symbol"]),
                side=side,
                ratio=int(lg.get("ratio") or 1),
                mid=None if prem is None else float(prem),
            )
        )
    return out


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _profiles(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[dt.date, str], dict]:
    """run_id -> account_profile, day -> account_profile, day -> git_sha (quant runs)."""
    by_run: dict[str, str] = {}
    by_day: dict[dt.date, str] = {}
    sha_day: dict[dt.date, str] = {}
    if "run_manifests" not in _tables(conn):
        return by_run, by_day, sha_day
    for r in conn.execute("SELECT run_id, job, payload FROM run_manifests ORDER BY created_at"):
        try:
            p = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        prof = p.get("account_profile")
        if prof:
            by_run[str(r["run_id"])] = str(prof)
        day = p.get("trading_day")
        if day and prof and str(r["job"]).startswith("quant"):
            d = dt.date.fromisoformat(str(day))
            by_day[d] = str(prof)
            if p.get("git_sha"):
                sha_day[d] = str(p["git_sha"])
    return by_run, by_day, sha_day


def _profile_for(run_id: str | None, day: dt.date, by_run: Mapping, by_day: Mapping) -> str | None:
    if run_id and run_id in by_run:
        return str(by_run[run_id])
    return by_day.get(day)


def _measure_overrides(conn: sqlite3.Connection) -> list[tuple[dt.date, str | None]]:
    """Applied ``menu_measure`` registry changes (Slack overrides beat the YAML default),
    oldest first, as (ET day, value or None for a revert)."""
    if "config_changes" not in _tables(conn):
        return []
    out = []
    for r in conn.execute(
        "SELECT new, at FROM config_changes WHERE key='menu_measure' AND status='applied'"
        " ORDER BY at, id"
    ):
        raw = r["new"]
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            v = raw
        out.append((_et_day(str(r["at"])), None if v in (None, "") else str(v)))
    return out


def _measure_for(
    day: dt.date,
    sha: str | None,
    overrides: Sequence[tuple[dt.date, str | None]],
    at_sha: Callable[[str | None], str | None] | None,
) -> str | None:
    """The day's live menu measure: the last applied override on or before *day*, else
    ``pipeline.menu_measure`` of ``config/exits.yaml`` at the day's code sha."""
    over = [v for d, v in overrides if d <= day]
    if over and over[-1] is not None:
        return over[-1]
    return at_sha(sha) if at_sha else None


def _quote_legs(payload: Mapping[str, Any], legs: Sequence[LegQuote]) -> list[LegQuote] | None:
    q = {str(x["occ_symbol"]): x for x in payload.get("legs") or []}
    out = []
    for lg in legs:
        x = q.get(lg.symbol)
        if x is None or x.get("bid") is None or x.get("ask") is None:
            return None
        out.append(
            lg.model_copy(
                update={
                    "bid": float(x["bid"]),
                    "ask": float(x["ask"]),
                    "mid": float(x.get("mid") or (float(x["bid"]) + float(x["ask"])) / 2.0),
                }
            )
        )
    return out


def _regate(conn: sqlite3.Connection, h: str) -> tuple[float, float] | None:
    """(mid, spread) of the last ``order:band_moved`` re-gate of proposal *h*, if any."""
    r = conn.execute(
        "SELECT payload FROM decisions WHERE proposal_hash=? AND reason_code='order:band_moved'"
        " ORDER BY at DESC LIMIT 1",
        (h,),
    ).fetchone()
    if r is None:
        return None
    p = json.loads(r["payload"] or "{}")
    try:
        mid, far = float(p["mid"]), float(p["far_touch"])
    except (KeyError, TypeError, ValueError):
        return None
    return mid, 2.0 * abs(far - mid)


def load_live(
    conn: sqlite3.Connection,
    *,
    arm: str,
    since: dt.date | None = None,
    band_reach: float = 1.0,
    menu_measure_for: Callable[[str | None], str | None] | None = None,
) -> tuple[list[LiveFill], list[LiveProposal], list[LivePosition], list[LiveStance]]:
    """Everything the recon reads from one store (*conn* must be read-only)."""
    tabs = _tables(conn)
    by_run, by_day, sha_day = _profiles(conn)
    overrides = _measure_overrides(conn)
    props = {
        r["proposal_hash"]: r
        for r in conn.execute(
            "SELECT proposal_hash, day, ticker, kind, structure_json, run_id, candidate_id,"
            " regime FROM proposals"
        )
    }
    fills: list[LiveFill] = []
    for e in conn.execute(
        "SELECT * FROM executions WHERE filled_qty > 0 AND fill_price IS NOT NULL"
        " ORDER BY started_at"
    ):
        p = props.get(e["proposal_hash"])
        if p is None:
            continue
        st = json.loads(p["structure_json"])
        side = "open" if e["kind"] == "open" else "close"
        f = conn.execute(
            "SELECT f.filled_at FROM fills f JOIN orders o ON o.id = f.order_id"
            " WHERE o.proposal_hash=? ORDER BY f.filled_at DESC LIMIT 1",
            (e["proposal_hash"],),
        ).fetchone()
        filled_at = _ts(f["filled_at"] if f else (e["finished_at"] or e["started_at"]))
        day = filled_at.astimezone(ET).date()
        if since is not None and day < since:
            continue
        legs = _legs_of(st)
        kind, sleeve = str(st.get("kind")), st.get("sleeve")
        if side == "close" and e["structure_id"]:
            os_ = conn.execute(
                "SELECT structure_json FROM open_structures WHERE id=?", (e["structure_id"],)
            ).fetchone()
            if os_ is not None:
                ost = json.loads(os_["structure_json"])
                kind, sleeve = str(ost.get("kind")), ost.get("sleeve")
        mid = spread = None
        src = "none"
        rg = _regate(conn, e["proposal_hash"]) if "re-priced" in str(e["detail"] or "") else None
        if rg is not None:
            (mid, spread), src = rg, "regate"
        elif side == "open":
            mc = conn.execute(
                "SELECT payload FROM market_contexts WHERE proposal_hash=?"
                " ORDER BY created_at DESC LIMIT 1",
                (e["proposal_hash"],),
            ).fetchone()
            q = None if mc is None else _quote_legs(json.loads(mc["payload"]), legs)
            nb = None if q is None else combo_nbbo(q)
            if q is not None and nb is not None:
                legs, (mid, spread), src = q, nb, "market_contexts"
        if mid is None and e["band_lo"] is not None and e["band_hi"] is not None:
            lo, hi = float(e["band_lo"]), float(e["band_hi"])
            prem = [lg.mid for lg in legs]
            if all(m is not None for m in prem):
                mid = sum(lg.side * lg.ratio * float(lg.mid or 0.0) for lg in legs)
            else:
                mid = lo
            spread = 2.0 * (hi - lo) / band_reach if band_reach > 0 else None
            src = "band"
        fills.append(
            LiveFill(
                arm=arm,
                proposal_hash=e["proposal_hash"],
                side=side,
                ticker=str(p["ticker"]),
                kind=kind,
                sleeve=sleeve,
                profile=_profile_for(e["run_id"], day, by_run, by_day),
                day=day,
                filled_at=filled_at,
                fill=float(e["fill_price"]),
                contracts=int(e["filled_qty"]),
                steps_used=int(e["steps_used"] or 0),
                legs=legs,
                mid=mid,
                spread=spread,
                nbbo_source=src,
            )
        )
    stance_of = {}
    if "candidates" in tabs:
        stance_of = {
            r["id"]: r["stance"] for r in conn.execute("SELECT id, stance FROM candidates")
        }
    proposals: list[LiveProposal] = []
    for h, p in props.items():
        if p["kind"] != "open":
            continue
        day = dt.date.fromisoformat(str(p["day"]))
        if since is not None and day < since:
            continue
        st = json.loads(p["structure_json"])
        prof = _profile_for(p["run_id"], day, by_run, by_day)
        proposals.append(
            LiveProposal(
                arm=arm,
                proposal_hash=h,
                day=day,
                ticker=str(p["ticker"]),
                kind=str(st.get("kind")),
                sleeve=st.get("sleeve"),
                profile=prof,
                menu_measure=_measure_for(day, sha_day.get(day), overrides, menu_measure_for),
                stance=stance_of.get(p["candidate_id"]),
                legs=_legs_of(st),
            )
        )
    proposals.sort(key=lambda x: (x.day, x.ticker, x.proposal_hash))
    positions: list[LivePosition] = []
    for r in conn.execute(
        "SELECT * FROM open_structures WHERE status='closed' ORDER BY opened_at, id"
    ):
        entry_day = _et_day(r["opened_at"])
        if since is not None and entry_day < since:
            continue
        st = json.loads(r["structure_json"])
        o = conn.execute(
            "SELECT realised_pnl, exit_reason FROM outcomes WHERE proposal_hash IN (?, ?)"
            " AND realised_pnl IS NOT NULL ORDER BY at DESC LIMIT 1",
            (r["open_proposal_hash"], r["exit_proposal_hash"] or ""),
        ).fetchone()
        p = props.get(r["open_proposal_hash"])
        exit_day = (
            dt.date.fromisoformat(str(r["exit_day"])) if r["exit_day"] else _et_day(r["closed_at"])
        )
        entry, close = float(r["entry_net"]), r["close_net"]
        pnl: float | None = None
        if o is not None:
            pnl = float(o["realised_pnl"])
        elif close is not None:  # pay convention: P&L = -(paid to open + paid to close)
            pnl = -(entry + float(close)) * MULT * int(r["contracts"])
        positions.append(
            LivePosition(
                arm=arm,
                structure_id=str(r["id"]),
                ticker=str(r["ticker"]),
                kind=str(st.get("kind")),
                sleeve=st.get("sleeve"),
                profile=_profile_for(p["run_id"] if p else None, entry_day, by_run, by_day),
                regime=None if p is None else p["regime"],
                legs=_legs_of(st),
                contracts=int(r["contracts"]),
                entry_day=entry_day,
                entry_fill=entry,
                exit_day=exit_day,
                close_fill=None if close is None else float(close),
                exit_reason=r["exit_reason"] or (o["exit_reason"] if o else None),
                realised_pnl=pnl,
            )
        )
    stances: dict[tuple[dt.date, str], tuple[str, str]] = {}
    for r in conn.execute(
        "SELECT subject, payload, at FROM decisions WHERE persona='research'"
        " AND stage='shortlist' AND reason_code='shortlisted' ORDER BY at"
    ):
        try:
            s = json.loads(r["payload"] or "{}").get("stance")
        except ValueError:
            continue
        if s not in _STANCE_LABEL or not r["subject"]:
            continue
        d = _et_day(r["at"])
        if since is not None and d < since:
            continue
        stances[(d, str(r["subject"]).upper())] = (str(s), str(r["at"]))  # last of the day wins
    st_rows = [
        LiveStance(arm=arm, day=d, ticker=t, stance=s) for (d, t), (s, _) in sorted(stances.items())
    ]
    return fills, proposals, positions, st_rows


def menu_measure_at(repo: Path) -> Callable[[str | None], str | None]:
    """``pipeline.menu_measure`` of ``config/exits.yaml`` at a git sha (cached; ``None`` on
    any failure: unknown sha, no git, unparsable)."""
    cache: dict[str, str | None] = {}

    def get(sha: str | None) -> str | None:
        if not sha:
            return None
        if sha not in cache:
            try:
                text = subprocess.run(  # noqa: S603 - fixed argv, sha from our own manifest
                    ["git", "-C", str(repo), "show", f"{sha}:config/exits.yaml"],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout
                pipe = (yaml.safe_load(text) or {}).get("pipeline") or {}
                cache[sha] = str(pipe.get("menu_measure") or "control")
            except (OSError, subprocess.SubprocessError, yaml.YAMLError):
                cache[sha] = None
        return cache[sha]

    return get


# ---------------------------------------------------------------------------
# Replay against the EOD history
# ---------------------------------------------------------------------------


class _History:
    """Lazy per-ticker EOD history over a ``HistoryView`` (None = no cached history)."""

    def __init__(self, view: Any) -> None:
        self.view = view
        self._h: dict[str, Any] = {}
        self._marks: dict[str, dict[dt.date, pd.DataFrame]] = {}

    def get(self, ticker: str) -> Any | None:
        t = ticker.upper()
        if t not in self._h:
            try:
                ok = self.view.provider_for(t) is not None
                self._h[t] = self.view.load(t) if ok else None
            except (FileNotFoundError, KeyError, ValueError) as exc:
                log.warning("recon.history_missing", ticker=t, error=str(exc))
                self._h[t] = None
        return self._h[t]

    def marks(self, ticker: str, dte_max: int) -> dict[dt.date, pd.DataFrame]:
        h = self.get(ticker)
        if h is None:
            return {}
        if ticker not in self._marks:
            self._marks[ticker] = quote_marks(h.raw, h.closes, dte_max=dte_max)
        return self._marks[ticker]

    def asset(self, ticker: str) -> AssetClass:
        return "etf" if self.view.is_etf(ticker.upper()) else "stock"

    def rate(self, day: dt.date) -> float:
        return float(self.view.rates.rate(day))


def _eod_quotes(h: Any, day: dt.date, legs: Sequence[LegQuote]) -> list[LegQuote] | None:
    raw = h.raw_day(day)
    if raw is None or raw.empty:
        return None
    rows = raw[raw["symbol"].isin([lg.symbol for lg in legs])]
    q = {str(r.symbol): (r.bid, r.ask) for r in rows.itertuples()}
    out = []
    for lg in legs:
        b, a = q.get(lg.symbol, (None, None))
        try:
            bf, af = float(b), float(a)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(bf) and math.isfinite(af)) or af <= 0 or bf > af:
            return None
        out.append(lg.model_copy(update={"bid": bf, "ask": af, "mid": (bf + af) / 2.0}))
    return out


def slippage_rows(fills: Sequence[LiveFill], hist: _History) -> list[SlippageRow]:
    out = []
    for f in fills:
        x_eod = None
        note = None
        h = hist.get(f.ticker)
        if h is None:
            note = "no EOD history"
        else:
            q = _eod_quotes(h, f.day, [lg.model_copy(update={"mid": None}) for lg in f.legs])
            nb = None if q is None else combo_nbbo(q)
            if nb is None:
                note = "no EOD two-sided quote that session"
            else:
                x_eod = realised_x(f.fill, nb[0], nb[1])
        x = realised_x(f.fill, f.mid, f.spread)
        out.append(
            SlippageRow(
                arm=f.arm,
                proposal_hash=f.proposal_hash,
                side=f.side,
                ticker=f.ticker,
                asset=hist.asset(f.ticker),
                kind=f.kind,
                sleeve=f.sleeve,
                profile=f.profile,
                period=period_of(f.profile),
                tod=tod_bucket(f.filled_at),
                step=f.steps_used,
                contracts=f.contracts,
                fill=f.fill,
                mid=None if f.mid is None else round(f.mid, 4),
                spread=None if f.spread is None else round(f.spread, 4),
                nbbo_source=f.nbbo_source,
                x=None if x is None else round(x, 4),
                x_eod=None if x_eod is None else round(x_eod, 4),
                eod_note=note,
            )
        )
    return out


def _sim_menu(
    h: Any,
    ticker: str,
    day: dt.date,
    *,
    profile: str,
    settings: Any,
    cfg: Any,
    exits: Any,
    cost: Any,
    rate: float,
) -> list[Candidate]:
    """The E7.19 sim menu of *ticker* on *day*: every sleeve window of *profile*."""
    bt = cfg.backtest
    mc = exits.model.model_copy(update={"n_paths": bt.n_paths})
    base = cost.model_copy(update={"slippage_frac": bt.sim.slippage_x})
    window = bt.window_for(profile, settings.entry_dte_window)
    override = tuple(window) != settings.entry_dte_window
    out: list[Candidate] = []
    for sw in sleeve_windows(settings, window, override=override):
        specs = bt.specs_for(profile, sw.dte_min, sw.dte_max, sw.exit_block)
        ck = kind_costs(specs, base, bt.slippage_by_kind) if bt.slippage_by_kind else {}
        kw: dict[str, Any] = {
            "days": [day],
            "underlying": ticker,
            "specs": specs,
            "cost": base,
            "exits": exits,
            "mc": mc,
            "r": rate,
        }
        if ck:
            menus = build_menus_by_kind(h.chains, h.closes, cost_by_kind=ck, **kw)
        else:
            menus = build_menus(h.chains, h.closes, **kw)
        out += menus.get(day, [])
    return out


def overlap_rows(
    proposals: Sequence[LiveProposal],
    hist: _History,
    *,
    cfg: Any,
    exits: Any,
    cost: Any,
    settings_for: Callable[[str], Any],
    default_profile: str,
) -> list[OverlapRow]:
    out = []
    for p in proposals:
        prof = p.profile or default_profile
        kind = live_kind(p.kind, p.legs)
        key = leg_key(kind or p.kind, [(lg.symbol, lg.side) for lg in p.legs])
        row: dict[str, Any] = {
            "arm": p.arm,
            "day": p.day,
            "ticker": p.ticker,
            "asset": hist.asset(p.ticker),
            "profile": p.profile,
            "period": period_of(p.profile),
            "live_kind": None if kind is None else str(kind),
            "live_key": key,
            "menu_measure": p.menu_measure,
            "menu_size": 0,
        }
        h = hist.get(p.ticker)
        if h is None or p.day not in h.chains or p.day not in h.closes.index:
            out.append(OverlapRow(**row, match="skip", note="no EOD chain for that session"))
            continue
        s = settings_for(prof)
        menu = _sim_menu(
            h,
            p.ticker,
            p.day,
            profile=prof,
            settings=s,
            cfg=cfg,
            exits=exits,
            cost=cost,
            rate=hist.rate(p.day),
        )
        row["menu_size"] = len(menu)
        spot = float(h.closes[p.day])
        match, cand, dd, ds = match_structure(p.legs, kind, menu, spot=spot)
        allows = profile_allows_credit(prof, s.account_profiles_file)
        inc = live_incumbent(p.menu_measure or "control", allows_credit=allows)
        rks = rankers_for(list(Ranker), allows_credit=allows, incumbent=inc)
        kinds = stance_kinds(prof, s.account_profiles_file)
        live_lab = _KIND_TREND.get(str(kind), "sideways")
        live_menu = [c for c in menu if c.kind in kinds.get(live_lab, frozenset())]
        if cand is not None:
            row["live_passes_filters"] = passes_filters(cand.inputs, cfg.ranking.filters)
            row["live_managed_net_ev"] = cand.inputs.managed_net_ev
        ranks: dict[str, int | None] = {}
        for rk in rks:
            order = [
                c.key
                for c in rank(
                    [c.inputs for c in live_menu],
                    rk,
                    filters=cfg.ranking.filters,
                    vrp_threshold=cfg.ranking.vrp_threshold,
                )
            ]
            ranks[str(rk)] = (
                order.index(cand.inputs.key) + 1
                if cand is not None and cand.inputs.key in order
                else None
            )
        trend = label_trend_v2(h.closes.sort_index())
        sim_lab = str(trend.get(p.day, "unknown"))
        sim_menu = [c for c in menu if c.kind in kinds.get(sim_lab, frozenset())]
        top = rank(
            [c.inputs for c in sim_menu],
            inc,
            filters=cfg.ranking.filters,
            vrp_threshold=cfg.ranking.vrp_threshold,
        )
        top_c = next((c for c in sim_menu if top and c.inputs.key == top[0].key), None)
        out.append(
            OverlapRow(
                **row,
                match=match,
                expiry_diff_days=dd,
                strike_diff_pct=None if ds is None else round(ds, 4),
                ranks=ranks,
                sim_top_key=None if top_c is None else top_c.inputs.key,
                sim_top_kind=None if top_c is None else top_c.kind,
                sim_top_is_live=None
                if top_c is None
                else bool(match == "exact" and cand is not None and top_c.key == cand.key),
                note=f"sim stance {_TREND_STANCE.get(sim_lab, 'none')} (trend_v2), "
                f"live stance {p.stance or 'unknown'}",
            )
        )
    return out


def _picks(h: Any, day: dt.date, legs: Sequence[LegQuote]) -> list[LegPick] | None:
    q = _eod_quotes(h, day, legs)
    if q is None:
        return None
    out = []
    for lg in q:
        o = parse_occ(lg.symbol)
        out.append(
            LegPick(
                symbol=lg.symbol,
                right="call" if o.kind == OptionKind.CALL else "put",
                strike=float(o.strike),
                expiration=o.expiration,
                side=lg.side,
                mid=float(lg.mid or 0.0),
                spread=float((lg.ask or 0.0) - (lg.bid or 0.0)),
                delta=float("nan"),
                iv=float("nan"),
            )
        )
    return out


def exit_rows(
    positions: Sequence[LivePosition],
    hist: _History,
    *,
    cfg: Any,
    exits: Any,
    cost: Any,
    settings_for: Callable[[str], Any],
    default_profile: str,
) -> list[ExitRow]:
    bt = cfg.backtest
    base = cost.model_copy(update={"slippage_frac": bt.sim.slippage_x})
    spread_rule = bt.sim.spread_rule == "live"
    out = []
    for pos in positions:
        row: dict[str, Any] = {
            "arm": pos.arm,
            "structure_id": pos.structure_id,
            "ticker": pos.ticker,
            "asset": hist.asset(pos.ticker),
            "kind": pos.kind,
            "regime": pos.regime,
            "profile": pos.profile,
            "period": period_of(pos.profile),
            "contracts": pos.contracts,
            "entry_day": pos.entry_day,
            "live_exit_day": pos.exit_day,
            "live_reason": pos.exit_reason,
            "live_pnl": pos.realised_pnl,
            "entry_live": pos.entry_fill,
            "close_live": pos.close_fill,
        }
        kind = live_kind(pos.kind, pos.legs)
        h = hist.get(pos.ticker)
        if kind is None or h is None or pos.entry_day not in h.closes.index:
            note = "kind not in the sim" if kind is None else "no EOD history for the entry"
            out.append(ExitRow(**row, note=note))
            continue
        picks = _picks(h, pos.entry_day, pos.legs)
        if picks is None:
            out.append(ExitRow(**row, note="a leg is unquoted at the entry session's close"))
            continue
        o = open_trade(
            picks,
            spec=StrategySpec(kind=kind),
            underlying=pos.ticker,
            entry_date=pos.entry_day,
            spot=float(h.closes[pos.entry_day]),
            cost=base,
        )
        if o is None:
            out.append(ExitRow(**row, note="no positive post-cost risk at the EOD entry"))
            continue
        s = settings_for(pos.profile or default_profile)
        structure = analyze(_legs(picks, pos.ticker), as_of=pos.entry_day)
        block = s.exit_block_for_dte(o.dte)
        rules = resolve_rules(structure, exits.policy_for(STRUCTURE_KIND[kind], block))
        marks = hist.marks(pos.ticker, bt.data.chain_dte_max)
        row["entry_sim"] = round(sum(lg.side * lg.fill for lg in o.legs), 4)
        lm = marks.get(pos.exit_day)
        if lm is not None:
            m = {str(r.symbol): (float(r.mid), float(r.spread)) for r in lm.itertuples()}
            if all(p.symbol in m for p in picks):
                # closing pays the opposite side's fill on every leg (pay convention)
                close_net = sum(
                    -lg.side * base.fill(m[lg.symbol][0], m[lg.symbol][1], -lg.side)
                    for lg in o.legs
                )
                row["close_sim_at_live_day"] = round(close_net, 4)
                fees = o.open_fees + base.fees(len(o.legs))
                at_live = -(float(row["entry_sim"]) + close_net) * MULT - fees
                row["sim_pnl_at_live_exit"] = round(at_live * pos.contracts, 2)
        for proxy in (False, True):
            trade, _, _ = exit_walk(
                o,
                rules,
                entry_day=pos.entry_day,
                book=marks,
                closes=h.closes,
                inside=[],
                settings=s,
                spread_rule=spread_rule,
                cost=base,
                ranges=session_ranges(h.closes, h.ohlc) if proxy else None,
                rate=hist.rate,
            )
            if trade is None and o.expiration <= max(h.closes.index):
                past = h.closes[pd.Index(h.closes.index) <= o.expiration]
                trade = settle(o, float(past.iloc[-1]), base)
            pre = "proxy_" if proxy else "sim_"
            if trade is None:
                row[pre + "reason"] = f"no exit by data end {max(h.closes.index)}"
                continue
            row[pre + "exit_day"] = trade.exit_date or trade.expiration
            row[pre + "reason"] = trade.exit_reason
            row[pre + "pnl"] = round(trade.pnl * pos.contracts, 2)
        if pos.realised_pnl is not None:
            if row.get("sim_pnl") is not None:
                row["tracking_error"] = round(pos.realised_pnl - row["sim_pnl"], 2)
            if row.get("proxy_pnl") is not None:
                row["proxy_tracking_error"] = round(pos.realised_pnl - row["proxy_pnl"], 2)
            if row.get("sim_pnl_at_live_exit") is not None:
                at_live = float(row["sim_pnl_at_live_exit"])
                row["pricing_gap"] = round(pos.realised_pnl - at_live, 2)
                if row.get("sim_pnl") is not None:
                    row["timing_gap"] = round(at_live - row["sim_pnl"], 2)
        out.append(ExitRow(**row))
    return out


def stance_rows(
    stances: Sequence[LiveStance],
    closes_of: Callable[[str], pd.Series | None],
    *,
    asset_of: Callable[[str], AssetClass],
    m1: Mapping[tuple[dt.date, str], float] | None = None,
    market: str = "SPY",
) -> list[StanceRow]:
    """The LLM stance vs the sim proxies on the same (day, ticker)."""
    labels: dict[str, pd.Series] = {}

    def lab(t: str, d: dt.date) -> str:
        if t not in labels:
            c = closes_of(t)
            labels[t] = label_trend_v2(c.sort_index()) if c is not None and len(c) else pd.Series()
        v = labels[t].get(d, "unknown")
        return _TREND_STANCE.get(str(v), "unknown")

    m1_lab = {1.0: "bullish", -1.0: "bearish", 0.0: "neutral"}
    out = []
    for s in stances:
        mv = (m1 or {}).get((s.day, s.ticker))
        out.append(
            StanceRow(
                arm=s.arm,
                day=s.day,
                ticker=s.ticker,
                asset=asset_of(s.ticker),
                llm=s.stance,
                trend_v2=lab(s.ticker, s.day),
                market_v2=lab(market, s.day),
                m1_direction="unknown"
                if mv is None or not math.isfinite(mv)
                else m1_lab.get(float(mv), "unknown"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Aggregation + rendering
# ---------------------------------------------------------------------------


def _split(rows: Sequence[Any]) -> dict[str, list[Any]]:
    return {
        "All": list(rows),
        "Stocks": [r for r in rows if r.asset == "stock"],
        "ETFs": [r for r in rows if r.asset == "etf"],
    }


def _fmt(v: float | None, spec: str = ".3f") -> str:
    return "n/a" if v is None else format(v, spec)


def _x_cell(e: XEstimate) -> str:
    if e.n == 0:
        return "n=0"
    ci = "" if e.lo is None else f" [{e.lo:.3f}, {e.hi:.3f}] (±{(e.hi - e.lo) / 2:.3f})"
    flag = " insufficient" if e.insufficient else ""
    return f"{e.mean:.3f}{ci} n={e.n}{flag}"


def x_by(rows: Sequence[SlippageRow], field: str, attr: str = "x") -> dict[str, XEstimate]:
    groups: dict[str, list[float | None]] = {}
    for r in rows:
        groups.setdefault(str(getattr(r, field)), []).append(getattr(r, attr))
    return {k: estimate_x(v) for k, v in sorted(groups.items())}


def proposed_slippage_by_kind(rows: Sequence[SlippageRow]) -> dict[str, float | str]:
    """Kind -> proposed ``backtest.slippage_by_kind`` x (the measured ``x_eod`` mean,
    clamped at 0 like :func:`~arc.backtest.ranking.slippage_from_scorecard`), or
    ``insufficient``."""
    out: dict[str, float | str] = {}
    for k, e in x_by(rows, "kind", "x_eod").items():
        out[k] = "insufficient" if e.insufficient or e.mean is None else round(max(e.mean, 0.0), 3)
    return out


def _md_table(head: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    if not rows:
        return "_none_\n"
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def overlap_summary(rows: Sequence[OverlapRow]) -> dict[str, Any]:
    ok = [r for r in rows if r.match != "skip"]
    n = len(ok)

    def share(pred: Callable[[OverlapRow], bool]) -> float | None:
        return round(sum(1 for r in ok if pred(r)) / n, 3) if n else None

    return {
        "n": n,
        "skipped": len(rows) - n,
        "exact": share(lambda r: r.match == "exact"),
        "same_expiry_or_better": share(lambda r: r.match in ("exact", "same_expiry")),
        "kind_present": share(lambda r: r.match != "kind_absent"),
        "sim_top_same_kind": share(lambda r: r.sim_top_kind == r.live_kind),
        "sim_top_is_live": share(lambda r: bool(r.sim_top_is_live)),
    }


def stance_agreement(rows: Sequence[StanceRow]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for proxy in ("trend_v2", "market_v2", "m1_direction"):
        known = [r for r in rows if getattr(r, proxy) != "unknown"]
        agree = sum(1 for r in known if getattr(r, proxy) == r.llm)
        conf: dict[str, int] = {}
        for r in known:
            k = f"{r.llm}->{getattr(r, proxy)}"
            conf[k] = conf.get(k, 0) + 1
        out[proxy] = {
            "n": len(known),
            "agree": round(agree / len(known), 3) if known else None,
            "confusion": dict(sorted(conf.items())),
        }
    return out


def exit_summary(rows: Sequence[ExitRow]) -> dict[str, Any]:
    ok = [r for r in rows if r.tracking_error is not None]
    px = [r for r in rows if r.proxy_tracking_error is not None]
    match = [r for r in rows if r.sim_reason is not None and r.sim_exit_day is not None]
    lag = [(r.sim_exit_day - r.live_exit_day).days for r in match if r.sim_exit_day is not None]
    return {
        "n": len(rows),
        "replayed": len(ok),
        "tracking_error_sum": round(sum(r.tracking_error or 0.0 for r in ok), 2),
        "tracking_error_mean_abs": round(sum(abs(r.tracking_error or 0.0) for r in ok) / len(ok), 2)
        if ok
        else None,
        "proxy_tracking_error_sum": round(sum(r.proxy_tracking_error or 0.0 for r in px), 2),
        "same_exit_day": round(
            sum(1 for r in match if r.sim_exit_day == r.live_exit_day) / len(match), 3
        )
        if match
        else None,
        "sim_exits_later_days_mean": round(float(np.mean(lag)), 2) if lag else None,
    }


def reconciliation_doc(rep: ReconReport) -> dict[str, Any]:
    """The E7.14 Analyst slot (``docs/RESEARCH/studies/reconciliation.json``), aggregates
    only: kind x regime rows (``drift_lines``) plus a ``summary`` list of E7.22 lines."""
    groups: dict[tuple[str, str], list[ExitRow]] = {}
    for r in rep.exits:
        groups.setdefault((r.kind, r.regime or "unknown"), []).append(r)

    def mean(v: Sequence[float | None]) -> float | None:
        xs = [float(x) for x in v if x is not None]
        return round(float(np.mean(xs)), 4) if xs else None

    rows = []
    for (kind, regime), rs in sorted(groups.items()):
        te = [r.tracking_error for r in rs if r.tracking_error is not None]
        matched = [r for r in rs if r.sim_reason is not None]
        rows.append(
            {
                "kind": kind,
                "regime": regime,
                "n": len(rs),
                "entry_pred": mean([r.entry_sim for r in rs]),
                "entry_actual": mean([r.entry_live for r in rs]),
                "exit_pred": mean([r.close_sim_at_live_day for r in rs]),
                "exit_actual": mean([r.close_live for r in rs]),
                "exit_reason_match": round(
                    sum(1 for r in matched if r.sim_exit_day == r.live_exit_day) / len(matched), 3
                )
                if matched
                else None,
                "pnl_tracking_error": round(float(np.mean([abs(x) for x in te])), 2)
                if te
                else None,
                "pnl_bias": round(float(np.mean(te)), 2) if te else None,
            }
        )
    xs = x_by(rep.slippage, "kind", "x_eod")
    ov = overlap_summary(rep.overlap)
    sa = stance_agreement(rep.stance)
    ex = exit_summary(rep.exits)
    summary = [
        "E7.22 live<->sim recon (docs/RESEARCH/live-sim-recon.md); paper fills are a lower "
        "bound on live slippage",
        "x_eod per kind (fill vs EOD close book; sim assumes "
        f"{rep.sim_slippage_x}): " + ", ".join(f"{k} {_x_cell(e)}" for k, e in xs.items()),
        f"sim-menu overlap of live picks: exact {_fmt(ov['exact'])}, kind present "
        f"{_fmt(ov['kind_present'])}, n={ov['n']}",
        f"exit replay: tracking error sum ${ex['tracking_error_sum']:,.2f} over "
        f"{ex['replayed']} trades; same exit day {_fmt(ex['same_exit_day'])}",
        "LLM stance agreement: "
        + ", ".join(f"{k} {_fmt(v['agree'])} (n={v['n']})" for k, v in sa.items()),
    ]
    return {
        "generated_at": rep.generated_at.isoformat(),
        "git_sha": rep.git_sha,
        "sim_slippage_frac": rep.sim_slippage_x,
        "rows": rows,
        "summary": summary,
    }


def render_markdown(rep: ReconReport) -> str:
    """The weekly report (``docs/RESEARCH/live-sim-recon.md``): aggregates and our own
    fills only, no vendor (ThetaData) rows (D84)."""
    parts = [
        "# Live <-> sim reconciliation (E7.22, D125)\n",
        f"- generated {rep.generated_at.isoformat()} · git {rep.git_sha} · stores "
        f"{', '.join(rep.stores)} · since {rep.since or 'store start'}",
        f"- sim slippage assumption x = {rep.sim_slippage_x} (`backtest.sim.slippage_x`); "
        "report only: the owner flips `backtest.slippage_by_kind`",
        f"- **paper_bias:** {PAPER_BIAS}",
        "- every estimate prints n and its 90% bootstrap CI (seed 0); `insufficient` = n < 2 "
        f"or CI half-width > {MAX_CI_HALF}\n",
        "## 1. Slippage (x = (fill - mid) / spread, pay convention; + = cost)\n",
        "NBBO sources: opens `market_contexts.payload.legs[].bid/ask` (submit time), "
        "re-gated fills `decisions(order:band_moved).payload.mid/far_touch`, closes "
        "`proposals.structure_json.legs[].premium` + the D24 band `executions.band_hi - "
        "band_lo` (upper bound on x). `x_eod` = the same fill vs that session's ThetaData "
        "closing NBBO (what the sim would need).\n",
    ]
    for label, rs in _split(rep.slippage).items():
        live = x_by(rs, "kind")
        eod = x_by(rs, "kind", "x_eod")
        kinds = sorted(set(live) | set(eod))
        parts.append(f"### {label} (n={len(rs)})\n")
        parts.append(
            _md_table(
                ["kind", "x (submit NBBO)", "x_eod (EOD close)"],
                [
                    [
                        k,
                        _x_cell(live.get(k, estimate_x([]))),
                        _x_cell(eod.get(k, estimate_x([]))),
                    ]
                    for k in kinds
                ],
            )
        )
    for field in ("side", "sleeve", "tod", "step", "period", "arm", "nbbo_source"):
        est = x_by(rep.slippage, field)
        parts.append(f"By {field}: " + "; ".join(f"{k} {_x_cell(e)}" for k, e in est.items()))
        parts.append("")
    prop = proposed_slippage_by_kind(rep.slippage)
    parts.append("Proposed `backtest.slippage_by_kind` (from x_eod, clamped at 0):\n")
    parts.append("```yaml\nslippage_by_kind:")
    parts += [f"  {k}: {v}" for k, v in sorted(prop.items())] or ["  {}"]
    parts.append("```\n")
    parts.append("Per fill:\n")
    parts.append(
        _md_table(
            ["arm", "day/side", "ticker", "kind", "sleeve", "tod", "step", "fill", "x", "x_eod"],
            [
                [
                    r.arm,
                    r.side,
                    r.ticker,
                    r.kind,
                    r.sleeve or "",
                    r.tod,
                    r.step,
                    r.fill,
                    _fmt(r.x),
                    _fmt(r.x_eod) if r.x_eod is not None else (r.eod_note or "n/a"),
                ]
                for r in rep.slippage
            ],
        )
    )
    parts.append("## 2. Candidate overlap (live pick vs the E7.19 sim menu on the EOD book)\n")
    for label, rs in _split(rep.overlap).items():
        s = overlap_summary(rs)
        parts.append(
            f"- {label}: n={s['n']} (skipped {s['skipped']}): exact {_fmt(s['exact'])}, "
            f"same expiry or exact {_fmt(s['same_expiry_or_better'])}, kind present "
            f"{_fmt(s['kind_present'])}, sim top = live kind {_fmt(s['sim_top_same_kind'])}, "
            f"sim top = live pick {_fmt(s['sim_top_is_live'])}"
        )
    for period in ("pre_switch", "post_switch"):
        s = overlap_summary([r for r in rep.overlap if r.period == period])
        parts.append(f"- {period}: n={s['n']}, exact {_fmt(s['exact'])}")
    parts.append("")
    rk = sorted({k for r in rep.overlap for k in r.ranks})
    parts.append(
        _md_table(
            [
                "arm",
                "day",
                "ticker",
                "profile",
                "measure",
                "live",
                "match",
                "Δexp d",
                "Δstrike/spot",
                *[f"rank {k}" for k in rk],
                "sim top kind",
                "live passes filters",
                "live managed EV",
                "menu",
            ],
            [
                [
                    r.arm,
                    r.day,
                    r.ticker,
                    r.profile or "?",
                    r.menu_measure or "?",
                    r.live_kind or "n/a",
                    r.match,
                    "" if r.expiry_diff_days is None else r.expiry_diff_days,
                    _fmt(r.strike_diff_pct),
                    *[
                        "" if r.ranks.get(k) is None else r.ranks[k]  # type: ignore[index]
                        for k in rk
                    ],
                    r.sim_top_kind or "",
                    "" if r.live_passes_filters is None else r.live_passes_filters,
                    _fmt(r.live_managed_net_ev, ".2f"),
                    r.menu_size,
                ]
                for r in rep.overlap
            ],
        )
    )
    parts.append(
        "Ranks are within the sim menu of live's own direction (the stance menu holding "
        "live's kind); blank = the matched candidate fails `ranking.filters` or that ranker's "
        "own gate on the EOD book. Live's anti-chase / range-edge filters, Research's choice "
        "and the gate are not replayed. The sim's top pick uses the trend_v2 proxy stance "
        "and the day's menu measure.\n"
    )
    parts.append("## 3. Exit timing (closed live positions through the sim's exit walk)\n")
    for label, rs in _split(rep.exits).items():
        s = exit_summary(rs)
        parts.append(
            f"- {label}: n={s['n']}, replayed {s['replayed']}: tracking error (live - sim) "
            f"sum ${s['tracking_error_sum']:,.2f}, mean |err| "
            f"{_fmt(s['tracking_error_mean_abs'], ',.2f')}; with the intraday proxy sum "
            f"${s['proxy_tracking_error_sum']:,.2f}; same exit day {_fmt(s['same_exit_day'])}; "
            f"sim exits later by {_fmt(s['sim_exits_later_days_mean'], '.2f')} d on average"
        )
    parts.append("")
    parts.append(
        _md_table(
            [
                "arm",
                "ticker",
                "kind",
                "entry",
                "live exit",
                "live reason",
                "live P&L",
                "sim exit (EOD)",
                "sim reason",
                "sim P&L",
                "proxy exit",
                "proxy reason",
                "err",
                "sim @ live exit",
                "pricing gap",
                "timing gap",
                "note",
            ],
            [
                [
                    r.arm,
                    r.ticker,
                    r.kind,
                    r.entry_day,
                    r.live_exit_day,
                    r.live_reason or "",
                    _fmt(r.live_pnl, ",.2f"),
                    r.sim_exit_day or "",
                    r.sim_reason or "",
                    _fmt(r.sim_pnl, ",.2f"),
                    r.proxy_exit_day or "",
                    r.proxy_reason or "",
                    _fmt(r.tracking_error, ",.2f"),
                    _fmt(r.sim_pnl_at_live_exit, ",.2f"),
                    _fmt(r.pricing_gap, ",.2f"),
                    _fmt(r.timing_gap, ",.2f"),
                    r.note or "",
                ]
                for r in rep.exits
            ],
        )
    )
    parts.append(
        "`sim @ live exit` = the sim's own fills (EOD book, `slippage_x`) on live's entry and "
        "exit sessions; `pricing gap` = live - that (intraday fills vs EOD closes), `timing "
        "gap` = that - sim P&L (live's exit day vs the sim's exit rules). They sum to `err`.\n"
    )
    parts.append(
        "## 4. Stance gap (Research `decisions.payload.stance`, persona research, stage "
        "shortlist, last per day)\n"
    )
    parts.append(f"m1_direction: {rep.m1_note}\n")
    for label, rs in _split(rep.stance).items():
        sa = stance_agreement(rs)
        parts.append(
            f"- {label} (n={len(rs)}): "
            + "; ".join(
                f"{k} agree {_fmt(v['agree'])} (n={v['n']}) {v['confusion']}" for k, v in sa.items()
            )
        )
    parts.append("")
    if rep.notes:
        parts.append("## Notes\n")
        parts += [f"- {n}" for n in rep.notes]
    return "\n".join(parts).rstrip() + "\n"


def live_llm_series(rep: ReconReport) -> dict[str, Any]:
    """The E7.20 ``live_llm`` stance proxy rows (only days with a live stance)."""
    return {
        "generated_at": rep.generated_at.isoformat(),
        "source": "decisions.payload.stance (persona research, stage shortlist, last per day)",
        "rows": [
            {"date": r.day.isoformat(), "ticker": r.ticker, "stance": r.llm, "arm": r.arm}
            for r in rep.stance
        ],
    }


def write_outputs(
    rep: ReconReport, *, report_md: Path, reconciliation_json: Path, out_dir: Path
) -> dict[str, Path]:
    """Committed aggregates (markdown + the Analyst slot) and gitignored per-row detail."""
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markdown(rep), encoding="utf-8")
    reconciliation_json.parent.mkdir(parents=True, exist_ok=True)
    reconciliation_json.write_text(
        json.dumps(reconciliation_doc(rep), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    detail = out_dir / "recon.json"
    detail.write_text(rep.model_dump_json(indent=2) + "\n", encoding="utf-8")
    llm = out_dir / "live_llm_stances.json"
    llm.write_text(json.dumps(live_llm_series(rep), indent=2) + "\n", encoding="utf-8")
    return {
        "report": report_md,
        "reconciliation": reconciliation_json,
        "detail": detail,
        "llm": llm,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def arm_store_paths(conn: sqlite3.Connection) -> dict[str, Path]:
    """``<xp>:<arm>`` -> path of every recorded arm store that exists."""
    from arc.experiments.arms import arm_stores

    return {k: p for k, p in arm_stores(conn).items() if p.exists()}


def run_recon(
    db: Path,
    *,
    data_dir: Path,
    now: dt.datetime,
    since: dt.date | None = None,
    arm: Literal["control", "all"] = "control",
    view: Any | None = None,
    cfg: Any | None = None,
    exits: Any | None = None,
    cost: Any | None = None,
    settings: Any | None = None,
    menu_measure_for: Callable[[str | None], str | None] | None = None,
    m1: Mapping[tuple[dt.date, str], float] | None = None,
    git_sha: str = "unknown",
) -> ReconReport:
    """Replay the live store at *db* (read-only) through the sim pieces.

    *view* is a ``HistoryView`` over *data_dir* (built from ``config/ranking.yaml``'s
    data settings when ``None``); tests inject one over a fixture data dir.
    """
    from arc.backtest.costs import load_cost_model
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config

    cfg = cfg or load_ranking_file()
    exits = exits or load_exit_config()
    cost = cost or load_cost_model()
    base = settings or ArcSettings()
    conn = _ro(db)
    stores: dict[str, sqlite3.Connection] = {"control": conn}
    if arm == "all":
        for name, p in arm_store_paths(conn).items():
            stores[name] = _ro(p)
    fills: list[LiveFill] = []
    props: list[LiveProposal] = []
    poss: list[LivePosition] = []
    stances: list[LiveStance] = []
    for name, c in stores.items():
        f, p, o, s = load_live(
            c,
            arm=name,
            since=since,
            band_reach=float(base.execution_band_reach),
            menu_measure_for=menu_measure_for,
        )
        fills += f
        props += p
        poss += o
        stances += s
    days = [x.day for x in props] + [x.day for x in fills] + [x.entry_day for x in poss]
    days += [x.day for x in stances]
    start = min(days) if days else now.astimezone(ET).date()
    if view is None:
        from arc.backtest.data import HistoryView

        view = HistoryView(
            data_dir,
            settings=cfg.backtest.data,
            universe="c500",
            start=start,
            end=now.astimezone(ET).date(),
            cost=cost,
            flat_rate=cfg.backtest.risk_free_rate,
            min_iv_obs=base.iv_min_obs_rank,
        )
    hist = _History(view)
    prof_cache: dict[str, Any] = {}

    def settings_for(p: str) -> Any:
        if p not in prof_cache:
            prof_cache[p] = base.with_profile(p)
        return prof_cache[p]

    default_profile = str(base.account_profile)
    notes = []
    m1_note = (
        "supplied"
        if m1
        else "unavailable: no live m1_direction series (E27.2 publishes dev "
        "OOS only; the live shadow is not wired), so the column is `unknown`"
    )

    def closes_of(t: str) -> pd.Series | None:
        h = hist.get(t)
        return None if h is None else h.closes

    rep = ReconReport(
        generated_at=now,
        git_sha=git_sha,
        stores=sorted(stores),
        since=since,
        sim_slippage_x=float(cfg.backtest.sim.slippage_x),
        slippage=slippage_rows(fills, hist),
        overlap=overlap_rows(
            props,
            hist,
            cfg=cfg,
            exits=exits,
            cost=cost,
            settings_for=settings_for,
            default_profile=default_profile,
        ),
        exits=exit_rows(
            poss,
            hist,
            cfg=cfg,
            exits=exits,
            cost=cost,
            settings_for=settings_for,
            default_profile=default_profile,
        ),
        stance=stance_rows(stances, closes_of, asset_of=hist.asset, m1=m1),
        m1_note=m1_note,
        notes=notes,
    )
    for c in stores.values():
        c.close()
    log.info(
        "recon.done",
        fills=len(rep.slippage),
        proposals=len(rep.overlap),
        exits=len(rep.exits),
        stances=len(rep.stance),
    )
    return rep
