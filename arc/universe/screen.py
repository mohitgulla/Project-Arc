"""Deterministic liquidity screen for non-seed Scout candidates (D28).

:func:`screen_liquidity` is a **pure function** of the measured
:class:`LiquidityMetrics` and the :class:`LiquidityThresholds` from
``config/universe.yaml``. Every check must pass, and a missing number fails
closed. Loosening any threshold can only turn a fail into a pass (monotone;
property-tested).

:func:`measure_liquidity` gathers the numbers from a ``MarketDataProvider``
(the same one the scanner uses). It never raises: a data error becomes a
metrics object with ``error`` set, which the screen fails.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from arc.universe.config import LiquidityThresholds

if TYPE_CHECKING:
    from arc.data.base import MarketDataProvider, OptionContract

__all__ = [
    "ATM_TARGET_DTE",
    "LiquidityMetrics",
    "LiquidityThresholds",
    "ScreenResult",
    "measure_liquidity",
    "screen_liquidity",
]

ATM_TARGET_DTE = 30  # same expiry pick as the scanner's IV context (arc.scanner.scan)
_ADV_LOOKBACK_CALENDAR_DAYS = 45


class LiquidityMetrics(BaseModel):
    """What the screen judges. ``None`` = not measurable (fails closed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    as_of: _dt.date
    price: float | None = None
    adv_shares: float | None = None
    adv_sessions: int = 0
    expiries_in_window: int = 0
    atm_expiry: _dt.date | None = None
    atm_strike: float | None = None
    atm_open_interest: int | None = None
    atm_spread_pct: float | None = None
    error: str | None = None


class ScreenResult(BaseModel):
    """Screen verdict: ``passed`` plus one short line per failed check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    passed: bool
    failures: list[str] = Field(default_factory=list)
    metrics: LiquidityMetrics

    def detail(self) -> str:
        return "; ".join(self.failures) if self.failures else "passed"


def _fmt_n(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return f"{v:.0f}"


def screen_liquidity(m: LiquidityMetrics, t: LiquidityThresholds) -> ScreenResult:
    """Pure: does *m* clear every threshold in *t*? Missing data fails."""
    f: list[str] = []
    if m.error:
        f.append(f"no market data ({m.error[:80]})")
    if m.price is None:
        f.append("price unknown")
    elif m.price < t.min_price:
        f.append(f"price ${m.price:.2f} < ${t.min_price:g}")
    if m.adv_shares is None:
        f.append("ADV unknown")
    elif m.adv_shares < t.min_adv_shares:
        f.append(f"ADV {_fmt_n(m.adv_shares)} < {_fmt_n(t.min_adv_shares)}")
    if m.expiries_in_window < 1:
        f.append("no expiry in the DTE window")
    if m.atm_open_interest is None:
        f.append("near-ATM OI unknown")
    elif m.atm_open_interest < t.min_atm_open_interest:
        f.append(f"near-ATM OI {m.atm_open_interest} < {t.min_atm_open_interest}")
    if m.atm_spread_pct is None:
        f.append("ATM spread unknown")
    elif m.atm_spread_pct > t.max_atm_spread_pct:
        f.append(f"ATM spread {m.atm_spread_pct:.0%} > {t.max_atm_spread_pct:.0%}")
    return ScreenResult(ticker=m.ticker, passed=not f, failures=f, metrics=m)


def _spread_pct(c: OptionContract) -> float | None:
    if c.bid is None or c.ask is None or c.bid <= 0 or c.ask <= 0 or c.ask < c.bid:
        return None
    mid = (c.bid + c.ask) / 2
    return (c.ask - c.bid) / mid if mid > 0 else None


def measure_liquidity(
    market: MarketDataProvider,
    ticker: str,
    *,
    today: _dt.date,
    dte_window: tuple[int, int],
    thresholds: LiquidityThresholds,
    adv_market: MarketDataProvider | None = None,
) -> LiquidityMetrics:
    """Measure *ticker* (never raises). ADV uses completed sessions before *today* only."""
    from arc.data.base import reference_price

    ticker = ticker.upper()
    out: dict[str, object] = {"ticker": ticker, "as_of": today}
    try:
        price = reference_price(market, ticker, today=today)
        out["price"] = price
        # End the request yesterday: completed sessions only, and the free SIP tier
        # refuses any window that reaches into the last 15 minutes.
        bars = [
            b
            for b in (adv_market or market).history_bars(
                ticker,
                today - _dt.timedelta(days=_ADV_LOOKBACK_CALENDAR_DAYS),
                today - _dt.timedelta(days=1),
            )
            if b.timestamp.date() < today
        ][-thresholds.adv_days :]
        out["adv_sessions"] = len(bars)
        if len(bars) >= thresholds.adv_days:
            out["adv_shares"] = sum(b.volume for b in bars) / len(bars)
        lo, hi = dte_window
        exp_lo, exp_hi = today + _dt.timedelta(days=lo), today + _dt.timedelta(days=hi)
        chain = [
            c
            for c in market.option_chain(ticker, exp_lo, exp_hi)
            if exp_lo <= c.expiration <= exp_hi
        ]
        expiries = sorted({c.expiration for c in chain})
        out["expiries_in_window"] = len(expiries)
        if expiries and price:
            exp = min(expiries, key=lambda e: (abs((e - today).days - ATM_TARGET_DTE), e))
            near = [c for c in chain if c.expiration == exp]
            by_distance = sorted({c.strike for c in near}, key=lambda k: (abs(k - price), k))
            strike = by_distance[0]
            atm = [c for c in near if c.strike == strike]
            near_atm = set(by_distance[: thresholds.atm_strikes])
            ois = [
                c.open_interest
                for c in near
                if c.strike in near_atm and c.open_interest is not None
            ]
            spreads = [s for c in atm if (s := _spread_pct(c)) is not None]
            out |= {
                "atm_expiry": exp,
                "atm_strike": strike,
                "atm_open_interest": sum(ois) if ois else None,
                "atm_spread_pct": sum(spreads) / len(spreads) if spreads else None,
            }
    except Exception as exc:  # noqa: BLE001 - any data failure fails the screen closed
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return LiquidityMetrics.model_validate(out)
