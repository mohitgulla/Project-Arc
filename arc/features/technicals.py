"""Deterministic daily price indicators for one underlying (E16.2, D76/D78).

A small, fixed set of chart facts computed from the daily OHLC bars the regime
step already fetches: moving averages, Wilder RSI and ATR, ATR stretch versus
SMA20, the 20-day and 52-week range, relative strength versus SPY and the
sector ETF, a Bollinger-in-Keltner squeeze flag, and the prior-day levels.

They are context only. Research sees one code-rendered ``tech …`` segment
(``personas.research_technicals``); the gate never reads them (import-linter).
Pure: no network, no LLM, no clock. Every value uses bars dated on or before
``as_of`` only, and a field whose lookback is not met is ``None`` (its reason is
in ``missing``); nothing is filled in.

Conventions: every ``pct_*`` / ``ret*`` / ``rs_*`` / ``gap_pct`` / ``*_pct`` /
``bb_width20`` value is a fraction (``0.023`` = 2.3 %); ``*_atr`` values are in
ATR14 units; prices are in the bars' currency.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - used at runtime in the pydantic model
import math
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from arc.features._series import truncate_frame
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pandas as pd

__all__ = [
    "ATR_PERIOD",
    "MIN_TECH_BARS",
    "RSI_PERIOD",
    "AntiChaseRule",
    "StretchVerdict",
    "TechnicalFeatures",
    "compute_technicals",
    "is_stretched",
    "session_vwap",
    "squeeze_series",
    "vwap_stretch_atr",
    "wilder_atr",
    "wilder_rsi",
]

RSI_PERIOD = 14
ATR_PERIOD = 14
#: Fewer bars than this: no technicals at all (RSI14 and ATR14 need 15 bars).
MIN_TECH_BARS = ATR_PERIOD + 1
BB_PERIOD = 20
BB_SIGMAS = 2.0
KELTNER_PERIOD = 20
KELTNER_ATR_MULT = 1.5
YEAR_SESSIONS = 252
RS_WINDOWS = (20, 60)
IMPLIED_MOVE_SESSIONS = 20

RangePosition = Literal["above", "inside", "below"]


class IntradayBar(Protocol):
    """An OHLCV bar (e.g. ``arc.data.base.HistoryBar``) for the session VWAP."""

    @property
    def timestamp(self) -> dt.datetime: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...
    @property
    def volume(self) -> float: ...


class TechnicalFeatures(BaseModel):
    """Daily technical indicators for one ticker as of one session close."""

    model_config = ConfigDict(extra="forbid")

    as_of: dt.date = Field(..., description="Session date (ET); bars on or before it only")
    bar_date: dt.date = Field(..., description="Date of the last bar used (<= as_of)")
    close: float
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    pct_vs_sma20: float | None = Field(None, description="close / SMA20 - 1")
    pct_vs_sma50: float | None = None
    pct_vs_sma200: float | None = None
    sma50_gt_sma200: bool | None = None
    sma50_slope_20d: float | None = Field(None, description="SMA50 / SMA50 20 sessions ago - 1")
    rsi14: float | None = Field(None, ge=0.0, le=100.0, description="Wilder RSI(14)")
    atr14: float | None = Field(None, ge=0.0, description="Wilder ATR(14) of the true range")
    atr14_pct: float | None = Field(None, description="ATR14 / close")
    stretch_atr: float | None = Field(None, description="(close - SMA20) / ATR14")
    high20: float | None = Field(None, description="Highest high of the last 20 sessions")
    low20: float | None = Field(None, description="Lowest low of the last 20 sessions")
    dist_high20_atr: float | None = Field(None, description="(high20 - close) / ATR14")
    dist_low20_atr: float | None = Field(None, description="(close - low20) / ATR14")
    pct_from_52w_high: float | None = Field(None, description="close / 252-session high - 1")
    pct_from_52w_low: float | None = Field(None, description="close / 252-session low - 1")
    ret5d: float | None = Field(None, description="close / close 5 sessions ago - 1")
    implied_move_20d: float | None = Field(None, description="close * iv30 * sqrt(20/252)")
    realised_atr_move_20d: float | None = Field(None, description="ATR14 * sqrt(20)")
    implied_vs_atr: float | None = Field(
        None, description="implied_move_20d / realised_atr_move_20d"
    )
    rs_spy_20d: float | None = Field(None, description="20-session return minus SPY's")
    rs_spy_60d: float | None = Field(None, description="60-session return minus SPY's")
    sector_etf: str | None = Field(None, description="The sector ETF behind rs_sector_20d")
    rs_sector_20d: float | None = Field(None, description="20-session return minus the ETF's")
    bb_width20: float | None = Field(None, description="Bollinger(20, 2 sd) width / SMA20")
    squeeze_on: bool | None = Field(
        None, description="Bollinger(20, 2 sd) entirely inside Keltner(20, 1.5 ATR20)"
    )
    squeeze_days: int | None = Field(None, ge=0, description="Consecutive sessions on; 0 = off")
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    gap_pct: float | None = Field(None, description="Today's open / previous close - 1")
    close_vs_prev_range: RangePosition | None = None
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Building blocks (pure; numpy arrays in, floats out)
# ---------------------------------------------------------------------------


def wilder_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI of the last close: seed = simple mean of the first *period* moves.

    A series with no up and no down move (flat) is 50; no down moves is 100.
    ``None`` with fewer than ``period + 1`` closes.
    """
    if len(closes) < period + 1:
        return None
    diff = np.diff(closes.astype(float))
    gains = np.clip(diff, 0.0, None)
    losses = np.clip(-diff, 0.0, None)
    avg_g = float(gains[:period].mean())
    avg_l = float(losses[:period].mean())
    for g, lo in zip(gains[period:], losses[period:], strict=True):
        avg_g = (avg_g * (period - 1) + float(g)) / period
        avg_l = (avg_l * (period - 1) + float(lo)) / period
    if avg_l <= 0.0:
        return 50.0 if avg_g <= 0.0 else 100.0
    rs = avg_g / avg_l
    return float(min(100.0, max(0.0, 100.0 - 100.0 / (1.0 + rs))))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True range of bars 1..n-1 (bar 0 has no previous close)."""
    prev = close[:-1]
    h, lo = high[1:], low[1:]
    return np.maximum.reduce([h - lo, np.abs(h - prev), np.abs(lo - prev)])


def wilder_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ATR_PERIOD
) -> float | None:
    """Wilder ATR of the last bar: seed = mean of the first *period* true ranges.

    ``None`` with fewer than ``period + 1`` bars.
    """
    if len(close) < period + 1:
        return None
    tr = true_range(high, low, close)
    atr = float(tr[:period].mean())
    for x in tr[period:]:
        atr = (atr * (period - 1) + float(x)) / period
    return max(0.0, atr)


def squeeze_series(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Per-bar squeeze state (``True`` = on); bars without a full lookback are ``False``.

    On when the Bollinger band (SMA20 +/- 2 population sd) sits entirely inside the
    Keltner channel (SMA20 +/- 1.5 x the 20-bar simple mean true range). Both share
    the SMA20 midline, so the test is ``2 sd < 1.5 x ATR20``.
    """
    n = len(close)
    out = np.zeros(n, dtype=bool)
    if n < KELTNER_PERIOD + 1:
        return out
    tr = true_range(high, low, close)  # tr[i] is bar i + 1
    for i in range(KELTNER_PERIOD, n):
        window = close[i - BB_PERIOD + 1 : i + 1]
        sd = float(window.std(ddof=0))
        atr20 = float(tr[i - KELTNER_PERIOD : i].mean())
        out[i] = BB_SIGMAS * sd < KELTNER_ATR_MULT * atr20
    return out


def _ret(closes: pd.Series, n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    start = float(closes.iloc[-n - 1])
    return float(closes.iloc[-1]) / start - 1.0 if start > 0 else None


def _relative(own: pd.Series, ref: pd.Series | None, n: int) -> float | None:
    """*n*-session return of *own* minus *ref*'s over the same dates (both on as_of's bar)."""
    if ref is None or ref.empty or own.empty:
        return None
    joined = own.to_frame("own").join(ref.rename("ref"), how="inner")
    if joined.empty or joined.index[-1] != own.index[-1]:
        return None  # the reference has no bar on our last date
    a, b = _ret(joined["own"], n), _ret(joined["ref"], n)
    return None if a is None or b is None else a - b


def _sma(closes: np.ndarray, n: int) -> float | None:
    return float(closes[-n:].mean()) if len(closes) >= n else None


def _pct(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b <= 0 else a / b - 1.0


def _div(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b <= 0 else a / b


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_technicals(
    ohlc: pd.DataFrame,
    as_of: dt.date,
    *,
    iv30: float | None = None,
    benchmark: pd.Series | None = None,
    sector: pd.Series | None = None,
    sector_etf: str | None = None,
) -> TechnicalFeatures | None:
    """Indicators from daily *ohlc* (columns ``open high low close``, indexed by date).

    *benchmark* / *sector* are daily close series (SPY and the sector ETF). Every
    input is truncated to *as_of* first. ``None`` with fewer than
    :data:`MIN_TECH_BARS` bars (the caller records a warning).
    """
    df = truncate_frame(ohlc, as_of)
    if len(df) < MIN_TECH_BARS:
        return None
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    closes = df["close"].astype(float)
    n = len(c)
    close = float(c[-1])
    missing: list[str] = []

    def need(field: str, bars: int) -> bool:
        if n >= bars:
            return True
        missing.append(f"{field}: need {bars} bars, have {n}")
        return False

    sma20 = _sma(c, 20) if need("sma20", 20) else None
    sma50 = _sma(c, 50) if need("sma50", 50) else None
    sma200 = _sma(c, 200) if need("sma200", 200) else None
    slope = None
    if need("sma50_slope_20d", 70):
        slope = _pct(sma50, float(c[-70:-20].mean()))
    rsi = wilder_rsi(c)
    atr = wilder_atr(h, lo, c)
    atr_pos = atr if atr is not None and atr > 0 else None
    if atr is not None and atr_pos is None:
        missing.append("stretch/dist ATR: ATR14 is 0")

    high20 = low20 = None
    if need("high20/low20", 20):
        high20, low20 = float(h[-20:].max()), float(lo[-20:].min())
    hi52 = lo52 = None
    if need("52w range", YEAR_SESSIONS):
        hi52, lo52 = float(h[-YEAR_SESSIONS:].max()), float(lo[-YEAR_SESSIONS:].min())

    implied = None
    if iv30 is not None and iv30 > 0 and math.isfinite(iv30):
        implied = close * iv30 * math.sqrt(IMPLIED_MOVE_SESSIONS / YEAR_SESSIONS)
    else:
        missing.append("implied_move_20d: no iv30")
    atr_move = atr * math.sqrt(IMPLIED_MOVE_SESSIONS) if atr is not None else None

    rs: dict[int, float | None] = {w: _relative(closes, benchmark, w) for w in RS_WINDOWS}
    if benchmark is None:
        missing.append("rs_spy: no benchmark series")
    rs_sector = _relative(closes, sector, 20) if sector_etf else None

    sq = squeeze_series(h, lo, c)
    squeeze_on = squeeze_days = bb_width = None
    if need("squeeze", KELTNER_PERIOD + 1):
        squeeze_on = bool(sq[-1])
        days = 0
        for on in sq[::-1]:
            if not on:
                break
            days += 1
        squeeze_days = days
    if sma20 is not None and sma20 > 0:
        bb_width = 2.0 * BB_SIGMAS * float(c[-20:].std(ddof=0)) / sma20

    prev_h, prev_l, prev_c = float(h[-2]), float(lo[-2]), float(c[-2])
    position: RangePosition = "above" if close > prev_h else "below" if close < prev_l else "inside"
    return TechnicalFeatures(
        as_of=as_of,
        bar_date=df.index[-1],
        close=close,
        sma20=sma20,
        sma50=sma50,
        sma200=sma200,
        pct_vs_sma20=_pct(close, sma20),
        pct_vs_sma50=_pct(close, sma50),
        pct_vs_sma200=_pct(close, sma200),
        sma50_gt_sma200=None if sma50 is None or sma200 is None else sma50 > sma200,
        sma50_slope_20d=slope,
        rsi14=rsi,
        atr14=atr,
        atr14_pct=_div(atr, close),
        stretch_atr=None if sma20 is None or atr_pos is None else (close - sma20) / atr_pos,
        high20=high20,
        low20=low20,
        dist_high20_atr=None if high20 is None or atr_pos is None else (high20 - close) / atr_pos,
        dist_low20_atr=None if low20 is None or atr_pos is None else (close - low20) / atr_pos,
        pct_from_52w_high=_pct(close, hi52),
        pct_from_52w_low=_pct(close, lo52),
        ret5d=_ret(closes, 5),
        implied_move_20d=implied,
        realised_atr_move_20d=atr_move,
        implied_vs_atr=_div(implied, atr_move),
        rs_spy_20d=rs[20],
        rs_spy_60d=rs[60],
        sector_etf=sector_etf,
        rs_sector_20d=rs_sector,
        bb_width20=bb_width,
        squeeze_on=squeeze_on,
        squeeze_days=squeeze_days,
        prev_high=prev_h,
        prev_low=prev_l,
        prev_close=prev_c,
        gap_pct=_pct(float(o[-1]), prev_c),
        close_vs_prev_range=position,
        missing=missing,
    )


# ---------------------------------------------------------------------------
# E16.3 (D76/D78): anti-chase entry filter (pure; no clock, network or LLM)
# ---------------------------------------------------------------------------

Direction = Literal["bullish", "bearish"]
StretchStatus = Literal["stretched", "ok", "missing", "not_directional"]


class AntiChaseRule(BaseModel):
    """Thresholds of the anti-chase rule (D78 defaults; ``config/routines.yaml anti_chase:``).

    ``combine: all`` (D78): a bullish idea is stretched when ``stretch_atr >=
    max_stretch_atr`` **and** ``rsi14 >= rsi_overbought``. ``combine: any`` (the D76
    card rule, kept for later variants and the backtest grid): stretched when
    ``stretch_atr >= max_stretch_atr`` **or** (``rsi14 >= rsi_overbought`` and
    ``dist_high20_atr <= max_dist_high20_atr``). Bearish ideas mirror both
    (``-stretch``, ``rsi_oversold``, ``dist_low20_atr``).

    The optional VWAP part (``vwap``) adds one more trigger, OR-ed with the daily
    rule: ``(spot - session VWAP) / ATR14 >= max_vwap_stretch_atr`` (mirrored).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    combine: Literal["all", "any"] = "all"
    max_stretch_atr: float = Field(2.5, gt=0.0, le=10.0)
    rsi_overbought: float = Field(75.0, gt=50.0, le=100.0)
    rsi_oversold: float = Field(25.0, ge=0.0, lt=50.0)
    max_dist_high20_atr: float = Field(0.25, ge=0.0, le=5.0)
    vwap: bool = False
    max_vwap_stretch_atr: float = Field(0.75, gt=0.0, le=10.0)


class StretchVerdict(BaseModel):
    """The rule's outcome for one idea, with the numbers it read (journal payload)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StretchStatus
    direction: Direction | None = None
    triggers: list[str] = Field(default_factory=list, description="Conditions that tripped")
    missing: list[str] = Field(default_factory=list, description="Inputs the rule lacked")
    stretch_atr: float | None = None
    rsi14: float | None = None
    dist_extreme20_atr: float | None = Field(
        None, description="dist_high20_atr (bullish) / dist_low20_atr (bearish)"
    )
    vwap_stretch_atr: float | None = None
    rule: AntiChaseRule

    @property
    def stretched(self) -> bool:
        return self.status == "stretched"

    def text(self) -> str:
        """One plain line for the journal / card, e.g. ``+2.8 ATR over SMA20, RSI 79``."""
        if self.status == "not_directional":
            return "not a directional idea"
        if self.status == "missing":
            return "technicals missing: " + ", ".join(self.missing)
        return "; ".join(self.triggers) or "not stretched"


def _ge(v: float | None, x: float) -> bool | None:
    return None if v is None else v >= x


def _le(v: float | None, x: float) -> bool | None:
    return None if v is None else v <= x


def _and(*xs: bool | None) -> bool | None:
    """Three-valued AND: False wins, then unknown."""
    if any(x is False for x in xs):
        return False
    return None if any(x is None for x in xs) else True


def _or(*xs: bool | None) -> bool | None:
    """Three-valued OR: True wins, then unknown."""
    if any(x is True for x in xs):
        return True
    return None if any(x is None for x in xs) else False


def is_stretched(
    tech: TechnicalFeatures | None,
    stance: str,
    cfg: AntiChaseRule,
    *,
    vwap_stretch: float | None = None,
) -> StretchVerdict:
    """Is a directional long-premium idea chasing a move that is already stretched?

    *stance* ``bullish`` / ``bearish`` (anything else: ``not_directional``). The caller
    decides that the idea is long premium (the account profile maps the stance to
    debit structures only). *vwap_stretch* is ``(spot - vwap) / atr14`` when the VWAP
    part is on and the intraday fetch worked (``None``: skipped, reported missing).

    Missing inputs never drop an idea by themselves: a condition that cannot be
    decided leaves the verdict ``missing`` unless another condition already trips.
    """
    s = stance.strip().lower()
    if s not in ("bullish", "bearish"):
        return StretchVerdict(status="not_directional", rule=cfg)
    bull = s == "bullish"
    missing: list[str] = []
    if tech is None:
        stretch = rsi = dist = None
        missing.append("technicals")
    else:
        stretch, rsi = tech.stretch_atr, tech.rsi14
        dist = tech.dist_high20_atr if bull else tech.dist_low20_atr
        for name, v in (("stretch_atr", stretch), ("rsi14", rsi)):
            if v is None:
                missing.append(name)
        if cfg.combine == "any" and dist is None:
            missing.append("dist_high20_atr" if bull else "dist_low20_atr")
    sign = 1.0 if bull else -1.0
    stretch_hit = _ge(None if stretch is None else sign * stretch, cfg.max_stretch_atr)
    rsi_hit = _ge(rsi, cfg.rsi_overbought) if bull else _le(rsi, cfg.rsi_oversold)
    near_hit = _le(dist, cfg.max_dist_high20_atr)
    if cfg.combine == "all":
        daily = _and(stretch_hit, rsi_hit)
    else:
        daily = _or(stretch_hit, _and(rsi_hit, near_hit))
    # The VWAP part (optional): a failed intraday fetch skips it (reported missing);
    # the daily rule still decides on its own.
    vwap_hit = False
    if cfg.vwap:
        if vwap_stretch is None:
            missing.append("vwap")
        else:
            vwap_hit = sign * vwap_stretch >= cfg.max_vwap_stretch_atr
    verdict = True if vwap_hit else daily

    triggers: list[str] = []
    side = "over" if bull else "under"
    if stretch_hit and stretch is not None:
        triggers.append(f"{stretch:+.1f} ATR {side} SMA20 (limit {cfg.max_stretch_atr:g})")
    if rsi_hit and rsi is not None:
        lim = cfg.rsi_overbought if bull else cfg.rsi_oversold
        triggers.append(f"RSI {rsi:.0f} (limit {lim:g})")
    if cfg.combine == "any" and near_hit and rsi_hit and dist is not None:
        triggers.append(f"{dist:.2f} ATR from the 20-day {'high' if bull else 'low'}")
    if vwap_hit and vwap_stretch is not None:
        triggers.append(f"{vwap_stretch:+.2f} ATR vs VWAP (limit {cfg.max_vwap_stretch_atr:g})")
    status: StretchStatus = (
        "stretched" if verdict is True else "ok" if verdict is False else "missing"
    )
    if status != "stretched":
        triggers = []
    return StretchVerdict(
        status=status,
        direction="bullish" if bull else "bearish",
        triggers=triggers,
        missing=missing,
        stretch_atr=stretch,
        rsi14=rsi,
        dist_extreme20_atr=dist,
        vwap_stretch_atr=vwap_stretch,
        rule=cfg,
    )


def session_vwap(bars: Iterable[IntradayBar], *, start: dt.time, end: dt.datetime) -> float | None:
    """Volume-weighted typical price of the bars that open in [*start*, *end*).

    *bars* are intraday bars (timestamps tz-aware, any zone; ET is the session clock).
    Typical price = (high + low + close) / 3. ``None`` with no volume in the window.
    """
    pv = vol = 0.0
    for b in bars:
        ts = b.timestamp.astimezone(ET)
        if ts.date() != end.astimezone(ET).date() or ts.time() < start or ts >= end:
            continue
        v = float(b.volume or 0.0)
        if v <= 0:
            continue
        pv += v * (float(b.high) + float(b.low) + float(b.close)) / 3.0
        vol += v
    return pv / vol if vol > 0 else None


def vwap_stretch_atr(spot: float | None, vwap: float | None, atr14: float | None) -> float | None:
    """``(spot - vwap) / atr14``; ``None`` when any input is missing or ATR is 0."""
    if spot is None or vwap is None or atr14 is None or atr14 <= 0:
        return None
    return (spot - vwap) / atr14
