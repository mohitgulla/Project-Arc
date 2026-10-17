"""Pure feature formulas of the ML feature store (E27.1, D126).

Every function here takes input frames holding data dated **on or before** the day it
reports and returns numbers; nothing reads a file, a database, the network or the
clock. Both adapters (:mod:`arc.ml.features.adapters.history`,
:mod:`arc.ml.features.adapters.live`) build the same :class:`FeatureInputs` and call
:func:`compute_panel`, so a feature has exactly one formula (train/serve parity).

Two layers live here:

* **The v1 panel** (:func:`compute_panel` and the ``*_frame`` helpers): one row per
  (session, ticker), the registered features of :mod:`arc.ml.features.registry`.
  Every window is *window-local* (a value never depends on how much history precedes
  its window), so the live adapter's ~300-session read and the history adapter's
  2019→ read give the same number. That is why ATR14 here is the 14-session mean true
  range (not Wilder's recursion), drawdown uses a rolling 252-session high (not the
  all-time ``cummax``) and "sessions since the SPY label flipped" is capped.
* **The E7.18 loss-anatomy formulas** (:func:`price_state`, :func:`market_state`,
  :func:`event_features`, :func:`portfolio_state`, :func:`build_features` …), promoted
  from ``scripts/loss_anatomy_features.py`` unchanged; the script imports them from
  here (golden test ``tests/test_ml_anatomy_golden.py``). They describe one *book*'s
  trades and keep their original (path-dependent) definitions so the committed E7.18
  report is reproducible; the v1 panel does not use them.

Any edit to this file changes :data:`arc.ml.features.registry.FEATURE_SCHEMA_VERSION`.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import re
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from arc.features.regime import (
    V2_TREND_Z,
    label_vol_v2,
    vol_scaled_z,
)
from arc.features.vol import IV_LOOKBACK, MIN_IV_HISTORY

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

TRADING_DAYS = 252
#: Calendar days after a detected earnings reaction day at which the next one is
#: projected (history has no forward calendar; E7.18 used the same 91 days).
EARN_PROJECTION_DAYS = 91
#: "Sessions since the SPY trend label last flipped" is capped here (window-local).
FLIP_CAP = 120
HAR_WINDOW = 252
HAR_HORIZON = 21
PCT_WINDOW = 252
PCT_MIN_OBS = 120
CORR_WINDOW = 60
CLUSTER_CORR = 0.7

#: Columns of the per-ticker option input frame (the E7.16 ``option_stats/daily``
#: columns the panel reads; the live adapter fills the same names).
OPTION_INPUT_COLS: tuple[str, ...] = (
    "iv30", "iv60", "front_iv", "term_slope",
    "front_over_iv30", "skew_25d", "put25_iv", "call25_iv", "atm_spread_pct",
    "two_sided_pct", "oi_total", "oi_near_atm", "opt_volume", "put_call_oi", "put_call_vol",
)  # fmt: skip
OHLC_INPUT_COLS: tuple[str, ...] = ("open", "high", "low", "close")
EARN_SRC_NONE, EARN_SRC_PROJECTED, EARN_SRC_CALENDAR = 0, 1, 2


class FeatureInputs(BaseModel):
    """Everything :func:`compute_panel` reads; built by an adapter, never by compute.

    * ``ohlc``: per ticker, split-adjusted daily ``open high low close`` by session date.
    * ``opt``: per ticker, :data:`OPTION_INPUT_COLS` by session date (missing = NaN).
    * ``events``: past earnings reaction days ``ticker, event_date,
      realised_over_implied`` (detected from option history; the same table in both
      adapters).
    * ``upcoming``: a forward earnings calendar ``ticker, reaction_date, known_at``
      (live: Finnhub; history: empty). A row counts on day d only when ``known_at <= d``.
    * ``macro``: scheduled ``fomc`` / ``cpi`` / ``nfp`` release days (known in advance).
    * ``members``: point-in-time universe per quarter start (breadth, dispersion, corr).
    * ``etfs``: tickers with no earnings.
    * ``sessions``: the exchange session calendar covering the window and ~1 year after
      it (counts sessions to scheduled events).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra="forbid")

    ohlc: dict[str, pd.DataFrame]
    opt: dict[str, pd.DataFrame] = Field(default_factory=dict)
    events: pd.DataFrame = Field(
        default_factory=lambda: pd.DataFrame(
            columns=["ticker", "event_date", "realised_over_implied"]
        )
    )
    upcoming: pd.DataFrame = Field(
        default_factory=lambda: pd.DataFrame(columns=["ticker", "reaction_date", "known_at"])
    )
    macro: dict[str, list[dt.date]] = Field(default_factory=dict)
    members: dict[dt.date, frozenset[str]] = Field(default_factory=dict)
    etfs: frozenset[str] = frozenset()
    sessions: list[dt.date]
    market_ref: str = "SPY"


# ===========================================================================
# v1 panel: per-ticker price features
# ===========================================================================


def _close(px: pd.DataFrame) -> pd.Series:
    return px["close"].astype(float)


def _col(px: pd.DataFrame, name: str) -> pd.Series:
    if name in px:
        return px[name].astype(float)
    return pd.Series(np.nan, index=px.index, dtype=float)


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def atr14(px: pd.DataFrame, n: int = 14) -> pd.Series:
    """Mean true range of the last *n* sessions (window-local, unlike Wilder's ATR)."""
    c, h, lo = _close(px), _col(px, "high"), _col(px, "low")
    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1, skipna=False)
    return tr.rolling(n).mean()


def realised_vol(r: pd.Series, n: int) -> pd.Series:
    """Annualised close-to-close vol of the last *n* log returns (ddof 1)."""
    return r.rolling(n).std(ddof=1) * math.sqrt(TRADING_DAYS)


def parkinson_vol(px: pd.DataFrame, n: int) -> pd.Series:
    """Parkinson high/low range vol over *n* sessions, annualised."""
    hl = np.log(_col(px, "high") / _col(px, "low")) ** 2
    return np.sqrt(hl.rolling(n).mean() / (4.0 * math.log(2.0)) * TRADING_DAYS)


def _label_from_z(z: pd.Series, trend_z: float = V2_TREND_Z) -> pd.Series:
    return pd.Series(np.where(z >= trend_z, 1.0, np.where(z <= -trend_z, -1.0, 0.0)), z.index)


def trend_v2(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(v2 vol-scaled trend z, label +1 bull / 0 sideways / −1 bear) on *close*'s index."""
    c = close.dropna()
    c = c[c > 0]
    if len(c) < 2:  # noqa: PLR2004
        nan = pd.Series(np.nan, index=close.index)
        return nan, nan
    z = vol_scaled_z(c)
    z.index = c.index[len(c) - len(z) :]
    lab = _label_from_z(z)
    return z.reindex(close.index), lab.reindex(close.index)


def vol_state_v2(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """(own-history rv20 percentile 0..1, state −1 low / 0 mid / +1 high) per date.

    The live v2 vol read (:func:`arc.features.regime.label_vol_v2`); the percentile is
    NaN during its 252-value warm-up (the state then uses the fixed buckets).
    """
    c = close.dropna()
    c = c[c > 0]
    if len(c) < 22:  # noqa: PLR2004 - rv20 needs 21 closes
        nan = pd.Series(np.nan, index=close.index)
        return nan, nan
    v = label_vol_v2(c)
    code = {"low": -1.0, "mid": 0.0, "high": 1.0}
    pct = pd.Series(
        [np.nan if p is None else p / 100.0 for p in v.pct_rank], index=v.dates, dtype=float
    )
    state = pd.Series([code[str(s)] for s in v.state], index=v.dates, dtype=float)
    return pct.reindex(close.index), state.reindex(close.index)


def har_forecast(r: pd.Series, window: int = HAR_WINDOW, horizon: int = HAR_HORIZON) -> pd.Series:
    """HAR-RV forecast of the next *horizon* sessions' annualised vol, per date t.

    Regressors at s: ``RV_d = |r_s|·√252``, ``RV_w``/``RV_m`` = √(252·mean r²) over 5 / 22
    sessions; target at s = √(252·mean r²) over s+1..s+horizon. OLS with intercept on
    the last *window* rows whose target is complete by t (s ≤ t − horizon), so the fit
    never sees a return after t. Window-local; NaN when any row is incomplete or the
    forecast is ≤ 0.
    """
    r2 = r**2
    x = np.column_stack(
        [
            np.ones(len(r)),
            np.sqrt(r2 * TRADING_DAYS).to_numpy(),
            np.sqrt(r2.rolling(5).mean() * TRADING_DAYS).to_numpy(),
            np.sqrt(r2.rolling(22).mean() * TRADING_DAYS).to_numpy(),
        ]
    )
    fwd = r2.rolling(horizon).mean().shift(-horizon)
    y = np.sqrt(fwd * TRADING_DAYS).to_numpy()
    n = len(r)
    out = np.full(n, np.nan)
    first_t = window + horizon - 1
    if n <= first_t:
        return pd.Series(out, index=r.index)
    xw = np.lib.stride_tricks.sliding_window_view(x, window, axis=0)  # (n-w+1, 4, w)
    yw = np.lib.stride_tricks.sliding_window_view(y, window)  # (n-w+1, w)
    # window k covers s = k..k+w-1 and serves t = k + w - 1 + horizon
    ts = np.arange(first_t, n)
    ks = ts - horizon - window + 1
    xs, ys = xw[ks], yw[ks]
    ok = np.isfinite(xs).all(axis=(1, 2)) & np.isfinite(ys).all(axis=1) & np.isfinite(x[ts]).all(1)
    if ok.any():
        xtx = np.einsum("kiw,kjw->kij", xs[ok], xs[ok])
        xty = np.einsum("kiw,kw->ki", xs[ok], ys[ok])
        good = np.abs(np.linalg.det(xtx)) > 1e-12  # noqa: PLR2004
        beta = np.full((int(ok.sum()), 4), np.nan)
        beta[good] = np.linalg.solve(xtx[good], xty[good][..., None])[..., 0]
        f = np.einsum("ki,ki->k", x[ts[ok]], beta)
        out[ts[ok]] = np.where(f > 0, f, np.nan)
    return pd.Series(out, index=r.index)


def ticker_price_frame(px: pd.DataFrame, ref: pd.DataFrame | None) -> pd.DataFrame:
    """Group ``ticker_price`` per date of one ticker (*ref* = SPY's OHLC for beta)."""
    c = _close(px)
    r = log_returns(c)
    out = pd.DataFrame(index=px.index)
    for n in (1, 5, 20, 60):
        out[f"ret_{n}d"] = c / c.shift(n) - 1.0
    out["ret_252_21"] = c.shift(21) / c.shift(252) - 1.0
    atr = atr14(px)
    for n in (20, 50, 200):
        out[f"dist_ma{n}_atr"] = (c - c.rolling(n).mean()) / atr
    out["dd_252"] = c / c.rolling(252).max() - 1.0
    out["atr14_pct"] = atr / c
    out["gap_1d"] = _col(px, "open") / c.shift(1) - 1.0
    for n in (5, 20, 60):
        out[f"rv{n}"] = realised_vol(r, n)
        out[f"pk{n}"] = parkinson_vol(px, n)
    out["trend_z_v2"], out["trend_v2"] = trend_v2(c)
    out["vol_pct_v2"], out["vol_state_v2"] = vol_state_v2(c)
    if ref is not None and len(ref):
        rs = log_returns(_close(ref)).reindex(px.index)
        cov = r.rolling(CORR_WINDOW).cov(rs)
        var_s = rs.rolling(CORR_WINDOW).var()
        var_r = r.rolling(CORR_WINDOW).var()
        beta = cov / var_s
        out["beta_60"] = beta
        out["corr_60"] = cov / np.sqrt(var_s * var_r)
        out["idio_vol_60"] = np.sqrt((var_r - beta**2 * var_s).clip(lower=0.0) * TRADING_DAYS)
    else:
        out["beta_60"] = out["corr_60"] = out["idio_vol_60"] = np.nan
    out["_har_rv"] = har_forecast(r)
    return out


# ===========================================================================
# v1 panel: per-ticker option features
# ===========================================================================


def iv_rank_pct(
    iv30: pd.Series, lookback: int = IV_LOOKBACK, min_obs: int = MIN_IV_HISTORY
) -> tuple[pd.Series, pd.Series]:
    """(IV rank, IV percentile) per date over the last *lookback* IV30 **observations**.

    The live D55 rule (:func:`arc.features.vol.iv_rank` / ``iv_percentile``, the E7.16
    ``iv_rank_252`` / ``iv_pct_252`` columns): days without an IV30 are skipped, at least
    *min_obs* observations are required, a flat window has rank 0.5.
    """
    obs = iv30.dropna()
    v = obs.to_numpy(dtype=float)
    rank = np.full(len(v), np.nan)
    pct = np.full(len(v), np.nan)
    for i in range(len(v)):
        w = v[max(0, i - lookback + 1) : i + 1]
        if len(w) < min_obs:
            continue
        lo, hi, cur = w.min(), w.max(), w[-1]
        rank[i] = 0.5 if hi - lo <= 1e-12 else (cur - lo) / (hi - lo)  # noqa: PLR2004
        pct[i] = float((w[:-1] < cur).sum() / (len(w) - 1))
    return (
        pd.Series(rank, index=obs.index).reindex(iv30.index),
        pd.Series(pct, index=obs.index).reindex(iv30.index),
    )


def ticker_option_frame(opt: pd.DataFrame | None, price: pd.DataFrame) -> pd.DataFrame:
    """Group ``ticker_options`` per date of *price* (the ticker's price frame).

    OI is never carried forward: a day whose option rows have no open interest
    (``oi_total`` ≤ 0 / NaN, e.g. the ThetaData free tier) gets ``oi_missing = 1`` and
    NaN OI features (``null_policy: missing_indicator``).
    """
    idx = price.index
    o = (
        pd.DataFrame(index=idx, columns=list(OPTION_INPUT_COLS), dtype=float)
        if opt is None
        else opt.reindex(idx)
    )

    def g(c: str) -> pd.Series:
        return (
            pd.to_numeric(o[c], errors="coerce").astype(float)
            if c in o
            else pd.Series(np.nan, index=idx)
        )

    iv30 = g("iv30")
    out = pd.DataFrame(index=idx)
    out["iv30"] = iv30
    out["iv60"] = g("iv60")
    out["front_iv"] = g("front_iv")
    out["iv_rank"], out["iv_pct"] = iv_rank_pct(iv30)
    out["term_slope"] = g("term_slope")
    out["front_over_iv30"] = g("front_over_iv30")
    out["skew_25d"] = g("skew_25d")
    out["put_wing_25d"] = g("put25_iv") - iv30
    out["call_wing_25d"] = g("call25_iv") - iv30
    out["vrp20"] = iv30 - price["rv20"]
    out["iv30_over_har"] = iv30 / price["_har_rv"]
    out["atm_spread_pct"] = g("atm_spread_pct")
    out["two_sided_pct"] = g("two_sided_pct")
    oi_total = g("oi_total")
    missing = ~(oi_total > 0)
    out["oi_missing"] = missing.astype(float)
    out["oi_total"] = oi_total.where(~missing)
    out["oi_near_atm"] = g("oi_near_atm").where(~missing)
    out["put_call_oi"] = g("put_call_oi").where(~missing)
    vol = g("opt_volume")
    out["opt_volume"] = vol
    out["put_call_vol"] = g("put_call_vol")
    out["opt_vol_ratio_20"] = vol / vol.rolling(20).mean()
    return out


# ===========================================================================
# v1 panel: market features (one row per session)
# ===========================================================================


def trailing_pct(s: pd.Series, window: int = PCT_WINDOW, min_obs: int = PCT_MIN_OBS) -> pd.Series:
    """Share of the prior observations (≤ *window* − 1, NaNs dropped) strictly below today's.

    Same definition as :func:`arc.features.vol.iv_percentile`; NaN with fewer than
    *min_obs* observations in the window or no value today.
    """
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        cur = v[i]
        if not np.isfinite(cur):
            continue
        w = v[max(0, i - window + 1) : i + 1]
        w = w[np.isfinite(w)]
        if len(w) < min_obs:
            continue
        prior = w[:-1]
        out[i] = float((prior < cur).sum() / len(prior))
    return pd.Series(out, index=s.index)


def sessions_since_change(label: pd.Series, cap: int = FLIP_CAP) -> pd.Series:
    """Sessions since *label* last changed, capped at *cap* (window-local)."""
    v = label.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        if not np.isfinite(v[i]):
            continue
        n = 0
        while n < cap and i - n - 1 >= 0 and v[i - n - 1] == v[i]:
            n += 1
        if n < cap and (i - n - 1 < 0 or not np.isfinite(v[i - n - 1])):
            out[i] = np.nan  # the run reaches the start of the known labels: unknown
        else:
            out[i] = float(n)
    return pd.Series(out, index=label.index)


def member_mask(
    dates: Sequence[dt.date], tickers: Sequence[str], members: Mapping[dt.date, frozenset[str]]
) -> np.ndarray:
    """(dates × tickers) bool: ticker is in the PIT universe of the latest quarter ≤ d."""
    qs = sorted(members)
    out = np.zeros((len(dates), len(tickers)), dtype=bool)
    col = {t: j for j, t in enumerate(tickers)}
    for i, d in enumerate(dates):
        k = bisect.bisect_right(qs, d) - 1
        if k < 0:
            continue
        for t in members[qs[k]]:
            j = col.get(t)
            if j is not None:
                out[i, j] = True
    return out


def avg_pairwise_corr(rets: np.ndarray) -> float:
    """Mean off-diagonal correlation of the columns of *rets* (window × names, no NaN)."""
    w, n = rets.shape
    if n < 2 or w < 3:  # noqa: PLR2004
        return float("nan")
    sd = rets.std(axis=0, ddof=1)
    keep = sd > 0
    if keep.sum() < 2:  # noqa: PLR2004
        return float("nan")
    z = (rets[:, keep] - rets[:, keep].mean(axis=0)) / sd[keep]
    n = z.shape[1]
    total = float((z.sum(axis=1) ** 2).sum() / (w - 1))  # sum of every corr_ij incl. i = j
    return (total - n) / (n * (n - 1))


def market_frame(inputs: FeatureInputs, dates: Sequence[dt.date]) -> pd.DataFrame:
    """Group ``market`` per session in *dates* (needs SPY's OHLC + option rows)."""
    ref = inputs.market_ref
    spy = inputs.ohlc.get(ref)
    out = pd.DataFrame(index=list(dates))
    if spy is None or spy.empty:
        return out.assign(**{n: np.nan for n in MARKET_COLUMNS})
    c = _close(spy)
    m = pd.DataFrame(index=c.index)
    m["spy_trend_z"], m["spy_trend"] = trend_v2(c)
    m["spy_dist_ma50"] = c / c.rolling(50).mean() - 1.0
    m["spy_dist_ma200"] = c / c.rolling(200).mean() - 1.0
    m["spy_dd_252"] = c / c.rolling(252).max() - 1.0
    so = inputs.opt.get(ref)
    iv = (
        pd.to_numeric(so["iv30"], errors="coerce").astype(float).reindex(c.index)
        if so is not None and "iv30" in so
        else pd.Series(np.nan, index=c.index)
    )
    m["vix_proxy"] = iv
    m["vix_proxy_pct_252"] = trailing_pct(iv)
    m["spy_term_slope"] = (
        pd.to_numeric(so["term_slope"], errors="coerce").astype(float).reindex(c.index)
        if so is not None and "term_slope" in so
        else np.nan
    )
    m["spy_flip_age"] = sessions_since_change(m["spy_trend"])
    m = m.reindex(list(dates))
    cs = _cross_section(inputs, list(dates))
    return pd.concat([m, cs], axis=1)[list(MARKET_COLUMNS)]


def _cross_section(inputs: FeatureInputs, dates: list[dt.date]) -> pd.DataFrame:
    """Breadth (> MA50), 20d-return dispersion and 60d avg pairwise corr over PIT members."""
    names = sorted(t for t in inputs.ohlc if any(t in s for s in inputs.members.values()))
    out = pd.DataFrame(index=dates, columns=["breadth_ma50", "dispersion_20d", "avg_corr_60"])
    if not names or not dates:
        return out.astype(float)
    closes = pd.concat({t: _close(inputs.ohlc[t]) for t in names}, axis=1).sort_index()
    ma50 = closes.rolling(50).mean()
    ret20 = closes / closes.shift(20) - 1.0
    lr = np.log(closes / closes.shift(1))
    pos = {d: i for i, d in enumerate(closes.index)}
    mask = member_mask(dates, names, inputs.members)
    cv, mv, rv, lv = closes.to_numpy(), ma50.to_numpy(), ret20.to_numpy(), lr.to_numpy()
    res = np.full((len(dates), 3), np.nan)
    for i, d in enumerate(dates):
        k = pos.get(d)
        if k is None:
            continue
        mem = mask[i]
        ok = mem & np.isfinite(cv[k]) & np.isfinite(mv[k])
        if ok.any():
            res[i, 0] = float((cv[k][ok] > mv[k][ok]).mean())
        r = rv[k][mem & np.isfinite(rv[k])]
        if len(r) >= 2:  # noqa: PLR2004
            res[i, 1] = float(r.std(ddof=1))
        if k + 1 >= CORR_WINDOW:
            win = lv[k + 1 - CORR_WINDOW : k + 1][:, mem]
            full = np.isfinite(win).all(axis=0)
            res[i, 2] = avg_pairwise_corr(win[:, full])
    return pd.DataFrame(res, index=dates, columns=["breadth_ma50", "dispersion_20d", "avg_corr_60"])


MARKET_COLUMNS: tuple[str, ...] = (
    "spy_trend_z", "spy_trend", "spy_dist_ma50", "spy_dist_ma200", "spy_dd_252", "vix_proxy",
    "vix_proxy_pct_252", "spy_term_slope", "breadth_ma50", "dispersion_20d", "avg_corr_60",
    "spy_flip_age",
)  # fmt: skip


# ===========================================================================
# v1 panel: calendar + earnings
# ===========================================================================


def _pos(sessions: Sequence[dt.date], d: dt.date) -> int:
    """Index of the first session ≥ *d* (a non-session counts from the next session)."""
    return bisect.bisect_left(sessions, d)


def sessions_until(sessions: Sequence[dt.date], d: dt.date, event: dt.date) -> int:
    """Signed session count from *d* to *event* (0 = the event is session *d*)."""
    return _pos(sessions, event) - _pos(sessions, d)


def third_friday(year: int, month: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)


def calendar_frame(
    dates: Sequence[dt.date],
    macro: Mapping[str, Sequence[dt.date]],
    sessions: Sequence[dt.date],
) -> pd.DataFrame:
    """Group ``calendar`` per session (scheduled events are known in advance)."""
    rows = []
    sess = list(sessions)
    for d in dates:
        f: dict[str, float] = {}
        for k in ("fomc", "cpi", "nfp"):
            ds = sorted(macro.get(k, []))
            j = bisect.bisect_left(ds, d)
            f[f"sess_to_{k}"] = float(sessions_until(sess, d, ds[j])) if j < len(ds) else np.nan
        f["dow"] = float(d.weekday())
        nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        left = _pos(sess, nxt) - _pos(sess, d) - 1  # sessions after d in its month
        f["month_end_3s"] = float(left < 3)  # noqa: PLR2004
        tf = third_friday(d.year, d.month)
        if tf < d:
            y, mth = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)  # noqa: PLR2004
            tf = third_friday(y, mth)
        f["sess_to_opex"] = float(sessions_until(sess, d, tf))
        f["opex_week"] = float(d.isocalendar()[:2] == tf.isocalendar()[:2])
        rows.append(f)
    return pd.DataFrame(rows, index=list(dates), columns=list(CALENDAR_COLUMNS))


CALENDAR_COLUMNS: tuple[str, ...] = (
    "sess_to_fomc", "sess_to_cpi", "sess_to_nfp", "dow", "month_end_3s", "sess_to_opex",
    "opex_week",
)  # fmt: skip
EVENT_COLUMNS: tuple[str, ...] = (
    "sess_to_earn", "earn_src", "sess_since_earn", "last_earn_move_ratio",
)  # fmt: skip


def projected_reaction(last: dt.date, sessions: Sequence[dt.date]) -> dt.date | None:
    """The next reaction session projected from the last one (+91 calendar days)."""
    k = _pos(sessions, last + dt.timedelta(days=EARN_PROJECTION_DAYS))
    return sessions[k] if k < len(sessions) else None


def earnings_frame(
    dates: Sequence[dt.date],
    events: pd.DataFrame,
    upcoming: pd.DataFrame,
    sessions: Sequence[dt.date],
    *,
    etf: bool,
) -> pd.DataFrame:
    """Group ``event`` of one ticker per session.

    ``sess_to_earn`` counts sessions from d to the next **reaction session** (the first
    session whose close reflects the report): from the forward calendar when one is
    known by d (``earn_src = 2``), else projected from the last detected reaction day
    (``earn_src = 1``, may be negative when overdue), else NaN (``earn_src = 0``). The
    history adapter has no forward calendar, so it always projects; the live adapter
    projects whenever Finnhub has no date. ETFs: NaN, ``earn_src = 0``.
    """
    out = pd.DataFrame(np.nan, index=list(dates), columns=list(EVENT_COLUMNS))
    if etf:
        out["earn_src"] = float(EARN_SRC_NONE)
        return out
    sess = list(sessions)
    ev = events.sort_values("event_date") if len(events) else events
    ev_days = list(ev["event_date"]) if len(ev) else []
    ev_ratio = (
        pd.to_numeric(ev["realised_over_implied"], errors="coerce").to_numpy(dtype=float)
        if len(ev)
        else np.array([])
    )
    up = upcoming.sort_values("reaction_date") if len(upcoming) else upcoming
    up_days = list(up["reaction_date"]) if len(up) else []
    up_known = list(up["known_at"]) if len(up) else []
    for d in dates:
        j = bisect.bisect_right(ev_days, d) - 1
        last = ev_days[j] if j >= 0 else None
        cal = next((r for r, k in zip(up_days, up_known, strict=True) if r >= d and k <= d), None)
        src, nxt = EARN_SRC_NONE, None
        if cal is not None:
            src, nxt = EARN_SRC_CALENDAR, cal
        elif last is not None:
            p = projected_reaction(last, sess)
            if p is not None:
                src, nxt = EARN_SRC_PROJECTED, p
        out.loc[d, "earn_src"] = float(src)
        if nxt is not None:
            out.loc[d, "sess_to_earn"] = float(sessions_until(sess, d, nxt))
        if last is not None:
            out.loc[d, "sess_since_earn"] = float(sessions_until(sess, last, d))
            out.loc[d, "last_earn_move_ratio"] = float(ev_ratio[j])
    return out


# ===========================================================================
# v1 panel: portfolio state (one row, supplied by the caller)
# ===========================================================================


def portfolio_row(
    *,
    ticker: str,
    direction: str,
    book: pd.DataFrame,
    equity: float,
    equity_peak: float,
    groups: Mapping[str, str],
    returns: Mapping[str, pd.Series],
    day: dt.date,
) -> dict[str, float]:
    """Group ``portfolio`` for a candidate on *day* against the caller's open *book*.

    *book*: one row per open position with ``underlying``, ``direction``, ``risk``
    (max loss, $). *returns*: daily log returns per name (only rows ≤ *day* are read).
    ``same_cluster_open``: open names whose 60-session return correlation with *ticker*
    (window ending *day*, ≥ 40 overlapping sessions) exceeds 0.7.
    """
    n = len(book)
    same_dir = int((book["direction"] == direction).sum()) if direction != "neutral" and n else 0
    grp = groups.get(ticker, "unknown")
    clus = 0
    names = sorted(set(book["underlying"]) - {ticker}) if n else []
    if ticker in returns and names:
        a = returns[ticker]
        a = a[a.index <= day].tail(CORR_WINDOW)
        for nm in names:
            if nm not in returns:
                continue
            b = returns[nm].reindex(a.index)
            ok = a.notna() & b.notna()
            if ok.sum() >= 40 and float(np.corrcoef(a[ok], b[ok])[0, 1]) > CLUSTER_CORR:  # noqa: PLR2004
                clus += 1
    return {
        "open_positions": float(n),
        "open_risk_pct": float(book["risk"].sum()) / equity if n and equity else 0.0,
        "open_same_dir_share": same_dir / n if n else 0.0,
        "same_name_open": float((book["underlying"] == ticker).sum()) if n else 0.0,
        "same_cluster_open": float(clus),
        "same_sector_open": float(sum(groups.get(u, "?") == grp for u in book["underlying"]))
        if n
        else 0.0,
        "drawdown_from_peak": equity / equity_peak - 1.0 if equity_peak > 0 else 0.0,
    }


# ===========================================================================
# v1 panel assembly
# ===========================================================================

ID_COLUMNS: tuple[str, ...] = ("date", "ticker", "pit_member", "is_etf")


def compute_panel(
    inputs: FeatureInputs,
    dates: Sequence[dt.date],
    tickers: Iterable[str],
    *,
    columns: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """One row per (session in *dates*, ticker with a close that session).

    Columns: :data:`ID_COLUMNS` then *columns* (default: every panel feature of the
    registry, in registry order). Reads only input rows dated ≤ each row's date.
    """
    from arc.ml.features.registry import panel_feature_names

    cols = list(columns or panel_feature_names())
    dates = sorted(dates)
    if not dates:
        return pd.DataFrame(columns=[*ID_COLUMNS, *cols])
    lo, hi = dates[0], dates[-1]
    dset = set(dates)
    mkt = market_frame(inputs, dates)
    cal = calendar_frame(dates, inputs.macro, inputs.sessions)
    ref = inputs.ohlc.get(inputs.market_ref)
    qs = sorted(inputs.members)
    ev_by = {t: g for t, g in inputs.events.groupby("ticker")} if len(inputs.events) else {}
    up_by = {t: g for t, g in inputs.upcoming.groupby("ticker")} if len(inputs.upcoming) else {}
    empty_ev = inputs.events.iloc[0:0]
    empty_up = inputs.upcoming.iloc[0:0]
    frames: list[pd.DataFrame] = []
    for t in sorted(set(tickers)):
        px = inputs.ohlc.get(t)
        if px is None or px.empty:
            continue
        px = px[[c for c in OHLC_INPUT_COLS if c in px]].sort_index()
        px = px[px.index <= hi]
        p = ticker_price_frame(px, ref)
        o = ticker_option_frame(inputs.opt.get(t), p)
        days = [d for d in p.index if d >= lo and d in dset and pd.notna(px.loc[d, "close"])]
        if not days:
            continue
        e = earnings_frame(
            days,
            ev_by.get(t, empty_ev),
            up_by.get(t, empty_up),
            inputs.sessions,
            etf=t in inputs.etfs,
        )
        f = pd.concat([p.loc[days], o.loc[days], e], axis=1)
        f = f.join(mkt, how="left").join(cal, how="left")
        f["date"] = days
        f["ticker"] = t
        f["is_etf"] = t in inputs.etfs
        f["pit_member"] = [
            bool(qs) and (k := bisect.bisect_right(qs, d) - 1) >= 0 and t in inputs.members[qs[k]]
            for d in days
        ]
        frames.append(f.reindex(columns=[*ID_COLUMNS, *cols]))
        if progress is not None:
            progress(t)
    if not frames:
        return pd.DataFrame(columns=[*ID_COLUMNS, *cols])
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["date", "ticker"], kind="mergesort").reset_index(drop=True)
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype(float)
    return out


# ===========================================================================
# E7.18 loss-anatomy formulas (promoted verbatim from scripts/loss_anatomy_features.py)
# ===========================================================================

SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")
OPTION_COLS = (
    "iv30",
    "iv_rank_252",
    "iv_pct_252",
    "term_slope",
    "front_over_iv30",
    "skew_25d",
    "rv20",
    "rv60",
    "vrp20",
    "atm_spread_pct",
    "two_sided_pct",
    "oi_near_atm",
    "opt_volume",
    "put_call_oi",
    "put_call_vol",
    "d9_pass",
)
#: Close-at-DTE of config/exits.yaml (planned exit = expiration - 7 calendar days).
PLANNED_EXIT_DTE = 7

_ETF_RULES: tuple[tuple[str, str], ...] = (
    (
        r"\b(ultra|2x|3x|leveraged|bull 3x|bear 3x|daily .*bull|daily .*bear|-1x|inverse)",
        "leveraged",
    ),
    (r"\b(vix|volatility)\b", "vol"),
    (r"\b(gold|silver|oil|commodity|natural gas|metals|uranium|copper|agricultur)", "commodity"),
    (
        r"\b(treasury|bond|aggregate|corporate|high yield|municipal|tips|income|ltd duration)",
        "bond",
    ),
    (
        r"\b(emerging|international|china|japan|europe|brazil|india|eafe|developed|"
        r"germany|korea|taiwan|mexico|latin|world ex|ex-us|ex us|frontier)",
        "international",
    ),
    (
        r"\b(select sector|sector|semiconductor|biotech|bank|energy|technology|health|"
        r"financial|real estate|reit|utilities|industrial|materials|consumer|homebuilder|"
        r"retail|aerospace|transport|internet|software|cyber|clean|solar|innovation|"
        r"genomic|metals & mining|oil & gas|regional)",
        "sector",
    ),
)


def etf_class(symbol: str, name: str) -> str:
    """Asset-class tag of an ETF from its fund name (first matching rule wins)."""
    n = name.lower()
    for pat, label in _ETF_RULES:
        if re.search(pat, n):
            return label
    _ = symbol
    return "broad_equity"


def sic_sector(sic: int | None) -> str:
    """A coarse sector from a 4-digit SIC code (SEC Division structure, tech carved out)."""
    if sic is None:
        return "unknown"
    s = int(sic)
    tech = {
        3570,
        3571,
        3572,
        3575,
        3576,
        3577,
        3578,
        3579,
        3661,
        3663,
        3669,
        3670,
        3672,
        3674,
        3675,
        3677,
        3678,
        3679,
        3825,
        3826,
        7370,
        7371,
        7372,
        7373,
        7374,
        7377,
    }
    if s in tech:
        return "technology"
    if 2830 <= s <= 2836 or 3840 <= s <= 3851 or 8000 <= s <= 8099 or s == 5122:
        return "health"
    if s in (4812, 4813, 4822, 4832, 4833, 4841, 4899, 7812, 7819, 7822, 7900, 7990, 2711):
        return "communication"
    if 1000 <= s <= 1499 or 2900 <= s <= 2999 or s in (4610, 4612, 4613, 4922, 4923):
        return "energy_materials"
    if 7300 <= s <= 7399:
        return "business_services"  # payments, platforms, data (7389 NEC)
    if 7800 <= s <= 7849:
        return "communication"
    if 4900 <= s <= 4999:
        return "utilities"
    if 6000 <= s <= 6411 or 6500 <= s <= 6799:
        return "financials_realestate" if s < 6500 or s >= 6700 else "real_estate"
    if 5000 <= s <= 5999 or 7000 <= s <= 7299 or s in (3711, 3714, 3751, 2300, 2320, 3021):
        return "consumer"
    if 2000 <= s <= 2199:
        return "consumer_staples"
    if 1500 <= s <= 1799 or 3400 <= s <= 3569 or 3580 <= s <= 3829 or 4000 <= s <= 4799:
        return "industrials"
    if 2200 <= s <= 3399:
        return "materials_mfg"
    return "other"


# ---------------------------------------------------------------------------
# Price / market state (causal series, looked up at the entry day)
# ---------------------------------------------------------------------------


def price_state(px: pd.DataFrame) -> pd.DataFrame:
    """Per-date causal price features of one ticker."""
    c = px["close"].astype(float)
    r = np.log(c / c.shift(1))
    sd20 = r.rolling(20).std()
    out = pd.DataFrame(index=px.index)
    for n in (5, 20, 60):
        out[f"ret_{n}d"] = c / c.shift(n) - 1.0
    for n in (20, 50, 200):
        out[f"dist_ma{n}"] = c / c.rolling(n).mean() - 1.0
    rv20 = sd20 * np.sqrt(252)
    out["rv20_pctile"] = rv20.rolling(252, min_periods=60).rank(pct=True)
    o = px["open"].astype(float) if "open" in px else pd.Series(np.nan, index=px.index)
    out["gap_pct"] = o / c.shift(1) - 1.0
    big = (r.abs() > 2.0 * sd20.shift(1)).to_numpy()
    since, last = [], None
    for i, b in enumerate(big):
        if b:
            last = i
        since.append(np.nan if last is None else float(i - last))
    out["days_since_2sd"] = since
    out["dd_from_high"] = c / c.cummax() - 1.0
    return out


def market_state(
    prices: Mapping[str, pd.DataFrame],
    opt: Mapping[str, pd.DataFrame],
    breadth_names: Iterable[str],
) -> pd.DataFrame:
    """One row per session: SPY trend, VIX proxy (SPY iv30), credit, breadth, dispersion."""
    spy = prices["SPY"]["close"].astype(float)
    m = pd.DataFrame(index=spy.index)
    for n in (20, 50, 200):
        m[f"spy_dist_ma{n}"] = spy / spy.rolling(n).mean() - 1.0
    m["spy_ret_20d"] = spy / spy.shift(20) - 1.0
    m["spy_dd_from_high"] = spy / spy.cummax() - 1.0
    so = opt["SPY"].reindex(m.index)
    iv = so["iv30"].astype(float)
    m["vix_proxy"] = iv
    m["vix_proxy_pctile"] = iv.rolling(252, min_periods=60).rank(pct=True)
    m["vix_proxy_chg_5d"] = iv - iv.shift(5)
    m["spy_term_slope"] = so["term_slope"].astype(float)
    r = np.log(spy / spy.shift(1))
    m["spy_rv_minus_iv"] = r.rolling(20).std() * np.sqrt(252) - iv
    if "HYG" in prices and "TLT" in prices:
        ratio = (prices["HYG"]["close"] / prices["TLT"]["close"]).reindex(m.index)
        m["credit_hyg_tlt_20d"] = ratio / ratio.shift(20) - 1.0
    above, total = pd.Series(0.0, index=m.index), pd.Series(0.0, index=m.index)
    for t in breadth_names:
        if t not in prices:
            continue
        c = prices[t]["close"].astype(float)
        ma = c.rolling(50).mean()
        ok = ma.notna()
        above = above.add(((c > ma) & ok).astype(float).reindex(m.index), fill_value=0.0)
        total = total.add(ok.astype(float).reindex(m.index), fill_value=0.0)
    m["breadth_above_ma50"] = above / total.replace(0.0, np.nan)
    secs = [
        (prices[t]["close"] / prices[t]["close"].shift(20) - 1.0).reindex(m.index).rename(t)
        for t in SECTOR_ETFS
        if t in prices
    ]
    if secs:
        m["sector_dispersion_20d"] = pd.concat(secs, axis=1).std(axis=1)
    # SPY daily history starts 2019-09-03, so the 200-day MA is undefined before
    # mid-June 2020; there the label falls back to the 50-day MA alone (else the
    # Feb-Mar 2020 crash would read "mixed").
    d50, d200 = m["spy_dist_ma50"], m["spy_dist_ma200"]
    m["spy_trend"] = np.where(
        d50.isna(),
        "unknown",
        np.where(
            d200.isna(),
            np.where(d50 > 0, "up", "down"),
            np.where(
                (d50 > 0) & (d200 > 0),
                "up",
                np.where((d50 < 0) & (d200 < 0), "down", "mixed"),
            ),
        ),
    )
    m["vix_bucket"] = pd.cut(
        m["vix_proxy"],
        [0, 0.15, 0.20, 0.30, np.inf],
        labels=["<15", "15-20", "20-30", ">=30"],
        right=False,
    ).astype(str)
    return m


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _sessions_between(sessions: list[dt.date], a: dt.date, d: dt.date) -> int:
    """Sessions strictly after *a* up to and including *d* (signed)."""
    import bisect

    i, j = bisect.bisect_right(sessions, a), bisect.bisect_right(sessions, d)
    return j - i


def event_features(
    t: pd.DataFrame,
    earnings: pd.DataFrame,
    macro: Mapping[str, list[dt.date]],
    sessions: list[dt.date],
) -> pd.DataFrame:
    """Earnings and macro columns per trade (entry-time + ``post_*`` hold descriptors)."""
    by_t: dict[str, pd.DataFrame] = {
        k: g.sort_values("event_date") for k, g in earnings.groupby("ticker")
    }
    rows = []
    for r in t.itertuples():
        e0: dt.date = r.entry_date
        planned = r.expiration - dt.timedelta(days=PLANNED_EXIT_DTE)
        f: dict[str, object] = {}
        g = by_t.get(r.underlying)
        if g is not None and not r.etf:
            past = g[g["event_date"] <= e0]
            last = past["event_date"].iloc[-1] if len(past) else None
            f["days_since_earn"] = (e0 - last).days if last else np.nan
            f["days_to_earn_proj"] = ((last + dt.timedelta(days=91)) - e0).days if last else np.nan
            f["earn_proj_in_window"] = (
                bool(last and e0 < last + dt.timedelta(days=91) <= planned) if last else None
            )
            inside = g[(g["event_date"] > e0) & (g["event_date"] <= r.exit_day)]
            f["post_earn_in_hold"] = bool(len(inside))
            if len(inside):
                f["post_earn_implied_move"] = float(inside["implied_move"].iloc[0])
                f["post_earn_realised_move"] = float(inside["realised_move"].iloc[0])
        for k in ("fomc", "cpi", "nfp"):
            ds = macro.get(k, [])
            f[f"{k}_in_window"] = any(e0 < d <= planned for d in ds)
            f[f"{k}_within_2s"] = any(
                0 < _sessions_between(sessions, e0, d) <= 2 for d in ds if d > e0
            )
            f[f"post_{k}_in_hold"] = any(e0 < d <= r.exit_day for d in ds)
        rows.append(f)
    return pd.DataFrame(rows, index=t.index)


# ---------------------------------------------------------------------------
# Portfolio state at entry
# ---------------------------------------------------------------------------


def portfolio_state(
    t: pd.DataFrame,
    equity: pd.Series,
    prices: Mapping[str, pd.DataFrame],
    groups: Mapping[str, str],
    corr_cut: float = 0.7,
) -> pd.DataFrame:
    """Open book at each entry (positions with entry < day < exit, plus same-day opens).

    ``same_cluster_open``: open names whose 60-session log-return correlation with the
    entry name (window ending at the entry day) exceeds *corr_cut*.
    """
    rets = {k: np.log(v["close"] / v["close"].shift(1)) for k, v in prices.items()}
    rows = []
    eq_prev = equity.shift(1).fillna(equity.iloc[0])
    for r in t.itertuples():
        d = r.entry_date
        ob = t[(t["entry_date"] < d) & (t["exit_day"] > d)]
        today = t[(t["entry_date"] == d) & (t.index != r.Index)]
        eq = float(eq_prev.get(d, equity.iloc[0]))
        same_dir = int((ob["direction"] == r.direction).sum()) if r.direction != "neutral" else 0
        grp = groups.get(r.underlying, "unknown")
        names = sorted(set(ob["underlying"]) - {r.underlying})
        clus = 0
        if r.underlying in rets and names:
            a = rets[r.underlying]
            a = a[a.index <= d].tail(60)
            for n in names:
                if n not in rets:
                    continue
                b = rets[n].reindex(a.index)
                ok = a.notna() & b.notna()
                if ok.sum() >= 40 and float(np.corrcoef(a[ok], b[ok])[0, 1]) > corr_cut:
                    clus += 1
        rows.append(
            {
                "open_positions": len(ob),
                "open_risk_pct": float(ob["risk"].sum()) / eq if eq else np.nan,
                "open_same_dir_share": same_dir / len(ob) if len(ob) else 0.0,
                "same_name_open": int((ob["underlying"] == r.underlying).sum()),
                "same_group_open": int(sum(groups.get(n, "?") == grp for n in ob["underlying"])),
                "same_cluster_open": clus,
                "new_risk_today_pct": float(today["risk"].sum() + r.risk) / eq if eq else np.nan,
                "risk_pct_equity": float(r.risk) / eq if eq else np.nan,
                "drawdown_at_entry": float(eq / equity[equity.index < d].cummax().iloc[-1] - 1.0)
                if (equity.index < d).any()
                else 0.0,
            }
        )
    return pd.DataFrame(rows, index=t.index)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def _lookup(frame: pd.DataFrame, day: dt.date) -> pd.Series:
    """Last row of *frame* dated <= *day* (point-in-time), or an all-NaN row."""
    sub = frame[frame.index <= day]
    return sub.iloc[-1] if len(sub) else pd.Series(np.nan, index=frame.columns)


def trade_columns(t: pd.DataFrame) -> pd.DataFrame:
    out = t.copy()
    out["R"] = out["pnl_unit"] / out["max_loss_unit"]
    out["R_mid"] = (out["pnl_mid"] / out["contracts"]) / out["max_loss_unit"]
    out["loser_05R"] = out["R"] <= -0.5
    spec = out["spec"].astype(str)
    out["delta"] = [float(m[1]) / 100 if (m := re.search(r"_d(\d+)", s)) else np.nan for s in spec]
    out["width"] = out["credit_width"].fillna(out["debit_width"])
    out["dte_entry"] = [
        (e - d).days for e, d in zip(out["expiration"], out["entry_date"], strict=True)
    ]
    out["spread_pct_premium"] = (
        (out["entry_net"] - out["entry_net_mid"]).abs() / 0.25 / out["entry_net_mid"].abs()
    )
    return out


def build_features(
    t: pd.DataFrame,
    equity: pd.Series,
    *,
    prices: Mapping[str, pd.DataFrame],
    opt: Mapping[str, pd.DataFrame],
    market: pd.DataFrame,
    earnings: pd.DataFrame,
    macro: Mapping[str, list[dt.date]],
    groups: Mapping[str, str],
) -> pd.DataFrame:
    """One row per trade of one book (see the module docstring for the PIT contract)."""
    t = trade_columns(t)
    sessions = sorted(market.index)
    pstate = {k: price_state(v) for k, v in prices.items() if k in set(t["underlying"])}
    rows = []
    for r in t.itertuples():
        d = r.entry_date
        f: dict[str, object] = {}
        if r.underlying in pstate:
            f |= _lookup(pstate[r.underlying], d).add_prefix("px_").to_dict()
        if r.underlying in opt:
            f |= _lookup(opt[r.underlying], d).add_prefix("opt_").to_dict()
        f |= (
            _lookup(market.drop(columns=["spy_trend", "vix_bucket"]), d)
            .add_prefix("mkt_")
            .to_dict()
        )
        mrow = market[market.index <= d]
        f["mkt_spy_trend"] = mrow["spy_trend"].iloc[-1] if len(mrow) else "unknown"
        f["mkt_vix_bucket"] = mrow["vix_bucket"].iloc[-1] if len(mrow) else "nan"
        rows.append(f)
    feat = pd.DataFrame(rows, index=t.index)
    ev = event_features(t, earnings, macro, sessions)
    ps = portfolio_state(t, equity, prices, groups)
    out = pd.concat([t, feat, ev, ps], axis=1)
    out["group"] = [groups.get(u, "unknown") for u in out["underlying"]]
    out["weekday"] = [d.strftime("%a") for d in out["entry_date"]]
    out["month"] = [d.month for d in out["entry_date"]]
    out["half"] = [f"{d.year}H{1 if d.month <= 6 else 2}" for d in out["entry_date"]]
    out["gap_day"] = out["px_gap_pct"].abs() >= 0.02
    return out


def regime_change_soon(market: pd.DataFrame, n: int = 10) -> pd.Series:
    """Descriptive (uses the future label): SPY trend label changes within *n* sessions."""
    lab = market["spy_trend"]
    chg = lab.ne(lab.shift(-1)).astype(int)[::-1].rolling(n, min_periods=1).max()[::-1]
    return chg.astype(bool).rename("post_regime_change_10s")
