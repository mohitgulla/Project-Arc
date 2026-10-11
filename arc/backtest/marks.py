"""Backtest mark modes (E7.10, D84): ``quoted`` (closing NBBO), ``smile`` and ``close``.

* **quoted** (new): per contract ``mid = (bid + ask) / 2`` and ``spread = ask − bid``
  from the closing OPRA NBBO (ThetaData EOD). A contract is **untradeable** and
  dropped when (first matching reason wins, each counted):

  1. ``no_quote``: bid or ask missing;
  2. ``ask_le_zero``: ask ≤ 0;
  3. ``crossed_locked``: bid ≥ ask;
  4. ``zero_bid``: bid = 0 *and* the spread rule below fails (a zero-bid leg can never
     be sold; with ``spread/mid = 200 %`` it fails the default spread rule for buying
     too). A zero-bid contract the spread rule keeps stays with ``sellable = False``,
     and :func:`arc.backtest.strategies.select_legs` never sells it;
  5. ``wide_spread``: ``spread / mid > max_quoted_spread_pct`` (default 0.35, per leg).

  ``quoted_fallback: smile`` re-prices the dropped contracts off the session's
  fitted smile (:func:`smile_marks` fitted on the surviving quoted contracts only)
  instead of dropping them; ``drop`` (default) drops them. The IV and delta solve use
  the quoted mid at the session's rate (:class:`RateCurve`).
* **smile** / **close**: moved here unchanged from ``arc.backtest.ranking`` (E7.5);
  ``ranking`` re-exports them. A golden test pins both byte-for-byte.

Every function reads one session's rows only, so no mode can look ahead.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - runtime in signatures pydantic never sees
import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import norm

from arc.backtest.chain import _delta_vec, implied_vol_vec
from arc.pricing.bs import price_vectorized

if TYPE_CHECKING:
    from collections.abc import Mapping

    from arc.backtest.costs import CostModel

__all__ = [
    "DROP_REASONS",
    "DropCounts",
    "MarksMode",
    "QuotedFallback",
    "RateCurve",
    "clean_chains",
    "mark_chains",
    "quoted_marks",
    "remark_chains",
    "smile_deviation",
    "smile_marks",
]

MarksMode = Literal["quoted", "smile", "close"]
QuotedFallback = Literal["drop", "smile"]
DROP_REASONS: tuple[str, ...] = (
    "no_quote",
    "ask_le_zero",
    "crossed_locked",
    "zero_bid",
    "wide_spread",
)
#: ``<data-dir>/rates/treasury_3m.parquet`` (``date``, ``rate`` as a decimal).
TREASURY_FILE = Path("rates") / "treasury_3m.parquet"


class DropCounts(BaseModel):
    """Contracts seen and dropped per quoted-mark rule (one session or a sum)."""

    model_config = ConfigDict(extra="forbid")

    contracts: int = 0
    no_quote: int = 0
    ask_le_zero: int = 0
    crossed_locked: int = 0
    zero_bid: int = 0
    wide_spread: int = 0
    smile_filled: int = Field(0, description="Dropped contracts re-priced by the fallback")
    kept: int = 0

    def add(self, other: DropCounts) -> DropCounts:
        return DropCounts.model_validate(
            {k: getattr(self, k) + getattr(other, k) for k in DropCounts.model_fields}
        )

    def pct(self, reason: str) -> float:
        return 0.0 if self.contracts == 0 else 100.0 * getattr(self, reason) / self.contracts


class RateCurve:
    """Risk-free rate per session: the cached 3-month T-bill when present, else flat.

    ``rate_source: treasury`` reads ``<data-dir>/rates/treasury_3m.parquet`` (ThetaData's
    free daily treasury series, when a pull caches it) and uses the latest value dated
    on or before the session (never a later one). Sessions before the first cached
    value, or ``rate_source: flat``, use the flat rate.
    """

    def __init__(self, flat: float, series: pd.Series | None = None) -> None:
        self.flat = float(flat)
        self.series = None if series is None or series.empty else series.sort_index()

    @classmethod
    def load(
        cls, data_dir: Path | str, *, flat: float, source: Literal["flat", "treasury"]
    ) -> RateCurve:
        path = Path(data_dir) / TREASURY_FILE
        if source == "flat" or not path.is_file():
            return cls(flat)
        df = pd.read_parquet(path)
        s = pd.Series(df["rate"].to_numpy(dtype=float), index=list(df["date"]))
        return cls(flat, s)

    @property
    def source(self) -> str:
        return "flat" if self.series is None else "treasury"

    def rate(self, day: dt.date) -> float:
        if self.series is None:
            return self.flat
        past = self.series[pd.Index(self.series.index) <= day]
        return self.flat if past.empty else float(past.iloc[-1])


def quoted_marks(
    rows: pd.DataFrame,
    spot: float,
    *,
    r: float,
    q: float = 0.0,
    max_spread_pct: float = 0.35,
    fallback: QuotedFallback = "drop",
    cost: CostModel | None = None,
) -> tuple[pd.DataFrame, DropCounts]:
    """One session's raw rows → the quoted chain (mid/spread/dte/iv/delta/sellable).

    Rows expiring on/before the session are ignored (not counted). ``fallback="smile"``
    needs *cost* (the smile marks' estimated spread).
    """
    if rows.empty:
        empty = rows.assign(mid=[], spread=[], dte=[], iv=[], delta=[], quoted=[], sellable=[])
        return empty, DropCounts()
    df = rows.copy()
    df["dte"] = (pd.to_datetime(df["expiration"]) - pd.to_datetime(df["date"])).dt.days.astype(int)
    df = df[df["dte"] > 0].reset_index(drop=True)
    bid = df["bid"].to_numpy(dtype=float) if "bid" in df else np.full(len(df), np.nan)
    ask = df["ask"].to_numpy(dtype=float) if "ask" in df else np.full(len(df), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        mid = (bid + ask) / 2.0
        spread = ask - bid
        wide = ~(spread / mid <= max_spread_pct)
    no_quote = ~(np.isfinite(bid) & np.isfinite(ask))
    ask_le_zero = ~no_quote & (ask <= 0)
    crossed = ~no_quote & ~ask_le_zero & (bid >= ask)
    ok_so_far = ~(no_quote | ask_le_zero | crossed)
    zero_bid = ok_so_far & (bid <= 0) & wide
    wide_spread = ok_so_far & ~zero_bid & wide
    drop = no_quote | ask_le_zero | crossed | zero_bid | wide_spread
    counts = {
        "contracts": len(df),
        "no_quote": int(no_quote.sum()),
        "ask_le_zero": int(ask_le_zero.sum()),
        "crossed_locked": int(crossed.sum()),
        "zero_bid": int(zero_bid.sum()),
        "wide_spread": int(wide_spread.sum()),
    }
    df["mid"] = mid
    df["spread"] = spread
    df["quoted"] = ~drop
    df["sellable"] = ~drop & (bid > 0)
    t = df["dte"].to_numpy(dtype=float) / 365.0
    k = df["strike"].to_numpy(dtype=float)
    is_call = (df["right"].astype(str) == "call").to_numpy()
    flag = np.where(is_call, "c", "p")
    price = np.where(drop, np.nan, mid)
    iv = implied_vol_vec(price, spot, k, t, r, q, flag)
    df["iv"] = iv
    df["delta"] = _delta_vec(spot, k, t, r, q, iv, is_call)
    kept = df[~drop]
    filled = 0
    if fallback == "smile" and drop.any():
        if cost is None:
            msg = "quoted_fallback=smile needs a cost model"
            raise ValueError(msg)
        dropped = df[drop].assign(iv=np.nan, delta=np.nan)
        sm = smile_marks(pd.concat([kept, dropped], ignore_index=True), spot, r=r, cost=cost)
        fill = sm[sm["symbol"].isin(set(dropped["symbol"]))].assign(quoted=False, sellable=True)
        filled = len(fill)
        kept = pd.concat([kept, fill], ignore_index=True)
    counts["smile_filled"] = filled
    counts["kept"] = len(kept)
    return kept.reset_index(drop=True), DropCounts.model_validate(counts)


def mark_chains(
    history: pd.DataFrame,
    closes: pd.Series,
    *,
    mode: MarksMode,
    cost: CostModel,
    rate: RateCurve,
    dte_min: int = 1,
    dte_max: int = 70,
    max_spread_pct: float = 0.35,
    fallback: QuotedFallback = "drop",
    max_leg_iv_dev: float | None = 0.15,
    smile_window: int = 5,
) -> tuple[dict[dt.date, pd.DataFrame], dict[dt.date, DropCounts]]:
    """Per-session marked chains for any mode (+ quoted drop counts per session).

    ``smile`` / ``close`` are exactly the E7.5 ``arc backtest rank`` path at the flat
    rate (``prepare_chains`` → :func:`remark_chains` / :func:`clean_chains`).
    """
    from arc.backtest.engine import prepare_chains

    if mode != "quoted":
        chains = prepare_chains(
            history, closes, cost=cost, r=rate.flat, dte_min=dte_min, dte_max=dte_max
        )
        if mode == "smile":
            return remark_chains(chains, closes, r=rate.flat, cost=cost), {}
        return clean_chains(chains, max_dev=max_leg_iv_dev, window=smile_window), {}
    out: dict[dt.date, pd.DataFrame] = {}
    drops: dict[dt.date, DropCounts] = {}
    if history.empty:
        return out, drops
    h = history.copy()
    dte = (pd.to_datetime(h["expiration"]) - pd.to_datetime(h["date"])).dt.days
    h = h[(dte >= dte_min) & (dte <= dte_max)]
    for day, rows in h.groupby("date", sort=True):
        spot = closes.get(day)
        if spot is None or not pd.notna(spot):
            continue
        out[day], drops[day] = quoted_marks(
            rows,
            float(spot),
            r=rate.rate(day),
            max_spread_pct=max_spread_pct,
            fallback=fallback,
            cost=cost,
        )
    return out, drops


# ---------------------------------------------------------------------------
# smile / close (E7.5, moved from arc.backtest.ranking unchanged)
# ---------------------------------------------------------------------------


def smile_deviation(chain: pd.DataFrame, window: int = 5) -> pd.Series:
    """Per row: ``iv / median(iv of the *window* nearest strikes, same expiry and right) − 1``.

    Alpaca history has trade closes only; a contract that last traded hours before
    the close has a stale price and an IV off its neighbours. NaN IV → NaN.
    """
    out = pd.Series(np.nan, index=chain.index, dtype=float)
    ok = chain[np.isfinite(chain["iv"])]
    for _, g in ok.groupby(["expiration", "right"], sort=False):
        g = g.sort_values("strike")
        med = g["iv"].rolling(window, center=True, min_periods=3).median()
        out.loc[g.index] = g["iv"] / med - 1.0
    return out


def smile_marks(
    ch: pd.DataFrame,
    spot: float,
    *,
    r: float,
    cost: CostModel,
    min_points: int = 5,
    iterations: int = 2,
) -> pd.DataFrame:
    """Re-mark one session's chain from a same-session fitted IV smile.

    Alpaca EOD closes are last trades printed at different times, so neighbouring
    strikes routinely violate monotonicity and put-call parity. Per expiration, a
    volume-weighted quadratic in log-moneyness ``log(K/F)`` is fitted to the OTM
    options' IVs (puts below the forward, calls above), with points more than
    3 MADs off the fit dropped and the fit repeated *iterations* times. Every row
    in the fitted strike range is then priced by BSM at the fitted IV: ``mid``,
    ``iv``, ``delta`` and the estimated ``spread`` are replaced. Rows outside the
    range (no extrapolation) and expiries with < *min_points* OTM points are dropped.
    Uses the session's own rows only, so it cannot look ahead.
    """
    keep: list[pd.DataFrame] = []
    base = ch[np.isfinite(ch["iv"]) & (ch["iv"] > 0)]
    for exp, g in ch.groupby("expiration", sort=True):
        t = float(g["dte"].iloc[0]) / 365.0
        fwd = spot * math.exp(r * t)
        gb = base[base["expiration"] == exp]
        otm = gb[
            ((gb["right"] == "put") & (gb["strike"] < fwd))
            | ((gb["right"] == "call") & (gb["strike"] >= fwd))
        ]
        if len(otm) < min_points:
            continue
        x = np.log(otm["strike"].to_numpy(dtype=float) / fwd)
        y = otm["iv"].to_numpy(dtype=float)
        w = np.sqrt(np.nan_to_num(otm["volume"].to_numpy(dtype=float)) + 1.0)
        mask = np.ones(len(x), dtype=bool)
        coef = np.polyfit(x, y, 2, w=w)
        for _ in range(iterations):
            res = y - np.polyval(coef, x)
            mad = float(np.median(np.abs(res - np.median(res)))) or 1e-9
            mask = np.abs(res) <= 3.0 * 1.4826 * mad
            if mask.sum() < min_points:
                break
            coef = np.polyfit(x[mask], y[mask], 2, w=w[mask])
        lo, hi = float(x[mask].min()), float(x[mask].max())
        gx = np.log(g["strike"].to_numpy(dtype=float) / fwd)
        inside = (gx >= lo) & (gx <= hi)
        if not inside.any():
            continue
        g = g[inside].copy()
        sig = np.clip(np.polyval(coef, gx[inside]), 0.03, 3.0)
        k = g["strike"].to_numpy(dtype=float)
        is_call = (g["right"].astype(str) == "call").to_numpy()
        flag = np.where(is_call, "c", "p")
        n = len(g)
        mid = price_vectorized(flag, np.full(n, spot), k, np.full(n, t), np.full(n, r), sig)
        g["mid"] = np.round(np.maximum(mid, 0.0), 4)
        g["iv"] = sig
        with np.errstate(invalid="ignore", divide="ignore"):
            d1 = (np.log(spot / k) + (r + 0.5 * sig**2) * t) / (sig * math.sqrt(t))
        g["delta"] = np.where(is_call, norm.cdf(d1), norm.cdf(d1) - 1.0)
        g["spread"] = [cost.spread(float(m)) for m in g["mid"]]
        keep.append(g[g["mid"] > 0])
    if not keep:
        return ch.iloc[0:0]
    return pd.concat(keep, ignore_index=True)


def remark_chains(
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    *,
    r: float,
    cost: CostModel,
) -> dict[dt.date, pd.DataFrame]:
    """:func:`smile_marks` for every session (session spot from *closes* at that date)."""
    out: dict[dt.date, pd.DataFrame] = {}
    for d, ch in chains.items():
        if d in closes.index and len(ch):
            out[d] = smile_marks(ch, float(closes[d]), r=r, cost=cost)
    return out


def clean_chains(
    chains: Mapping[dt.date, pd.DataFrame], *, max_dev: float | None, window: int = 5
) -> dict[dt.date, pd.DataFrame]:
    """Drop rows whose IV is more than *max_dev* off their strike neighbours' median.

    Each session is filtered on its own rows only (no look-ahead). The cleaned chains
    are used for the menus *and* the exit marks, so a stale close can neither make a
    structure look cheap at entry nor fire a spurious stop / take profit later.
    """
    if max_dev is None:
        return dict(chains)
    out: dict[dt.date, pd.DataFrame] = {}
    for d, ch in chains.items():
        dev = smile_deviation(ch, window)
        out[d] = ch[dev.abs() <= max_dev].reset_index(drop=True)
    return out
