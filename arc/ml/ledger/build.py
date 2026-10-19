"""E7.21 (D125, D126): the candidate outcome ledger.

One row per menu candidate per session, ticker, account profile and entry-DTE sleeve:
the sim's own menus (:func:`arc.backtest.sim.sleeve_menus`, all structures of the
profile, every expiry of the sleeve window), the live-shaped day context of that menu
(stance, entry filters, top-k pool, rule verdicts, eligibility) and the outcome labels
under the live exits of the candidate's sleeve, at several fill slippages
(:mod:`arc.ml.ledger.outcomes`).

Layout (``data/ml/ledger/v<LEDGER_SCHEMA>/``)::

    profile=<p>/year=<y>/part-<TICKER>.parquet   candidate rows
    days/year=<y>/part-<TICKER>.parquet          one row per (session, ticker) day info
    active.parquet                               sim active-list proxy per session (finalize)
    _done/<y>/<TICKER>                           resume markers, one per (ticker, year)
    manifest.json

Columns: identity and entry facts at the session-d close; context (``ctx_*``); labels
read d+1… only and are named ``label_*``. Feature columns are not stored: readers join
the E27.1 store (:mod:`arc.ml.features`) on (date, ticker); the manifest records its
schema version. Holdout rows (D84, from 2024-01-01) carry ``split = holdout`` and are
refused by :func:`load_ledger` unless explicitly allowed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.entry_filter import candidate_be_atr, stretched_days
from arc.backtest.ranking import _legs, stance_kinds
from arc.backtest.sim import (
    _blackout,
    _day_info,
    _derived,
    _regime_blocked,
    build_view,
    funnel_active,
    market_trends,
    menu_cache_ctx,
    monthly_momentum,
    pool_menus,
    profile_sleeves,
    sleeve_menus,
    sleeve_name,
    split_adjusted,
    spread_ok,
    ticker_books,
    ticker_prep,
)
from arc.backtest.strategies import CREDIT_KINDS, StrategyKind
from arc.features.technicals import compute_technicals, is_mid_range
from arc.ml.ledger.outcomes import (
    LegBook,
    candidate_rules,
    labels_via_sim,
    price_labels,
    settle_spot,
    splits_inside,
    walk,
)
from arc.ml.m1.dataset import DEV_END, HOLDOUT_START
from arc.scanner.rank import passes_filters, rank
from arc.structures import analyze
from arc.structures.analytics import buying_power

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from arc.backtest.ranking import Candidate, RankingFile
    from arc.backtest.sim import ViewSpec
    from arc.config import ArcSettings
    from arc.exits.policy import ExitConfig
    from arc.gate.rules import Derived
    from arc.ml.ledger.outcomes import Labels, Walk
    from arc.scanner.rank import Ranker

log = structlog.get_logger(__name__)

#: Bumped on any change to the row schema or the label definitions.
LEDGER_SCHEMA = "1"
#: Fill slippage grid (x of mid ± x·spread) the labels are priced at.
DEFAULT_XS: tuple[float, ...] = (0.0, 0.25, 0.5)

_SPEC_DELTA = re.compile(r"_d(\d+)")
_SPEC_WIDTH = re.compile(r"_w([0-9.]+)")
_DIRECTION = {
    StrategyKind.LONG_CALL: "bullish",
    StrategyKind.BULL_CALL: "bullish",
    StrategyKind.BULL_PUT: "bullish",
    StrategyKind.LONG_PUT: "bearish",
    StrategyKind.BEAR_PUT: "bearish",
    StrategyKind.BEAR_CALL: "bearish",
    StrategyKind.IRON_CONDOR: "neutral",
}
_TREND_STANCE = {"bull": "bullish", "bear": "bearish"}
_STOP = frozenset({"stop", "stop_intraday_proxy"})
_LOCK = frozenset({"profit_lock", "lock_intraday_proxy"})
_TP = frozenset({"take_profit"})


def xtag(x: float) -> str:
    """Column suffix of slippage *x*: 0 → ``x000``, 0.25 → ``x025``, 0.5 → ``x050``."""
    return f"x{round(x * 100):03d}"


def split_of(day: dt.date) -> str:
    return "holdout" if day >= HOLDOUT_START else "dev"


# ---------------------------------------------------------------------------
# Sampling (deterministic, never random)
# ---------------------------------------------------------------------------


class Sampling(BaseModel):
    """Keep every in-band candidate; keep 1 in *n* of the rest by a stable hash.

    ``n = 1`` keeps every row. Bands are the live scanner's anchor |Δ| bands
    (credit: the short leg; debit and long premium: the long leg).
    """

    model_config = ConfigDict(frozen=True)

    n: int = Field(1, ge=1)
    credit_band: tuple[float, float]
    debit_band: tuple[float, float]

    def in_band(self, kind: str, anchor_delta: float) -> bool:
        lo, hi = self.credit_band if kind in CREDIT_KINDS else self.debit_band
        return lo - 1e-9 <= anchor_delta <= hi + 1e-9

    def weight(self, ticker: str, day: dt.date, key: str, kind: str, anchor: float) -> int:
        """Inverse-probability weight of a kept row; 0 = not sampled."""
        if self.n == 1 or self.in_band(kind, anchor):
            return 1
        h = hashlib.sha256(f"{ticker}|{day.isoformat()}|{key}".encode()).digest()
        return self.n if int.from_bytes(h[:8], "big") % self.n == 0 else 0


def sampling_for(settings: ArcSettings, sample_frac: float | None) -> Sampling:
    n = 1 if not sample_frac or sample_frac >= 1.0 else max(1, round(1.0 / sample_frac))
    return Sampling(
        n=n,
        credit_band=(settings.scanner_short_delta_min, settings.scanner_short_delta_max),
        debit_band=(settings.scanner_long_delta_min, settings.scanner_long_delta_max),
    )


# ---------------------------------------------------------------------------
# One (ticker, window) of rows
# ---------------------------------------------------------------------------


class LedgerInputs(BaseModel):
    """Run-level inputs every ticker job shares (picklable)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    cfg: Any
    exits: Any
    cost: Any
    settings: dict[str, Any] = Field(..., description="Profile → ArcSettings")
    windows: dict[str, tuple[int, int]]
    rankers: dict[str, list[Any]]
    stances: dict[str, dict[str, frozenset[str]]] | None
    xs: tuple[float, ...] = DEFAULT_XS
    sampling: Sampling
    range_rule: Any = Field(None, description="RangeEdgeRule (None = range edges off)")
    anti_chase: bool = True


class _Chain(BaseModel):
    """Per-leg OI / volume / sellable at the entry session."""

    model_config = ConfigDict(frozen=True)

    oi: dict[str, float]
    volume: dict[str, float]
    sellable: dict[str, bool]


def _chain_facts(chain: pd.DataFrame | None) -> _Chain:
    if chain is None or chain.empty:
        return _Chain(oi={}, volume={}, sellable={})
    syms = chain["symbol"].astype(str).tolist()

    def col(name: str) -> list[float]:
        if name not in chain:
            return [math.nan] * len(syms)
        return pd.to_numeric(chain[name], errors="coerce").astype(float).tolist()

    sell = chain["sellable"].astype(bool).tolist() if "sellable" in chain else [True] * len(syms)
    return _Chain(
        oi=dict(zip(syms, col("open_interest"), strict=True)),
        volume=dict(zip(syms, col("volume"), strict=True)),
        sellable=dict(zip(syms, sell, strict=True)),
    )


def _min(vals: Sequence[float]) -> float:
    ok = [v for v in vals if v is not None and math.isfinite(v)]
    return min(ok) if ok else math.nan


def entry_facts(c: Candidate, spot: float, atr: float | None, facts: _Chain) -> dict[str, object]:
    """The candidate's own entry facts at the session-d close (no label reads)."""
    legs = _legs(c.picks, c.underlying)
    st = analyze(legs, as_of=c.day)
    net_mid = float(st.net_debit_credit)
    dte = (c.picks[0].expiration - c.day).days
    bes = [float(b) for b in st.breakevens]
    be_sigma = math.nan
    if bes and c.atm_iv and c.atm_iv > 0 and dte > 0 and spot > 0:
        move = min(abs(b - spot) for b in bes)
        be_sigma = move / (spot * c.atm_iv * math.sqrt(dte / 365.0))
    be_atr = candidate_be_atr(c, spot, atr) if atr is not None else None
    bp = buying_power(legs)
    spread_sum = sum(p.spread for p in c.picks)
    shorts = [p.symbol for p in c.picks if p.side < 0]
    return {
        "net_mid": net_mid,
        "max_loss_mid": c.max_loss_mid,
        "max_gain_mid": None if st.max_gain is None else float(st.max_gain),
        "be_atr": be_atr,
        "be_sigma": be_sigma,
        "spread_to_premium": spread_sum / abs(net_mid) if net_mid else math.nan,
        "leg_oi_min": _min([facts.oi.get(p.symbol, math.nan) for p in c.picks]),
        "leg_volume_min": _min([facts.volume.get(p.symbol, math.nan) for p in c.picks]),
        "shorts_sellable": all(facts.sellable.get(s, True) for s in shorts),
        "bp_unit": None if bp is None else float(bp),
        "derived": _derived(c, 1, c.max_loss_mid, abs(net_mid)),
    }


def _label_cols(
    xs: Sequence[float],
    labs: Mapping[float, Labels | None],
    w: Walk | None,
    max_loss_mid: float,
) -> dict[str, object]:
    out: dict[str, object] = {}
    any_lab = next((lab for lab in labs.values() if lab is not None), None)
    reason = None if any_lab is None else any_lab.exit_reason
    out["label_exit_reason"] = reason
    out["label_exit_date"] = None if any_lab is None else any_lab.exit_day
    out["label_days_held"] = None
    out["label_exit_delays"] = None if w is None else w.delays
    out["label_hit_stop"] = reason in _STOP if reason else None
    out["label_hit_tp"] = reason in _TP if reason else None
    out["label_hit_lock"] = reason in _LOCK if reason else None
    out["label_r_at_mid"] = (
        any_lab.pnl_mid / max_loss_mid if any_lab is not None and max_loss_mid > 0 else None
    )
    for x in xs:
        tg = xtag(x)
        lab = labs.get(x)
        ok = lab is not None and lab.max_loss > 0
        out[f"label_pnl_{tg}"] = lab.pnl if lab is not None else None
        out[f"label_max_loss_{tg}"] = lab.max_loss if lab is not None else None
        out[f"label_r_net_{tg}"] = lab.pnl / lab.max_loss if ok and lab else None
        out[f"label_win_{tg}"] = (lab.pnl > 0) if lab is not None else None
        out[f"label_mae_r_{tg}"] = lab.mae / lab.max_loss if ok and lab else None
        out[f"label_mfe_r_{tg}"] = lab.mfe / lab.max_loss if ok and lab else None
        out[f"label_r_hold_{tg}"] = (
            lab.hold_pnl / lab.hold_max_loss if lab is not None and lab.hold_max_loss > 0 else None
        )
    return out


def _filter_reason(
    stance: str | None,
    stretched: frozenset[str],
    mid_range: bool,
    credit_for_stance: bool,
) -> str | None:
    """D78/D98/D128 reason code that hit the (ticker, day) idea, else ``None``."""
    if stance is None:
        return None
    if stance in stretched:
        return "stretched_debit_only" if credit_for_stance else "stretched_entry"
    if mid_range:
        return "mid_range_debit_only" if credit_for_stance else "mid_range_entry"
    return None


def ticker_rows(
    ticker: str,
    *,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    ohlc: pd.DataFrame | None,
    days: Sequence[dt.date],
    info: Mapping[dt.date, Any],
    inp: LedgerInputs,
    rate: Callable[[dt.date], float] | None,
    quotes: Mapping[dt.date, pd.DataFrame] | None,
    splits: Mapping[dt.date, float] | None,
    trends: Mapping[dt.date, Any] | None,
    cache: Any = None,
    cache_ctx: Any = None,
) -> dict[str, list[dict[str, object]]]:
    """Profile → candidate rows of *ticker* for decision *days* (pure on its inputs)."""
    cfg: RankingFile = inp.cfg
    exits: ExitConfig = inp.exits
    sim = cfg.backtest.sim
    if sim.fill_spread != "quoted":
        msg = "the ledger prices quoted fills only (backtest.sim.fill_spread: quoted)"
        raise ValueError(msg)
    closes = closes.sort_index()
    o = ohlc if ohlc is not None else pd.DataFrame()
    techs = {d: compute_technicals(o, d) for d in days} if not o.empty else {}
    prep = ticker_prep(
        chains,
        closes,
        decision_days=days,
        cfg=cfg,
        exits=exits,
        cost=inp.cost,
        sim=sim,
        ohlc=ohlc,
        splits=splits,
        techs=techs,
    )
    stretched = prep.stretched
    if stretched is None and inp.anti_chase:
        stretched = stretched_days(o, prep.dd, cfg.backtest.anti_chase, techs=techs)
    atr = prep.atr
    if atr is None:
        from arc.backtest.entry_filter import atr14_days

        atr = atr14_days(o, closes, prep.dd, techs=techs)
    bt = cfg.backtest

    def rate_of(d: dt.date) -> float:
        return bt.risk_free_rate if rate is None else float(rate(d))

    book = chains if quotes is None else quotes
    facts = {d: _chain_facts(chains.get(d)) for d in prep.dd}
    walks: dict[tuple[dt.date, str, str | None], Walk | None] = {}
    built: list[tuple[str, Any, dict[dt.date, list[Candidate]], dict[dt.date, Any]]] = []
    for profile, window in sorted(inp.windows.items()):
        for sw in profile_sleeves(inp.settings[profile], window, sim):
            menus, _ = sleeve_menus(
                ticker,
                chains,
                closes,
                sw=sw,
                profile=profile,
                dd=prep.dd,
                cfg=cfg,
                exits=exits,
                mc=prep.mc,
                base=prep.base,
                rate=rate,
                day_stance=prep.day_stance,
                cache=cache,
                cache_ctx=cache_ctx,
            )
            pools = pool_menus(
                menus,
                closes,
                profile=profile,
                cfg=cfg,
                rankers=inp.rankers[profile],
                stances=inp.stances,
                trend=prep.trend,
                vol=prep.vol,
                stretched=prep.stretched,
                atr=prep.atr,
                sim=sim,
            )
            built.append((profile, sw, menus, pools))
    legs = {
        p.symbol: (p.right, p.strike, p.expiration)
        for _, _, menus, _ in built
        for m in menus.values()
        for c in m
        for p in c.picks
    }
    lbook = LegBook(book, legs, closes, ranges=prep.ranges, rate=rate_of)
    out: dict[str, list[dict[str, object]]] = {p: [] for p in sorted(inp.windows)}
    for profile, _sw, menus, pools in built:
        settings: ArcSettings = inp.settings[profile]
        kinds_for = stance_kinds(profile, settings.account_profiles_file)
        rows = out[profile]
        incumbent: Ranker = inp.rankers[profile][0]
        for d, menu in sorted(menus.items()):
            if not menu:
                continue
            kept_menu, topk = pools.get(d, ([], set()))
            kept_keys = {c.inputs.key for c in kept_menu}
            order = rank(
                [c.inputs for c in menu],
                incumbent,
                filters=cfg.ranking.filters,
                vrp_threshold=cfg.ranking.vrp_threshold,
            )
            pos = {r.key: i + 1 for i, r in enumerate(order)}
            spot = float(closes[d])
            trend = str(prep.trend.get(d, "unknown"))
            st_hits = (stretched or {}).get(d, frozenset())
            reasons: dict[str, str | None] = {}
            for direction, lab in (("bullish", "bull"), ("bearish", "bear")):
                mid = False
                if inp.range_rule is not None:
                    v = is_mid_range(techs.get(d), trend, direction, inp.range_rule)
                    mid = v.status == "mid_range"
                credit_ok = any(k in CREDIT_KINDS for k in kinds_for.get(lab, frozenset()))
                reasons[direction] = _filter_reason(direction, st_hits, mid, credit_ok)
            di = info.get(d)
            mt = None if trends is None else trends.get(d)
            for c in menu:
                anchor = _anchor(c)
                wgt = inp.sampling.weight(ticker, d, c.inputs.key, c.kind, anchor)
                if wgt == 0:
                    continue
                reason = reasons.get(_DIRECTION[StrategyKind(c.kind)])
                ef = entry_facts(c, spot, atr.get(d), facts[d])
                der: Derived = ef.pop("derived")  # type: ignore[assignment]
                rules = candidate_rules(c, settings, exits)
                wkey = (d, c.inputs.key, settings.exit_block_for_dte(_dte(c)))
                labs, w = _labels(
                    c,
                    rules=rules,
                    lbook=lbook,
                    walks=walks,
                    wkey=wkey,
                    settings=settings,
                    spread_rule=prep.spread_rule,
                    inp=inp,
                    chains=chains,
                    closes=closes,
                    quotes=quotes,
                    splits=splits,
                    ranges=prep.ranges,
                    rate_of=rate_of,
                )
                row: dict[str, object] = {
                    "date": d,
                    "ticker": ticker,
                    "profile": profile,
                    "sleeve": sleeve_name(settings, _dte(c)),
                    "spec": c.spec,
                    "kind": c.kind,
                    "direction": _DIRECTION[StrategyKind(c.kind)],
                    "credit": c.kind in CREDIT_KINDS,
                    "expiration": c.picks[0].expiration,
                    "dte": _dte(c),
                    "anchor_delta": anchor,
                    "width_pct": _width(c.spec),
                    "key": c.inputs.key,
                    "legs": " ".join(f"{'+' if p.side > 0 else '-'}{p.symbol}" for p in c.picks),
                    "split": split_of(d),
                    "sample_weight": wgt,
                    "spot": spot,
                    "atm_iv": c.atm_iv,
                    "rv_forecast": c.rv_forecast,
                    "static_pop": c.static_pop,
                    "static_net_ev": c.static_net_ev,
                    "expected_days_held": c.expected_days_held,
                    **ef,
                    "credit_width": c.inputs.credit_width,
                    "debit_width": c.inputs.debit_width,
                    "ev_proxy": c.inputs.ev_proxy,
                    "managed_net_ev": c.inputs.managed_net_ev,
                    "managed_pop": c.inputs.managed_pop,
                    "rorc_day": c.inputs.rorc_day,
                    "vrp": c.inputs.vrp,
                    "est_cost": c.inputs.est_cost,
                    "rank_incumbent": pos.get(c.inputs.key),
                    "ctx_trend": trend,
                    "ctx_vol": str(prep.vol.get(d, "unknown")),
                    "ctx_passes_filters": passes_filters(c.inputs, cfg.ranking.filters),
                    "ctx_sim_menu": c.inputs.key in kept_keys,
                    "ctx_in_topk": c.inputs.key in topk,
                    "ctx_filter_reason": reason,
                    "ctx_credit_only": reason in ("stretched_debit_only", "mid_range_debit_only"),
                    "ctx_entry_spread_ok": all(
                        spread_ok(p.mid, p.spread, settings) for p in c.picks
                    ),
                    "ctx_eligible": None if di is None else di.eligible,
                    "ctx_screen_pass": None if di is None else di.screen_pass,
                    "ctx_seed": None if di is None else di.seed,
                    "ctx_etf": None if di is None else di.etf,
                    "ctx_liquidity": None if di is None else di.liquidity,
                    "ctx_earnings_blackout": None
                    if di is None
                    else _blackout(c, der, di, settings),
                    "ctx_regime_blocked": None
                    if trends is None
                    else _regime_blocked(c, der, mt, settings),
                    **_label_cols(inp.xs, labs, w, c.max_loss_mid),
                }
                ex = row["label_exit_date"]
                if isinstance(ex, dt.date):
                    row["label_days_held"] = (ex - d).days
                row["label_end_date"] = ex if ex is not None else c.picks[0].expiration
                row["label_crosses_holdout"] = d <= DEV_END < row["label_end_date"]  # type: ignore[operator]
                rows.append(row)
    return out


def _dte(c: Candidate) -> int:
    return (c.picks[0].expiration - c.day).days


def _anchor(c: Candidate) -> float:
    m = _SPEC_DELTA.search(c.spec.removeprefix(c.kind))
    return int(m.group(1)) / 100.0 if m else math.nan


def _width(spec: str) -> float | None:
    m = _SPEC_WIDTH.search(spec)
    return float(m.group(1).rstrip("max")) / 100.0 if m else None


def _labels(
    c: Candidate,
    *,
    rules: Any,
    lbook: LegBook,
    walks: dict[tuple[dt.date, str, str | None], Walk | None],
    wkey: tuple[dt.date, str, str | None],
    settings: ArcSettings,
    spread_rule: bool,
    inp: LedgerInputs,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    quotes: Mapping[dt.date, pd.DataFrame] | None,
    splits: Mapping[dt.date, float] | None,
    ranges: Mapping[dt.date, tuple[float, float]] | None,
    rate_of: Callable[[dt.date], float],
) -> tuple[dict[float, Labels | None], Walk | None]:
    costs = {x: inp.cost.model_copy(update={"slippage_frac": x}) for x in inp.xs}
    if splits_inside(c, splits):
        return {
            x: labels_via_sim(
                c,
                cost=cm,
                chains=chains,
                closes=closes,
                exits=inp.exits,
                settings=settings,
                spread_rule=spread_rule,
                marks=quotes,
                splits=splits,
                ranges=ranges,
                rate=rate_of,
            )
            for x, cm in costs.items()
        }, None
    ss = settle_spot(c, closes)
    if ss is None:
        return dict.fromkeys(inp.xs), None
    if wkey not in walks:
        walks[wkey] = walk(c, rules, lbook, settings=settings, spread_rule=spread_rule)
    w = walks[wkey]
    assert w is not None
    spot = float(closes[c.day])
    return {
        x: price_labels(c, w, spot_entry=spot, spot_settle=ss, cost=cm) for x, cm in costs.items()
    }, w


def n_concurrent(df: pd.DataFrame) -> pd.Series:
    """Same-ticker, same-profile candidates whose label windows overlap each row's.

    A window is ``(date, label_end_date]``. Counted inside *df* (one part); the row
    itself is excluded.
    """
    out = pd.Series(0, index=df.index, dtype="int64")
    if df.empty:
        return out
    for _, g in df.groupby(["ticker", "profile"], sort=False):
        s = pd.to_datetime(g["date"]).to_numpy("datetime64[D]")
        e = pd.to_datetime(g["label_end_date"]).to_numpy("datetime64[D]")
        ss, es = np.sort(s), np.sort(e)
        # others overlap unless they end on/before my start or start on/after my end
        ended = np.searchsorted(es, s, side="right")
        later = len(ss) - np.searchsorted(ss, e, side="left")
        out.loc[g.index] = len(g) - ended - later - 1
    return out.clip(lower=0)


# ---------------------------------------------------------------------------
# Orchestration: (ticker, year) jobs, resumable
# ---------------------------------------------------------------------------


def version_dir(out_root: Path) -> Path:
    return out_root / f"v{LEDGER_SCHEMA}"


def part_path(vdir: Path, profile: str, year: int, ticker: str) -> Path:
    return vdir / f"profile={profile}" / f"year={year}" / f"part-{ticker}.parquet"


def done_path(vdir: Path, year: int, ticker: str) -> Path:
    return vdir / "_done" / str(year) / ticker


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def ledger_job(
    ticker: str,
    years: Sequence[int],
    *,
    spec: ViewSpec,
    inp: LedgerInputs,
    out_root: str,
    cache_dir: str | None,
    spy_closes: pd.Series | None,
) -> dict[str, object]:
    """Every pending (ticker, year) of *ticker*: rows, day infos, done markers."""
    from arc.backtest.menucache import MenuCache

    t0 = time.monotonic()
    vdir = version_dir(Path(out_root))
    view = build_view(spec, inp.cfg, inp.cost)
    h = view.load(ticker)
    quotes, splits = ticker_books(h, inp.cfg, inp.cfg.backtest.sim)
    ctx = menu_cache_ctx(h, inp.cfg, inp.exits)
    cache = MenuCache(Path(cache_dir)) if cache_dir is not None else None
    first = next(iter(inp.settings.values()))
    rows_n = 0
    for y in years:
        days = [d for d in h.closes.index if d.year == y and spec.start <= d <= spec.end]
        info = {d: _day_info(view, ticker, d) for d in days}
        trends = None
        if spy_closes is not None and first.regime_gate_enabled:
            trends = market_trends(spy_closes, first.regime_gate_ma, days)
        per = ticker_rows(
            ticker,
            chains=h.chains,
            closes=h.closes,
            ohlc=h.ohlc,
            days=days,
            info=info,
            inp=inp,
            rate=view.rates.rate,
            quotes=quotes,
            splits=splits,
            trends=trends,
            cache=cache,
            cache_ctx=ctx,
        )
        for profile, rows in per.items():
            df = pd.DataFrame(rows)
            if not df.empty:
                df["n_concurrent"] = n_concurrent(df)
            _write_atomic(df, part_path(vdir, profile, y, ticker))
            rows_n += len(df)
        dinfo = pd.DataFrame(
            [
                {
                    "date": d,
                    "ticker": ticker,
                    "close": float(h.closes[d]),
                    **(info[d].model_dump()),
                }
                for d in days
            ]
        )
        _write_atomic(dinfo, vdir / "days" / f"year={y}" / f"part-{ticker}.parquet")
        dp = done_path(vdir, y, ticker)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(json.dumps({"rows": rows_n}) + "\n")
    view.evict(ticker)
    log.info("ml.ledger.ticker", ticker=ticker, years=list(years), rows=rows_n)
    return {
        "ticker": ticker,
        "rows": rows_n,
        "seconds": time.monotonic() - t0,
        "cache_hits": 0 if cache is None else cache.hits,
        "cache_misses": 0 if cache is None else cache.misses,
    }


def pending(vdir: Path, tickers: Sequence[str], years: Sequence[int]) -> dict[str, list[int]]:
    """Ticker → years without a done marker (resume)."""
    out: dict[str, list[int]] = {}
    for t in tickers:
        ys = [y for y in years if not done_path(vdir, y, t).exists()]
        if ys:
            out[t] = ys
    return out


def momentum_closes(
    data_dir: Path, tickers: Sequence[str], start: dt.date, end: dt.date
) -> dict[str, pd.Series]:
    """Split-adjusted closes per name from ``start - 400d`` (the view's warm-up), exactly
    the sim's ``SimTicker.adj_closes`` (raw closes with :func:`split_ratios` from the
    adjusted OHLC), so the 12-1 momentum score sees its full lookback."""
    from arc.backtest.entry_filter import OhlcStore
    from arc.backtest.sim import split_ratios
    from arc.backtest.underlying import UnderlyingStore

    lo = start - dt.timedelta(days=400)
    us, os_ = UnderlyingStore(data_dir), OhlcStore(data_dir)
    out: dict[str, pd.Series] = {}
    for t in tickers:
        bars = us.read_bars(t)
        idx = pd.Index(bars.index)
        raw = bars[(idx >= lo) & (idx <= end)]["close"].dropna()
        ohlc = os_.read(t)
        oidx = pd.Index(ohlc.index)
        ohlc = ohlc[(oidx >= lo) & (oidx <= end)]
        splits = split_ratios(raw, pd.Series(ohlc["close"])) if len(ohlc) else {}
        out[t] = split_adjusted(raw, splits) if splits else raw
    return out


def finalize_active(
    vdir: Path,
    *,
    sim: Any,
    aliases: Mapping[str, str],
    data_dir: Path | None = None,
) -> pd.DataFrame:
    """The sim's active-list proxy per session from every ticker's day infos.

    Writes ``active.parquet`` (date, ticker, ctx_screened, ctx_active, ctx_tier):
    the cross-name context a single (ticker, year) job can't compute.
    """
    from arc.backtest.sim import DayInfo

    files = sorted((vdir / "days").glob("year=*/part-*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    fields = set(DayInfo.model_fields)
    infos: dict[str, dict[dt.date, DayInfo]] = {}
    closes: dict[str, pd.Series] = {}
    for t, g in df.groupby("ticker"):
        g = g.sort_values("date")
        infos[str(t)] = {
            r["date"]: DayInfo(**{k: r[k] for k in fields if k in r}) for r in g.to_dict("records")
        }
        closes[str(t)] = pd.Series(g["close"].to_numpy(float), index=list(g["date"]))
    if data_dir is not None:  # the sim's adj_closes, with the 12-1 momentum warm-up
        closes = momentum_closes(Path(data_dir), sorted(infos), min(df["date"]), max(df["date"]))
    sessions = sorted(set(df["date"]))
    etfs = frozenset(t for t, i in infos.items() if any(v.etf for v in i.values()))
    mom = monthly_momentum(closes, infos, sessions, sim, aliases, etfs=etfs)
    rows = []
    for d in sessions:
        day_infos = {t: i[d] for t, i in infos.items() if d in i}
        eligible = [t for t in sorted(day_infos) if day_infos[t].eligible]
        screened, active, tier_of = funnel_active(d, day_infos, eligible, mom.get(d, []), sim)
        sset, aset = set(screened), set(active)
        rows.extend(
            {
                "date": d,
                "ticker": t,
                "ctx_screened": t in sset,
                "ctx_active": t in aset,
                "ctx_tier": tier_of.get(t),
            }
            for t in sorted(day_infos)
        )
    out = pd.DataFrame(rows)
    _write_atomic(out, vdir / "active.parquet")
    return out


class BuildResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    vdir: str
    jobs: int
    rows: int
    seconds: float
    manifest: dict[str, Any]


def build_ledger(
    *,
    data_dir: Path,
    out_root: Path,
    universe: str,
    start: dt.date,
    end: dt.date,
    data_end: dt.date,
    profiles: Sequence[str],
    cfg: RankingFile,
    cost: Any,
    exits: ExitConfig,
    settings: ArcSettings,
    tickers: Sequence[str] | None = None,
    workers: int = 1,
    sample_frac: float | None = None,
    xs: Sequence[float] = DEFAULT_XS,
    cache_dir: Path | None = None,
    range_rule: Any = None,
    progress: Callable[[str], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> BuildResult:
    """Every pending (ticker, year) through :func:`ledger_job`, then the active list
    and the manifest. Menus are built with ``backtest.stance: none`` (all structures,
    every direction) whatever the ranking file says; the trend-stance verdict is a
    context column (``ctx_sim_menu``)."""
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

    from arc.backtest.sim import ViewSpec
    from arc.backtest.sim_report import _git_sha, _sha, study_inputs
    from arc.ml.features.registry import schema_version
    from arc.universe.config import MomentumConfig

    t0 = time.monotonic()
    bt = cfg.backtest
    trend_stances = bt.stance == "trend"
    cfg_none = cfg.model_copy(update={"backtest": bt.model_copy(update={"stance": "none"})})
    # rankers: the live incumbent of each profile (managed_net_ev under D128) plus the
    # baseline keys whose top-k defines the sim's pool
    per_profile, windows, rk, stances = study_inputs(
        profiles, cfg.ranking.rankers, cfg, exits, settings
    )
    spec = ViewSpec(
        data_dir=str(data_dir),
        universe=universe,
        start=start,
        end=end,
        data_end=data_end,
        flat_rate=bt.risk_free_rate,
        min_iv_obs=settings.iv_min_obs_rank,
    )
    view = build_view(spec, cfg, cost)
    names = sorted({t.upper() for t in (tickers or view.symbols)})
    names = [t for t in names if view.provider_for(t) is not None]
    inp = LedgerInputs(
        cfg=cfg_none,
        exits=exits,
        cost=cost.model_copy(update={"slippage_frac": bt.sim.slippage_x}),
        settings=per_profile,
        windows=windows,
        rankers={p: list(v) for p, v in rk.items()},
        stances=stances if trend_stances else None,
        xs=tuple(xs),
        sampling=sampling_for(settings, sample_frac),
        range_rule=range_rule,
    )
    spy = None
    if settings.regime_gate_enabled:
        from arc.backtest.underlying import UnderlyingStore, load_closes

        spy = load_closes(
            UnderlyingStore(data_dir), "SPY", start - dt.timedelta(days=400), data_end
        )
        spy = None if spy.empty else spy
    vdir = version_dir(out_root)
    years = list(range(start.year, end.year + 1))
    todo = pending(vdir, names, years)
    kw: dict[str, Any] = {
        "spec": spec,
        "inp": inp,
        "out_root": str(out_root),
        "cache_dir": str(cache_dir) if cache_dir is not None else None,
        "spy_closes": spy,
    }
    say = progress or (lambda _s: None)
    say(f"ledger v{LEDGER_SCHEMA}: {len(todo)}/{len(names)} tickers pending, years {years}")
    stats: list[dict[str, object]] = []
    if workers > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            pending_jobs = list(todo.items())
            live: dict[Any, str] = {}
            while pending_jobs or live:
                while pending_jobs and len(live) < workers and not (stop and stop()):
                    t, ys = pending_jobs.pop(0)
                    live[ex.submit(ledger_job, t, ys, **kw)] = t
                if not live:
                    break
                done, _ = wait(live, return_when=FIRST_COMPLETED)
                for f in done:
                    t = live.pop(f)
                    stats.append(f.result())
                    say(f"  {t}: {stats[-1]['rows']} rows, {stats[-1]['seconds']:.0f}s")
    else:
        for t, ys in todo.items():
            if stop and stop():
                break
            stats.append(ledger_job(t, ys, **kw))
            say(f"  {t}: {stats[-1]['rows']} rows, {stats[-1]['seconds']:.0f}s")
    left = pending(vdir, names, years)
    if left:
        say(f"stopped with {len(left)} tickers pending (yield file present); rerun to resume")
    finalize_active(
        vdir, sim=bt.sim, aliases=MomentumConfig().share_class_aliases, data_dir=data_dir
    )
    meta = {
        "git_sha": _git_sha(),
        "config_hashes": {
            "ranking": _sha(cfg.model_dump(mode="json")),
            "exits": _sha(exits.model_dump(mode="json")),
            "costs": _sha(cost.model_dump(mode="json")),
            "account_profiles": _file_sha(settings.account_profiles_file),
        },
        "universe": universe,
        "universe_version": view.version,
        "feature_schema": schema_version(),
        "start": start,
        "end": end,
        "data_end": data_end,
        "holdout_start": HOLDOUT_START,
        "profiles": list(profiles),
        "rankers": {p: [str(r) for r in v] for p, v in rk.items()},
        "menu_stance": "none",
        "xs": list(xs),
        "sampling": inp.sampling.model_dump(mode="json"),
        "tickers": len(names),
        "workers": workers,
        "jobs_this_run": len(stats),
        "build_seconds_this_run": round(time.monotonic() - t0, 1),
        "ticker_seconds_sum": round(sum(float(s["seconds"]) for s in stats), 1),  # type: ignore[arg-type]
    }
    body = write_manifest(vdir, meta)
    return BuildResult(
        vdir=str(vdir),
        jobs=len(stats),
        rows=int(body["rows"]),  # type: ignore[arg-type]
        seconds=time.monotonic() - t0,
        manifest=dict(body),
    )


def _file_sha(path: Any) -> str | None:
    p = Path(path) if path is not None else None
    if p is None or not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Manifest + reader
# ---------------------------------------------------------------------------


def _null_rates(df: pd.DataFrame) -> dict[str, float]:
    cols = [c for c in df.columns if c.startswith("label_")]
    return {c: round(float(df[c].isna().mean()), 4) for c in cols} if len(df) else {}


def write_manifest(vdir: Path, meta: Mapping[str, object]) -> dict[str, object]:
    """Counts per year × sleeve × kind and label null rates, plus *meta*."""
    files = sorted(vdir.glob("profile=*/year=*/part-*.parquet"))
    counts: dict[str, int] = {}
    nulls: dict[str, float] = {}
    total = 0
    frames = []
    for f in files:
        df = pd.read_parquet(f, columns=None)
        if df.empty:
            continue
        frames.append(df[[c for c in df.columns if c.startswith("label_")]])
        y = f.parent.name.removeprefix("year=")
        p = f.parent.parent.name.removeprefix("profile=")
        for (s, k), n in df.groupby(["sleeve", "kind"]).size().items():
            key = f"{p}/{y}/{s}/{k}"
            counts[key] = counts.get(key, 0) + int(n)
        total += len(df)
    if frames:
        nulls = _null_rates(pd.concat(frames, ignore_index=True))
    body = {
        "schema": LEDGER_SCHEMA,
        **meta,
        "rows": total,
        "row_counts": dict(sorted(counts.items())),
        "label_null_rates": nulls,
        "bytes": sum(f.stat().st_size for f in files),
    }
    (vdir / "manifest.json").write_text(
        json.dumps(body, indent=2, sort_keys=True, default=str) + "\n"
    )
    return body


class HoldoutRefusedError(RuntimeError):
    """A ledger read asked for holdout rows without an explicit, logged reason."""


def load_ledger(
    root: Path,
    *,
    split: str = "dev",
    profiles: Sequence[str] | None = None,
    years: Sequence[int] | None = None,
    columns: Sequence[str] | None = None,
    allow_holdout: bool = False,
    reason: str | None = None,
    features_root: Path | None = None,
) -> pd.DataFrame:
    """Read ledger rows of *split* (``dev`` / ``holdout`` / ``all``).

    Holdout rows (D84) are refused unless ``allow_holdout=True`` with a *reason*,
    which is logged. With ``active.parquet`` present the cross-name context columns
    are joined; *features_root* (an E27.1 ``v<schema>`` dir) joins the feature panel
    on (date, ticker).
    """
    if split not in ("dev", "holdout", "all"):
        msg = f"split must be dev, holdout or all, not {split!r}"
        raise ValueError(msg)
    if split != "dev":
        if not allow_holdout or not reason:
            msg = "holdout rows need allow_holdout=True and a reason (D84)"
            raise HoldoutRefusedError(msg)
        log.warning("ml.ledger.holdout_read", reason=reason, split=split)
    vdir = (
        root
        if (root / "manifest.json").exists() or root.name.startswith("v")
        else version_dir(root)
    )
    files = sorted(vdir.glob("profile=*/year=*/part-*.parquet"))
    frames = []
    for f in files:
        p = f.parent.parent.name.removeprefix("profile=")
        y = int(f.parent.name.removeprefix("year="))
        if profiles is not None and p not in profiles:
            continue
        if years is not None and y not in years:
            continue
        df = pd.read_parquet(f, columns=list(columns) if columns else None)
        if not df.empty:
            frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if out.empty:
        return out
    if split != "all" and "split" in out:
        out = out[out["split"] == split].reset_index(drop=True)
    act = vdir / "active.parquet"
    if act.exists() and {"date", "ticker"} <= set(out.columns):
        a = pd.read_parquet(act)
        out["date"] = pd.to_datetime(out["date"]).dt.date
        a["date"] = pd.to_datetime(a["date"]).dt.date
        out = out.merge(a, on=["date", "ticker"], how="left")
    if features_root is not None and {"date", "ticker"} <= set(out.columns):
        out = _join_features(out, features_root)
    return out


def ledger_stats(root: Path, *, split: str = "dev", reason: str | None = None) -> str:
    """Counts, label base rates per profile/sleeve/kind/year and mean ``r_net`` per x."""
    df = load_ledger(
        root,
        split=split,
        allow_holdout=split != "dev",
        reason=reason,
    )
    if df.empty:
        return f"ledger {root}: no rows ({split})\n"
    df["year"] = pd.to_datetime(df["date"]).dt.year
    xs = sorted(c.removeprefix("label_r_net_") for c in df.columns if c.startswith("label_r_net_"))
    w = df.get("sample_weight", pd.Series(1, index=df.index)).astype(float)
    lines = [f"ledger {root} · split {split} · {len(df)} rows · weight sum {w.sum():.0f}"]
    agg: dict[str, tuple[str, str]] = {"n": ("key", "size")}
    for tg in xs:
        agg[f"r_{tg}"] = (f"label_r_net_{tg}", "mean")
        agg[f"win_{tg}"] = (f"label_win_{tg}", "mean")
    agg["stop"] = ("label_hit_stop", "mean")
    agg["tp"] = ("label_hit_tp", "mean")
    agg["days"] = ("label_days_held", "mean")
    g = df.groupby(["profile", "sleeve", "kind", "year"]).agg(**agg).round(3)  # type: ignore[call-overload]
    with pd.option_context("display.width", 250, "display.max_rows", 500):
        lines.append(g.to_string())
    lines.append(
        "mean r_net per x: " + ", ".join(f"{tg}={df[f'label_r_net_{tg}'].mean():+.4f}" for tg in xs)
    )
    return "\n".join(lines) + "\n"


def _join_features(df: pd.DataFrame, froot: Path) -> pd.DataFrame:
    days = sorted(set(pd.to_datetime(df["date"]).dt.date))
    panels = []
    for d in days:
        f = froot / f"date={d.isoformat()}.parquet"
        if f.exists():
            panels.append(pd.read_parquet(f))
    if not panels:
        return df
    feat = pd.concat(panels, ignore_index=True)
    dcol = "date" if "date" in feat else "session"
    feat[dcol] = pd.to_datetime(feat[dcol]).dt.date
    feat = feat.rename(columns={dcol: "date"})
    tcol = "ticker" if "ticker" in feat else "symbol"
    feat = feat.rename(columns={tcol: "ticker"})
    clash = [c for c in feat.columns if c in df.columns and c not in ("date", "ticker")]
    feat = feat.rename(columns={c: f"f_{c}" for c in clash})
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    return out.merge(feat, on=["date", "ticker"], how="left")
