"""Synthetic EOD history for the E7.10 data-layer tests (no network, deterministic).

``synth_rows`` builds one underlying's option rows per session from BSM at a chosen
ATM IV per expiry, with closing NBBO (``bid``/``ask``), trade closes, volume and open
interest, so the quoted, smile and close mark modes, IV30, earnings detection and the
liquidity screen all have something real to read.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from arc.data.history.base import OptionEodRow
from arc.data.history.store import ParquetHistoryStore
from arc.pricing.bs import price_vectorized
from arc.utils.calendar import sessions_between

R = 0.04
#: (session, expiry) -> ATM IV; default flat 25 vol with a gentle cycle.
IvFn = Callable[[int, dt.date, dt.date], float]


def default_iv(i: int, day: dt.date, exp: dt.date) -> float:
    return 0.25 + 0.05 * math.sin(i / 9.0)


def expiries_for(day: dt.date, n: int = 4) -> list[dt.date]:
    """Fridays 1, 2, 4 and 6 weeks out (a weekly front and three monthlies-ish)."""
    fri = day + dt.timedelta(days=(4 - day.weekday()) % 7 or 7)
    return [fri + dt.timedelta(weeks=w) for w in (0, 1, 3, 5)][:n]


def synth_rows(
    ticker: str,
    sessions: Sequence[dt.date],
    spots: Sequence[float],
    *,
    provider: str = "thetadata",
    iv_fn: IvFn = default_iv,
    strikes_each_side: int = 8,
    step: float = 2.5,
    spread_frac: float = 0.04,
    oi: float = 400.0,
    quoted: bool = True,
) -> dict[dt.date, list[OptionEodRow]]:
    out: dict[dt.date, list[OptionEodRow]] = {}
    for i, (day, spot) in enumerate(zip(sessions, spots, strict=True)):
        rows: list[OptionEodRow] = []
        atm = round(spot / step) * step
        ks = [atm + step * j for j in range(-strikes_each_side, strikes_each_side + 1)]
        for exp in expiries_for(day):
            t = (exp - day).days / 365.0
            sig = iv_fn(i, day, exp)
            for right in ("call", "put"):
                flag = np.full(len(ks), "c" if right == "call" else "p")
                px = price_vectorized(
                    flag,
                    np.full(len(ks), spot),
                    np.asarray(ks, dtype=float),
                    np.full(len(ks), t),
                    np.full(len(ks), R),
                    np.full(len(ks), sig),
                )
                for k, p in zip(ks, px, strict=True):
                    p = float(p)
                    if p < 0.01:
                        continue
                    half = max(0.01, spread_frac * p / 2)
                    rows.append(
                        OptionEodRow(
                            provider=provider,
                            underlying=ticker,
                            date=day,
                            symbol=f"{ticker}{exp:%y%m%d}{right[0].upper()}{int(k * 1000):08d}",
                            expiration=exp,
                            strike=k,
                            right=right,
                            close=round(p, 4),
                            volume=50.0,
                            bid=round(p - half, 4) if quoted else None,
                            ask=round(p + half, 4) if quoted else None,
                            open_interest=oi if quoted else None,
                        )
                    )
        out[day] = rows
    return out


def spot_path(n: int, start: float = 100.0, seed: int = 3) -> list[float]:
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0, 0.012, n)
    return [float(start * math.exp(x)) for x in np.cumsum(r)]


def write_history(
    data_dir: Path,
    ticker: str,
    rows: dict[dt.date, list[OptionEodRow]],
    *,
    provider: str = "thetadata",
    with_oi: bool = True,
) -> None:
    store = ParquetHistoryStore(data_dir)
    for day, rs in rows.items():
        store.write_day(provider, ticker, day, rs, with_oi=with_oi)


def write_underlying(
    data_dir: Path, ticker: str, sessions: Sequence[dt.date], spots: Sequence[float]
) -> None:
    from arc.backtest.entry_filter import OhlcStore
    from arc.backtest.underlying import UnderlyingStore

    bars = pd.DataFrame(
        {"close": list(spots), "volume": [2_000_000.0] * len(spots)}, index=list(sessions)
    )
    UnderlyingStore(data_dir).write_bars(ticker, bars)
    ohlc = pd.DataFrame(
        {
            "open": list(spots),
            "high": [s * 1.01 for s in spots],
            "low": [s * 0.99 for s in spots],
            "close": list(spots),
            "vwap": list(spots),
        },
        index=list(sessions),
    )
    OhlcStore(data_dir).write(ticker, ohlc)


def write_universe(
    data_dir: Path,
    symbols: Sequence[str],
    membership: Sequence[tuple[dt.date, str, bool]],
    *,
    version: str = "u-test",
    asset_types: dict[str, str] | None = None,
) -> str:
    """A minimal E7.7 build: pull list in *symbols* order (all c125), membership rows."""
    from arc.backtest.universe import (
        MembershipRow,
        UniverseBuild,
        UniverseConfig,
        UniverseManifest,
        UniverseRow,
        write_build,
    )

    cfg = UniverseConfig(target_size=4, checkpoints=[2, 4], always=[])
    rows = [
        UniverseRow(
            position=i + 1,
            symbol=s,
            bucket=4,
            bucket_name="rest",
            asset_type=(asset_types or {}).get(s, "stock"),  # type: ignore[arg-type]
            checkpoint=cfg.checkpoint_label(i + 1),
        )
        for i, s in enumerate(symbols)
    ]
    mem = [
        MembershipRow(quarter=q, symbol=s, in_top=top, in_pull_list=s in symbols, rank=1)
        for q, s, top in membership
    ]
    manifest = UniverseManifest(
        version=version,
        built_at=dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC),
        config_hash=cfg.config_hash(),
        config=cfg.model_dump(mode="json"),
        as_of=dt.date(2026, 10, 9),
        stage1_only=True,
        candidate_source="test",
        candidate_count=len(symbols),
        pull_list_size=len(symbols),
    )
    write_build(UniverseBuild(rows=rows, membership=mem, manifest=manifest), data_dir)
    return version


def sessions(start: dt.date, n: int) -> list[dt.date]:
    out = sessions_between(start, start + dt.timedelta(days=n * 2 + 10))
    return list(out[:n])


def marks_golden_payload(remark: Callable[..., object], clean: Callable[..., object]) -> dict:
    """smile + close mark outputs on a fixed alpaca-style chain (golden for the E7.10 move).

    *remark* / *clean* are ``remark_chains`` / ``clean_chains`` (from wherever they live).
    """
    from arc.backtest.costs import load_cost_model
    from arc.backtest.engine import prepare_chains

    days = sessions(dt.date(2024, 4, 15), 3)
    spots = [100.0, 101.3, 99.2]
    rows = synth_rows("XYZ", days, spots, provider="alpaca", quoted=False)
    # two stale prints: far off their neighbours' IV
    flat = [r.model_dump() for d in days for r in rows[d]]
    flat[7]["close"] = round(flat[7]["close"] * 1.7, 4)
    flat[40]["close"] = round(flat[40]["close"] * 0.5, 4)
    raw = pd.DataFrame(flat)
    closes = pd.Series(spots, index=days)
    cost = load_cost_model(Path(__file__).resolve().parent.parent / "config" / "costs.yaml")
    chains = prepare_chains(raw, closes, cost=cost, r=R, dte_min=1, dte_max=70)
    cols = ["symbol", "mid", "spread", "iv", "delta"]

    def dump(ch: dict) -> dict:
        out = {}
        for d in sorted(ch):
            g = ch[d].sort_values("symbol")[cols]
            out[d.isoformat()] = [
                [r[0], *(None if not math.isfinite(float(v)) else float(v) for v in r[1:])]
                for r in g.itertuples(index=False)
            ]
        return out

    return {
        "smile": dump(remark(chains, closes, r=R, cost=cost)),  # type: ignore[arg-type]
        "close": dump(clean(chains, max_dev=0.15, window=5)),  # type: ignore[arg-type]
    }
