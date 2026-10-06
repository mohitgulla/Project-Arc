"""``arc iv validate`` (E4.12, D55): our IV series vs the Option Strategist weekly file.

For each ticker with an ``optionstrategist`` row on the latest OS date, compare on
that date:

- our iv30 (forward, else backfill) vs OS ``cur_iv``          -> |diff| in vol pts
- our 252-obs IV percentile vs OS percentile                  -> |diff| in pct pts
  (also shown over OS's own window, ``Days`` readings, for the method-vs-window read)
- our HV20 (close-to-close, 252-day annualised) vs OS ``hv20`` -> |diff| in vol pts

Pass bar (card E4.12): median |iv diff| <= 2.5 vol pts, median |percentile diff|
<= 15 pts, and >= 80 % of names within 25 percentile pts.
"""

from __future__ import annotations

import datetime as _dt
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arc.iv.store import OPTIONSTRATEGIST, IvStore
from arc.scanner.iv import iv_percentile

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

PASS_IV_PTS = 2.5
PASS_PCT_MEDIAN = 15.0
PASS_PCT_WITHIN = 25.0
PASS_SHARE_WITHIN = 0.80
LOOKBACK = 252


@dataclass(frozen=True)
class Comparison:
    ticker: str
    day: _dt.date
    ours_iv: float | None
    os_iv: float
    ours_pct: float | None
    ours_pct_oswin: float | None
    os_pct: float
    os_days: int | None
    ours_hv20: float | None
    os_hv20: float | None
    observations: int

    @property
    def iv_diff(self) -> float | None:
        return None if self.ours_iv is None else abs(self.ours_iv - self.os_iv) * 100

    @property
    def pct_diff(self) -> float | None:
        return None if self.ours_pct is None else abs(self.ours_pct - self.os_pct) * 100

    @property
    def hv_diff(self) -> float | None:
        if self.ours_hv20 is None or self.os_hv20 is None:
            return None
        return abs(self.ours_hv20 - self.os_hv20) * 100


@dataclass
class Validation:
    day: _dt.date | None
    rows: list[Comparison] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # OS rows we have no series for

    def _vals(self, attr: str) -> list[float]:
        return [v for r in self.rows if (v := getattr(r, attr)) is not None]

    def median(self, attr: str) -> float | None:
        v = self._vals(attr)
        return statistics.median(v) if v else None

    @property
    def share_pct_within(self) -> float | None:
        v = self._vals("pct_diff")
        return sum(1 for x in v if x <= PASS_PCT_WITHIN) / len(v) if v else None

    def verdict(self) -> dict[str, bool | None]:
        iv, pct, share = self.median("iv_diff"), self.median("pct_diff"), self.share_pct_within
        return {
            "median_iv": None if iv is None else iv <= PASS_IV_PTS,
            "median_pct": None if pct is None else pct <= PASS_PCT_MEDIAN,
            "share_within": None if share is None else share >= PASS_SHARE_WITHIN,
        }

    @property
    def passed(self) -> bool:
        v = self.verdict()
        return all(x is True for x in v.values())


def hv20(closes: Sequence[float]) -> float | None:
    """Annualised 20-return close-to-close HV (sample stdev, 252 days)."""
    import math

    if len(closes) < 21:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:], strict=True)]
    return statistics.stdev(rets) * math.sqrt(252)


def validate(
    conn: sqlite3.Connection,
    *,
    tickers: Sequence[str] | None = None,
    closes: Callable[[str, _dt.date], list[float]] | None = None,
) -> Validation:
    """Compare on the latest OS date. *closes(ticker, day)* gives the underlying's
    daily closes up to *day* (oldest first) for HV20; ``None`` skips HV."""
    row = conn.execute(
        "SELECT MAX(day) FROM iv_daily WHERE source = ?", (OPTIONSTRATEGIST,)
    ).fetchone()
    if not row or row[0] is None:
        return Validation(day=None)
    day = _dt.date.fromisoformat(str(row[0]))
    store = IvStore(conn)
    want = {t.upper() for t in tickers} if tickers else None
    out = Validation(day=day)
    for os_row in store.rows_on(day, OPTIONSTRATEGIST):
        if want is not None and os_row.ticker not in want:
            continue
        series = store.series(os_row.ticker, until=day)
        if day not in series:
            out.missing.append(os_row.ticker)
            continue
        today = series[day]
        n = min(len(series), LOOKBACK)
        pct = iv_percentile(series, day, today, lookback=LOOKBACK) if len(series) >= 2 else None
        oswin = os_row.ext_days or LOOKBACK
        pct_os = (
            iv_percentile(series, day, today, lookback=max(oswin, 2)) if len(series) >= 2 else None
        )
        ours_hv = hv20(closes(os_row.ticker, day)) if closes else None
        out.rows.append(
            Comparison(
                ticker=os_row.ticker,
                day=day,
                ours_iv=today,
                os_iv=os_row.iv30,
                ours_pct=pct,
                ours_pct_oswin=pct_os,
                os_pct=float(os_row.ext_percentile or 0.0),
                os_days=os_row.ext_days,
                ours_hv20=ours_hv,
                os_hv20=os_row.hv20,
                observations=n,
            )
        )
    out.rows.sort(key=lambda r: r.ticker)
    return out


def _f(v: float | None, pct: bool = False) -> str:
    if v is None:
        return "  n/a"
    return f"{v * 100:5.1f}" if pct else f"{v:5.1f}"


def format_validation(v: Validation, *, worst: int = 5) -> str:
    if v.day is None:
        return "no optionstrategist rows stored: run `arc iv import-optionstrategist` first"
    lines = [
        f"IV validation vs Option Strategist on {v.day} ({len(v.rows)} names; internal use only)",
        f"{'ticker':<7} {'ours iv':>7} {'os iv':>6} {'|d|':>5}   {'ours %':>6} {'os %':>5} "
        f"{'|d|':>5} {'ours%@os':>8}   {'hv20':>5} {'os hv':>5} {'|d|':>5}  obs",
    ]
    for r in v.rows:
        lines.append(
            f"{r.ticker:<7} {_f(r.ours_iv, True):>7} {_f(r.os_iv, True):>6} {_f(r.iv_diff):>5}   "
            f"{_f(r.ours_pct, True):>6} {_f(r.os_pct, True):>5} {_f(r.pct_diff):>5} "
            f"{_f(r.ours_pct_oswin, True):>8}   {_f(r.ours_hv20, True):>5} "
            f"{_f(r.os_hv20, True):>5} {_f(r.hv_diff):>5}  {r.observations}"
        )
    if v.missing:
        lines.append(f"no series on {v.day} for: {', '.join(sorted(v.missing)[:30])}")
    med_iv, med_pct, med_hv = v.median("iv_diff"), v.median("pct_diff"), v.median("hv_diff")
    share = v.share_pct_within
    ok = v.verdict()

    def mark(x: bool | None) -> str:
        return "n/a" if x is None else ("PASS" if x else "FAIL")

    lines += [
        "",
        f"median |iv diff|   {_f(med_iv)} vol pts  (bar <= {PASS_IV_PTS})   "
        f"{mark(ok['median_iv'])}",
        f"median |pct diff|  {_f(med_pct)} pts      (bar <= {PASS_PCT_MEDIAN:g})    "
        f"{mark(ok['median_pct'])}",
        f"within {PASS_PCT_WITHIN:g} pct pts   "
        f"{'n/a' if share is None else f'{share:.0%}'}          (bar >= {PASS_SHARE_WITHIN:.0%})"
        f"   {mark(ok['share_within'])}",
        f"median |hv20 diff| {_f(med_hv)} vol pts  (info)",
        f"overall: {'PASS' if v.passed else 'FAIL'}",
    ]
    for attr, label in (("iv_diff", "iv"), ("pct_diff", "percentile")):
        top = sorted(
            (r for r in v.rows if getattr(r, attr) is not None),
            key=lambda r: -getattr(r, attr),
        )[:worst]
        if top:
            lines.append(
                f"worst {label}: " + ", ".join(f"{r.ticker} {getattr(r, attr):.1f}" for r in top)
            )
    return "\n".join(lines)
