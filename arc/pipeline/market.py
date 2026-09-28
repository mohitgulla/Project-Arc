"""Deterministic inputs for sizing and the gate: account, portfolio, quotes, earnings.

Everything here is plain code over market data, the audit store and the broker
account. No persona text is read. Wherever a fact is unknown, it fails closed:

* an unpriceable open position blocks the whole propose step,
  :class:`PortfolioError`;
* an unknown earnings date is left out of ``next_earnings``, so the gate
  rejects short premium on it;
* a missing ``last_equity`` becomes 0, so the gate's daily-loss rule fails.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING

import structlog

from arc.gate.inputs import AccountSnapshot, ClosedLot, MarketSnapshot, Portfolio, Position, Quote
from arc.models import Greeks, Leg, LegIntent
from arc.scanner.iv import atm_iv
from arc.structures import MarketInputs, analyze, max_gain_loss, net_greeks, parse_occ
from arc.utils.calendar import ET, dte_calendar

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence

    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.data.base import MarketDataProvider, OptionContract
    from arc.models import Structure

log = structlog.get_logger(__name__)

__all__ = [
    "ETF_UNDERLYINGS",
    "PortfolioError",
    "PricedStructure",
    "account_snapshot",
    "build_portfolio",
    "limit_price",
    "market_snapshot",
    "next_earnings",
    "price_structure",
    "settled_cash",
]

# Funds have no earnings reports. Every other underlying needs a known date
# (from the earnings connector), or the gate fails closed on short premium.
ETF_UNDERLYINGS: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU"}
)


class PortfolioError(RuntimeError):
    """Open positions could not be valued, so no new trade is proposed (fail closed)."""


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


def settled_cash(info: AccountInfo) -> Decimal:
    """Cash a no-margin (``cash_settled``) account can spend now (D25).

    Alpaca reports no "settled cash" field. The most conservative of the fields it
    does report is used: ``cash`` (includes unsettled proceeds on a cash account),
    ``non_marginable_buying_power`` (cash not backed by margin) and
    ``options_buying_power`` (what Alpaca will let options orders use), floored at 0.
    A field Alpaca leaves out is skipped; ``cash`` is always present.
    """
    fields = [info.cash, info.non_marginable_buying_power, info.options_buying_power]
    return max(min(v for v in fields if v is not None), Decimal(0))


def account_snapshot(info: AccountInfo, now: _dt.datetime) -> AccountSnapshot:
    """Gate view of the account. The halt flag is stamped later by ``HaltSwitch.apply``."""
    return AccountSnapshot(
        equity=info.equity,
        last_equity=info.last_equity if info.last_equity is not None else Decimal(0),
        settled_cash=settled_cash(info),
        as_of=now,
    )


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------


def next_earnings(
    conn: sqlite3.Connection, tickers: Iterable[str], today: _dt.date
) -> dict[str, _dt.date | None]:
    """Next known earnings date per ticker, taken from the earnings connector's raw docs.

    An ETF maps to ``None`` (it has no earnings). A stock without a stored future
    date is **omitted**, meaning unknown, so the gate fails closed on short premium.
    """
    out: dict[str, _dt.date | None] = {}
    rows = conn.execute("SELECT url, tickers_hint FROM raw_docs WHERE source = 'earnings'")
    dates: dict[str, list[_dt.date]] = defaultdict(list)
    for row in rows.fetchall():
        # url: https://finnhub.io/calendar/earnings/<SYMBOL>/<YYYY-MM-DD>
        parts = str(row["url"]).rstrip("/").split("/")
        try:
            day = _dt.date.fromisoformat(parts[-1])
        except ValueError:
            continue
        for sym in json.loads(row["tickers_hint"] or "[]") or [parts[-2]]:
            dates[str(sym).upper()].append(day)
    for t in tickers:
        t = t.upper()
        future = sorted(d for d in dates.get(t, []) if d >= today)
        if future:
            out[t] = future[0]
        elif t in ETF_UNDERLYINGS:
            out[t] = None
    return out


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


def _group_legs(
    conn: sqlite3.Connection | None,
    positions: Sequence[BrokerPosition],
) -> list[tuple[str, str, list[Leg]]]:
    """Open option legs as valuation groups ``(root, label, legs)``: one per structure.

    E6.3 / Sentinel S-5: broker legs are attributed to Arc's ``open_structures``
    (:func:`arc.reconcile.attribution.attribute`), so each structure is valued on
    its own even when two share an underlying and an expiration. Legs no local
    structure accounts for are grouped by ``(root, expiration)``; the reconciler
    reports them as a mismatch. ``check_per_underlying`` sums the rows per root.
    """
    from arc.reconcile.attribution import attribute, broker_legs, holdings_from_rows

    try:
        legs, _ = broker_legs(positions)
    except ValueError as exc:
        raise PortfolioError(str(exc)) from exc
    entry = {leg.occ_symbol: leg.avg_entry_price for leg in legs}
    holdings = []
    if conn is not None:
        rows = conn.execute(
            "SELECT * FROM open_structures WHERE status = 'open' ORDER BY opened_at, id"
        ).fetchall()
        holdings = holdings_from_rows(dict(r) for r in rows)
    att = attribute(holdings, legs)
    groups: list[tuple[str, str, list[Leg]]] = [
        (
            h.ticker,
            h.structure_id,
            [
                Leg(
                    occ_symbol=sym,
                    side=LegIntent.LONG if q > 0 else LegIntent.SHORT,
                    ratio=abs(q),
                    premium=h.premiums.get(sym) or entry.get(sym),
                )
                for sym, q in sorted(h.legs.items())
                if q != 0
            ],
        )
        for h in att.attributed
    ]
    for (root, exp), group in sorted(att.leftover_groups(entry).items()):
        groups.append((root, f"{root} {exp.isoformat()}", group))
    return groups


def build_portfolio(
    conn: sqlite3.Connection,
    positions: Sequence[BrokerPosition],
    market: MarketDataProvider,
    *,
    now: _dt.datetime,
    wash_sale_days: int,
    r: float,
) -> Portfolio:
    """Open option positions (max loss per underlying + net Greeks) plus recent closed lots.

    Raises :class:`PortfolioError` when any open position cannot be valued
    (e.g. undefined risk, missing quotes): no new trade is proposed then.
    """
    today = now.astimezone(ET).date()
    open_positions: list[Position] = []
    greeks = Greeks()
    groups = _group_legs(conn, positions)
    for root, label, legs in groups:
        try:
            _, max_loss = max_gain_loss(legs)
        except Exception as exc:
            msg = f"cannot value open {root} position ({label}): {exc}"
            raise PortfolioError(msg) from exc
        if max_loss is None:
            msg = f"open {root} position ({label}) has unbounded risk"
            raise PortfolioError(msg)
        open_positions.append(Position(underlying=root, max_loss=max_loss))
        exps = sorted({parse_occ(leg.occ_symbol).expiration for leg in legs})
        try:
            spot = market.underlying_quote(root).mid
            chain = {
                parse_occ(c.symbol).format(): c
                for c in market.option_chain(root, exps[0], exps[-1])
            }
            by_exp: dict[_dt.date, list[Leg]] = defaultdict(list)
            for leg in legs:
                by_exp[parse_occ(leg.occ_symbol).expiration].append(leg)
            for exp, exp_legs in by_exp.items():
                ivs = {leg.occ_symbol: chain[leg.occ_symbol].implied_volatility for leg in exp_legs}
                if any(not v for v in ivs.values()):
                    msg = f"no IV for an open {root} leg"
                    raise PortfolioError(msg)
                g = net_greeks(
                    exp_legs,
                    MarketInputs(spot=spot, r=r, ivs={k: float(v) for k, v in ivs.items() if v}),
                    dte_calendar(today, exp),
                )
                greeks = Greeks(
                    delta=greeks.delta + g.delta,
                    gamma=greeks.gamma + g.gamma,
                    vega=greeks.vega + g.vega,
                    theta=greeks.theta + g.theta,
                )
        except PortfolioError:
            raise
        except Exception as exc:
            msg = f"cannot price open {root} position: {exc}"
            raise PortfolioError(msg) from exc

    since = (now - _dt.timedelta(days=wash_sale_days)).astimezone(_dt.UTC).isoformat()
    rows = conn.execute(
        "SELECT ticker, closed_at, realized_pnl FROM tax_lots "
        "WHERE closed_at IS NOT NULL AND closed_at >= ?",
        (since,),
    ).fetchall()
    lots = [ClosedLot.from_row(dict(r)) for r in rows]
    held: dict[str, int] = defaultdict(int)
    for _, _, legs in groups:
        for leg in legs:
            held[leg.occ_symbol] += leg.ratio if leg.side == LegIntent.LONG else -leg.ratio
    return Portfolio(positions=open_positions, greeks=greeks, closed_lots=lots, legs=dict(held))


# ---------------------------------------------------------------------------
# Pricing a chosen structure against a fresh chain
# ---------------------------------------------------------------------------


class PricedStructure:
    """A structure re-priced at mid from fresh quotes, plus the gate's quote map.

    ``spot`` and ``atm_iv`` (the expiry's ATM IV, ``None`` if the chain has no IVs)
    feed the E2.4 exit model.
    """

    def __init__(
        self,
        structure: Structure,
        contracts: dict[str, OptionContract],
        *,
        spot: float | None = None,
        atm_iv: float | None = None,
        spot_as_of: _dt.datetime | None = None,
    ) -> None:
        self.structure = structure
        self.contracts = contracts
        self.spot = spot
        self.atm_iv = atm_iv
        self.spot_as_of = spot_as_of

    def leg_spreads(self) -> dict[str, float]:
        """Quoted ask − bid per leg (per share)."""
        return {
            k: c.ask - c.bid
            for k, c in self.contracts.items()
            if c.ask is not None and c.bid is not None
        }


def price_structure(
    market: MarketDataProvider,
    legs: Sequence[tuple[str, LegIntent, int]],
    *,
    as_of: _dt.date,
    r: float,
) -> PricedStructure:
    """Fetch the chain for the legs' expiry and rebuild the structure at current mids.

    Raises ``LookupError`` if a leg is missing from the chain or has no usable quote.
    """
    occs = [parse_occ(sym) for sym, _, _ in legs]
    root = occs[0].root
    exp = occs[0].expiration
    chain = {parse_occ(c.symbol).format(): c for c in market.option_chain(root, exp, exp)}
    uq = market.underlying_quote(root)
    spot = uq.mid
    out_legs: list[Leg] = []
    used: dict[str, OptionContract] = {}
    for occ, (_, side, ratio) in zip(occs, legs, strict=True):
        key = occ.format()
        c = chain.get(key)
        if c is None or c.bid is None or c.ask is None or c.mid is None:
            msg = f"no usable quote for {key}"
            raise LookupError(msg)
        used[key] = c
        out_legs.append(
            Leg(occ_symbol=key, side=side, ratio=ratio, premium=Decimal(str(round(c.mid, 4))))
        )
    ivs = {k: float(c.implied_volatility) for k, c in used.items() if c.implied_volatility}
    market_inputs = MarketInputs(spot=spot, r=r, ivs=ivs) if len(ivs) == len(used) else None
    return PricedStructure(
        analyze(out_legs, as_of=as_of, market=market_inputs),
        used,
        spot=spot,
        atm_iv=atm_iv(list(chain.values()), spot),
        spot_as_of=uq.timestamp,
    )


def limit_price(net: Decimal, tick: float) -> Decimal:
    """Round the mid net price onto the tick, toward the marketable side.

    ``ROUND_CEILING`` pays up by at most one tick on a debit and gives up at most
    one tick of credit (-1.6555 becomes -1.65). The gate still checks the result
    against the combo NBBO.
    """
    t = Decimal(str(tick))
    return (net / t).to_integral_value(rounding=ROUND_CEILING) * t


def market_snapshot(
    contracts: dict[str, OptionContract], earnings: dict[str, _dt.date | None]
) -> MarketSnapshot:
    """Gate quotes for the legs. A contract without a quote timestamp is left out (fails closed)."""
    quotes: dict[str, Quote] = {}
    for sym, c in contracts.items():
        if c.bid is None or c.ask is None or c.quote_timestamp is None:
            continue
        quotes[sym] = Quote(
            bid=Decimal(str(c.bid)), ask=Decimal(str(c.ask)), as_of=c.quote_timestamp
        )
    return MarketSnapshot(quotes=quotes, next_earnings=earnings)
