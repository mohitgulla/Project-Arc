"""E16.3 (D76/D78): the anti-chase entry filter in the ranking backtest.

``backtest.entry_filter: none | anti_chase | anti_chase_vwap`` (``config/ranking.yaml``,
default ``none`` = the E7.5 run unchanged). With a filter on, every decision day's
menu loses its directional long-premium candidates (long calls/puts, debit
verticals) whose stance (the trend label's bullish/bearish) is *stretched* under
:func:`arc.features.technicals.is_stretched`, computed from daily OHLC bars dated on
or before the decision day only. Credit structures and neutral days are never
filtered, matching the live filter (a credit-capable stance is left alone).

``anti_chase_vwap`` adds the VWAP part at the decision close: ``(close - the day's
VWAP) / ATR14``, from the daily bar's ``vwap`` field (Alpaca's session VWAP for the
bar). That is exactly the live VWAP stretch evaluated at the close, the backtest's
only decision time.

The entry-filter decision rule (fixed in the E16.3 card *before* the run) is
:func:`entry_filter_challenge`; it differs from the ranker switch rule because the
filter only removes trades.

OHLC bars are split-adjusted (``adjustment=split``): the indicators are ratios, and a
raw series would show a fake 90 % crash on a 10:1 split day. They are cached under
``<data_dir>/underlying_ohlc/<SYM>.parquet``; option strikes keep using the raw closes.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - runtime in index comparisons
import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.strategies import CREDIT_KINDS
from arc.features.technicals import (
    AntiChaseRule,
    compute_technicals,
    is_stretched,
    vwap_stretch_atr,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.backtest.ranking import BootstrapSpec, Candidate, DecisionRule, RankRun

log = structlog.get_logger()

__all__ = [
    "BeAtrDays",
    "EntryFilter",
    "EntryFilterRule",
    "OhlcStore",
    "apply_be_filter",
    "apply_entry_filter",
    "atr14_days",
    "entry_filter_challenge",
    "load_ohlc",
    "stretched_days",
]

EntryFilter = Literal["none", "anti_chase", "anti_chase_vwap"]
OHLC_COLUMNS = ("open", "high", "low", "close", "vwap")
_TREND_STANCE = {"bull": "bullish", "bear": "bearish"}
# Bars before the first decision day the indicators need (SMA20/ATR14/RSI14 + slack).
WARMUP_DAYS = 60


class EntryFilterRule(BaseModel):
    """The E16.3 recommend-the-forward-experiment rule, fixed before the run.

    Recommend only if the filtered run has a smaller max drawdown **and** a net P&L
    that is not lower in >= ``min_subperiods_not_lower`` of the trend sub-periods,
    **and** the bootstrap CI's lower bound of the daily P&L difference is above
    ``-ci_floor_frac_of_base * |incumbent net P&L|``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_subperiods_not_lower: int = Field(2, ge=1)
    ci_floor_frac_of_base: float = Field(0.10, ge=0.0, le=1.0)

    def text(self, ci: float, subperiods: Sequence[str]) -> str:
        return (
            f"Recommend the forward experiment only if the filtered run has a smaller max "
            f"drawdown **and** a net P&L not lower in ≥ {self.min_subperiods_not_lower} of "
            f"{len(subperiods)} trend sub-periods ({', '.join(subperiods)}), **and** the "
            f"{ci:.0%} moving-block bootstrap CI of the daily P&L difference has a lower "
            f"bound > −{self.ci_floor_frac_of_base:.0%} of the incumbent's |net P&L|."
        )


# ---------------------------------------------------------------------------
# OHLC cache (split-adjusted daily bars with the bar VWAP)
# ---------------------------------------------------------------------------


class OhlcSource(Protocol):
    def daily_ohlc(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        """Columns ``open high low close vwap`` indexed by session date, ascending."""
        ...


class AlpacaOhlcSource:  # pragma: no cover - network
    """Alpaca SIP daily bars, split-adjusted, one request per symbol."""

    def __init__(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient

        from arc.data.alpaca import _get_keys

        key, secret = _get_keys()
        self._client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    def daily_ohlc(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        from arc.utils.calendar import now_et

        end = min(end, now_et().date() - dt.timedelta(days=1))  # free SIP: > 15 min old
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            start=dt.datetime.combine(start, dt.time.min, tzinfo=ET),
            end=dt.datetime.combine(end, dt.time.max, tzinfo=ET),
            timeframe=TimeFrame.Day,
            feed=DataFeed.SIP,
            adjustment=Adjustment.SPLIT,
        )
        data = getattr(self._client.get_stock_bars(req), "data", {})
        rows = {
            b.timestamp.astimezone(ET).date(): (b.open, b.high, b.low, b.close, b.vwap)
            for b in data.get(symbol, [])
        }
        return _frame(rows)


def _frame(rows: Mapping[dt.date, Sequence[float | None]]) -> pd.DataFrame:
    df = pd.DataFrame.from_dict(dict(rows), orient="index", columns=list(OHLC_COLUMNS))
    return df.astype(float).sort_index()


class OhlcStore:
    """Parquet cache: ``<root>/underlying_ohlc/<SYM>.parquet`` (date + OHLC + vwap)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root) / "underlying_ohlc"

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol.upper()}.parquet"

    def read(self, symbol: str) -> pd.DataFrame:
        p = self.path_for(symbol)
        if not p.is_file():
            return _frame({})
        df = pd.read_parquet(p)
        return pd.DataFrame(
            {c: df[c].to_numpy(dtype=float) for c in OHLC_COLUMNS}, index=list(df["date"])
        ).sort_index()

    def write(self, symbol: str, df: pd.DataFrame) -> Path:
        p = self.path_for(symbol)
        p.parent.mkdir(parents=True, exist_ok=True)
        out = pd.DataFrame({"date": list(df.index), **{c: df[c].to_numpy() for c in OHLC_COLUMNS}})
        tmp = p.with_suffix(".parquet.tmp")
        out.to_parquet(tmp, index=False)
        tmp.replace(p)
        return p


def load_ohlc(
    store: OhlcStore,
    symbol: str,
    start: dt.date,
    end: dt.date,
    source: OhlcSource | None = None,
) -> pd.DataFrame:
    """Cached OHLC covering [start, end]; fetch from *source* when the cache falls short."""
    cached = store.read(symbol)
    have = len(cached) > 0 and min(cached.index) <= start and max(cached.index) >= end
    if not have and source is not None:
        fetched = source.daily_ohlc(symbol, start, end)
        merged = pd.concat([cached, fetched])
        merged = merged[~pd.Index(merged.index).duplicated(keep="last")].sort_index()
        store.write(symbol, merged)
        log.info("backtest.ohlc_fetched", symbol=symbol, rows=len(fetched))
        cached = merged
    idx = pd.Index(cached.index)
    return cached[(idx >= start) & (idx <= end)]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def stretched_days(
    ohlc: pd.DataFrame,
    days: Sequence[dt.date],
    rule: AntiChaseRule,
    *,
    vwap: bool = False,
) -> dict[dt.date, frozenset[str]]:
    """Decision day → the stances (``bullish`` / ``bearish``) that are stretched that day.

    Indicators use bars dated <= the day only (``compute_technicals`` truncates). A day
    with too few bars or missing inputs is never stretched (the live rule keeps the
    idea when it lacks data). With *vwap*, the VWAP part reads the day's bar VWAP.
    """
    r = rule.model_copy(update={"vwap": vwap})
    out: dict[dt.date, frozenset[str]] = {}
    if ohlc.empty:
        return {d: frozenset() for d in days}
    for d in days:
        tech = compute_technicals(ohlc, d)
        vs: float | None = None
        if vwap and tech is not None and d in ohlc.index:
            bar_vwap = float(ohlc.at[d, "vwap"])
            vs = vwap_stretch_atr(
                tech.close, bar_vwap if math.isfinite(bar_vwap) else None, tech.atr14
            )
        out[d] = frozenset(
            s for s in ("bullish", "bearish") if is_stretched(tech, s, r, vwap_stretch=vs).stretched
        )
    return out


def apply_entry_filter(
    menus: Mapping[dt.date, list[Candidate]],
    trend: pd.Series,
    stretched: Mapping[dt.date, frozenset[str]],
) -> tuple[dict[dt.date, list[Candidate]], int]:
    """Drop the day's long-premium candidates when the trend stance is stretched.

    Returns the filtered menus and the number of decision days whose menu lost at
    least one candidate. Credit kinds and sideways/unknown days pass untouched.
    """
    out: dict[dt.date, list[Candidate]] = {}
    hit = 0
    for d, menu in menus.items():
        stance = _TREND_STANCE.get(str(trend.get(d, "unknown")))
        if stance is None or stance not in stretched.get(d, frozenset()):
            out[d] = menu
            continue
        kept = [c for c in menu if c.kind in CREDIT_KINDS]
        hit += len(kept) < len(menu)
        out[d] = kept
    return out, hit


# ---------------------------------------------------------------------------
# E16.5 (D76): breakeven realism (backtest.max_be_atr)
# ---------------------------------------------------------------------------

#: Decision day -> ATR14 in the option chain's (raw, unadjusted) price units.
BeAtrDays = dict[dt.date, float]


def atr14_days(ohlc: pd.DataFrame, closes: pd.Series, days: Sequence[dt.date]) -> BeAtrDays:
    """ATR14 per decision day from split-adjusted *ohlc* (bars <= the day only).

    The OHLC is split-adjusted while strikes and *closes* are raw, so the ATR is put
    back into raw units with ``raw close / adjusted close`` of the same day (1.0 away
    from a split). A day without enough bars or a close is left out (nothing dropped).
    """
    out: BeAtrDays = {}
    if ohlc.empty:
        return out
    for d in days:
        tech = compute_technicals(ohlc, d)
        if tech is None or tech.atr14 is None or tech.atr14 <= 0 or d not in closes.index:
            continue
        raw, adj = float(closes[d]), float(tech.close)
        if not (math.isfinite(raw) and math.isfinite(adj)) or adj <= 0:
            continue
        out[d] = tech.atr14 * raw / adj
    return out


def candidate_be_atr(c: Candidate, spot: float, atr14: float) -> float | None:
    """``be_atr`` of a backtest candidate (the live :func:`structure_be_atr` on its legs)."""
    from arc.backtest.ranking import _legs
    from arc.scanner.be_atr import structure_be_atr
    from arc.structures import analyze

    try:
        st = analyze(_legs(c.picks, c.underlying), as_of=c.day)
    except ValueError:
        return None
    return structure_be_atr(st, spot, atr14)


def apply_be_filter(
    menus: Mapping[dt.date, list[Candidate]],
    closes: pd.Series,
    atr: Mapping[dt.date, float],
    max_be_atr: float,
) -> tuple[dict[dt.date, list[Candidate]], int]:
    """Drop debit candidates whose directional breakeven is > *max_be_atr* ATR√t.

    Returns the filtered menus and the number of candidates dropped. Credit kinds and
    days without ATR14 pass untouched (the live rule keeps them too).
    """
    out: dict[dt.date, list[Candidate]] = {}
    dropped = 0
    for d, menu in menus.items():
        a = atr.get(d)
        if a is None or d not in closes.index or not menu:
            out[d] = menu
            continue
        spot = float(closes[d])
        kept = []
        for c in menu:
            if c.kind not in CREDIT_KINDS:
                v = candidate_be_atr(c, spot, a)
                if v is not None and v > max_be_atr:
                    dropped += 1
                    continue
            kept.append(c)
        out[d] = kept
    return out, dropped


# ---------------------------------------------------------------------------
# Decision rule
# ---------------------------------------------------------------------------


def entry_filter_challenge(
    base: RankRun,
    run: RankRun,
    *,
    rule: EntryFilterRule,
    subperiods: DecisionRule,
    boot: BootstrapSpec,
) -> dict[str, object]:
    """*run* (filter on) vs *base* (filter off), same ranker: the E16.3 rule's inputs."""
    from arc.backtest.ranking import _max_dd, block_bootstrap_ci, subperiod_stats

    labels = subperiods.subperiods
    bs, rs = subperiod_stats(base, labels), subperiod_stats(run, labels)
    not_lower = [lab for lab in labels if rs[lab][0] >= bs[lab][0]]
    idx = base.equity.index.union(run.equity.index)
    a = run.equity.reindex(idx).ffill().bfill()
    b = base.equity.reindex(idx).ffill().bfill()
    diff = (a.diff().fillna(0.0) - b.diff().fillna(0.0)).to_numpy(dtype=float)
    point, lo, hi = block_bootstrap_ci(
        np.asarray(diff),
        resamples=boot.resamples,
        block=boot.block_days,
        ci=boot.ci,
        seed=boot.seed,
    )
    base_pnl = float(base.trades["pnl"].sum()) if len(base.trades) else 0.0
    floor = -rule.ci_floor_frac_of_base * abs(base_pnl)
    dd_base, dd_run = _max_dd(base.equity), _max_dd(run.equity)
    smaller_dd = dd_run < dd_base
    return {
        "max_dd_base": dd_base,
        "max_dd_filtered": dd_run,
        "smaller_dd": smaller_dd,
        "subperiods_not_lower": len(not_lower),
        "not_lower": ",".join(not_lower) or "–",
        "pnl_diff": point,
        "ci_lo": lo,
        "ci_hi": hi,
        "ci_floor": floor,
        "recommend": bool(
            smaller_dd and len(not_lower) >= rule.min_subperiods_not_lower and lo > floor
        ),
    }
