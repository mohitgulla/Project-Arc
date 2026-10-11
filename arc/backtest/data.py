"""One point-in-time loader for every backtest (E7.10, D84): :class:`HistoryView`.

Per ``(ticker, session d)`` it serves what a live decision would have known at d's
close — and nothing later:

* the **marked chain** (:mod:`arc.backtest.marks`): ``quoted`` closing NBBO on the
  ``thetadata`` partition (with OI), else the ``alpaca`` fallback partition in its
  own mode (``alpaca_marks``, default ``smile``: Alpaca rows have no bid/ask);
* the raw underlying close (strikes are raw) and split-adjusted OHLC;
* **IV30 / IV rank / IV percentile** (:mod:`arc.backtest.ivhist`, the live code);
* **next earnings** (:mod:`arc.backtest.earnings_hist`, detected ≤ d + projection);
* **eligibility** (:class:`EligibilityFlags`): in the checkpoint (``c125|c250|c500``)
  **and** in E7.7's membership for d's quarter (``pit``) **and** a raw close on d. The
  live D9 liquidity screen (``arc.universe.screen.screen_liquidity``, the pure live
  function) is computed from d's own data and exposed as a flag + reasons only;
  E7.11 decides whether to apply it (seed names are never screened, as live).

Session d's chain, IV and screen read d's rows only; IV rank reads IV30 on days ≤ d;
earnings read detections ≤ d; ADV reads the ``adv_days`` sessions ending at d (d is a
completed session at the decision close). Tests multiply every later row by 3 and
assert the day-d view is unchanged.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.earnings_hist import (
    EarningsEvent,
    EarningsRule,
    NextEarnings,
    detect_events,
    next_earnings,
)
from arc.backtest.entry_filter import OhlcStore
from arc.backtest.ivhist import IvPoint, TermPoint, iv_points, term_points_by_day
from arc.backtest.marks import (
    DropCounts,
    MarksMode,
    QuotedFallback,
    RateCurve,
    mark_chains,
)
from arc.backtest.underlying import UnderlyingStore
from arc.backtest.universe import checkpoint_symbols, load_universe
from arc.data.history.store import ParquetHistoryStore
from arc.features.vol import IV_LOOKBACK, MIN_IV_HISTORY
from arc.universe.config import LiquidityThresholds
from arc.universe.screen import ATM_TARGET_DTE, LiquidityMetrics, screen_liquidity

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.backtest.costs import CostModel

log = structlog.get_logger()

__all__ = [
    "ChainDay",
    "DataSettings",
    "EligibilityFlags",
    "HistoryView",
    "TickerHistory",
    "quarter_start",
]

_FORBID = ConfigDict(extra="forbid", frozen=True)


class DataSettings(BaseModel):
    """``backtest.data`` in ``config/ranking.yaml``."""

    model_config = _FORBID

    provider: str = Field("thetadata", description="Primary history partition (quoted NBBO)")
    fallback_provider: str | None = Field(
        "alpaca", description="Partition used for a ticker the primary has no sessions for"
    )
    marks: MarksMode = Field("quoted", description="Mark mode on the primary partition")
    alpaca_marks: Literal["smile", "close"] = Field(
        "smile", description="Mark mode on the fallback partition (no bid/ask there)"
    )
    max_quoted_spread_pct: float = Field(0.35, gt=0.0, description="Per leg, spread / mid")
    quoted_fallback: QuotedFallback = "drop"
    rate_source: Literal["flat", "treasury"] = "flat"
    chain_dte_min: int = Field(1, ge=1)
    chain_dte_max: int = Field(75, ge=1, description="75 = the live iv.record chain window")
    pit: bool = Field(True, description="Also require E7.7 membership for d's quarter")
    universe_version: str | None = Field(None, description="E7.7 build (None = latest)")
    iv_lookback: int = Field(IV_LOOKBACK, ge=2)
    earnings: EarningsRule = Field(default_factory=lambda: EarningsRule())
    screen: LiquidityThresholds = Field(
        default_factory=lambda: LiquidityThresholds(),
        description="Live D9 screen thresholds (flag only; never applied here)",
    )
    screen_dte_window: tuple[int, int] = Field(
        (30, 45), description="The profile entry window the screen's expiry check uses"
    )


class EligibilityFlags(BaseModel):
    """Whether *ticker* could be traded on *day*, and the D9 screen verdict as a flag."""

    model_config = _FORBID

    ticker: str
    day: dt.date
    quarter: dt.date
    in_checkpoint: bool
    in_pit: bool = Field(..., description="E7.7 membership for the quarter (True if pit off)")
    has_close: bool
    eligible: bool = Field(..., description="in_checkpoint and in_pit and has_close")
    screen_pass: bool
    screen_reasons: list[str] = Field(default_factory=list)
    seed: bool = Field(False, description="Always-include name (never screened live)")


class ChainDay(BaseModel):
    """Everything known about one ticker at one session's close."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    ticker: str
    day: dt.date
    provider: str
    marks: str
    rate: float
    spot: float | None = Field(None, description="Raw consolidated close (strike units)")
    ohlc: dict[str, float] | None = Field(None, description="Split-adjusted OHLC + vwap")
    chain: pd.DataFrame = Field(..., description="Marked chain: mid/spread/dte/iv/delta/...")
    drops: DropCounts = Field(default_factory=lambda: DropCounts())
    iv: IvPoint | None = None
    earnings: NextEarnings
    eligibility: EligibilityFlags


def quarter_start(d: dt.date) -> dt.date:
    return dt.date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


class TickerHistory:
    """One ticker's derived history over a window (built once per :class:`HistoryView`)."""

    def __init__(
        self,
        *,
        ticker: str,
        provider: str,
        marks: str,
        raw: pd.DataFrame,
        closes: pd.Series,
        volume: pd.Series,
        ohlc: pd.DataFrame,
        chains: dict[dt.date, pd.DataFrame],
        drops: dict[dt.date, DropCounts],
        points: dict[dt.date, list[TermPoint]],
        iv: dict[dt.date, IvPoint],
        events: list[EarningsEvent],
    ) -> None:
        self.ticker = ticker
        self.provider = provider
        self.marks = marks
        self.raw = raw
        self.closes = closes
        self.volume = volume
        self.ohlc = ohlc
        self.chains = chains
        self.drops = drops
        self.points = points
        self.iv = iv
        self.events = events

    @property
    def sessions(self) -> list[dt.date]:
        return sorted(self.chains)

    def raw_day(self, day: dt.date) -> pd.DataFrame:
        if self.raw.empty:
            return self.raw
        return self.raw[self.raw["date"] == day]


class HistoryView:
    """Point-in-time history for one checkpoint universe (see the module docstring).

    *start*/*end* bound the decision sessions; option rows are read from
    ``start - warmup_days`` so IV rank and earnings have their trailing history.
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        settings: DataSettings,
        universe: str,
        start: dt.date,
        end: dt.date,
        cost: CostModel,
        flat_rate: float,
        min_iv_obs: int = MIN_IV_HISTORY,
        etfs: frozenset[str] = frozenset(),
        warmup_days: int = 400,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.universe = universe.lower()
        self.start = start
        self.end = end
        self.cost = cost
        self.min_iv_obs = min_iv_obs
        self.read_from = start - dt.timedelta(days=warmup_days)
        self.store = ParquetHistoryStore(self.data_dir)
        self.underlying = UnderlyingStore(self.data_dir)
        self.ohlc_store = OhlcStore(self.data_dir)
        self.rates = RateCurve.load(self.data_dir, flat=flat_rate, source=settings.rate_source)
        manifest, rows, membership = load_universe(self.data_dir, settings.universe_version)
        self.version = manifest.version
        self.symbols = checkpoint_symbols(rows, self.universe)
        self._in_checkpoint = frozenset(self.symbols)
        self._seed = frozenset(manifest.always) | frozenset(r.symbol for r in rows if r.bucket == 1)
        self._etfs = etfs | frozenset(r.symbol for r in rows if r.asset_type == "etf")
        m = membership
        ok = (m["in_top"].astype(bool) | m["bypass"].astype(bool)) & m["in_pull_list"].astype(bool)
        self._pit: dict[dt.date, frozenset[str]] = {
            _as_date(q): frozenset(g["symbol"]) for q, g in m[ok].groupby("quarter")
        }
        self._cache: dict[str, TickerHistory] = {}

    # -- providers -----------------------------------------------------------

    def provider_for(self, ticker: str) -> str | None:
        """Primary partition when it has sessions for *ticker* in the window, else fallback."""
        for p in (self.settings.provider, self.settings.fallback_provider):
            if p is None:
                continue
            if any(self.read_from <= d <= self.end for d in self.store.cached_dates(p, ticker)):
                return p
        return None

    def is_etf(self, ticker: str) -> bool:
        return ticker.upper() in self._etfs

    # -- per ticker ------------------------------------------------------------

    def load(self, ticker: str) -> TickerHistory:
        t = ticker.upper()
        if t in self._cache:
            return self._cache[t]
        s = self.settings
        provider = self.provider_for(t)
        bars = self.underlying.read_bars(t)
        idx = pd.Index(bars.index)
        bars = bars[(idx >= self.read_from) & (idx <= self.end)]
        closes = bars["close"].dropna()
        ohlc = self.ohlc_store.read(t)
        oidx = pd.Index(ohlc.index)
        ohlc = ohlc[(oidx >= self.read_from) & (oidx <= self.end)]
        if provider is None:
            raw = self.store.read(s.provider, t, self.read_from, self.end)
            mode = s.marks
        else:
            raw = self.store.read(provider, t, self.read_from, self.end)
            mode = s.marks if provider == s.provider else s.alpaca_marks
        chains, drops = mark_chains(
            raw,
            closes,
            mode=mode,
            cost=self.cost,
            rate=self.rates,
            dte_min=s.chain_dte_min,
            dte_max=s.chain_dte_max,
            max_spread_pct=s.max_quoted_spread_pct,
            fallback=s.quoted_fallback,
        )
        points = term_points_by_day(chains, closes)
        iv = iv_points(t, points, lookback=s.iv_lookback, min_obs=self.min_iv_obs)
        market = None
        ref = s.earnings.earn_market_ref
        if ref and ref.upper() != t and not self.is_etf(t) and self.provider_for(ref):
            market = self.load(ref).points
        events = detect_events(t, points, s.earnings, etf=self.is_etf(t), market=market)
        h = TickerHistory(
            ticker=t,
            provider=provider or s.provider,
            marks=mode,
            raw=raw,
            closes=closes,
            volume=bars["volume"],
            ohlc=ohlc,
            chains=chains,
            drops=drops,
            points=points,
            iv=iv,
            events=events,
        )
        self._cache[t] = h
        log.info("backtest.data.loaded", ticker=t, provider=h.provider, sessions=len(chains))
        return h

    # -- per ticker-day ------------------------------------------------------

    def next_earnings(self, ticker: str, day: dt.date) -> NextEarnings:
        h = self.load(ticker)
        return next_earnings(h.events, day, self.settings.earnings, etf=self.is_etf(ticker))

    def eligibility(self, ticker: str, day: dt.date) -> EligibilityFlags:
        t = ticker.upper()
        h = self.load(t)
        q = quarter_start(day)
        in_cp = t in self._in_checkpoint
        in_pit = (t in self._pit.get(q, frozenset())) if self.settings.pit else True
        has_close = day in h.closes.index and bool(np.isfinite(h.closes[day]))
        screen = screen_liquidity(self.liquidity_metrics(h, day), self.settings.screen)
        return EligibilityFlags(
            ticker=t,
            day=day,
            quarter=q,
            in_checkpoint=in_cp,
            in_pit=in_pit,
            has_close=has_close,
            eligible=in_cp and in_pit and has_close,
            screen_pass=screen.passed,
            screen_reasons=screen.failures,
            seed=t in self._seed,
        )

    def liquidity_metrics(self, h: TickerHistory, day: dt.date) -> LiquidityMetrics:
        """The live D9 screen's inputs from day d's own rows (and ADV ending at d)."""
        th = self.settings.screen
        out: dict[str, object] = {"ticker": h.ticker, "as_of": day}
        if day in h.closes.index:
            out["price"] = float(h.closes[day])
        vol = h.volume[pd.Index(h.volume.index) <= day].dropna().iloc[-th.adv_days :]
        out["adv_sessions"] = len(vol)
        if len(vol) >= th.adv_days:
            out["adv_shares"] = float(vol.mean())
        raw = h.raw_day(day)
        price = out.get("price")
        if raw.empty or price is None:
            if raw.empty:
                out["error"] = "no option rows"
            return LiquidityMetrics.model_validate(out)
        lo, hi = self.settings.screen_dte_window
        dte = (pd.to_datetime(raw["expiration"]) - pd.to_datetime(raw["date"])).dt.days
        win = raw[(dte >= lo) & (dte <= hi)]
        expiries = sorted(set(win["expiration"]))
        out["expiries_in_window"] = len(expiries)
        if expiries:
            exp = min(expiries, key=lambda e: (abs((e - day).days - ATM_TARGET_DTE), e))
            near = win[win["expiration"] == exp]
            p = float(price)  # type: ignore[arg-type]
            ks = sorted(set(near["strike"].astype(float)), key=lambda k: (abs(k - p), k))
            strike = ks[0]
            near_atm = set(ks[: th.atm_strikes])
            oi = near[near["strike"].isin(near_atm)]["open_interest"].dropna()
            atm = near[near["strike"] == strike]
            spreads = [
                (a - b) / ((a + b) / 2)
                for b, a in zip(atm["bid"], atm["ask"], strict=True)
                if pd.notna(b) and pd.notna(a) and b > 0 and a > 0 and a >= b
            ]
            out |= {
                "atm_expiry": exp,
                "atm_strike": strike,
                "atm_open_interest": int(oi.sum()) if len(oi) else None,
                "atm_spread_pct": sum(spreads) / len(spreads) if spreads else None,
            }
        return LiquidityMetrics.model_validate(out)

    def day(self, ticker: str, day: dt.date) -> ChainDay:
        h = self.load(ticker)
        o = None
        if day in h.ohlc.index:
            row = h.ohlc.loc[day]
            o = {k: float(row[k]) for k in row.index if pd.notna(row[k])}
        chain = h.chains.get(day)
        return ChainDay(
            ticker=h.ticker,
            day=day,
            provider=h.provider,
            marks=h.marks,
            rate=self.rates.rate(day),
            spot=float(h.closes[day]) if day in h.closes.index else None,
            ohlc=o,
            chain=chain if chain is not None else h.raw.iloc[0:0],
            drops=h.drops.get(day, DropCounts()),
            iv=h.iv.get(day),
            earnings=self.next_earnings(h.ticker, day),
            eligibility=self.eligibility(h.ticker, day),
        )

    def sessions(self, ticker: str) -> list[dt.date]:
        """Decision sessions in [start, end] with a marked chain."""
        return [d for d in self.load(ticker).sessions if self.start <= d <= self.end]

    def eligible(self, day: dt.date, tickers: Sequence[str] | None = None) -> list[str]:
        """Checkpoint names eligible on *day* (priority order)."""
        return [t for t in (tickers or self.symbols) if self.eligibility(t, day).eligible]


def _as_date(v: object) -> dt.date:
    if isinstance(v, pd.Timestamp):
        return v.date()
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))
