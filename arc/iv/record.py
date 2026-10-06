"""Forward daily 30-DTE IV (``iv.record``, E4.12 / D55) with a Cboe ``iv30`` cross-check.

For each ticker (active list + open underlyings + today's candidates, ETFs included):
the Alpaca chain for 7-75 DTE, spot from :func:`arc.data.base.market_spot` (never a
one-sided half-price mid), and :func:`arc.features.vol.atm_iv_from_chain` at 30 DTE,
stored as ``alpaca_cm30``. SPY, QQQ and up to ``iv_crosscheck_max_names`` other names
are compared with Cboe's delayed ``iv30``; a row whose ``|ours - cboe|`` exceeds
``iv_crosscheck_max_pts`` carries ``detail.breach = true``, which the monitor's
``iv_crosscheck`` check turns into an [Ops] alert.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from arc.data.base import market_spot
from arc.features.vol import TARGET_DTE, atm_term_points, bracketing_points, constant_maturity_iv
from arc.iv.store import FORWARD, IvRow, IvStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from arc.data.base import MarketDataProvider

log = structlog.get_logger(__name__)

CHAIN_DTE_MIN = 7
CHAIN_DTE_MAX = 75
CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
#: Always cross-checked (index ETFs: Cboe's iv30 is liquid and stable there).
CROSSCHECK_ALWAYS = ("SPY", "QQQ")
#: Cboe prefixes cash indices with "_" (none are in our universe today).
_CBOE_INDEX = {"SPX", "NDX", "RUT", "VIX", "DJX", "XSP"}


def cboe_symbol(ticker: str) -> str:
    t = ticker.upper()
    return f"_{t}" if t in _CBOE_INDEX else t.replace(".", "/")


def parse_cboe_iv30(body: bytes | str) -> float | None:
    """Cboe delayed-quote JSON -> ``iv30`` as a decimal (``None`` when absent/zero)."""
    try:
        data = json.loads(body)["data"]
        v = float(data["iv30"])
    except (KeyError, TypeError, ValueError):
        return None
    return v / 100.0 if math.isfinite(v) and v > 0 else None


def fetch_cboe_iv30(
    ticker: str, get: Callable[[str], bytes]
) -> float | None:  # pragma: no cover - thin
    return parse_cboe_iv30(get(CBOE_QUOTE_URL.format(symbol=cboe_symbol(ticker))))


@dataclass(frozen=True)
class CrossCheck:
    ticker: str
    ours: float
    cboe: float | None
    max_pts: float

    @property
    def diff_pts(self) -> float | None:
        return None if self.cboe is None else round((self.ours - self.cboe) * 100, 2)

    @property
    def breach(self) -> bool:
        d = self.diff_pts
        return d is not None and abs(d) > self.max_pts


@dataclass
class RecordResult:
    day: _dt.date
    rows: list[IvRow] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    checks: list[CrossCheck] = field(default_factory=list)

    @property
    def breaches(self) -> list[CrossCheck]:
        return [c for c in self.checks if c.breach]

    def metrics(self) -> dict[str, Any]:
        return {
            "recorded": len(self.rows),
            "errors": len(self.errors),
            "crosscheck": {
                c.ticker: {
                    "ours": round(c.ours * 100, 2),
                    "cboe": None if c.cboe is None else round(c.cboe * 100, 2),
                    "diff_pts": c.diff_pts,
                }
                for c in self.checks
            },
            "breaches": [c.ticker for c in self.breaches],
        }


def iv30_from_chain(
    market: MarketDataProvider,
    ticker: str,
    day: _dt.date,
    *,
    max_spread_pct: float,
    target_dte: int = TARGET_DTE,
) -> IvRow:
    """One ``alpaca_cm30`` row for *ticker* on *day* (raises ``LookupError`` if none)."""
    spot = market_spot(market, ticker, day, max_spread_pct=max_spread_pct)
    if spot.price is None:
        msg = "no usable spot (one-sided quote and no recent close)"
        raise LookupError(msg)
    chain = market.option_chain(
        ticker, day + _dt.timedelta(days=CHAIN_DTE_MIN), day + _dt.timedelta(days=CHAIN_DTE_MAX)
    )
    points = atm_term_points(chain, spot.price, day)
    if not points:
        msg = f"no contract with an IV in the {CHAIN_DTE_MIN}-{CHAIN_DTE_MAX} DTE chain"
        raise LookupError(msg)
    lo, hi = bracketing_points(points, target_dte=target_dte)
    n_iv = sum(
        1 for c in chain if c.implied_volatility and c.implied_volatility > 0 and c.expiration > day
    )
    return IvRow(
        ticker=ticker.upper(),
        day=day,
        iv30=constant_maturity_iv(points, target_dte=target_dte),
        method="chain_cm30",
        source=FORWARD,
        spot=round(spot.price, 4),
        spot_basis=spot.basis,
        n_contracts=n_iv,
        detail={
            "chain_contracts": len(chain),
            "bracket": [
                None if p is None else {"dte": p[0], "atm_iv": round(p[1], 6)} for p in (lo, hi)
            ],
            "expiries": len(points),
        },
    )


def crosscheck_names(tickers: Sequence[str], max_names: int) -> list[str]:
    """SPY, QQQ, then the first *max_names* other tickers (input order)."""
    others = [t for t in tickers if t not in CROSSCHECK_ALWAYS][:max_names]
    return [*CROSSCHECK_ALWAYS, *others]


def record_day(
    conn: sqlite3.Connection,
    market: MarketDataProvider,
    tickers: Sequence[str],
    day: _dt.date,
    *,
    now: _dt.datetime,
    max_spread_pct: float,
    cboe_get: Callable[[str], bytes] | None,
    crosscheck_max_pts: float,
    crosscheck_max_names: int,
) -> RecordResult:
    """Record *day*'s 30-DTE IV for *tickers*; cross-check against Cboe (``cboe_get``)."""
    out = RecordResult(day=day)
    wanted = list(dict.fromkeys(t.upper() for t in tickers))
    check = set(crosscheck_names(wanted, crosscheck_max_names)) if cboe_get else set()
    # SPY/QQQ are always cross-checked, even when not on today's list.
    for t in [*wanted, *(c for c in CROSSCHECK_ALWAYS if c in check and c not in wanted)]:
        try:
            row = iv30_from_chain(market, t, day, max_spread_pct=max_spread_pct)
        except Exception as exc:  # noqa: BLE001 - one ticker must not sink the rest
            out.errors[t] = str(exc)[:300]
            log.warning("iv.record_failed", ticker=t, error=str(exc))
            continue
        if t in check and cboe_get is not None:
            try:
                cboe = fetch_cboe_iv30(t, cboe_get)
            except Exception as exc:  # noqa: BLE001 - the cross-check is best effort
                log.warning("iv.cboe_failed", ticker=t, error=str(exc))
                cboe = None
            cc = CrossCheck(t, row.iv30, cboe, crosscheck_max_pts)
            out.checks.append(cc)
            row.detail.update(
                {
                    "cboe_iv30": cboe,
                    "cboe_diff_pts": cc.diff_pts,
                    "crosscheck_max_pts": crosscheck_max_pts,
                    "breach": cc.breach,
                }
            )
        out.rows.append(row)
    IvStore(conn).upsert(out.rows, now=now)
    log.info(
        "iv.recorded",
        day=str(day),
        rows=len(out.rows),
        errors=len(out.errors),
        breaches=[c.ticker for c in out.breaches],
    )
    return out
