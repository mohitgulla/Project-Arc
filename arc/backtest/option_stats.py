"""Per-ticker daily option stats from the cached Theta history (E7.16, D84).

A **follower** of the E7.9 pull: once a ticker's sessions are all cached (quotes + OI),
its whole history is summarised from disk. No network, no LLM, no audit-DB access, so
it never competes with the pull for ThetaData's request slots, and a formula fix is a
minutes-long recompute instead of a re-pull. Aggregates only (ThetaData retail terms).

Outputs under ``<data-dir>/option_stats/``:

* ``daily/underlying=<T>.parquet``: one row per cached session (:data:`DAILY_COLUMNS`).
* ``term/underlying=<T>.parquet``: the per-session ATM term points (``ivhist``), the
  input of IV30 and earnings detection; SPY's is the market reference for every
  other name's detection.
* ``earnings_events.parquet``: one row per detected event (:data:`EVENT_COLUMNS`).
* ``liquidity_quarterly.parquet``: per (quarter, ticker) option-liquidity score,
  D9 pass rate and quarter-to-quarter persistence (:data:`QUARTERLY_COLUMNS`).
* ``manifest.json``: per ticker, sessions / last session / source signature / code
  hash; a ticker is recomputed only when its source files or the code+settings change.
* ``checkpoints.json``: ``{c125: {complete_at, tickers, rows, wall_seconds}, ...}``.

**Same code paths as live and the backtest.** Marks: :func:`arc.backtest.marks.mark_chains`
with the ``backtest.data`` settings. IV30 / IV rank / percentile:
:mod:`arc.backtest.ivhist` (live ``atm_term_points`` + cm30 + ``iv_rank``). D9:
:func:`arc.backtest.data.day_liquidity_metrics` + the live
:func:`arc.universe.screen.screen_liquidity`. Earnings:
:func:`arc.backtest.earnings_hist.detect_events`. RV: the live
:func:`arc.features.vol.rolling_historical_vol` (close-to-close, ddof 1, √252).

**Definitions of the columns with no existing live formula** (decimal vols):

* ``skew_25d``: per expiry 7–75 DTE, the IV at Δ −0.25 (puts) and Δ +0.25 (calls) by
  linear interpolation in delta over that expiry's quoted contracts (an expiry whose
  deltas don't bracket ±0.25 is skipped); each side is taken to 30 DTE with the live
  cm30 total-variance interpolation; ``skew_25d = put25 − call25``.
* ``iv60`` (cm60 on the same points), ``term_slope = iv60 − iv30``; ``front_iv`` = the
  ATM IV of the nearest expiry ≥ 7 DTE, ``front_over_iv30 = front_iv / iv30``.
* ``atm_spread_pct``: median ``(ask − bid) / mid`` over the raw two-sided contracts at
  the ``atm_strikes`` strikes nearest spot of every expiry 7–45 DTE (raw rows, so the
  marks' 35 % wide-spread drop doesn't bias it). ``two_sided_pct``: share of the
  session's raw rows with ``bid > 0`` and ``ask ≥ bid``.
* ``oi_near_atm``: the E7.9 step 5b / ``Stage2Config`` near-ATM OI (per expiry ≤
  ``max_dte``, the ATM strike ± ``strike_range`` listed strikes, both rights);
  ``d9_atm_oi`` / ``d9_atm_spread_pct``: the live D9 screen's own two inputs.
* ``rv20`` / ``rv60`` on split-adjusted closes (``underlying_ohlc``; raw closes when a
  name has no OHLC file), so a split is not a 50 % move; ``vrp20 = iv30 − rv20``.

Point-in-time: a session's row reads that session's option rows, closes ≤ d, volume
≤ d and the IV30 series ≤ d; nothing later (tested by mutating every later row).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.data import DataSettings, day_liquidity_metrics, quarter_start
from arc.backtest.earnings_hist import EarningsEvent, detect_events
from arc.backtest.entry_filter import OhlcStore
from arc.backtest.ivhist import TermPoint, iv30_from_points, iv_points, term_points
from arc.backtest.marks import RateCurve, mark_chains
from arc.backtest.underlying import UnderlyingStore
from arc.backtest.universe import Stage2Config, UniverseRow, universe_dir
from arc.data.history.store import OI_META_KEY, ParquetHistoryStore
from arc.features.vol import (
    MIN_IV_HISTORY,
    constant_maturity_iv,
    rolling_historical_vol,
)
from arc.iv.record import CHAIN_DTE_MAX, CHAIN_DTE_MIN
from arc.universe.screen import screen_liquidity
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from arc.backtest.costs import CostModel
    from arc.backtest.ivhist import IvPoint

log = structlog.get_logger()

__all__ = [
    "DAILY_COLUMNS",
    "EVENT_COLUMNS",
    "QUARTERLY_COLUMNS",
    "StatsConfig",
    "StatsRunner",
    "TickerResult",
    "code_hash",
    "daily_path",
    "day_stats",
    "event_stats",
    "liquidity_quarterly",
    "pull_complete",
    "read_daily",
    "read_iv_points",
    "iv_settings_hash",
    "settings_hash",
    "skew_25d",
    "stage2_near_atm_oi",
    "stats_dir",
    "ticker_stats",
]

DIR_NAME = "option_stats"
#: Bump when a formula here changes in a way the source-file hash can't see.
FORMULA_VERSION = 1
SKEW_DELTA = 0.25
SPREAD_DTE = (7, 45)
TARGET_60 = 60
#: Sessions per read chunk: bounds memory (a liquid name is ~2-6k rows/session).
CHUNK_SESSIONS = 126

DAILY_COLUMNS: tuple[str, ...] = (
    "ticker", "date", "spot", "spot_adj", "iv30", "iv30_expiries", "iv_rank_252",
    "iv_pct_252", "iv_obs",
    "iv60", "term_slope", "front_iv", "front_dte", "front_over_iv30", "skew_25d",
    "put25_iv", "call25_iv", "rv20", "rv60", "vrp20", "atm_spread_pct", "two_sided_pct",
    "oi_total", "oi_near_atm", "opt_volume", "put_call_oi", "put_call_vol",
    "d9_atm_oi", "d9_atm_spread_pct", "d9_pass", "d9_reasons", "rows", "quoted_rows",
    "expiries", "max_dte_seen",
)  # fmt: skip
EVENT_COLUMNS: tuple[str, ...] = (
    "ticker", "event_date", "prev_date", "basis", "front_expiration", "pre_iv_front",
    "post_iv_front", "iv_crush_pts", "iv_crush_pct", "jump_excess", "crush_excess",
    "implied_move", "realised_move", "realised_over_implied",
)  # fmt: skip
QUARTERLY_COLUMNS: tuple[str, ...] = (
    "quarter", "ticker", "near_atm_oi", "opt_volume_20d", "score", "sessions_used",
    "score_source", "sessions_in_q", "pass_rate_d9", "liquid", "liquid_prev_q", "persistent",
)  # fmt: skip

_RAW_COLS = [
    "date", "expiration", "strike", "right", "symbol", "close", "volume",
    "bid", "ask", "open_interest",
]  # fmt: skip


class StatsConfig(BaseModel):
    """What the stats are computed with (hashed into the manifest)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "thetadata"
    data: DataSettings = Field(default_factory=lambda: DataSettings())
    stage2: Stage2Config = Field(default_factory=lambda: Stage2Config())
    flat_rate: float = 0.045
    min_iv_obs: int = MIN_IV_HISTORY
    liquid_pass_rate: float = Field(
        0.5, ge=0.0, le=1.0, description="pass_rate_d9 at/above which a quarter is 'liquid'"
    )


def stats_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / DIR_NAME


def daily_path(data_dir: Path | str, ticker: str) -> Path:
    return stats_dir(data_dir) / "daily" / f"underlying={ticker.upper()}.parquet"


def term_path(data_dir: Path | str, ticker: str) -> Path:
    return stats_dir(data_dir) / "term" / f"underlying={ticker.upper()}.parquet"


def settings_hash(cfg: StatsConfig) -> str:
    """Hash of every setting the stats read (part of the manifest code hash)."""
    blob = json.dumps(cfg.model_dump(mode="json"), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def iv_settings_hash(ds: DataSettings, *, provider: str, flat_rate: float, min_iv_obs: int) -> str:
    """Hash of the settings IV30 / IV rank depend on.

    :class:`arc.backtest.data.HistoryView` computes the same hash from its own settings
    and reads ``daily/`` only on a match (``iv_source: option_stats``), else recomputes.
    """
    keys = (
        "marks", "max_quoted_spread_pct", "quoted_fallback", "rate_source",
        "chain_dte_min", "chain_dte_max", "iv_lookback",
    )  # fmt: skip
    d = {k: getattr(ds, k) for k in keys}
    d |= {"provider": provider, "flat_rate": flat_rate, "min_iv_obs": min_iv_obs}
    blob = json.dumps(d, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _cfg_iv_hash(cfg: StatsConfig) -> str:
    return iv_settings_hash(
        cfg.data, provider=cfg.provider, flat_rate=cfg.flat_rate, min_iv_obs=cfg.min_iv_obs
    )


def code_hash(cfg: StatsConfig) -> str:
    """Source of every module the numbers come from + the settings + FORMULA_VERSION."""
    import arc.backtest.chain as m_chain
    import arc.backtest.data as m_data
    import arc.backtest.earnings_hist as m_earn
    import arc.backtest.ivhist as m_iv
    import arc.backtest.marks as m_marks
    import arc.features.vol as m_vol
    import arc.universe.screen as m_screen

    h = hashlib.sha256()
    for m in (m_chain, m_data, m_earn, m_iv, m_marks, m_vol, m_screen):
        h.update(Path(str(m.__file__)).read_bytes())
    h.update(Path(__file__).read_bytes())
    h.update(f"{FORMULA_VERSION}:{settings_hash(cfg)}".encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# per-session formulas (pure)
# ---------------------------------------------------------------------------


def _interp_delta(g: pd.DataFrame, target: float) -> float | None:
    """IV at *target* delta by linear interpolation in delta (None unless bracketed)."""
    g = g[np.isfinite(g["iv"]) & np.isfinite(g["delta"]) & (g["iv"] > 0)]
    if len(g) < 2:
        return None
    g = g.sort_values("delta")
    d = g["delta"].to_numpy(dtype=float)
    v = g["iv"].to_numpy(dtype=float)
    if not (d[0] <= target <= d[-1]):
        return None
    d, idx = np.unique(d, return_index=True)
    return float(np.interp(target, d, v[idx]))


def skew_25d(
    chain: pd.DataFrame,
    *,
    dte_min: int = CHAIN_DTE_MIN,
    dte_max: int = CHAIN_DTE_MAX,
    target_dte: int = 30,
) -> tuple[float | None, float | None, float | None]:
    """(skew, put25 IV, call25 IV) at ~30 DTE from one session's marked chain."""
    if chain.empty or "delta" not in chain:
        return None, None, None
    puts: list[tuple[int, float]] = []
    calls: list[tuple[int, float]] = []
    win = chain[(chain["dte"] >= dte_min) & (chain["dte"] <= dte_max)]
    for dte, g in win.groupby("dte", sort=True):
        p = _interp_delta(g[g["right"] == "put"], -SKEW_DELTA)
        c = _interp_delta(g[g["right"] == "call"], SKEW_DELTA)
        if p is not None and c is not None:  # both sides on the same expiry
            puts.append((int(dte), p))  # type: ignore[call-overload]
            calls.append((int(dte), c))  # type: ignore[call-overload]
    if not puts:
        return None, None, None
    p30 = constant_maturity_iv(puts, target_dte=target_dte)
    c30 = constant_maturity_iv(calls, target_dte=target_dte)
    return p30 - c30, p30, c30


def stage2_near_atm_oi(raw: pd.DataFrame, spot: float, *, day: dt.date, cfg: Stage2Config) -> float:
    """E7.9 step 5b near-ATM OI: per expiry ≤ max_dte, ATM strike ± strike_range, both rights."""
    if raw.empty or not math.isfinite(spot) or spot <= 0:
        return 0.0
    dte = (pd.to_datetime(raw["expiration"]) - pd.Timestamp(day)).dt.days
    df = raw.loc[(dte >= 0) & (dte <= cfg.max_dte), ["expiration", "strike", "open_interest"]]
    total = 0.0
    for _, g in df.groupby("expiration"):
        strikes = sorted(set(g["strike"].astype(float)))
        i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
        keep = set(strikes[max(0, i - cfg.strike_range) : i + cfg.strike_range + 1])
        oi = pd.to_numeric(g.loc[g["strike"].isin(keep), "open_interest"], errors="coerce")
        total += float(oi.fillna(0).sum())
    return total


def _atm_spread(raw: pd.DataFrame, spot: float, day: dt.date, atm_strikes: int) -> float | None:
    dte = (pd.to_datetime(raw["expiration"]) - pd.Timestamp(day)).dt.days
    lo, hi = SPREAD_DTE
    w = raw[(dte >= lo) & (dte <= hi)]
    vals: list[np.ndarray] = []
    for _, g in w.groupby("expiration"):
        ks = sorted(set(g["strike"].astype(float)), key=lambda k: (abs(k - spot), k))
        near = g[g["strike"].isin(set(ks[:atm_strikes]))]
        b = near["bid"].to_numpy(dtype=float)
        a = near["ask"].to_numpy(dtype=float)
        ok = np.isfinite(b) & np.isfinite(a) & (b > 0) & (a >= b)
        if ok.any():
            vals.append((a[ok] - b[ok]) / ((a[ok] + b[ok]) / 2.0))
    if not vals:
        return None
    return float(np.median(np.concatenate(vals)))


def _ratio(num: float, den: float) -> float | None:
    return num / den if den > 0 else None


def day_stats(
    ticker: str,
    day: dt.date,
    raw: pd.DataFrame,
    chain: pd.DataFrame,
    points: Sequence[TermPoint],
    *,
    spot: float | None,
    closes: pd.Series,
    volume: pd.Series,
    cfg: StatsConfig,
) -> dict[str, object]:
    """Every column of one session except the series ones (IV rank, RV, spot_adj)."""
    th = cfg.data.screen
    out: dict[str, object] = {"ticker": ticker, "date": day, "spot": spot}
    out["rows"] = len(raw)
    iv30, n_exp = iv30_from_points(list(points))
    out["iv30"], out["iv30_expiries"] = iv30, n_exp
    win = [(p.dte, p.iv) for p in points if CHAIN_DTE_MIN <= p.dte <= CHAIN_DTE_MAX]
    iv60 = constant_maturity_iv(win, target_dte=TARGET_60) if win else None
    out["iv60"] = iv60
    out["term_slope"] = None if iv60 is None or iv30 is None else iv60 - iv30
    if win:
        fd, fv = min(win)
        out["front_iv"], out["front_dte"] = fv, fd
        out["front_over_iv30"] = fv / iv30 if iv30 else None
    sk, p25, c25 = skew_25d(chain)
    out["skew_25d"], out["put25_iv"], out["call25_iv"] = sk, p25, c25
    if raw.empty:
        out |= {"two_sided_pct": None, "oi_total": 0.0, "opt_volume": 0.0, "expiries": 0}
    else:
        b = raw["bid"].to_numpy(dtype=float)
        a = raw["ask"].to_numpy(dtype=float)
        two = np.isfinite(b) & np.isfinite(a) & (b > 0) & (a >= b)
        out["quoted_rows"] = int(two.sum())
        out["two_sided_pct"] = float(two.mean())
        oi = pd.to_numeric(raw["open_interest"], errors="coerce").fillna(0.0)
        vol = pd.to_numeric(raw["volume"], errors="coerce").fillna(0.0)
        put = (raw["right"].astype(str) == "put").to_numpy()
        out["oi_total"] = float(oi.sum())
        out["opt_volume"] = float(vol.sum())
        out["put_call_oi"] = _ratio(float(oi[put].sum()), float(oi[~put].sum()))
        out["put_call_vol"] = _ratio(float(vol[put].sum()), float(vol[~put].sum()))
        out["expiries"] = int(raw["expiration"].nunique())
        dte = (pd.to_datetime(raw["expiration"]) - pd.Timestamp(day)).dt.days
        out["max_dte_seen"] = int(dte.max())
        if spot is not None and spot > 0:
            out["atm_spread_pct"] = _atm_spread(raw, spot, day, th.atm_strikes)
            out["oi_near_atm"] = stage2_near_atm_oi(raw, spot, day=day, cfg=cfg.stage2)
    m = day_liquidity_metrics(
        ticker,
        day,
        raw,
        closes=closes,
        volume=volume,
        thresholds=th,
        dte_window=cfg.data.screen_dte_window,
    )
    sc = screen_liquidity(m, th)
    out["d9_atm_oi"] = m.atm_open_interest
    out["d9_atm_spread_pct"] = m.atm_spread_pct
    out["d9_pass"] = sc.passed
    out["d9_reasons"] = sc.detail()
    return out


# ---------------------------------------------------------------------------
# per ticker
# ---------------------------------------------------------------------------


class TickerResult(BaseModel):
    """One ticker's computed frames (written by :class:`StatsRunner`)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    ticker: str
    sessions: int
    last_session: dt.date | None
    events: int
    wall_seconds: float
    daily: pd.DataFrame = Field(exclude=True)
    term: pd.DataFrame = Field(exclude=True)
    event_rows: pd.DataFrame = Field(exclude=True)


def _read_raw(
    store: ParquetHistoryStore, provider: str, ticker: str, days: Sequence[dt.date]
) -> pd.DataFrame:
    tables = []
    for d in days:
        p = store.path_for(provider, ticker, d)
        if p.is_file():
            tables.append(pd.read_parquet(p, columns=_RAW_COLS))
    tables = [t for t in tables if len(t)]
    if not tables:
        return pd.DataFrame(columns=_RAW_COLS)
    return pd.concat(tables, ignore_index=True)


def _adjusted_closes(data_dir: Path, ticker: str, raw_closes: pd.Series) -> pd.Series:
    o = OhlcStore(data_dir).read(ticker)
    if o.empty:
        return raw_closes
    return o["close"].dropna()


def _points_frame(ticker: str, points: Mapping[dt.date, Sequence[TermPoint]]) -> pd.DataFrame:
    rows = [
        {"ticker": ticker, "date": d, "expiration": p.expiration, "dte": p.dte, "iv": p.iv}
        for d, ps in sorted(points.items())
        for p in ps
    ]
    return pd.DataFrame(rows, columns=["ticker", "date", "expiration", "dte", "iv"])


def read_term_points(data_dir: Path | str, ticker: str) -> dict[dt.date, list[TermPoint]] | None:
    p = term_path(data_dir, ticker)
    if not p.is_file():
        return None
    df = pd.read_parquet(p)
    out: dict[dt.date, list[TermPoint]] = {}
    for d, g in df.groupby("date", sort=True):
        out[_as_date(d)] = [
            TermPoint(expiration=_as_date(e), dte=int(n), iv=float(v))
            for e, n, v in zip(g["expiration"], g["dte"], g["iv"], strict=True)
        ]
    return out


def event_stats(
    ticker: str,
    events: Sequence[EarningsEvent],
    points: Mapping[dt.date, Sequence[TermPoint]],
    *,
    chains_prev: Mapping[dt.date, pd.DataFrame],
    raw_closes: pd.Series,
    move_closes: pd.Series,
    basis: str,
) -> pd.DataFrame:
    """One row per event: IV crush, implied move (front ATM straddle / spot), realised move."""
    days = sorted(points)
    pos = {d: i for i, d in enumerate(days)}
    rows: list[dict[str, object]] = []
    for e in events:
        i = pos.get(e.day)
        prev = days[i - 1] if i else None
        r: dict[str, object] = {
            "ticker": ticker,
            "event_date": e.day,
            "prev_date": prev,
            "basis": basis,
            "front_expiration": e.front_expiration,
            "jump_excess": e.jump,
            "crush_excess": e.crush,
        }
        if prev is not None:
            pre = next((p.iv for p in points[prev] if p.expiration == e.front_expiration), None)
            post = next((p.iv for p in points[e.day] if p.expiration == e.front_expiration), None)
            r["pre_iv_front"], r["post_iv_front"] = pre, post
            if pre is not None and post is not None:
                r["iv_crush_pts"] = pre - post
                r["iv_crush_pct"] = 1.0 - post / pre if pre > 0 else None
            spot = raw_closes.get(prev)
            ch = chains_prev.get(prev)
            if spot is not None and ch is not None and not ch.empty and spot > 0:
                f = ch[ch["expiration"] == e.front_expiration]
                if not f.empty:
                    k = min(set(f["strike"].astype(float)), key=lambda x: (abs(x - spot), x))
                    at = f[f["strike"] == k]
                    c = at.loc[at["right"] == "call", "mid"]
                    p_ = at.loc[at["right"] == "put", "mid"]
                    if len(c) and len(p_):
                        r["implied_move"] = (float(c.iloc[0]) + float(p_.iloc[0])) / float(spot)
            a, b = move_closes.get(e.day), move_closes.get(prev)
            if a is not None and b is not None and b > 0:
                r["realised_move"] = abs(float(a) / float(b) - 1.0)
            im, rm = r.get("implied_move"), r.get("realised_move")
            if isinstance(im, float) and isinstance(rm, float) and im > 0:
                r["realised_over_implied"] = rm / im
        rows.append(r)
    return pd.DataFrame(rows, columns=list(EVENT_COLUMNS))


def ticker_stats(
    data_dir: Path | str,
    ticker: str,
    *,
    cfg: StatsConfig,
    cost: CostModel,
    etf: bool = False,
    market_points: Mapping[dt.date, Sequence[TermPoint]] | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> TickerResult:
    """Daily rows, term points and earnings events of one ticker (from disk only)."""
    t0 = time.monotonic()
    data_dir = Path(data_dir)
    t = ticker.upper()
    store = ParquetHistoryStore(data_dir)
    ds = cfg.data
    days = sorted(
        d
        for d in store.cached_dates(cfg.provider, t)
        if (start is None or d >= start) and (end is None or d <= end)
    )
    bars = UnderlyingStore(data_dir).read_bars(t)
    raw_closes = bars["close"].dropna()
    volume = bars["volume"]
    adj = _adjusted_closes(data_dir, t, raw_closes)
    rate = RateCurve.load(data_dir, flat=cfg.flat_rate, source=ds.rate_source)
    rows: list[dict[str, object]] = []
    points: dict[dt.date, list[TermPoint]] = {}
    for k in range(0, len(days), CHUNK_SESSIONS):
        chunk = days[k : k + CHUNK_SESSIONS]
        raw = _read_raw(store, cfg.provider, t, chunk)
        chains, _ = mark_chains(
            raw,
            raw_closes,
            mode=ds.marks,
            cost=cost,
            rate=rate,
            dte_min=ds.chain_dte_min,
            dte_max=ds.chain_dte_max,
            max_spread_pct=ds.max_quoted_spread_pct,
            fallback=ds.quoted_fallback,
        )
        by_day = {d: g for d, g in raw.groupby("date", sort=False)} if len(raw) else {}
        empty = raw.iloc[0:0]
        for d in chunk:
            spot = raw_closes.get(d)
            spot_f = float(spot) if spot is not None and pd.notna(spot) else None
            if spot_f is None and d not in by_day:
                continue  # before listing / after delisting: an empty file and no close
            chain = chains.get(d, empty)
            pts = term_points(chain, spot_f, d) if spot_f and d in chains else []
            if spot_f is not None and d in chains:
                points[d] = pts
            rows.append(
                day_stats(
                    t,
                    d,
                    by_day.get(d, empty),
                    chain,
                    pts,
                    spot=spot_f,
                    closes=raw_closes,
                    volume=volume,
                    cfg=cfg,
                )
            )
    daily = pd.DataFrame(rows, columns=list(DAILY_COLUMNS)) if rows else _empty_daily()
    if len(daily):
        iv = iv_points(t, points, lookback=ds.iv_lookback, min_obs=cfg.min_iv_obs)
        daily["iv_rank_252"] = [_iv_attr(iv, d, "iv_rank") for d in daily["date"]]
        daily["iv_pct_252"] = [_iv_attr(iv, d, "iv_percentile") for d in daily["date"]]
        daily["iv_obs"] = [_iv_attr(iv, d, "n_obs") for d in daily["date"]]
        daily["spot_adj"] = [_get(adj, d) for d in daily["date"]]
        for w in (20, 60):
            rv = _rv(adj, w)
            daily[f"rv{w}"] = [_get(rv, d) for d in daily["date"]]
        daily["vrp20"] = pd.to_numeric(daily["iv30"], errors="coerce") - pd.to_numeric(
            daily["rv20"], errors="coerce"
        )
        daily = _coerce_daily(daily)
    ref = ds.earnings.earn_market_ref
    use_market = None if (ref and ref.upper() == t) else market_points
    events = detect_events(t, points, ds.earnings, etf=etf, market=use_market)
    basis = "etf" if etf else (f"{ref.lower()}_relative" if use_market is not None else "raw")
    prev_days = sorted({p for p in _prev_days(points, events) if p is not None})
    chains_prev: dict[dt.date, pd.DataFrame] = {}
    if prev_days:
        raw = _read_raw(store, cfg.provider, t, prev_days)
        chains_prev, _ = mark_chains(
            raw,
            raw_closes,
            mode=ds.marks,
            cost=cost,
            rate=rate,
            dte_min=ds.chain_dte_min,
            dte_max=ds.chain_dte_max,
            max_spread_pct=ds.max_quoted_spread_pct,
            fallback=ds.quoted_fallback,
        )
    ev = event_stats(
        t,
        events,
        points,
        chains_prev=chains_prev,
        raw_closes=raw_closes,
        move_closes=adj,
        basis=basis,
    )
    return TickerResult(
        ticker=t,
        sessions=len(daily),
        last_session=days[-1] if days else None,
        events=len(ev),
        wall_seconds=round(time.monotonic() - t0, 2),
        daily=daily,
        term=_points_frame(t, points),
        event_rows=ev,
    )


def _prev_days(
    points: Mapping[dt.date, Sequence[TermPoint]], events: Sequence[EarningsEvent]
) -> list[dt.date | None]:
    days = sorted(points)
    pos = {d: i for i, d in enumerate(days)}
    return [days[pos[e.day] - 1] if pos.get(e.day) else None for e in events]


def _rv(closes: pd.Series, window: int) -> pd.Series:
    s = closes[closes > 0]
    if len(s) < window + 1:
        return pd.Series(dtype=float)
    return rolling_historical_vol(s, window)


def _get(s: pd.Series, d: dt.date) -> float | None:
    v = s.get(d)
    return float(v) if v is not None and pd.notna(v) else None


def _iv_attr(iv: Mapping[dt.date, IvPoint], d: dt.date, name: str) -> object:
    p = iv.get(d)
    return None if p is None else getattr(p, name)


def _empty_daily() -> pd.DataFrame:
    return _coerce_daily(pd.DataFrame(columns=list(DAILY_COLUMNS)))


_TEXT = {"ticker", "d9_reasons"}
_INT = {
    "rows", "quoted_rows", "expiries", "max_dte_seen", "front_dte", "iv_obs", "iv30_expiries",
}  # fmt: skip


def _coerce_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DAILY_COLUMNS:
        if c in _TEXT:
            df[c] = df[c].astype("string")
        elif c == "date":
            df[c] = pd.to_datetime(df[c]).dt.date
        elif c == "d9_pass":
            df[c] = df[c].astype("boolean")
        elif c in _INT:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def _as_date(v: object) -> dt.date:
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------


def read_daily(data_dir: Path | str, ticker: str) -> pd.DataFrame:
    p = daily_path(data_dir, ticker)
    if not p.is_file():
        return _empty_daily()
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def read_iv_points(
    data_dir: Path | str, ticker: str, *, expect_settings: str | None = None
) -> dict[dt.date, IvPoint] | None:
    """IvPoints from ``daily/`` (the HistoryView hook); None when absent or stale.

    *expect_settings* is the caller's :func:`iv_settings_hash`; a ticker whose manifest
    entry was computed with other settings returns None so the caller recomputes.
    """
    from arc.backtest.ivhist import IvPoint

    if expect_settings is not None:
        entry = load_manifest(data_dir).get("tickers", {}).get(ticker.upper())
        if not entry or entry.get("iv_hash") != expect_settings:
            return None
    df = read_daily(data_dir, ticker)
    if df.empty:
        return None
    out: dict[dt.date, IvPoint] = {}
    for r in df.itertuples(index=False):
        if r.spot is None or not pd.notna(r.spot):
            continue
        out[r.date] = IvPoint(
            ticker=ticker.upper(),
            day=r.date,
            iv30=_f(r.iv30),
            iv_rank=_f(r.iv_rank_252),
            iv_percentile=_f(r.iv_pct_252),
            n_obs=int(r.iv_obs) if pd.notna(r.iv_obs) else 0,
            expiries=int(r.iv30_expiries) if pd.notna(r.iv30_expiries) else 0,
        )
    return out


def _f(v: object) -> float | None:
    return float(v) if v is not None and pd.notna(v) else None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# quarterly liquidity
# ---------------------------------------------------------------------------


def liquidity_quarterly(
    daily: Mapping[str, pd.DataFrame],
    calendar: Sequence[dt.date],
    *,
    cfg: StatsConfig,
    quarters: Mapping[str, Sequence[dt.date]] | None = None,
    optliq: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per (quarter Q, ticker): the E7.9 5b score (sessions < Q), D9 pass rate inside Q.

    *quarters* restricts each ticker's quarters (the universe membership); default
    every quarter start its daily rows cover. *optliq* (E7.9's sidecar) supplies
    ``near_atm_oi`` / ``opt_volume_20d`` / ``score`` / ``sessions_used`` when present.
    """
    cal = sorted(calendar)
    s2 = cfg.stage2
    ol: dict[tuple[dt.date, str], tuple[float, float, float, int]] = {}
    if optliq is not None and not optliq.empty:
        for r in optliq.itertuples(index=False):
            ol[(_as_date(r.quarter), str(r.symbol))] = (
                float(r.near_atm_oi),
                float(r.opt_volume_20d),
                float(r.score),
                int(r.sessions_used),
            )
    out: list[dict[str, object]] = []
    for t, df in sorted(daily.items()):
        if df.empty:
            continue
        by_day = df.set_index("date")
        qs = (
            sorted(set(quarters.get(t, ())))
            if quarters is not None
            else sorted({quarter_start(d) for d in by_day.index})
        )
        prev_liquid: bool | None = None
        prev_q: dt.date | None = None
        for q in qs:
            nq = dt.date(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1)
            inq = by_day[(pd.Index(by_day.index) >= q) & (pd.Index(by_day.index) < nq)]
            n_in = len(inq)
            pass_rate = float(inq["d9_pass"].fillna(False).astype(bool).mean()) if n_in else None
            if (q, t) in ol:
                oi, vol, score, used = ol[(q, t)]
                source = "e79_optliq"
            else:
                window = [d for d in cal if d < q][-s2.volume_days :]
                vol = float(sum(_num(by_day, d, "opt_volume") for d in window))
                used = sum(1 for d in window if d in by_day.index)
                oi = _num(by_day, window[-1], "oi_near_atm") if window else 0.0
                score = s2.oi_weight * oi + s2.volume_weight * vol
                source = "option_stats"
            liquid = None if pass_rate is None else pass_rate >= cfg.liquid_pass_rate
            consecutive = prev_q is not None and _next_q(prev_q) == q
            out.append(
                {
                    "quarter": q,
                    "ticker": t,
                    "near_atm_oi": oi,
                    "opt_volume_20d": vol,
                    "score": score,
                    "sessions_used": used,
                    "score_source": source,
                    "sessions_in_q": n_in,
                    "pass_rate_d9": pass_rate,
                    "liquid": liquid,
                    "liquid_prev_q": prev_liquid if consecutive else None,
                    "persistent": (bool(liquid and prev_liquid) if consecutive else None),
                }
            )
            prev_liquid, prev_q = liquid, q
    return pd.DataFrame(out, columns=list(QUARTERLY_COLUMNS))


def _next_q(q: dt.date) -> dt.date:
    return dt.date(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1)


def _num(by_day: pd.DataFrame, d: dt.date, col: str) -> float:
    if d not in by_day.index:
        return 0.0
    v = by_day.at[d, col]
    return float(v) if pd.notna(v) else 0.0


# ---------------------------------------------------------------------------
# pull completeness + manifest + runner
# ---------------------------------------------------------------------------


def pull_complete(
    store: ParquetHistoryStore,
    provider: str,
    ticker: str,
    sessions: Sequence[dt.date],
    *,
    check_oi: bool = True,
) -> bool:
    """Every session in *sessions* cached (and stamped with OI): the pull is done with it."""
    have = store.cached_dates(provider, ticker)
    if not set(sessions) <= have:
        return False
    if not check_oi:
        return True
    for d in sessions:
        kv = pq.read_metadata(store.path_for(provider, ticker, d)).metadata or {}
        if kv.get(OI_META_KEY) != b"1":
            return False
    return True


def source_signature(store: ParquetHistoryStore, provider: str, ticker: str) -> dict[str, object]:
    """Session-file count + the directory's mtime (cheap: no per-file stat).

    Every store write is a tmp-file rename into the ticker directory (``write_day``,
    ``add_open_interest``), which bumps the directory mtime.
    """
    d = store.path_for(provider, ticker, dt.date(2000, 1, 1)).parent
    if not d.is_dir():
        return {"files": 0, "dir_mtime_ns": 0}
    n = sum(1 for e in os.scandir(d) if e.name.endswith(".parquet"))
    return {"files": n, "dir_mtime_ns": d.stat().st_mtime_ns}


def manifest_path(data_dir: Path | str) -> Path:
    return stats_dir(data_dir) / "manifest.json"


def load_manifest(data_dir: Path | str) -> dict:
    p = manifest_path(data_dir)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"tickers": {}}


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


Outcome = Literal["computed", "skipped", "incomplete"]


class StatsRunner:
    """Builds / refreshes the stats for a universe's tickers (priority order)."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        cfg: StatsConfig,
        cost: CostModel,
        rows: Sequence[UniverseRow],
        start: dt.date,
        end: dt.date,
        version: str | None = None,
        membership: pd.DataFrame | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.cost = cost
        self.rows = sorted(rows, key=lambda r: r.position)
        self.order = [r.symbol for r in self.rows]
        self.etfs = frozenset(r.symbol for r in self.rows if r.asset_type == "etf")
        self.start = start
        self.end = end
        self.version = version
        self.membership = membership
        self.sessions = sessions_between(start, end)
        self.store = ParquetHistoryStore(self.data_dir)
        self.code = code_hash(cfg)
        self.iv_hash = _cfg_iv_hash(cfg)
        self.manifest = load_manifest(self.data_dir)
        self.manifest.setdefault("tickers", {})

    # -- state ---------------------------------------------------------------

    def up_to_date(self, ticker: str) -> bool:
        e = self.manifest["tickers"].get(ticker)
        if not e or e.get("code_hash") != self.code:
            return False
        sig = source_signature(self.store, self.cfg.provider, ticker)
        if e.get("source") != sig or e.get("window") != [str(self.start), str(self.end)]:
            return False
        # Computed before the market reference (SPY) existed: redo once it does.
        return not (
            e.get("market_ref") is None
            and self._wants_market(ticker)
            and term_path(self.data_dir, self._ref() or "").is_file()
        )

    def _ref(self) -> str | None:
        ref = self.cfg.data.earnings.earn_market_ref
        return ref.upper() if ref else None

    def _wants_market(self, ticker: str) -> bool:
        ref = self._ref()
        return ref is not None and ref != ticker and ticker not in self.etfs

    def complete_in_pull(self, ticker: str) -> bool:
        return pull_complete(self.store, self.cfg.provider, ticker, self.sessions)

    # -- work ----------------------------------------------------------------

    def run_ticker(self, ticker: str, *, require_complete: bool = True) -> Outcome:
        t = ticker.upper()
        if self.up_to_date(t):
            return "skipped"
        complete = self.complete_in_pull(t)
        if require_complete and not complete:
            return "incomplete"
        ref = self._ref()
        market = None
        if ref is not None and self._wants_market(t):
            market = read_term_points(self.data_dir, ref)
            if market is None:
                log.warning("option_stats.no_market_ref", ticker=t, ref=ref)
        sig = source_signature(self.store, self.cfg.provider, t)
        res = ticker_stats(
            self.data_dir,
            t,
            cfg=self.cfg,
            cost=self.cost,
            etf=t in self.etfs,
            market_points=market,
            start=self.start,
            end=self.end,
        )
        _write_parquet(res.daily, daily_path(self.data_dir, t))
        _write_parquet(res.term, term_path(self.data_dir, t))
        self._merge_events(t, res.event_rows)
        self.manifest["tickers"][t] = {
            "sessions": res.sessions,
            "last_session": res.last_session,
            "events": res.events,
            "wall_seconds": res.wall_seconds,
            "source": sig,
            "code_hash": self.code,
            "iv_hash": self.iv_hash,
            "window": [str(self.start), str(self.end)],
            "market_ref": ref if market is not None else None,
            "complete_in_pull": complete,
        }
        self.manifest["code_hash"] = self.code
        _write_json(manifest_path(self.data_dir), self.manifest)
        log.info(
            "option_stats.ticker_done",
            ticker=t,
            sessions=res.sessions,
            events=res.events,
            seconds=res.wall_seconds,
        )
        return "computed"

    def _merge_events(self, ticker: str, ev: pd.DataFrame) -> None:
        p = stats_dir(self.data_dir) / "earnings_events.parquet"
        old = pd.read_parquet(p) if p.is_file() else pd.DataFrame(columns=list(EVENT_COLUMNS))
        old = old[old["ticker"] != ticker]
        parts = [x for x in (old, ev) if len(x)]
        df = pd.concat(parts, ignore_index=True) if parts else ev
        if len(df):
            df = df.sort_values(["ticker", "event_date"]).reset_index(drop=True)
        _write_parquet(df, p)

    def write_quarterly(self) -> Path:
        daily = {t: read_daily(self.data_dir, t) for t in self.manifest["tickers"]}
        quarters = None
        if self.membership is not None and len(self.membership):
            m = self.membership.copy()
            m["quarter"] = pd.to_datetime(m["quarter"]).dt.date
            quarters = {s: list(g["quarter"]) for s, g in m.groupby("symbol")}
        optliq = None
        if self.version:
            op = universe_dir(self.data_dir) / f"{self.version}.optliq.parquet"
            if op.is_file():
                optliq = pd.read_parquet(op)
        df = liquidity_quarterly(
            daily, self.sessions, cfg=self.cfg, quarters=quarters, optliq=optliq
        )
        p = stats_dir(self.data_dir) / "liquidity_quarterly.parquet"
        _write_parquet(df, p)
        return p

    def checkpoint_status(self) -> dict[str, dict[str, object]]:
        """Per checkpoint: tickers done (current code + complete in the pull) / total."""
        out: dict[str, dict[str, object]] = {}
        labels = sorted({r.checkpoint for r in self.rows}, key=lambda c: int(c[1:]))
        for cp in labels:
            size = int(cp[1:])
            names = [r.symbol for r in self.rows if r.position <= size]
            done = [t for t in names if self._done(t)]
            out[cp] = {
                "names": len(names),
                "done": len(done),
                "rows": sum(int(self.manifest["tickers"][t]["sessions"]) for t in done),
                "wall_seconds": round(
                    sum(float(self.manifest["tickers"][t]["wall_seconds"]) for t in done), 1
                ),
            }
        return out

    def record_checkpoints(self, now: dt.datetime) -> list[str]:
        """Write ``checkpoints.json``; returns checkpoints newly complete at *now*."""
        p = stats_dir(self.data_dir) / "checkpoints.json"
        try:
            old = json.loads(p.read_text())
        except (OSError, ValueError):
            old = {}
        new: list[str] = []
        for cp, st in self.checkpoint_status().items():
            rec = dict(old.get(cp) or {})
            rec |= {k: st[k] for k in ("names", "done", "rows", "wall_seconds")}
            if st["done"] == st["names"] and not rec.get("complete_at"):
                rec["complete_at"] = now.isoformat()
                new.append(cp)
            old[cp] = rec
        old["version"] = self.version
        _write_json(p, old)
        return new

    def pass_once(
        self,
        *,
        tickers: Iterable[str] | None = None,
        require_complete: bool = True,
        on_done: Callable[[str, Outcome], None] | None = None,
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for t in tickers or self.order:
            try:
                o = self.run_ticker(t, require_complete=require_complete)
            except Exception as exc:  # noqa: BLE001 - one bad name never stops the follower
                err = f"{type(exc).__name__}: {exc}"
                log.error("option_stats.ticker_failed", ticker=t, error=err)
                continue
            out[t] = o
            if on_done is not None:
                on_done(t, o)
        return out

    def follow(
        self,
        *,
        now: Callable[[], dt.datetime],
        sleep: Callable[[float], None],
        poll_seconds: float = 600.0,
        max_polls: int | None = None,
        on_event: Callable[[str], None] | None = None,
        tickers: Sequence[str] | None = None,
    ) -> int:
        """Poll until every one of *tickers* (default: the whole list) is done; returns polls.

        Each pass walks the names in priority order and computes those the pull has
        completed; *sleep* is called between passes (it may raise to stop early).
        """
        polls = 0
        names = list(tickers or self.order)
        while True:
            polls += 1
            res = self.pass_once(tickers=names)
            if any(v == "computed" for v in res.values()):
                self.write_quarterly()
            for cp in self.record_checkpoints(now()):
                st = self.checkpoint_status()[cp]
                done = max(1, int(st["done"]))  # type: ignore[call-overload]
                msg = (
                    f"{cp.upper()} stats complete: {st['done']} tickers, {st['rows']:,} daily "
                    f"rows, {st['wall_seconds']:.0f} s compute "
                    f"({float(st['wall_seconds']) / done:.1f} s/ticker)"  # type: ignore[arg-type]
                )
                log.info("option_stats.checkpoint_complete", checkpoint=cp, **st)
                if on_event is not None:
                    on_event(msg)
            if all(self._done(t) for t in names):
                return polls
            if max_polls is not None and polls >= max_polls:
                return polls
            sleep(poll_seconds)

    def _done(self, ticker: str) -> bool:
        e = self.manifest["tickers"].get(ticker.upper())
        return bool(e and e.get("code_hash") == self.code and e.get("complete_in_pull"))
