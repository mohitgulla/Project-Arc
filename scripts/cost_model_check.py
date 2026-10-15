"""E7.14 study 6 (cost model check): measured closing-NBBO spreads vs config/costs.yaml.

Reads ThetaData EOD rows for a checkpoint universe, one session in every ``--every``
(default 5), and buckets each two-sided contract by:

* liquidity: the underlying's total option volume that session (the study framework's
  LIQUIDITY_BUCKETS);
* moneyness: |K / S − 1| (OTM side only: puts K < S, calls K >= S), 0-2.5 / 2.5-5 /
  5-10 / 10-20 %;
* DTE: 7-20 / 21-35 / 36-50 / 51-75.

Per bucket: contracts, median mid, median quoted spread, median spread/mid, the
costs.yaml estimate ``max(spread_min, spread_pct * mid)``, the measured/estimated ratio
(median and mean), and the share the estimate understates. Then fits the two knobs
(spread_pct, spread_min) that minimise median |log(actual / estimate)| on the
tradeable region (spread/mid <= 0.35, the backtest's quoted-mark drop rule).

Aggregates only (ThetaData terms): no quote row leaves this script.

    .venv/bin/python scripts/cost_model_check.py --universe c125 --out <dir>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arc.backtest.costs import load_cost_model  # noqa: E402
from arc.backtest.study import LIQUIDITY_BUCKETS  # noqa: E402
from arc.backtest.underlying import UnderlyingStore  # noqa: E402
from arc.backtest.universe import checkpoint_symbols, load_universe  # noqa: E402
from arc.data.history.store import ParquetHistoryStore  # noqa: E402

MONEY = (
    (0.0, 0.025, "0-2.5%"),
    (0.025, 0.05, "2.5-5%"),
    (0.05, 0.10, "5-10%"),
    (0.10, 0.20, "10-20%"),
)
DTE = ((7, 20, "7-20"), (21, 35, "21-35"), (36, 50, "36-50"), (51, 75, "51-75"))
MAX_SPREAD_PCT = 0.35


def _label(
    x: np.ndarray, edges: tuple[tuple[float, float, str], ...], *, right_closed: bool
) -> np.ndarray:
    out = np.full(len(x), "", dtype=object)
    for lo, hi, name in edges:
        m = (x >= lo) & ((x <= hi) if right_closed else (x < hi))
        out[m & (out == "")] = name
    return out


def _liq(v: float) -> str:
    for name, hi in LIQUIDITY_BUCKETS:
        if v < hi:
            return name
    return LIQUIDITY_BUCKETS[-1][0]


def ticker_rows(args: tuple[str, str, str, int, str, str]) -> pd.DataFrame:
    data_dir, provider, ticker, every, start, end = args
    store = ParquetHistoryStore(Path(data_dir))
    days = sorted(
        d
        for d in store.cached_dates(provider, ticker)
        if dt.date.fromisoformat(start) <= d <= dt.date.fromisoformat(end)
    )[::every]
    if not days:
        return pd.DataFrame()
    closes = UnderlyingStore(Path(data_dir)).read_bars(ticker)["close"]
    frames = []
    for d in days:
        spot = closes.get(d)
        if spot is None or not np.isfinite(spot):
            continue
        raw = store.read(provider, ticker, d, d)
        if raw.empty or "bid" not in raw:
            continue
        vol_total = (
            float(np.nan_to_num(raw["volume"].to_numpy(float)).sum()) if "volume" in raw else 0.0
        )
        dte = (pd.to_datetime(raw["expiration"]) - pd.Timestamp(d)).dt.days.to_numpy()
        bid = raw["bid"].to_numpy(float)
        ask = raw["ask"].to_numpy(float)
        k = raw["strike"].to_numpy(float)
        call = (raw["right"].astype(str) == "call").to_numpy()
        ok = (
            np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > bid) & (dte >= 7) & (dte <= 75)
        )
        otm = np.where(call, k >= spot, k < spot)
        m = np.abs(k / spot - 1.0)
        ok &= otm & (m <= 0.20)
        if not ok.any():
            continue
        mid = (bid[ok] + ask[ok]) / 2.0
        frames.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "liq": _liq(vol_total),
                    "money": _label(m[ok], MONEY, right_closed=False),
                    "dte": _label(dte[ok].astype(float), DTE, right_closed=True),
                    "mid": mid,
                    "spread": ask[ok] - bid[ok],
                }
            )
        )
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _estimate(mid: np.ndarray, pct: float, floor: float) -> np.ndarray:
    return np.maximum(floor, pct * mid)


def bucket_table(
    df: pd.DataFrame, pct: float, floor: float, key: list[str]
) -> list[dict[str, object]]:
    est = _estimate(df["mid"].to_numpy(), pct, floor)
    d = df.assign(est=est, ratio=df["spread"] / est, rel=df["spread"] / df["mid"])
    out = []
    for k, g in d.groupby(key, sort=True):
        out.append(
            {
                **dict(zip(key, k if isinstance(k, tuple) else (k,), strict=True)),
                "contracts": len(g),
                "median_mid": round(float(g["mid"].median()), 3),
                "median_spread": round(float(g["spread"].median()), 3),
                "median_spread_pct_of_mid": round(float(g["rel"].median()) * 100, 2),
                "median_estimate": round(float(g["est"].median()), 3),
                "median_ratio": round(float(g["ratio"].median()), 2),
                "mean_ratio": round(float(g["ratio"].mean()), 2),
                "estimate_understates_share": round(float((g["ratio"] > 1).mean()), 3),
            }
        )
    return out


def fit(df: pd.DataFrame) -> tuple[float, float, float]:
    """(spread_pct, spread_min, median |log ratio|) minimising the median |log ratio|."""
    t = df[df["spread"] / df["mid"] <= MAX_SPREAD_PCT]
    mid, spr = t["mid"].to_numpy(), t["spread"].to_numpy()
    best = (0.0, 0.0, np.inf)
    for pct in np.arange(0.01, 0.201, 0.005):
        for floor in np.arange(0.01, 0.151, 0.01):
            err = float(np.median(np.abs(np.log(spr / _estimate(mid, pct, floor)))))
            if err < best[2]:
                best = (round(float(pct), 3), round(float(floor), 2), err)
    return best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--universe", default="c125")
    ap.add_argument("--provider", default="thetadata")
    ap.add_argument("--every", type=int, default=5, help="Sample one session in N")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    manifest, rows, _ = load_universe(a.data_dir, None)
    tickers = checkpoint_symbols(rows, a.universe)
    jobs = [(str(a.data_dir), a.provider, t, a.every, a.start, a.end) for t in tickers]
    with ProcessPoolExecutor(a.workers) as ex:
        parts = [p for p in ex.map(ticker_rows, jobs) if len(p)]
    df = pd.concat(parts, ignore_index=True)
    cost = load_cost_model()
    pct, floor, err = fit(df)
    cur_err = float(
        np.median(
            np.abs(
                np.log(
                    df.loc[df["spread"] / df["mid"] <= MAX_SPREAD_PCT, "spread"]
                    / _estimate(
                        df.loc[df["spread"] / df["mid"] <= MAX_SPREAD_PCT, "mid"].to_numpy(),
                        cost.spread_pct,
                        cost.spread_min,
                    )
                )
            )
        )
    )
    tradeable = df[df["spread"] / df["mid"] <= MAX_SPREAD_PCT]
    res = {
        "universe": a.universe,
        "universe_version": manifest.version,
        "provider": a.provider,
        "sample_every": a.every,
        "tickers": len(parts),
        "contracts": len(df),
        "tradeable_contracts": len(tradeable),
        "current": {
            "spread_pct": cost.spread_pct,
            "spread_min": cost.spread_min,
            "median_abs_log_err": round(cur_err, 3),
        },
        "fitted": {"spread_pct": pct, "spread_min": floor, "median_abs_log_err": round(err, 3)},
        "all": bucket_table(df.assign(all="all"), cost.spread_pct, cost.spread_min, ["all"]),
        "tradeable": bucket_table(
            tradeable.assign(all="tradeable"), cost.spread_pct, cost.spread_min, ["all"]
        ),
        "by_liquidity": bucket_table(tradeable, cost.spread_pct, cost.spread_min, ["liq"]),
        "by_moneyness": bucket_table(tradeable, cost.spread_pct, cost.spread_min, ["money"]),
        "by_dte": bucket_table(tradeable, cost.spread_pct, cost.spread_min, ["dte"]),
        "by_liquidity_moneyness": bucket_table(
            tradeable, cost.spread_pct, cost.spread_min, ["liq", "money"]
        ),
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "cost_model_check.json").write_text(json.dumps(res, indent=1) + "\n")
    print(
        json.dumps(
            {
                k: res[k]
                for k in ("contracts", "tradeable_contracts", "current", "fitted", "tradeable")
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
