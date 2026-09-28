"""Performance metrics and walk-forward splits over closed trades.

Metric definitions (all on the per-unit trade list, one contract per trade):

* ``trades``, ``win_rate`` (pnl > 0), ``avg_pnl``, ``total_pnl``.
* ``profit_factor`` = Σ winning pnl / |Σ losing pnl| (``inf`` if no losers).
* ``avg_ror`` = mean(pnl / max_loss) — return on risk per trade.
* ``avg_cost`` = mean(pnl_mid − pnl): dollars of slippage + fees per trade.
* ``cost_pct_premium`` = Σ cost / Σ |entry premium at mid| × 100 — costs as a
  share of the premium paid or collected.
* ``max_dd`` — max peak-to-trough drop of cumulative pnl with trades booked on
  their **expiration** date (realised equity curve), in dollars.
* ``tail_months`` — the worst calendar months by pnl booked at expiration.

Walk-forward: :func:`walk_forward_splits` builds rolling (train, test) windows
by entry date. Test: a trade belongs to the window its entry falls in (the
*decision* was made in-window even if it expires later). Train: only trades
that have also **expired** by the train end are used, so selection never sees
an outcome that was unknown at the time. Parameter selection on train and
out-of-sample scoring on test is done by :func:`walk_forward_eval`.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Metrics",
    "Split",
    "breakdown",
    "compute_metrics",
    "max_drawdown",
    "monthly_pnl",
    "walk_forward_eval",
    "walk_forward_splits",
]


class Metrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    trades: int
    win_rate: float
    profit_factor: float
    avg_pnl: float
    total_pnl: float
    total_pnl_mid: float
    avg_ror: float
    avg_cost: float
    cost_pct_premium: float
    max_dd: float
    worst_month: str | None
    worst_month_pnl: float


def max_drawdown(pnl: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative sum (≥ 0), starting from 0."""
    peak = 0.0
    eq = 0.0
    dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def monthly_pnl(df: pd.DataFrame) -> pd.Series:
    """pnl summed by expiration month (``YYYY-MM``), ascending by month."""
    if df.empty:
        return pd.Series(dtype=float)
    m = pd.to_datetime(df["expiration"]).dt.strftime("%Y-%m")
    return df.groupby(m)["pnl"].sum().sort_index()


def compute_metrics(df: pd.DataFrame) -> Metrics:
    """Metrics for a trades frame from :func:`arc.backtest.engine.trades_frame`."""
    n = len(df)
    if n == 0:
        return Metrics(
            trades=0,
            win_rate=math.nan,
            profit_factor=math.nan,
            avg_pnl=math.nan,
            total_pnl=0.0,
            total_pnl_mid=0.0,
            avg_ror=math.nan,
            avg_cost=math.nan,
            cost_pct_premium=math.nan,
            max_dd=0.0,
            worst_month=None,
            worst_month_pnl=0.0,
        )
    pnl = df["pnl"].astype(float)
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    pf = math.inf if losses == 0 else wins / losses
    gross = df["pnl_mid"].astype(float)
    costs = gross - pnl
    premium = (df["entry_net_mid"].astype(float).abs() * 100.0).sum()
    cost_pct = math.nan if premium == 0 else float(costs.sum() / premium * 100.0)
    ordered = df.sort_values(["expiration", "entry_date"])["pnl"].astype(float).tolist()
    months = monthly_pnl(df)
    return Metrics(
        trades=n,
        win_rate=float((pnl > 0).mean()),
        profit_factor=float(pf),
        avg_pnl=float(pnl.mean()),
        total_pnl=float(pnl.sum()),
        total_pnl_mid=float(gross.sum()),
        avg_ror=float((pnl / df["max_loss"].astype(float)).mean()),
        avg_cost=float(costs.mean()),
        cost_pct_premium=cost_pct,
        max_dd=max_drawdown(ordered),
        worst_month=str(months.idxmin()),
        worst_month_pnl=float(months.min()),
    )


def breakdown(df: pd.DataFrame, by: str | list[str]) -> pd.DataFrame:
    """One metrics row per group of *by* (e.g. ``"trend"``, ``["spec", "vol"]``)."""
    keys = [by] if isinstance(by, str) else by
    rows = []
    for k, g in df.groupby(keys, sort=True):
        kk = k if isinstance(k, tuple) else (k,)
        rows.append({**dict(zip(keys, kk, strict=True)), **compute_metrics(g).model_dump()})
    return pd.DataFrame(rows)


class Split(BaseModel):
    model_config = ConfigDict(frozen=True)

    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def walk_forward_splits(
    start: dt.date, end: dt.date, *, train_months: int = 6, test_months: int = 2
) -> list[Split]:
    """Rolling month-aligned splits; each test window follows its train window, no overlap.

    The step equals *test_months*, so test windows tile [first test start, end].
    """
    if train_months < 1 or test_months < 1:
        msg = "train_months and test_months must be >= 1"
        raise ValueError(msg)
    first = dt.date(start.year, start.month, 1)
    out: list[Split] = []
    k = 0
    while True:
        tr_s = _add_months(first, k)
        te_s = _add_months(tr_s, train_months)
        te_e = _add_months(te_s, test_months) - dt.timedelta(days=1)
        if te_s > end:
            break
        out.append(
            Split(
                train_start=max(tr_s, start),
                train_end=te_s - dt.timedelta(days=1),
                test_start=te_s,
                test_end=min(te_e, end),
            )
        )
        k += test_months
    return out


def _in(df: pd.DataFrame, a: dt.date, b: dt.date) -> pd.DataFrame:
    e = df["entry_date"]
    return df[(e >= a) & (e <= b)]


def walk_forward_eval(
    df: pd.DataFrame,
    splits: Sequence[Split],
    *,
    min_train_trades: int = 10,
    score: str = "avg_ror",
) -> pd.DataFrame:
    """Pick the best ``spec`` on each train window by *score*, report it out-of-sample.

    Returns one row per split: chosen spec, its in-sample score and its
    out-of-sample metrics. Specs with fewer than *min_train_trades* train
    trades are ineligible; a split with no eligible spec is reported with
    ``chosen = None``.
    """
    rows = []
    for s in splits:
        tr = _in(df, s.train_start, s.train_end)
        # No look-ahead: a train trade is only usable if its outcome (expiry) is
        # known by the end of the train window.
        tr = tr[tr["expiration"] <= s.train_end]
        te = _in(df, s.test_start, s.test_end)
        best: tuple[float, str] | None = None
        for spec, g in tr.groupby("spec", sort=True):
            if len(g) < min_train_trades:
                continue
            v = getattr(compute_metrics(g), score)
            if not math.isfinite(v):
                continue
            if best is None or v > best[0]:
                best = (v, str(spec))
        row: dict[str, object] = {**s.model_dump(), "chosen": None, "train_score": math.nan}
        if best is not None:
            row["chosen"] = best[1]
            row["train_score"] = best[0]
            oos = compute_metrics(te[te["spec"] == best[1]])
            row.update({f"oos_{k}": v for k, v in oos.model_dump().items()})
        rows.append(row)
    return pd.DataFrame(rows)


def walk_forward_oos(df: pd.DataFrame, evaluated: pd.DataFrame) -> pd.DataFrame:
    """Concatenate the out-of-sample trades of each split's chosen spec (the WF equity)."""
    parts = [
        _in(df, r["test_start"], r["test_end"]).loc[lambda x, c=r["chosen"]: x["spec"] == c]
        for _, r in evaluated.iterrows()
        if r["chosen"] is not None
    ]
    return pd.concat(parts, ignore_index=True) if parts else df.iloc[0:0]
