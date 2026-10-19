"""Live-shaped portfolio simulator (card E7.11, PLAN D84): the daily cross-name funnel.

The E7.5 harness (:func:`arc.backtest.ranking.run_portfolio`) picks top-1 **per ticker**
and visits tickers alphabetically when caps bind, sizes on $100K and has no screen,
blackout or capacity limit. This module trades the way Arc trades live, at each
session close, from E7.10's point-in-time view (:class:`arc.backtest.data.HistoryView`):

1. **Close, then mark.** Positions whose exit fired (or that expired) are booked;
   the book is marked at that session's mids.
2. **Eligible set.** Checkpoint ∩ PIT membership ∩ a close. The live D9 screen
   (``backtest.sim.apply_screen``) applies to every name except the seed.
3. **Active-list proxy** (:func:`arc.universe.tiers.resolve_active`, cap
   ``active_cap``): core = the seed; momentum = the top ``momentum_size`` by the live
   momentum rule on closes ≤ d (risk-adjusted 12-1 month return, refreshed on the
   first session of each month like the live monthly tier, ranked by
   :func:`arc.universe.momentum.select_members`); the remaining slots go to the rest
   of the screened set by option liquidity (that day's option volume). Discovery and
   Trending cannot be replayed (no historical LLM, Reddit or Stocktwits inputs).
4. **Stance proxy.** The live regime model's trend label (``regime_model``, v2) →
   the profile's ``stance_strategies`` (and ``backtest.vol_gate``).
5. **Menus** (E7.10 menu cache). E7.19 (D125): a profile with entry-DTE sleeves
   (``config/account_profiles.yaml``) builds one menu per sleeve, on its window and
   its exit block (:func:`sleeve_windows`), and tags every candidate with its sleeve;
   a single-window profile is one implicit ``default`` sleeve. Then the hard filters,
   the earnings blackout through the pure :func:`arc.gate.rules.check_earnings_blackout`
   on E7.10's projected dates, and the optional entry filters.
6. **Cross-name ranking.** Every active name's top ``per_name_depth`` candidates are
   pooled and sorted by (ranker key, option liquidity desc, symbol, candidate key):
   shuffling the ticker order cannot change the result.
   E7.19: each name contributes its top ``per_name_depth`` per sleeve (live: one menu
   per open sleeve); the sleeves compete in the one pool.
7. **Open** down the pool until ``max_open_positions`` binds; a candidate whose
   sleeve is full (gate rule ``sleeve_max_positions``) is skipped as ``sleeve_full``. Sizing is D18
   :func:`arc.sizing.size_contracts` on the marked equity of the account (D80:
   $25K, ``backtest.sim.equity``) with the name's open max loss as
   ``existing_max_loss``. Pure gate rules applied: :data:`GATE_RULES_APPLIED`.
8. **Fill model.** Each leg fills at ``mid ± x·spread`` (``slippage_x``, per-kind
   ``backtest.slippage_by_kind``). No fill when a leg's spread breaks the live rule
   (``spread ≤ max(spread_max_pct · mid, spread_max_abs)``) or a leg has no tradeable
   quote that session; a failed exit retries next session (``exit_delayed``).
9. **Capacity.** Contracts ≤ ``max_pct_oi`` of each leg's OI and ≤ ``max_pct_volume``
   of its session volume (clip or skip). A partition without OI (Alpaca) applies
   the volume limit only.
10. **Exits.** ``config/exits.yaml`` on EOD marks (stop → lock → TP → DTE), as E7.5.
    E7.19: each position resolves its policy once at entry through
    ``ArcSettings.exit_block_for_dte(entry DTE)`` and ``ExitConfig.policy_for`` (the
    live position manager's call path), so the short sleeve gets its own block.
    Rules flagged intraday (``stop_eod_only: false``, ``profit_lock.eod_only:
    false``) are also checked on a daily-range proxy (``intraday_proxy: range``): every
    leg re-priced at the session's underlying high and low at its EOD IV (sticky
    strike), stop first, exit at the proxy price minus the spread cost
    (``stop_intraday_proxy`` / ``lock_intraday_proxy``). The proxy reads only session
    d's high/low for d's check; it over-states stops on whipsaw days.

Everything that depends on one ticker (menus, outcome paths, eligibility, liquidity)
is computed by :func:`sim_ticker_job` in a worker; :func:`run_sim` is a single-process
walk over those results. ``selection: per_ticker`` keeps the E7.5 walk (top-1 per
ticker, alphabetical, realised-equity sizing) for reproducibility.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.chain import implied_vol_vec
from arc.backtest.correlations import pit_closes
from arc.backtest.engine import MULT, STRUCTURE_KIND, Trade, close_early, open_trade, settle
from arc.backtest.entry_filter import (
    apply_be_filter,
    apply_entry_filter,
    apply_spread_premium_filter,
    atr14_days,
    stretched_days,
)
from arc.backtest.menucache import MenuCache, MenuCacheContext, config_hash
from arc.backtest.ranking import (
    Candidate,
    Outcome,
    _legs,
    apply_stance,
    build_menus,
    build_menus_by_kind,
    kind_costs,
    labels_for,
    trend_stances,
)
from arc.backtest.sim_config import SimSettings
from arc.backtest.strategies import StrategyKind, StrategySpec
from arc.exits.policy import ExitReason, check_rules, lock_fires, resolve_rules
from arc.features.correlation import MIN_OBS, WINDOW, return_corr
from arc.gate.caps import alloc_pct_for, deployed_max_loss, open_risk
from arc.gate.cluster import corr_key
from arc.gate.inputs import AccountSnapshot, MarketSnapshot, MarketTrend, Portfolio, Position
from arc.gate.rules import (
    Derived,
    check_cluster_cap,
    check_daily_loss,
    check_dte_window,
    check_earnings_blackout,
    check_max_gain,
    check_max_open_positions,
    check_open_risk,
    check_per_underlying,
    check_short_premium_regime,
    check_sleeve_positions,
    price_ceiling,
)
from arc.models import Proposal, Structure
from arc.pricing.bs import price_vectorized
from arc.scanner.rank import Ranker, RankFilters, passes_filters, rank, sort_key
from arc.sizing import size_contracts
from arc.structures import analyze
from arc.universe.momentum import HoldingRow, select_members
from arc.universe.tiers import Tier, TierMember, resolve_active
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.backtest.costs import CostModel
    from arc.backtest.data import HistoryView
    from arc.backtest.earnings_hist import NextEarnings
    from arc.backtest.ranking import RankingFile
    from arc.config import ArcSettings
    from arc.exits.policy import ExitConfig, ExitModelConfig, ResolvedRules

log = structlog.get_logger()

__all__ = [
    "GATE_RULES_APPLIED",
    "GATE_RULES_NOT_APPLIED",
    "NOT_REPLAYED",
    "PROXY_EXITS",
    "SKIP_REASONS",
    "DayInfo",
    "SimCandidate",
    "SimOutcome",
    "SimRun",
    "SimSettings",
    "SimTicker",
    "active_list",
    "capacity_cap",
    "exit_search",
    "exit_walk",
    "momentum_scores",
    "market_trends",
    "run_sim",
    "sim_outcome",
    "sim_ticker_from_chains",
    "sim_ticker_job",
    "sleeve_windows",
    "spread_ok",
]

#: Pure gate rules the walk applies (listed in every report).
GATE_RULES_APPLIED: tuple[str, ...] = (
    "check_per_underlying",
    "check_max_open_positions",
    "check_daily_loss",
    "check_max_gain",
    "check_earnings_blackout",
    "check_short_premium_regime (D131: SPY close vs its point-in-time SMA, when the walk "
    "loads SPY)",
    "check_open_risk (D131: open-risk cap on the held book; sizing fits its headroom)",
    "check_cluster_cap (D131: point-in-time 60-session return correlations of the held "
    "names from the walk's own closes, strictly before the session)",
    "cash_settled fit (check_account_profile's cash sub-check; cash_settled profiles)",
    "margin buying-power fit (check_account_profile's D129 sub-check: options buying "
    "power = equity - held max loss; margin profiles, sim.margin_buying_power)",
    "check_sleeve_positions (sleeve_max_positions; E7.19, sleeve profiles)",
    "check_dte_window (through the sleeve windows; E7.19, sleeve profiles)",
)
#: Live rules the walk does not apply, and why (also in every report).
GATE_RULES_NOT_APPLIED: tuple[str, ...] = (
    "check_greek_caps (needs per-leg greeks of the whole book; not in the menu)",
    "check_dte_window on single-window profiles (the menu's own DTE window decides; "
    "backtest.dte_windows may differ)",
    "check_spread_tick / check_band / tick grid (the fill model's spread rule stands in)",
    "check_wash_sale, check_order_budget, check_day_trades, check_halt, approval TTL, "
    "data freshness (operational; no history)",
)
NOT_REPLAYED = (
    "Discovery and Trending tiers are not replayed (no historical LLM, Reddit or "
    "Stocktwits inputs): the active list is core (seed) + momentum + the rest of the "
    "screened set by option liquidity."
)
#: Skip / funnel reason codes (skips.csv ``reason``).
SKIP_REASONS: tuple[str, ...] = (
    "screen",
    "blackout",
    "no_candidate",
    "unsettled",
    "sizing",
    "gate_per_underlying",
    "gate_max_open_positions",
    "gate_daily_loss",
    "gate_max_gain",
    "cash",
    "capacity",
    "no_fill",
    "exit_delayed",
    "new_risk_cap",
    "sleeve_full",
    "buying_power",
    "gate_dte_window",
    "gate_short_premium_regime",
    "gate_open_risk_cap",
    "gate_cluster_cap",
)
#: E7.19: exit labels of the intraday-rule daily-range proxy (trades.csv exit_reason).
PROXY_EXITS: dict[str, str] = {"stop": "stop_intraday_proxy", "profit_lock": "lock_intraday_proxy"}
DEFAULT_SLEEVE = "default"


# ---------------------------------------------------------------------------
# Per-ticker results (built in a worker)
# ---------------------------------------------------------------------------


class SimOutcome(BaseModel):
    """What one unit of a candidate does under the fill model and exits."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    entry_ok: bool = Field(..., description="Every leg's entry spread passes the live rule")
    leg_oi: list[float | None] = Field(default_factory=list)
    leg_volume: list[float | None] = Field(default_factory=list)
    exit_delays: int = Field(0, description="Sessions an exit waited for a tradeable quote")


class SimCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate: Candidate
    result: SimOutcome | None = Field(None, description="None = cannot be filled or settled")
    sleeve: str = Field(
        DEFAULT_SLEEVE, description="E7.19: entry-DTE sleeve (ArcSettings.sleeve_for_dte)"
    )
    #: E7.20 (D125): the same candidate filled at other slippage x (``fill x -> outcome``);
    #: exit timing is shared, only fills, max loss and marks differ. Empty = control x only.
    alt: dict[float, SimOutcome | None] = Field(default_factory=dict)
    #: E7.20: the stances (trend labels ``bull``/``bear``/``sideways``, or
    #: :data:`arc.backtest.stance.ANY`) under which this candidate is in its name's top
    #: ``per_name_depth``; the walk keeps it only on a session whose proxy stance is in
    #: here. None = an untagged (pre-E7.20) build, stance already applied.
    stances: frozenset[str] | None = None

    def at_x(self, x: float | None) -> SimCandidate:
        """This candidate with its outcome at fill slippage *x* (None = control x)."""
        if x is None:
            return self
        return self.model_copy(update={"result": self.alt[x]})


class DayInfo(BaseModel):
    """One ticker at one session's close (all point-in-time)."""

    model_config = ConfigDict(frozen=True)

    eligible: bool
    screen_pass: bool
    seed: bool
    etf: bool = False
    liquidity: float = Field(0.0, description="That session's option contract volume")
    earnings_known: bool = True
    next_earnings: dt.date | None = None


class SimTicker(BaseModel):
    """Everything :func:`run_sim` needs from one ticker."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ticker: str
    closes: pd.Series
    adj_closes: pd.Series | None = Field(
        None, description="E7.17: split-adjusted closes (momentum); None = no split"
    )
    days: dict[dt.date, DayInfo] = Field(default_factory=dict)
    trend: pd.Series
    vol: pd.Series
    #: profile -> day -> candidates after stance/filters (each name's top per ranker)
    menus: dict[str, dict[dt.date, list[SimCandidate]]] = Field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    provider: str | None = None


# ---------------------------------------------------------------------------
# Fill model / capacity (pure)
# ---------------------------------------------------------------------------


def spread_ok(mid: float, spread: float, settings: ArcSettings) -> bool:
    """The live per-leg spread rule (PLAN §5, ``check_spread_tick``): ≤ pct·mid or ≤ abs."""
    if not (math.isfinite(mid) and math.isfinite(spread)):
        return False
    return spread <= max(settings.spread_max_pct * mid, settings.spread_max_abs) + 1e-9


def capacity_cap(
    oi: Sequence[float | None], volume: Sequence[float | None], sim: SimSettings
) -> int | None:
    """Max contracts the legs' OI and session volume allow (``None`` = no data, no cap)."""
    caps: list[int] = []
    for o in oi:
        if o is not None and math.isfinite(o):
            caps.append(math.floor(sim.max_pct_oi * o + 1e-9))
    for v in volume:
        if v is not None and math.isfinite(v):
            caps.append(math.floor(sim.max_pct_volume * v + 1e-9))
    return min(caps) if caps else None


def _num(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ---------------------------------------------------------------------------
# Exit marks and splits (E7.17)
# ---------------------------------------------------------------------------

#: A ratio within this relative distance of a whole number is that whole-number split.
_SPLIT_TOL = 0.005


def quote_marks(
    raw: pd.DataFrame, closes: pd.Series, *, dte_max: int
) -> dict[dt.date, pd.DataFrame]:
    """Per session, ``symbol``/``mid``/``spread`` of every two-sided closing quote.

    The exit book (E7.17): live marks a held position on its legs' quotes whatever their
    width, so the sim must too. A quote counts when bid and ask are finite, ``ask > 0``
    and ``bid <= ask`` (a zero bid is a valid mark for a long leg). Entry selection still
    reads the stricter marked chain (:func:`arc.backtest.marks.quoted_marks`); only
    sessions with an underlying close are kept, like the chains.
    """
    if raw.empty:
        return {}
    df = raw[["date", "symbol", "expiration", "bid", "ask"]]
    dte = (pd.to_datetime(df["expiration"]) - pd.to_datetime(df["date"])).dt.days
    bid = pd.to_numeric(df["bid"], errors="coerce")
    ask = pd.to_numeric(df["ask"], errors="coerce")
    ok = (dte >= 1) & (dte <= dte_max) & bid.notna() & ask.notna() & (ask > 0) & (bid >= 0)
    ok &= bid <= ask
    df = pd.DataFrame(
        {
            "date": df["date"][ok],
            "symbol": df["symbol"][ok].astype(str),
            "mid": ((bid + ask) / 2.0)[ok],
            "spread": (ask - bid)[ok],
        }
    )
    have = set(closes.index)
    return {
        d: g[["symbol", "mid", "spread"]].reset_index(drop=True)
        for d, g in df.groupby("date", sort=True)
        if d in have
    }


def split_ratios(closes: pd.Series, adj_closes: pd.Series) -> dict[dt.date, float]:
    """Split events from raw vs split-adjusted closes: ``{first session after: ratio}``.

    ``ratio`` = shares after / before (4.0 for a 4:1 split, 0.1 for a 1:10 reverse
    split). A ratio within 0.5 % of a whole number is snapped to it; anything else (a
    spin-off adjustment such as DELL 2021-11) is returned as measured.
    """
    j = pd.concat([closes.rename("raw"), adj_closes.rename("adj")], axis=1).dropna()
    j = j[(j["raw"] > 0) & (j["adj"] > 0)]
    if len(j) < 2:
        return {}
    f = j["raw"] / j["adj"]
    r = (f.shift(1) / f).iloc[1:]
    out: dict[dt.date, float] = {}
    for d, v in r.items():
        v = float(v)
        if 0.87 < v < 1.15:
            continue
        snap = round(v) if v >= 1 else 1.0 / round(1.0 / v)
        out[d] = float(snap) if abs(v / snap - 1.0) <= _SPLIT_TOL else v  # type: ignore[index]
    return out


def split_adjusted(closes: pd.Series, splits: Mapping[dt.date, float]) -> pd.Series:
    """*closes* divided by every later split's ratio (continuous across splits)."""
    if not splits:
        return closes
    idx = pd.Index(closes.index)
    out = closes.astype(float).copy()
    for d, r in splits.items():
        out[idx < d] = out[idx < d] / r
    return out


def _standard_split(r: float) -> bool:
    return r >= 2.0 and float(r).is_integer()


def _restruck(o: Any, f: float) -> dict[str, str]:
    """Leg symbol -> the contract it became after *f* x forward splits (strike / f)."""
    if f == 1.0:
        return {lg.symbol: lg.symbol for lg in o.legs}
    from arc.structures import format_occ

    return {
        lg.symbol: format_occ(
            o.underlying, o.expiration, lg.right, Decimal(str(round(lg.strike / f, 3)))
        )
        for lg in o.legs
    }


# ---------------------------------------------------------------------------
# E7.19 (D125): entry-DTE sleeves and the intraday-rule range proxy
# ---------------------------------------------------------------------------


class SleeveWindow(BaseModel):
    """One menu window of a profile: a live entry-DTE sleeve (or the implicit default)."""

    model_config = ConfigDict(frozen=True)

    name: str
    dte_min: int
    dte_max: int
    exit_block: str | None = None


def sleeve_windows(
    settings: ArcSettings, window: tuple[int, int], *, override: bool = False
) -> list[SleeveWindow]:
    """The menu windows of *settings*' profile, read from ``config/account_profiles.yaml``.

    A profile with named sleeves gets one window per sleeve, with the sleeve's
    ``exit_policy`` block. A single-window profile, or any profile whose window the
    backtest overrides (*override*: ``backtest.dte_windows``), is one implicit
    ``default`` sleeve on *window* with the kind policy, exactly the pre-E7.19 menu.
    """
    if override or not settings.profile.has_sleeves:
        return [SleeveWindow(name=DEFAULT_SLEEVE, dte_min=window[0], dte_max=window[1])]
    return [
        SleeveWindow(name=s.name, dte_min=s.dte_min, dte_max=s.dte_max, exit_block=s.exit_policy)
        for s in settings.entry_sleeves
    ]


def sleeve_name(settings: ArcSettings, dte: int) -> str:
    """The sleeve containing entry DTE *dte* (``ArcSettings.sleeve_for_dte``), else default."""
    s = settings.sleeve_for_dte(dte)
    return s.name if s is not None else DEFAULT_SLEEVE


def session_ranges(
    closes: pd.Series, ohlc: pd.DataFrame | None
) -> dict[dt.date, tuple[float, float]]:
    """Per session, the underlying's (high, low) in raw strike units.

    ``underlying_ohlc`` is split-adjusted to today; each day is scaled by that day's
    raw close / adjusted close, so the range sits on the same strikes as the chain.
    The range always contains the raw close. Sessions without a bar are left out.
    """
    if ohlc is None or ohlc.empty or not {"high", "low", "close"} <= set(ohlc.columns):
        return {}
    out: dict[dt.date, tuple[float, float]] = {}
    for d, row in ohlc.iterrows():
        if d not in closes.index:
            continue
        raw, adj = float(closes[d]), float(row["close"])
        hi, lo = float(row["high"]), float(row["low"])
        if not all(math.isfinite(v) and v > 0 for v in (raw, adj, hi, lo)):
            continue
        k = raw / adj
        out[d] = (max(hi * k, raw), min(lo * k, raw))  # type: ignore[index]
    return out


def proxy_marks(
    legs: Sequence[Any],
    marks: Mapping[str, tuple[float, float]],
    *,
    spot: float,
    extreme: float,
    expiration: dt.date,
    day: dt.date,
    r: float,
) -> dict[str, tuple[float, float]] | None:
    """Every leg re-priced at underlying *extreme* at its EOD IV (sticky strike).

    The IV is solved from the leg's EOD mid at the EOD *spot* (European BSM, no
    dividend, ``implied_vol_vec``); the spread stays the EOD quoted spread, so a fill
    there pays the same spread cost. ``None`` when a leg's IV cannot be solved.
    """
    t = max((expiration - day).days, 0) / 365.0
    if t <= 0:
        return None
    mids = np.array([marks[lg.symbol][0] for lg in legs], dtype=float)
    k = np.array([lg.strike for lg in legs], dtype=float)
    flag = np.array(["c" if lg.right == "call" else "p" for lg in legs])
    tt = np.full(len(legs), t)
    iv = implied_vol_vec(mids, spot, k, tt, r, 0.0, flag)
    if not np.isfinite(iv).all():
        return None
    px = price_vectorized(flag, np.full(len(legs), extreme), k, tt, r, iv)
    return {lg.symbol: (float(v), marks[lg.symbol][1]) for lg, v in zip(legs, px, strict=True)}


def range_proxy_rule(
    rules: ResolvedRules,
    legs: Sequence[Any],
    marks: Mapping[str, tuple[float, float]],
    *,
    spot: float,
    rng: tuple[float, float],
    expiration: dt.date,
    day: dt.date,
    r: float,
    peak: float | None,
) -> tuple[str, dict[str, tuple[float, float]], float] | None:
    """The intraday-flagged rule the session's range would have fired, stop first.

    Only rules the policy runs intraday are checked (``stop_eod_only: false``,
    ``profit_lock.eod_only: false``); *peak* is the peak P&L of the earlier EOD
    marks. Each extreme (high, low) is priced by :func:`proxy_marks`; the stop is
    checked on both before the lock (pessimistic: when both touch, the stop wins),
    and of two firing extremes the worse P&L is taken. Returns ``(rule, marks at that
    extreme, extreme)`` or ``None``. Reads only this session's range.
    """
    pol = rules.policy
    stop = rules.stop_pnl if not pol.stop_eod_only else None
    lock_on = pol.profit_lock is not None and not pol.profit_lock.eod_only
    if stop is None and not lock_on:
        return None
    priced: list[tuple[float, dict[str, tuple[float, float]], float]] = []
    for x in rng:
        m = proxy_marks(legs, marks, spot=spot, extreme=x, expiration=expiration, day=day, r=r)
        if m is None:
            return None
        pnl = sum(lg.side * m[lg.symbol][0] for lg in legs) - rules.entry_net
        priced.append((pnl, m, x))
    priced.sort(key=lambda e: e[0])  # worst first
    if stop is not None:
        for pnl, m, x in priced:
            if pnl <= stop:
                return "stop", m, x
    if lock_on:
        for pnl, m, x in priced:
            if lock_fires(rules, pnl=pnl, peak=peak, eod=False):
                return "profit_lock", m, x
    return None


_Close = tuple[dt.date, ExitReason, dict[str, tuple[float, float]], float, str | None]


def exit_search(
    o: Any,
    rules: ResolvedRules,
    *,
    entry_day: dt.date,
    book: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    inside: Sequence[tuple[dt.date, float]],
    settings: ArcSettings,
    spread_rule: bool,
    ranges: Mapping[dt.date, tuple[float, float]] | None = None,
    rate: Callable[[dt.date], float] | float = 0.0,
) -> tuple[_Close | None, dict[dt.date, float], int]:
    """The session-by-session exit search of :func:`sim_outcomes` (E7.20/E7.22).

    Walks *book* (``symbol``/``mid``/``spread`` per session) after *entry_day* and
    before the expiry: the intraday range proxy first (*ranges*), then the EOD rules;
    a fired exit waits for a tradeable session. Found on mids and quoted spreads only,
    so it is the same for every fill model. Returns ``(close or None if nothing fired
    before the book or the expiry ran out, structure mid value per marked session,
    sessions an exit waited)``; ``close = (session, reason, per-leg (mid, spread) to
    fill at, spot, proxy label or None)``. *inside* = split events inside the hold.
    """
    day_mids: dict[dt.date, float] = {}
    peak: float | None = None
    pending = None
    delays = 0
    for day in sorted(book):
        if day <= entry_day:
            continue
        if day >= o.expiration:
            break
        f = math.prod(r for d, r in inside if d <= day)
        sym = _restruck(o, f)
        ch = book[day]
        rows = ch[ch["symbol"].isin(list(sym.values()))]
        raw_m = {str(x.symbol): (float(x.mid), float(x.spread)) for x in rows.itertuples()}
        if not all(s in raw_m for s in sym.values()):
            if pending is not None:
                delays += 1
            continue
        # per original (pre-split) contract: price x f
        m = {old: (raw_m[new][0] * f, raw_m[new][1] * f) for old, new in sym.items()}
        spot = float(closes[day]) * f if day in closes.index else float("nan")
        value_mid = sum(lg.side * m[lg.symbol][0] for lg in o.legs)
        rng = None if ranges is None else ranges.get(day)
        if pending is None and rng is not None and math.isfinite(spot):
            hit = range_proxy_rule(
                rules,
                o.legs,
                m,
                spot=spot,
                rng=(rng[0] * f, rng[1] * f),
                expiration=o.expiration,
                day=day,
                r=float(rate(day)) if callable(rate) else float(rate),
                peak=peak,
            )
            if hit is not None and (
                not spread_rule or all(spread_ok(*raw_m[s], settings) for s in sym.values())
            ):
                rule, pm, x = hit
                return (day, ExitReason(rule), pm, x, PROXY_EXITS[rule]), day_mids, delays
        day_mids[day] = value_mid
        if pending is None:
            pnl = value_mid - rules.entry_net
            pending = check_rules(rules, pnl=pnl, dte=(o.expiration - day).days, peak_pnl=peak)
            peak = pnl if peak is None else max(peak, pnl)
        if pending is not None:
            if spread_rule and not all(spread_ok(*raw_m[s], settings) for s in sym.values()):
                delays += 1
                continue
            return (day, pending, m, spot, None), day_mids, delays
    return None, day_mids, delays


def _close_trade(op: Any, close: _Close, *, cost: CostModel, fill_cost: CostModel | None) -> Trade:
    """Price an :func:`exit_search` close for one fill model."""
    day, reason, cm, spot, label = close
    if fill_cost is not None:
        cm = {k: (v[0], fill_cost.spread(v[0])) for k, v in cm.items()}
    trade = close_early(op, day=day, marks=cm, spot=spot, reason=reason, cost=cost)
    if label is not None:
        trade = trade.model_copy(update={"exit_reason": label})
    return trade


def exit_walk(
    o: Any,
    rules: ResolvedRules,
    *,
    entry_day: dt.date,
    book: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    inside: Sequence[tuple[dt.date, float]],
    settings: ArcSettings,
    spread_rule: bool,
    cost: CostModel,
    fill_cost: CostModel | None = None,
    ranges: Mapping[dt.date, tuple[float, float]] | None = None,
    rate: Callable[[dt.date], float] | float = 0.0,
) -> tuple[Trade | None, dict[dt.date, float], int]:
    """:func:`exit_search` priced under one fill model (E7.22: the recon's exit replay).

    Returns ``(trade or None if nothing fired before the book or the expiry ran out,
    unrealised net P&L per marked session, sessions an exit waited)``.
    """
    close, day_mids, delays = exit_search(
        o,
        rules,
        entry_day=entry_day,
        book=book,
        closes=closes,
        inside=inside,
        settings=settings,
        spread_rule=spread_rule,
        ranges=ranges,
        rate=rate,
    )
    entry_fill = sum(lg.side * lg.fill for lg in o.legs)
    day_marks = {d: (v - entry_fill) * MULT - o.open_fees for d, v in day_mids.items()}
    trade = None if close is None else _close_trade(o, close, cost=cost, fill_cost=fill_cost)
    return trade, day_marks, delays


def sim_outcome(
    c: Candidate,
    *,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    cost: CostModel,
    exits: ExitConfig,
    settings: ArcSettings,
    spread_rule: bool,
    marks: Mapping[dt.date, pd.DataFrame] | None = None,
    splits: Mapping[dt.date, float] | None = None,
    fill_cost: CostModel | None = None,
    ranges: Mapping[dt.date, tuple[float, float]] | None = None,
    rate: Callable[[dt.date], float] | float = 0.0,
    sleeves: bool = True,
) -> SimOutcome | None:
    """Play *c* forward: entry at mid ± x·spread, exits on EOD marks, retry on no fill.

    ``None`` when the entry cannot be priced or the trade cannot settle in the data.
    A session where a leg is unquoted (or, with *spread_rule*, too wide) cannot close
    the position: a fired exit waits for the next tradeable session. A position still
    waiting at expiry settles at intrinsic. With every leg quoted and *spread_rule*
    off this equals :func:`arc.backtest.ranking.simulate_outcome`.

    E7.17: *marks* is the exit book (:func:`quote_marks`, every two-sided quote);
    ``None`` marks on *chains* (the entry-filtered chain, E7.11). *splits*
    (:func:`split_ratios`) follow a whole-number forward split inside the hold onto the
    re-struck contract (strike / r, price × r) and settle on the adjusted close; any
    other adjustment inside the hold returns ``None``. *fill_cost* (estimated-spread
    counterfactual) re-prices every fill's spread as ``fill_cost.spread(mid)``.

    E7.19: the exit policy is resolved once, at entry, from the entry DTE's sleeve
    (``settings.exit_block_for_dte``; *sleeves* False = the kind policy, for a
    one-window menu that overrides the profile's sleeves). *ranges*
    (:func:`session_ranges`; ``None`` = no proxy) add the intraday-rule daily-range
    check (:func:`range_proxy_rule`) on each session before its EOD rules; *rate* is
    the session's risk-free rate for it.
    """
    return sim_outcomes(
        c,
        chains=chains,
        closes=closes,
        fills=[(cost, fill_cost)],
        exits=exits,
        settings=settings,
        spread_rule=spread_rule,
        marks=marks,
        splits=splits,
        ranges=ranges,
        rate=rate,
        sleeves=sleeves,
    )[0]


def sim_outcomes(
    c: Candidate,
    *,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    fills: Sequence[tuple[CostModel, CostModel | None]],
    exits: ExitConfig,
    settings: ArcSettings,
    spread_rule: bool,
    marks: Mapping[dt.date, pd.DataFrame] | None = None,
    splits: Mapping[dt.date, float] | None = None,
    ranges: Mapping[dt.date, tuple[float, float]] | None = None,
    rate: Callable[[dt.date], float] | float = 0.0,
    sleeves: bool = True,
) -> list[SimOutcome | None]:
    """:func:`sim_outcome` under several fill models ``(cost, fill_cost)`` at once (E7.20).

    The exit rules trigger on mid marks and the spread rule on quoted spreads, so the
    exit session, its reason and the delays do not depend on the fill slippage: they
    are found once, and each fill model only prices the entry, the exit and the
    unrealised marks. ``fills=[(cost, fill_cost)]`` is exactly :func:`sim_outcome`.
    """
    spec_kind = StrategyKind(c.kind)
    opens = []
    for cost, fill_cost in fills:
        picks = c.picks
        if fill_cost is not None:
            picks = [p.model_copy(update={"spread": fill_cost.spread(p.mid)}) for p in picks]
        opens.append(
            open_trade(
                picks,
                spec=StrategySpec(kind=spec_kind),
                underlying=c.underlying,
                entry_date=c.day,
                spot=float(closes[c.day]),
                cost=cost,
            )
        )
    none: list[SimOutcome | None] = [None] * len(fills)
    o = next((x for x in opens if x is not None), None)
    if o is None:
        return none
    last_close = max(closes.index)
    if o.expiration > last_close:
        return none
    inside = sorted((d, r) for d, r in (splits or {}).items() if c.day < d <= o.expiration)
    if any(not _standard_split(r) for _, r in inside):
        return none
    total = math.prod(r for _, r in inside)
    past = closes[pd.Index(closes.index) <= o.expiration]
    if past.empty:
        return none
    entry_chain = chains.get(c.day)
    leg_oi: list[float | None] = []
    leg_vol: list[float | None] = []
    for p in c.picks:
        row = None
        if entry_chain is not None and "symbol" in entry_chain:
            hit = entry_chain[entry_chain["symbol"] == p.symbol]
            row = None if hit.empty else hit.iloc[0]
        leg_oi.append(None if row is None else _num(row.get("open_interest")))
        leg_vol.append(None if row is None else _num(row.get("volume")))
    entry_ok = (not spread_rule) or all(spread_ok(p.mid, p.spread, settings) for p in c.picks)
    structure = analyze(_legs(c.picks, c.underlying), as_of=c.day)
    block = settings.exit_block_for_dte(o.dte) if sleeves else None  # E7.19: once, at entry
    rules = resolve_rules(structure, exits.policy_for(STRUCTURE_KIND[spec_kind], block))
    close, day_mids, delays = exit_search(
        o,
        rules,
        entry_day=c.day,
        book=chains if marks is None else marks,
        closes=closes,
        inside=inside,
        settings=settings,
        spread_rule=spread_rule,
        ranges=ranges,
        rate=rate,
    )
    out: list[SimOutcome | None] = []
    for (cost, fill_cost), op in zip(fills, opens, strict=True):
        if op is None:
            out.append(None)
            continue
        held = settle(op, float(past.iloc[-1]) * total, cost)
        trade = held
        if close is not None:
            trade = _close_trade(op, close, cost=cost, fill_cost=fill_cost)
        entry_fill = sum(lg.side * lg.fill for lg in op.legs)
        exit_day = trade.exit_date or trade.expiration
        res = Outcome(
            trade=trade,
            exit_day=exit_day,
            marks={
                d: (v - entry_fill) * MULT - op.open_fees
                for d, v in day_mids.items()
                if d < exit_day
            },
            hold_to_expiry_pnl=held.pnl,
            entry_debit=entry_fill,
            open_fees=op.open_fees,
        )
        out.append(
            SimOutcome(
                outcome=res,
                entry_ok=entry_ok,
                leg_oi=leg_oi,
                leg_volume=leg_vol,
                exit_delays=delays,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Building one ticker
# ---------------------------------------------------------------------------


def _top_keys(
    menu: Sequence[Candidate],
    rankers: Sequence[Ranker],
    filters: RankFilters,
    vrp: float,
    depth: int,
) -> set[str]:
    keys: set[str] = set()
    for rk in rankers:
        keys |= {
            c.key
            for c in rank([c.inputs for c in menu], rk, filters=filters, vrp_threshold=vrp)[:depth]
        }
    return keys


def sim_ticker_from_chains(
    ticker: str,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    *,
    decision_days: Sequence[dt.date],
    days: Mapping[dt.date, DayInfo],
    cfg: RankingFile,
    exits: ExitConfig,
    cost: CostModel,
    settings: Mapping[str, ArcSettings],
    windows: Mapping[str, tuple[int, int]],
    rankers: Mapping[str, Sequence[Ranker]],
    stances: Mapping[str, Mapping[str, frozenset[str]]] | None,
    sim: SimSettings,
    rate: Any = None,
    ohlc: pd.DataFrame | None = None,
    label_closes: pd.Series | None = None,
    cache: MenuCache | None = None,
    cache_ctx: MenuCacheContext | None = None,
    provider: str | None = None,
    quotes: Mapping[dt.date, pd.DataFrame] | None = None,
    splits: Mapping[dt.date, float] | None = None,
    stance_labels: frozenset[str] = frozenset(),
    alt_x: Sequence[float] = (),
) -> SimTicker:
    """Menus (cached), stance/filters and outcome paths for one ticker.

    *rate* maps a session to its risk-free rate (``RateCurve.rate``); ``None`` = the
    flat ``backtest.risk_free_rate``. Only each name's top ``per_name_depth``
    candidates per ranker get an outcome path; the walk pools exactly those.
    E7.17: *quotes* is the exit book (:func:`quote_marks`; ``None`` = mark on
    *chains*), *splits* the ticker's split events (:func:`split_ratios`): outcomes
    follow them, and trend labels and momentum read split-adjusted closes.

    E7.19 (D125): each profile's menu is built per entry-DTE sleeve
    (:func:`sleeve_windows`: its window, its exit block in the managed-exit model),
    each sleeve's own top ``per_name_depth`` get outcome paths, and every candidate
    is tagged with its sleeve. A single-window profile is one ``default`` sleeve.
    With ``backtest.sim.intraday_proxy: range`` (cross_name only) outcomes add the
    daily-range check of intraday-flagged rules on *ohlc*.

    E7.20 (D125): *stance_labels* (:func:`arc.backtest.stance.labels_needed`) tag the
    menu per stance so the walk can apply any stance proxy; *alt_x* adds each kept
    candidate's outcome at those fill slippage x. Both empty = the pre-E7.20 build.
    """
    closes = closes.sort_index()
    bt = cfg.backtest
    mc: ExitModelConfig = exits.model.model_copy(update={"n_paths": bt.n_paths})
    dd = [d for d in decision_days if d in chains and d in closes.index]
    adj = split_adjusted(closes, splits or {})
    if label_closes is None:
        label_closes = adj
    trend, vol = labels_for(label_closes, sim.regime_model)
    day_stance = trend_stances(trend) if bt.direction_tilt > 0 else None
    stretched = None
    # E7.19 (D128): the live anti-chase filter on the live-shaped walk
    live_filter = sim.entry_filters == "live" and sim.selection == "cross_name"
    if bt.entry_filter != "none" or live_filter:
        stretched = stretched_days(
            ohlc if ohlc is not None else pd.DataFrame(),
            dd,
            bt.anti_chase,
            vwap=bt.entry_filter == "anti_chase_vwap",
        )
    atr = (
        atr14_days(ohlc if ohlc is not None else pd.DataFrame(), closes, dd)
        if bt.max_be_atr is not None
        else None
    )
    base = cost.model_copy(update={"slippage_frac": sim.slippage_x})
    # per_ticker reproduces the E7.5 walk: no spread rule on its fills or exits
    spread_rule = sim.spread_rule == "live" and sim.selection == "cross_name"
    ranges = None
    if sim.intraday_proxy == "range" and sim.selection == "cross_name":
        ranges = session_ranges(closes, ohlc)

    def rate_of(d: dt.date) -> float:
        return bt.risk_free_rate if rate is None else float(rate(d))

    out: dict[str, dict[dt.date, list[SimCandidate]]] = {}
    for profile, window in sorted(windows.items()):
        per_day: dict[dt.date, list[SimCandidate]] = {}
        # an explicit window other than the profile's envelope (backtest.dte_windows, a
        # test) or the E7.5 per_ticker walk keeps the one-window menu
        override = (
            tuple(window) != settings[profile].entry_dte_window or sim.selection == "per_ticker"
        )
        for sw in sleeve_windows(settings[profile], window, override=override):
            for d, kept in _sleeve_menu(
                ticker,
                chains,
                closes,
                sw=sw,
                profile=profile,
                dd=dd,
                cfg=cfg,
                exits=exits,
                mc=mc,
                base=base,
                settings=settings[profile],
                rankers=rankers[profile],
                stances=stances,
                trend=trend,
                vol=vol,
                day_stance=day_stance,
                stretched=stretched,
                atr=atr,
                sim=sim,
                spread_rule=spread_rule,
                rate=rate,
                rate_of=rate_of,
                cache=cache,
                cache_ctx=cache_ctx,
                quotes=quotes,
                splits=splits,
                ranges=ranges,
                stance_labels=stance_labels,
                alt_x=alt_x,
            ).items():
                per_day.setdefault(d, []).extend(kept)
        out[profile] = per_day
    return SimTicker(
        ticker=ticker,
        closes=closes,
        adj_closes=adj if splits else None,
        days=dict(days),
        trend=trend,
        vol=vol,
        menus=out,
        cache_hits=cache.hits if cache is not None else 0,
        cache_misses=cache.misses if cache is not None else 0,
        provider=provider,
    )


def _sleeve_menu(
    ticker: str,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    *,
    sw: SleeveWindow,
    profile: str,
    dd: Sequence[dt.date],
    cfg: RankingFile,
    exits: ExitConfig,
    mc: ExitModelConfig,
    base: CostModel,
    settings: ArcSettings,
    rankers: Sequence[Ranker],
    stances: Mapping[str, Mapping[str, frozenset[str]]] | None,
    trend: pd.Series,
    vol: pd.Series,
    day_stance: Mapping[dt.date, str] | None,
    stretched: Any,
    atr: Any,
    sim: SimSettings,
    spread_rule: bool,
    rate: Any,
    rate_of: Callable[[dt.date], float],
    cache: MenuCache | None,
    cache_ctx: MenuCacheContext | None,
    quotes: Mapping[dt.date, pd.DataFrame] | None,
    splits: Mapping[dt.date, float] | None,
    ranges: Mapping[dt.date, tuple[float, float]] | None,
    stance_labels: frozenset[str] = frozenset(),
    alt_x: Sequence[float] = (),
) -> dict[dt.date, list[SimCandidate]]:
    """One (profile, sleeve) menu per session: build, stance/filters, top keys, outcomes.

    E7.20 (D125): with *stance_labels* the menu is tagged per stance instead of being
    cut to the session's own trend label (:func:`_tagged_menu`); with *alt_x* every kept
    candidate also carries its outcome filled at each of those slippage x
    (:attr:`SimCandidate.alt`; menus and ranking stay priced at the control cost model).
    """
    bt = cfg.backtest
    lo, hi = sw.dte_min, sw.dte_max
    specs = bt.specs_for(profile, lo, hi, sw.exit_block)
    ck = kind_costs(specs, base, bt.slippage_by_kind) if bt.slippage_by_kind else {}

    def outcome(c: Candidate, tags: frozenset[str] | None) -> SimCandidate:
        own = ck.get(c.kind, base)
        fills: list[tuple[CostModel, CostModel | None]] = [
            (own, own if sim.fill_spread == "estimated" else None)
        ]
        for x in alt_x:  # E7.20: every kind at the grid's x
            cx = base.model_copy(update={"slippage_frac": float(x)})
            fills.append((cx, cx if sim.fill_spread == "estimated" else None))
        res = sim_outcomes(
            c,
            chains=chains,
            closes=closes,
            fills=fills,
            exits=exits,
            settings=settings,
            spread_rule=spread_rule,
            marks=quotes,
            splits=splits,
            ranges=ranges,
            rate=rate_of,
        )
        dte = (c.picks[0].expiration - c.day).days
        tag = sleeve_name(settings, dte) if sw.name != DEFAULT_SLEEVE else DEFAULT_SLEEVE
        return SimCandidate(
            candidate=c,
            result=res[0],
            sleeve=tag,
            alt={float(x): r for x, r in zip(alt_x, res[1:], strict=True)},
            stances=tags,
        )

    menus: dict[dt.date, list[Candidate]] = {}
    by_rate: dict[float, list[dt.date]] = {}
    for d in dd:  # build_menus takes one rate: group sessions by it (flat = one call)
        by_rate.setdefault(bt.risk_free_rate if rate is None else float(rate(d)), []).append(d)
    for r, rdays in by_rate.items():
        kw: dict[str, Any] = {
            "days": rdays,
            "underlying": ticker,
            "specs": specs,
            "cost": base,
            "exits": exits,
            "mc": mc,
            "r": r,
            "stances": day_stance,
            "tilt": bt.direction_tilt,
            "cache": cache,
            "cache_ctx": cache_ctx,
        }
        # exactly the E7.5 ticker_job calls, so per_ticker mode reproduces its menus
        if ck:
            menus |= build_menus_by_kind(chains, closes, cost_by_kind=ck, **kw)
        else:
            menus |= build_menus(chains, closes, **kw)
    if bt.max_spread_premium is not None:  # E7.20: stance-independent, before ranking
        menus, _ = apply_spread_premium_filter(menus, bt.max_spread_premium)
    if stances is not None:
        gate = {str(k): frozenset(v) for k, v in bt.vol_gate.get(profile, {}).items()}
        if stance_labels:
            return _tagged_menu(
                menus,
                stance_labels,
                kinds=stances[profile],
                gate=gate,
                vol=vol,
                stretched=stretched,
                atr=atr,
                closes=closes,
                cfg=cfg,
                rankers=rankers,
                sim=sim,
                outcome=outcome,
            )
        menus = apply_stance(menus, trend, stances[profile], vol=vol, vol_gate=gate or None)
    elif stance_labels:
        return _tagged_menu(
            menus,
            stance_labels,
            kinds=None,
            gate={},
            vol=vol,
            stretched=stretched,
            atr=atr,
            closes=closes,
            cfg=cfg,
            rankers=rankers,
            sim=sim,
            outcome=outcome,
        )
    if stretched is not None:
        menus, _ = apply_entry_filter(menus, trend, stretched)
    if atr is not None and bt.max_be_atr is not None:
        menus, _ = apply_be_filter(menus, closes, atr, bt.max_be_atr)
    per_day: dict[dt.date, list[SimCandidate]] = {}
    for d, menu in menus.items():
        if not menu:
            continue
        keys = _top_keys(
            menu,
            rankers,
            cfg.ranking.filters,
            cfg.ranking.vrp_threshold,
            sim.per_name_depth,
        )
        per_day[d] = [outcome(c, None) for c in menu if c.inputs.key in keys]
    return per_day


def _tagged_menu(
    menus: Mapping[dt.date, list[Candidate]],
    labels: frozenset[str],
    *,
    kinds: Mapping[str, frozenset[str]] | None,
    gate: Mapping[str, frozenset[str]],
    vol: pd.Series,
    stretched: Any,
    atr: Any,
    closes: pd.Series,
    cfg: RankingFile,
    rankers: Sequence[Ranker],
    sim: SimSettings,
    outcome: Callable[[Candidate, frozenset[str] | None], SimCandidate],
) -> dict[dt.date, list[SimCandidate]]:
    """E7.20 (D125): one stance-independent menu per session, tagged per stance.

    For every stance in *labels* the menu goes through exactly the filters the
    untagged build applies for a session of that trend label (stance → structures,
    vol gate, the anti-chase entry filter, breakeven realism) and its top
    ``per_name_depth`` per ranker are found. Each kept candidate carries the stances it
    is a top pick under (:attr:`SimCandidate.stances`); the walk keeps it on a session
    whose proxy stance is one of them. So for the stance the session's own trend label
    gives, the walk sees exactly the untagged build's candidates.
    """
    from arc.backtest.stance import ANY

    bt = cfg.backtest
    base: Mapping[dt.date, list[Candidate]] = menus
    if atr is not None and bt.max_be_atr is not None:  # stance-independent: once
        base, _ = apply_be_filter(menus, closes, atr, bt.max_be_atr)
    days = list(base)
    tags: dict[dt.date, dict[str, set[str]]] = {d: {} for d in days}
    # "unknown" too: an unlabelled session trades the whole menu without a stance filter
    # (stance none) and nothing with one, exactly as the untagged build
    for lab in sorted(labels | {"unknown"}):
        m: Mapping[dt.date, list[Candidate]] = base
        if kinds is not None:
            allowed = {ANY: frozenset().union(*kinds.values())} if lab == ANY else dict(kinds)
            m = apply_stance(
                m, pd.Series(lab, index=days, dtype=object), allowed, vol=vol, vol_gate=gate or None
            )
        if stretched is not None:  # ANY = no stance: the entry filter keeps the menu
            stance = "unknown" if lab == ANY else lab
            m, _ = apply_entry_filter(m, pd.Series(stance, index=days, dtype=object), stretched)
        for d, menu in m.items():
            if not menu:
                continue
            for k in _top_keys(
                menu,
                rankers,
                cfg.ranking.filters,
                cfg.ranking.vrp_threshold,
                sim.per_name_depth,
            ):
                tags[d].setdefault(k, set()).add(lab)
    per_day: dict[dt.date, list[SimCandidate]] = {}
    for d in days:
        if not tags[d]:
            continue
        per_day[d] = [
            outcome(c, frozenset(tags[d][c.inputs.key])) for c in base[d] if c.inputs.key in tags[d]
        ]
    return per_day


def _day_info(view: HistoryView, ticker: str, day: dt.date) -> DayInfo:
    e = view.eligibility(ticker, day)
    raw = view.load(ticker).raw_day(day)
    liq = float(pd.to_numeric(raw["volume"], errors="coerce").fillna(0).sum()) if len(raw) else 0.0
    etf = view.is_etf(ticker)
    ne: NextEarnings = view.next_earnings(ticker, day)
    return DayInfo(
        eligible=e.eligible,
        screen_pass=e.screen_pass,
        seed=e.seed,
        etf=etf,
        liquidity=liq,
        earnings_known=ne.basis != "unknown",
        next_earnings=ne.day,
    )


class ViewSpec(BaseModel):
    """How a worker rebuilds the :class:`HistoryView` (picklable)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    data_dir: str
    universe: str
    start: dt.date
    end: dt.date = Field(..., description="Last decision session")
    data_end: dt.date = Field(..., description="Last session read (exits settle by then)")
    flat_rate: float
    min_iv_obs: int


def build_view(spec: ViewSpec, cfg: RankingFile, cost: CostModel) -> HistoryView:
    from arc.backtest.data import HistoryView

    return HistoryView(
        spec.data_dir,
        settings=cfg.backtest.data,
        universe=spec.universe,
        start=spec.start,
        end=spec.data_end,
        cost=cost,
        flat_rate=spec.flat_rate,
        min_iv_obs=spec.min_iv_obs,
    )


def sim_ticker_job(
    tickers: Sequence[str],
    *,
    spec: ViewSpec,
    cfg: RankingFile,
    exits: ExitConfig,
    cost: CostModel,
    settings: Mapping[str, ArcSettings],
    windows: Mapping[str, tuple[int, int]],
    rankers: Mapping[str, Sequence[Ranker]],
    stances: Mapping[str, Mapping[str, frozenset[str]]] | None,
    sim: SimSettings,
    cache_dir: str | None,
    stance_labels: frozenset[str] = frozenset(),
    alt_x: Sequence[float] = (),
) -> list[SimTicker]:
    """A batch of tickers through one view (safe in a worker process)."""
    view = build_view(spec, cfg, cost)
    ds = cfg.backtest.data
    out: list[SimTicker] = []
    for t in tickers:
        h = view.load(t)
        sessions = [d for d in h.closes.index if spec.start <= d <= spec.end]
        days = {d: _day_info(view, t, d) for d in sessions}
        cache = MenuCache(cache_dir) if cache_dir is not None else None
        ctx = MenuCacheContext(
            marks=f"{h.marks}:{ds.max_quoted_spread_pct}:{ds.quoted_fallback}",
            exit_hash=config_hash(exits),
            n_paths=cfg.backtest.n_paths,
            seed=exits.model.seed,
            provider=h.provider,
        )
        live = sim.selection == "cross_name"  # per_ticker reproduces E7.5 unchanged
        quotes = None
        if live and sim.exit_marks == "all_quotes" and h.marks == "quoted":
            quotes = quote_marks(h.raw, h.closes, dte_max=ds.chain_dte_max)
        splits = None
        if live and sim.splits == "adjust" and len(h.ohlc):
            splits = split_ratios(h.closes, pd.Series(h.ohlc["close"])) or None
        out.append(
            sim_ticker_from_chains(
                t,
                h.chains,
                h.closes,
                decision_days=sessions,
                days=days,
                cfg=cfg,
                exits=exits,
                cost=cost,
                settings=settings,
                windows=windows,
                rankers=rankers,
                stances=stances,
                sim=sim,
                rate=view.rates.rate,
                ohlc=h.ohlc,
                cache=cache,
                cache_ctx=ctx,
                provider=view.provider_for(t),
                quotes=quotes,
                splits=splits,
                stance_labels=stance_labels,
                alt_x=alt_x,
            )
        )
        view.evict(t)
        log.info("backtest.sim.ticker", ticker=t, sessions=len(sessions))
    return out


# ---------------------------------------------------------------------------
# Momentum + active list (pure)
# ---------------------------------------------------------------------------


def momentum_scores(
    closes: Mapping[str, pd.Series], day: dt.date, *, lookback: int, skip: int
) -> dict[str, float]:
    """Risk-adjusted 12-1 momentum on closes ≤ *day* (the S&P Momentum index rule the
    live SPMO tier tracks): ``(P[t-skip] / P[t-lookback] - 1) / σ(daily returns)``.
    A name without ``lookback + 1`` closes has no score."""
    out: dict[str, float] = {}
    for t, s in closes.items():
        p = s[pd.Index(s.index) <= day].dropna()
        if len(p) < lookback + 1:
            continue
        w = p.iloc[-(lookback + 1) :].astype(float)
        if (w <= 0).any():
            continue
        ret = float(w.iloc[-1 - skip] / w.iloc[0] - 1.0)
        sd = float(np.log(w / w.shift(1)).dropna().std(ddof=1))
        if sd > 0 and math.isfinite(ret):
            out[t] = ret / sd
    return out


def momentum_members(
    scores: Mapping[str, float], *, size: int, etfs: frozenset[str], aliases: Mapping[str, str]
) -> list[str]:
    """Top *size* by score through the live :func:`select_members` (share classes, funds)."""
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [
        HoldingRow(rank=i + 1, symbol=t, name=t, weight=round(v, 6))
        for i, (t, v) in enumerate(ordered)
    ]
    picks, _ = select_members(rows, size=size, aliases=aliases, etfs=etfs)
    return [p.symbol for p in picks]


def active_list(
    day: dt.date,
    *,
    core: Sequence[str],
    momentum: Sequence[str],
    rest: Sequence[tuple[str, float]],
    sim: SimSettings,
) -> list[str]:
    """The active-list proxy through the live resolver (``rest`` ranked by liquidity)."""

    def members(names: Sequence[str], tier: Tier) -> list[TierMember]:
        return [
            TierMember(ticker=t, tier=tier, rank=i + 1, source="backtest", as_of=day)
            for i, t in enumerate(names)
        ]

    ranked_rest = [t for t, _ in sorted(rest, key=lambda tl: (-tl[1], tl[0]))]
    au = resolve_active(
        core=members(sorted(core), Tier.CORE),
        momentum=members(momentum, Tier.MOMENTUM),
        discoveries=members(ranked_rest, Tier.DISCOVERY),
        active_max=sim.active_cap,
        tier_sizes={Tier.MOMENTUM: sim.momentum_size},
        as_of=day,
        fill=sim.active_fill,
    )
    return au.tickers


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


class SimRun(BaseModel):
    """One ranker × profile walk."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ranker: str
    profile: str
    trades: pd.DataFrame
    equity: pd.Series
    decisions: pd.DataFrame
    skips: pd.DataFrame
    skip_counts: dict[str, int]


class _Pos(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying: str
    exit_day: dt.date
    contracts: int
    max_loss: Decimal
    cash_used: float
    outcome: Outcome
    entry_dte: int | None = None


class _PitCorr:
    """D131 (E28.5): point-in-time pair correlations for the walk's cluster cap.

    The value :func:`arc.backtest.correlations.pit_matrix` would hold for the pair
    (closes strictly before the session, the live ``betas`` job's formula, rounded as
    stored), computed lazily per pair the rule asks about rather than for every pair
    of the universe each session. Each name's split-adjusted (else raw) closes.
    Unknown (< 40 returns) is left out, which the rule treats as not correlated. Per-run
    cache, no global state.
    """

    def __init__(self, tickers: Mapping[str, SimTicker]) -> None:
        self._series = {
            t.upper(): r.adj_closes if r.adj_closes is not None else r.closes
            for t, r in tickers.items()
        }
        self._closes: dict[tuple[dt.date, str], dict[dt.date, float]] = {}
        self._pairs: dict[tuple[dt.date, str], float | None] = {}

    def _before(self, day: dt.date, t: str) -> dict[dt.date, float]:
        if (day, t) not in self._closes:
            s = self._series.get(t)
            self._closes[(day, t)] = {} if s is None else pit_closes({t: s}, day)[t]
        return self._closes[(day, t)]

    def correlations(self, day: dt.date, ticker: str, others: Sequence[str]) -> dict[str, float]:
        """``MarketSnapshot.correlations`` for an open on *ticker* (known pairs only)."""
        me = ticker.upper()
        out: dict[str, float] = {}
        for o in sorted({x.upper() for x in others} - {me}):
            k = corr_key(me, o)
            if (day, k) not in self._pairs:
                res = return_corr(self._before(day, me), self._before(day, o), WINDOW, MIN_OBS)
                self._pairs[(day, k)] = None if res.corr is None else round(res.corr, 6)
            c = self._pairs[(day, k)]
            if c is not None:
                out[k] = c
        return out


def _derived(c: Candidate, contracts: int, max_loss_unit: float, fill: float) -> Derived:
    """The gate's view of the open: the order's limit is the modelled entry fill."""
    legs = _legs(c.picks, c.underlying)
    structure = analyze(legs, as_of=c.day)
    assert structure.kind is not None
    return Derived(
        underlying=c.underlying,
        expiration=c.picks[0].expiration,
        kind=structure.kind,
        defined_risk=True,
        max_loss_total=Decimal(str(round(max_loss_unit, 2))) * contracts,
        net_price=structure.net_debit_credit,
        limit_price=Decimal(str(round(fill, 2))),
        price_ceiling=price_ceiling(legs),
    )


def _blackout(c: Candidate, d: Derived, info: DayInfo, settings: ArcSettings) -> bool:
    market = MarketSnapshot(
        next_earnings={c.underlying: info.next_earnings} if info.earnings_known else {}
    )
    proposal = Proposal.model_construct(earnings_play=False, risk_concurs=False)
    now = dt.datetime.combine(c.day, dt.time(16, 0), tzinfo=ET)
    return bool(check_earnings_blackout(proposal, d, market, settings, now))


def market_trends(
    closes: pd.Series, ma: int, sessions: Sequence[dt.date], *, ticker: str = "SPY"
) -> dict[dt.date, MarketTrend]:
    """D131 (E28.2): the market reference's close vs ``SMA(ma)`` per session, point in time.

    Each session's SMA is the mean of the *ma* closes on or before it (no look-ahead);
    a session without a close of its own or fewer than *ma* closes gets no entry, so
    the rule fails closed that day for the blocked kinds, like the live gate.
    """
    s = closes.dropna().sort_index()
    sma = s.rolling(ma, min_periods=ma).mean()
    out: dict[dt.date, MarketTrend] = {}
    for d in sessions:
        if d not in s.index or pd.isna(sma.get(d)):
            continue
        out[d] = MarketTrend(
            ticker=ticker,
            close=Decimal(str(round(float(s[d]), 4))),
            ma=ma,
            sma=Decimal(str(round(float(sma[d]), 4))),
            as_of=d,
        )
    return out


def _regime_blocked(
    c: Candidate, d: Derived, trend: MarketTrend | None, settings: ArcSettings
) -> bool:
    """The live ``short_premium_regime`` rule on one sim open (decided at the close)."""
    legs = _legs(c.picks, c.underlying)
    proposal = Proposal.model_construct(structure=Structure.model_construct(legs=legs))
    now = dt.datetime.combine(c.day, dt.time(16, 0), tzinfo=ET)
    market = MarketSnapshot(market_trend=trend)
    return bool(check_short_premium_regime(proposal, d, market, settings, now))


def _trade_row(
    *,
    ranker: str,
    profile: str,
    sc: SimCandidate,
    n: int,
    ml_unit: float,
    trend: Mapping[str, pd.Series],
    vol: Mapping[str, pd.Series],
    clipped: bool,
    liquidity: float,
    tier: str = "rest",
    etf: bool = False,
) -> dict[str, object]:
    best = sc.candidate
    assert sc.result is not None
    out = sc.result.outcome
    tr = out.trade
    t, day = best.underlying, best.day
    return {
        "ranker": ranker,
        "profile": profile,
        "underlying": t,
        "kind": tr.kind,
        "spec": best.spec,
        "entry_date": day,
        "exit_date": out.exit_day,
        "expiration": tr.expiration,
        "contracts": n,
        "capacity_clipped": clipped,
        "pnl": tr.pnl * n,
        "pnl_mid": tr.pnl_mid * n,
        "pnl_unit": tr.pnl,
        "max_loss_unit": ml_unit,
        "entry_net": tr.entry_net,
        "entry_net_mid": tr.entry_net_mid,
        "fees": tr.fees * n,
        "exit_reason": tr.exit_reason,
        "exit_delayed": sc.result.exit_delays,
        "days_held": (out.exit_day - day).days,
        "hold_to_expiry_pnl": out.hold_to_expiry_pnl * n,
        "managed_net_ev": (best.inputs.managed_net_ev or 0.0) * n,
        "managed_net_ev_unit": best.inputs.managed_net_ev,
        "managed_pop": best.inputs.managed_pop,
        "static_pop": best.static_pop,
        "static_net_ev_unit": best.static_net_ev,
        "expected_days_held": best.expected_days_held,
        "ev_proxy_unit": best.inputs.ev_proxy,
        "rorc_day": best.inputs.rorc_day,
        "vrp": best.inputs.vrp,
        "atm_iv": best.atm_iv,
        "rv_forecast": best.rv_forecast,
        "credit_width": best.inputs.credit_width,
        "debit_width": best.inputs.debit_width,
        "liquidity": liquidity,
        "tier": tier,
        "etf": etf,
        "spot_entry": tr.spot_entry,
        "spot_exit": tr.spot_exit,
        "trend": str(trend[t].get(day, "unknown")) if t in trend else "unknown",
        "vol": str(vol[t].get(day, "unknown")) if t in vol else "unknown",
        "legs": " ".join(f"{'+' if lg.side > 0 else '-'}{lg.symbol}" for lg in tr.legs),
        "sleeve": sc.sleeve,
        "dte_entry": tr.dte,
    }


def _monthly_momentum(
    tickers: Mapping[str, SimTicker],
    sessions: Sequence[dt.date],
    sim: SimSettings,
    aliases: Mapping[str, str],
) -> dict[dt.date, list[str]]:
    """Momentum members per session: recomputed on the first session of each month
    (live: monthly refresh), from names eligible that day, carried in between."""
    closes = {
        t: (r.adj_closes if r.adj_closes is not None else r.closes) for t, r in tickers.items()
    }
    etfs = frozenset(t for t, r in tickers.items() if any(i.etf for i in r.days.values()))
    out: dict[dt.date, list[str]] = {}
    cur: list[str] = []
    month: tuple[int, int] | None = None
    for d in sessions:
        if (d.year, d.month) != month:
            month = (d.year, d.month)
            elig = {t: s for t, s in closes.items() if (i := tickers[t].days.get(d)) and i.eligible}
            scores = momentum_scores(
                elig, d, lookback=sim.momentum_lookback, skip=sim.momentum_skip
            )
            cur = momentum_members(scores, size=sim.momentum_size, etfs=etfs, aliases=aliases)
        out[d] = cur
    return out


def run_sim(
    tickers: Mapping[str, SimTicker],
    *,
    ranker: Ranker,
    profile: str,
    settings: ArcSettings,
    sim: SimSettings,
    filters: RankFilters,
    vrp_threshold: float,
    sessions: Sequence[dt.date],
    end: dt.date,
    starting_equity: float | None = None,
    aliases: Mapping[str, str] | None = None,
    momentum: Mapping[dt.date, list[str]] | None = None,
    trends: Mapping[dt.date, MarketTrend] | None = None,
    stance_of: Mapping[str, pd.Series] | None = None,
    fill_x: float | None = None,
) -> SimRun:
    """Walk *sessions*; open on decision sessions ≤ *end* (see the module docstring).

    E7.20 (D125): *stance_of* (ticker → session → stance, :mod:`arc.backtest.stance`)
    and *fill_x* (a slippage x the build carries in :attr:`SimCandidate.alt`) select one
    robustness-grid cell of a grid build; both None = the control cell.

    *tickers* order never matters: names are always handled in sorted order and the
    cross-name pool is sorted by (ranker key, liquidity desc, symbol, key).

    *trends* (D131, E28.2): :func:`market_trends` for the ``short_premium_regime``
    rule (a session missing from it fails closed for the blocked kinds). ``None`` =
    the walk was built without the market reference: the rule is not applied.

    D131 (E28.5): with ``cluster_cap_enabled`` the ``cluster_cap`` rule runs on the
    held book with point-in-time correlations from the walk's own closes (:class:`_PitCorr`).
    """
    from arc.account_profiles import BuyingPower

    names = sorted(tickers)
    start_eq = sim.equity if starting_equity is None else starting_equity
    legacy = sim.selection == "per_ticker"
    cash_settled = settings.profile.buying_power is BuyingPower.CASH_SETTLED
    margin_bp = (
        not legacy
        and sim.margin_buying_power
        and settings.profile.buying_power is BuyingPower.MARGIN
    )
    keyfn = sort_key(ranker, vrp_threshold)
    trend = {t: tickers[t].trend for t in names}
    vol = {t: tickers[t].vol for t in names}
    pit_corr = _PitCorr(tickers) if settings.cluster_cap_enabled and not legacy else None
    if momentum is None and not legacy:
        momentum = _monthly_momentum(tickers, [d for d in sessions if d <= end], sim, aliases or {})
    realised = 0.0
    open_: list[_Pos] = []
    rows: list[dict[str, object]] = []
    skips: list[dict[str, object]] = []
    funnel: list[dict[str, object]] = []
    eq: dict[dt.date, float] = {}
    counts = dict.fromkeys(SKIP_REASONS, 0)
    prev_eq = start_eq

    def skip(day: dt.date, t: str, reason: str, key: str = "", detail: str = "") -> None:
        counts[reason] += 1
        skips.append({"day": day, "ticker": t, "reason": reason, "key": key, "detail": detail})

    def unrealised(day: dt.date) -> float:
        u = 0.0
        for p in open_:
            past = [v for d, v in p.outcome.marks.items() if d <= day]
            if past:
                u += past[-1] * p.contracts
        return u

    for day in sessions:
        still: list[_Pos] = []
        for p in open_:
            if p.exit_day <= day:
                realised += p.outcome.trade.pnl * p.contracts
            else:
                still.append(p)
        open_ = still
        marked = start_eq + realised + unrealised(day)
        equity = start_eq + realised if legacy else marked
        f: dict[str, object] = {"day": day, "decision": day <= end, "eligible": 0}
        f |= {"screened": 0, "active": 0}
        f |= {"candidates": 0, "opened": 0, "open_positions": len(open_), "equity": marked}
        if day <= end:
            opened = _decide(
                day,
                tickers=tickers,
                names=names,
                legacy=legacy,
                sim=sim,
                profile=profile,
                ranker=ranker,
                keyfn=keyfn,
                filters=filters,
                vrp_threshold=vrp_threshold,
                settings=settings,
                cash_settled=cash_settled,
                margin_bp=margin_bp,
                equity=equity,
                prev_equity=prev_eq,
                open_=open_,
                momentum=(momentum or {}).get(day, []),
                skip=skip,
                funnel=f,
                rows=rows,
                trend=trend,
                vol=vol,
                regime=None if trends is None else (trends.get(day),),
                pit_corr=pit_corr,
                stance_of=stance_of,
                fill_x=fill_x,
            )
            open_ = open_ + opened
        eq[day] = start_eq + realised + unrealised(day)
        f["open_positions"] = len(open_)
        prev_eq = eq[day]
        funnel.append(f)
    for p in open_:
        realised += p.outcome.trade.pnl * p.contracts
    trades = pd.DataFrame(rows)
    equity_s = pd.Series(eq, name="equity", dtype=float).sort_index()
    if open_ and len(equity_s):
        equity_s.iloc[-1] = start_eq + realised
    for t in trades.itertuples() if len(trades) else []:
        if int(t.exit_delayed) > 0:
            counts["exit_delayed"] += 1
    log.info("backtest.sim_run", ranker=ranker.value, profile=profile, trades=len(trades), **counts)
    return SimRun(
        ranker=ranker.value,
        profile=profile,
        trades=trades,
        equity=equity_s,
        decisions=pd.DataFrame(funnel),
        skips=pd.DataFrame(skips, columns=["day", "ticker", "reason", "key", "detail"]),
        skip_counts=counts,
    )


def _decide(
    day: dt.date,
    *,
    tickers: Mapping[str, SimTicker],
    names: Sequence[str],
    legacy: bool,
    sim: SimSettings,
    profile: str,
    ranker: Ranker,
    keyfn: Any,
    filters: RankFilters,
    vrp_threshold: float,
    settings: ArcSettings,
    cash_settled: bool,
    margin_bp: bool,
    equity: float,
    prev_equity: float,
    open_: list[_Pos],
    momentum: Sequence[str],
    skip: Any,
    funnel: dict[str, object],
    rows: list[dict[str, object]],
    trend: Mapping[str, pd.Series],
    vol: Mapping[str, pd.Series],
    regime: tuple[MarketTrend | None] | None = None,
    pit_corr: _PitCorr | None = None,
    stance_of: Mapping[str, pd.Series] | None = None,
    fill_x: float | None = None,
) -> list[_Pos]:
    """One decision session: funnel → pool → open. Returns the new positions.

    *regime*: ``(today's market trend or None,)`` applies ``short_premium_regime``;
    ``None`` skips the rule (no market reference loaded).

    E7.20: on a stance-tagged build a candidate competes only when the session's stance
    (*stance_of*, a proxy; default the ticker's own *trend* label) is one it was tagged
    for; *fill_x* takes each candidate's outcome at that slippage x (:attr:`SimCandidate.alt`).
    """
    infos = {t: i for t in names if (i := tickers[t].days.get(day)) is not None}
    eligible = [t for t in names if t in infos and infos[t].eligible]
    funnel["eligible"] = len(eligible)
    tier_of: dict[str, str] = {}  # E7.12: the study report's by-tier breakdown
    if legacy:
        active = [t for t in names if tickers[t].menus.get(profile, {}).get(day) is not None]
        funnel["screened"] = funnel["active"] = len(active)
        tier_of = {t: "core" for t in active if t in infos and infos[t].seed}
    else:
        screened: list[str] = []
        for t in eligible:
            if sim.apply_screen and not infos[t].seed and not infos[t].screen_pass:
                skip(day, t, "screen")
                continue
            screened.append(t)
        funnel["screened"] = len(screened)
        core = [t for t in screened if infos[t].seed]
        sset = set(screened)
        mom = [t for t in momentum if t in sset and t not in set(core)]
        rest = [(t, infos[t].liquidity) for t in screened if t not in set(core) | set(mom)]
        active = active_list(day, core=core, momentum=mom, rest=rest, sim=sim)
        funnel["active"] = len(active)
        tier_of = dict.fromkeys(core, "core") | dict.fromkeys(mom, "momentum")
    new: list[_Pos] = []
    account_eq = Decimal(str(round(equity, 2)))
    account = AccountSnapshot(
        equity=account_eq,
        last_equity=Decimal(str(round(prev_equity, 2))),
        as_of=dt.datetime.combine(day, dt.time(16, 0), tzinfo=ET),
    )
    if not legacy and sim.daily_loss_rule and check_daily_loss(account, settings):
        for t in active:
            skip(day, t, "gate_daily_loss")
        return new
    pool: list[tuple[tuple[object, ...], SimCandidate, float]] = []
    for t in active:
        menu = tickers[t].menus.get(profile, {}).get(day) or []
        if any(sc.stances is not None for sc in menu):  # E7.20: a stance-tagged build
            lab = str((stance_of or trend)[t].get(day, "unknown"))
            menu = [sc for sc in menu if sc.stances is None or lab in sc.stances]
        if legacy:
            ranked = rank(
                [c.candidate.inputs for c in menu],
                ranker,
                filters=filters,
                vrp_threshold=vrp_threshold,
            )
            if not ranked:
                if menu or day in tickers[t].menus.get(profile, {}):
                    skip(day, t, "no_candidate")
                continue
            best = next(c for c in menu if c.candidate.inputs.key == ranked[0].key)
            pool.append(((), best, 0.0))
            continue
        liq = infos[t].liquidity if t in infos else 0.0
        keyed = []
        for sc in menu:
            if sc.candidate.kind in sim.exclude_kinds:
                continue
            if not passes_filters(sc.candidate.inputs, filters):
                continue
            k = keyfn(sc.candidate.inputs)
            if k is not None:
                keyed.append((k, sc))
        keyed.sort(key=lambda ks: ks[0])
        if not keyed:
            skip(day, t, "no_candidate")
            continue
        per_sleeve: dict[str, int] = {}  # E7.19: the top per_name_depth of each sleeve
        for k, sc in keyed:
            if per_sleeve.get(sc.sleeve, 0) >= sim.per_name_depth:
                continue
            per_sleeve[sc.sleeve] = per_sleeve.get(sc.sleeve, 0) + 1
            pool.append(((*k[:3], -liq, t, sc.candidate.inputs.key), sc, liq))
    if not legacy:
        pool.sort(key=lambda e: e[0])  # type: ignore[arg-type, return-value]
    funnel["candidates"] = len(pool)
    sleeved = not legacy and settings.profile.has_sleeves
    for i, (_, sc0, liq) in enumerate(pool):
        sc = sc0.at_x(fill_x) if fill_x is not None and fill_x in sc0.alt else sc0
        c = sc.candidate
        t = c.underlying
        if sc.result is None:
            skip(day, t, "unsettled", c.inputs.key)
            continue
        out = sc.result.outcome
        held = open_ + new
        portfolio = Portfolio(
            positions=[
                Position(underlying=p.underlying, max_loss=p.max_loss, entry_dte=p.entry_dte)
                for p in held
            ]
        )
        if check_max_open_positions(portfolio, settings):
            if legacy:
                skip(day, t, "gate_max_open_positions", c.inputs.key)
                continue
            for _, rest_sc, _ in pool[i:]:
                skip(
                    day,
                    rest_sc.candidate.underlying,
                    "gate_max_open_positions",
                    rest_sc.candidate.inputs.key,
                )
            break
        ml_unit = out.trade.max_loss
        if sleeved:  # E7.19: the sleeve rules through the pure gate functions
            d1 = _derived(c, 1, ml_unit, out.entry_debit)
            now = dt.datetime.combine(c.day, dt.time(16, 0), tzinfo=ET)
            if check_dte_window(d1, settings, now):
                skip(day, t, "gate_dte_window", c.inputs.key)
                continue
            if check_sleeve_positions(portfolio, d1, settings, now):
                skip(day, t, "sleeve_full", c.inputs.key, sc.sleeve)
                continue
        mlu = Decimal(str(round(ml_unit, 2)))
        existing = sum((p.max_loss for p in held if p.underlying == t), Decimal(0))
        deployed = deployed_max_loss(portfolio)
        # D131 (E28.3): sizing fits the open-risk headroom, as the live quant.open does
        risk = open_risk(deployed, account_eq, settings.max_open_risk_pct)
        # D129: margin options buying power = equity - the held book's max loss
        bp_left = account_eq - deployed if margin_bp else None
        bp_per = mlu + Decimal(str(round(out.open_fees, 2))) if margin_bp else None
        size = size_contracts(
            suggestion=10**6,
            max_loss_per_contract=mlu,
            equity=account_eq,
            # D116: the tiered cap from the held book, as check_per_underlying computes it
            cap_pct=alloc_pct_for(deployed, account_eq, settings),
            existing_max_loss=Decimal(0) if legacy else existing,
            open_risk_headroom=None if legacy or not risk.shown else risk.headroom,
            cash_available=None if bp_left is None else max(bp_left, Decimal(0)),
            cash_per_contract=bp_per,
        )
        if not size.trade:
            code = str(size.code)
            reason = "buying_power" if margin_bp and "cash" in code else "sizing"
            skip(day, t, reason, c.inputs.key, code)
            continue
        n = size.contracts
        if not legacy and sim.size_scale < 1.0:
            n = math.floor(n * sim.size_scale + 1e-9)
            if n < 1:
                skip(day, t, "sizing", c.inputs.key, f"size_scale {sim.size_scale}")
                continue
        clipped = False
        if not legacy:
            if not sc.result.entry_ok:
                skip(day, t, "no_fill", c.inputs.key, "entry spread > live rule")
                continue
            if sim.capacity != "off":
                cap = capacity_cap(sc.result.leg_oi, sc.result.leg_volume, sim)
                if cap is not None and cap < n:
                    if sim.capacity == "skip" or cap < 1:
                        skip(day, t, "capacity", c.inputs.key, f"{n} > capacity {cap}")
                        continue
                    skip(day, t, "capacity", c.inputs.key, f"clipped {n} -> {cap}")
                    n, clipped = cap, True
        d = _derived(c, n, ml_unit, out.entry_debit)
        if check_per_underlying(d, account, portfolio, settings):
            skip(day, t, "gate_per_underlying", c.inputs.key)
            continue
        if not legacy:
            if check_open_risk(d, account, portfolio, settings):
                skip(day, t, "gate_open_risk_cap", c.inputs.key)
                continue
            if check_max_gain(d):
                skip(day, t, "gate_max_gain", c.inputs.key)
                continue
            if sim.earnings_blackout and _blackout(c, d, tickers[t].days[day], settings):
                skip(day, t, "blackout", c.inputs.key)
                continue
            if regime is not None and _regime_blocked(c, d, regime[0], settings):
                skip(day, t, "gate_short_premium_regime", c.inputs.key)
                continue
            if pit_corr is not None:  # D131 (E28.5): the held names' cluster, as of `day`
                corrs = pit_corr.correlations(day, t, [p.underlying for p in held])
                snap = MarketSnapshot(correlations=corrs)
                if check_cluster_cap(d, portfolio, snap, settings):
                    skip(day, t, "gate_cluster_cap", c.inputs.key)
                    continue
        cash_used = max(out.entry_debit, 0.0) * MULT * n + out.open_fees * n
        if cash_settled and cash_used > equity - sum(p.cash_used for p in held):
            skip(day, t, "cash", c.inputs.key)
            continue
        if bp_left is not None and bp_per is not None and bp_per * n > bp_left:
            skip(day, t, "buying_power", c.inputs.key)  # the D129 gate sub-check
            continue
        if not legacy and sim.max_new_risk_pct_day is not None:
            day_risk = sum((p.max_loss for p in new), Decimal(0)) + mlu * n
            if float(day_risk) > sim.max_new_risk_pct_day * equity + 1e-9:
                skip(day, t, "new_risk_cap", c.inputs.key)
                continue
        new.append(
            _Pos(
                underlying=t,
                exit_day=out.exit_day,
                contracts=n,
                max_loss=mlu * n,
                cash_used=cash_used,
                outcome=out,
                entry_dte=out.trade.dte,
            )
        )
        rows.append(
            _trade_row(
                ranker=ranker.value,
                profile=profile,
                sc=sc,
                n=n,
                ml_unit=ml_unit,
                trend=trend,
                vol=vol,
                clipped=clipped,
                liquidity=liq,
                tier=tier_of.get(t, "rest"),
                etf=bool(infos[t].etf) if t in infos else False,
            )
        )
    funnel["opened"] = len(new)
    return new
