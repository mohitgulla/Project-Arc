"""ATM implied vol, IV rank and IV percentile for the chain scanner.

Definitions (kept identical to the E4.3 feature definitions so the two agree):

- **ATM IV** of an expiration: call/put IV averaged per strike, then linearly
  interpolated in strike at spot. Spot outside the strike range clamps to the
  nearest strike.
- **IV rank**: ``(IV_today - min) / (max - min)`` over the trailing *lookback*
  observations including today. A flat window returns 0.5.
- **IV percentile**: share of the prior observations in that window with IV
  strictly below today's.

History is a per-ticker CSV ``<dir>/<TICKER>.csv`` with header ``date,atm_iv``
(one row per session, IV as a decimal, e.g. ``0.142``). The scanner can append
today's ATM IV (``arc scan --record-iv``) so the history accumulates daily.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from arc.data.base import OptionContract

__all__ = [
    "IvStats",
    "atm_iv",
    "iv_percentile",
    "iv_rank",
    "iv_stats",
    "load_iv_history",
    "record_iv",
]

_CSV_HEADER = ("date", "atm_iv")


class IvStats(BaseModel):
    """IV context for one underlying on one day."""

    atm_iv: float | None = Field(None, description="Today's ~30-DTE ATM IV (decimal)")
    atm_iv_expiration: dt.date | None = None
    iv_rank: float | None = Field(None, ge=0.0, le=1.0)
    iv_percentile: float | None = Field(None, ge=0.0, le=1.0)
    observations: int = Field(0, description="IV observations in the rank window (incl. today)")
    lookback: int = 252


def atm_iv(contracts: Iterable[OptionContract], spot: float) -> float | None:
    """ATM IV of *contracts* (one expiration) at *spot*; ``None`` if no IVs."""
    per_strike: dict[float, list[float]] = defaultdict(list)
    for c in contracts:
        iv = c.implied_volatility
        if iv is not None and iv > 0:
            per_strike[c.strike].append(iv)
    if not per_strike:
        return None
    pts = sorted((k, sum(v) / len(v)) for k, v in per_strike.items())
    if spot <= pts[0][0]:
        return pts[0][1]
    if spot >= pts[-1][0]:
        return pts[-1][1]
    for (k0, v0), (k1, v1) in zip(pts, pts[1:], strict=False):
        if k0 <= spot <= k1:
            w = (spot - k0) / (k1 - k0)
            return v0 + w * (v1 - v0)
    raise AssertionError("unreachable: spot is inside the strike range")  # pragma: no cover


def _window(history: Mapping[dt.date, float], as_of: dt.date, today_iv: float, lookback: int):
    if lookback < 2:
        msg = "lookback must be >= 2"
        raise ValueError(msg)
    prior = [v for d, v in sorted(history.items()) if d < as_of]
    return [*prior[-(lookback - 1) :], today_iv]


def iv_rank(
    history: Mapping[dt.date, float], as_of: dt.date, today_iv: float, *, lookback: int = 252
) -> float:
    """IV rank of *today_iv* against *history* before *as_of* (0..1)."""
    w = _window(history, as_of, today_iv, lookback)
    lo, hi = min(w), max(w)
    if hi - lo <= 1e-12:
        return 0.5
    return (today_iv - lo) / (hi - lo)


def iv_percentile(
    history: Mapping[dt.date, float], as_of: dt.date, today_iv: float, *, lookback: int = 252
) -> float:
    """Share of prior observations in the window strictly below *today_iv* (0..1)."""
    w = _window(history, as_of, today_iv, lookback)
    prior = w[:-1]
    if not prior:
        return 0.5
    return sum(1 for v in prior if v < today_iv) / len(prior)


def iv_stats(
    history: Mapping[dt.date, float],
    as_of: dt.date,
    today_iv: float | None,
    *,
    expiration: dt.date | None = None,
    lookback: int = 252,
    min_obs: int = 20,
) -> IvStats:
    """Bundle ATM IV with rank/percentile; rank fields stay ``None`` under *min_obs*."""
    if today_iv is None:
        return IvStats(lookback=lookback)
    n = len(_window(history, as_of, today_iv, lookback))
    enough = n >= min_obs
    return IvStats(
        atm_iv=today_iv,
        atm_iv_expiration=expiration,
        iv_rank=iv_rank(history, as_of, today_iv, lookback=lookback) if enough else None,
        iv_percentile=iv_percentile(history, as_of, today_iv, lookback=lookback)
        if enough
        else None,
        observations=n,
        lookback=lookback,
    )


def _path(directory: Path, ticker: str) -> Path:
    return Path(directory) / f"{ticker.upper()}.csv"


def load_iv_history(directory: Path | str, ticker: str) -> dict[dt.date, float]:
    """Read ``<directory>/<TICKER>.csv``; a missing file is an empty history."""
    path = _path(Path(directory), ticker)
    if not path.is_file():
        return {}
    out: dict[dt.date, float] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            iv = float(row["atm_iv"])
            if iv <= 0:
                msg = f"{path}: non-positive IV {iv} on {row['date']}"
                raise ValueError(msg)
            out[dt.date.fromisoformat(row["date"])] = iv
    return out


def record_iv(directory: Path | str, ticker: str, day: dt.date, iv: float) -> Path:
    """Upsert *day*'s ATM IV into the ticker's history CSV (sorted by date)."""
    if iv <= 0:
        msg = f"IV must be positive, got {iv}"
        raise ValueError(msg)
    path = _path(Path(directory), ticker)
    hist = load_iv_history(directory, ticker)
    hist[day] = iv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_CSV_HEADER)
        for d, v in sorted(hist.items()):
            w.writerow([d.isoformat(), f"{v:.6f}"])
    return path
