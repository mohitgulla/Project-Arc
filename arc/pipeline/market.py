"""Deterministic inputs for sizing and the gate: account, portfolio, quotes, earnings.

Everything here is plain code over market data, the audit store and the broker
account. No persona text is read. Wherever a fact is unknown, it fails closed:

* an unpriceable open position blocks the whole propose step,
  :class:`PortfolioError`;
* an unknown earnings date is left out of ``next_earnings``, so the gate
  rejects short premium on it;
* an unknown start-of-day equity (no Arc prior close and no broker
  ``last_equity``, :mod:`arc.reconcile.baseline`) becomes 0, so the gate's
  daily-loss rule fails.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.betas.store import betas_used
from arc.context.ttl import from_db
from arc.data.base import DEFAULT_SPOT_MAX_SPREAD_PCT, market_spot
from arc.gate.band import as_grid
from arc.gate.inputs import AccountSnapshot, ClosedLot, MarketSnapshot, Portfolio, Position, Quote
from arc.models import Greeks, Leg, LegIntent
from arc.scanner.iv import atm_iv
from arc.structures import MarketInputs, analyze, max_gain_loss, net_greeks, parse_occ
from arc.utils.calendar import ET, dte_calendar

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

    from arc.broker.base import AccountInfo, BrokerPosition
    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider, OptionContract
    from arc.gate.ticks import TickGrid
    from arc.models import Structure
    from arc.reconcile.baseline import Baseline

log = structlog.get_logger(__name__)

__all__ = [
    "ETF_UNDERLYINGS",
    "LegQuote",
    "PortfolioError",
    "PricedStructure",
    "account_baseline",
    "account_snapshot",
    "build_portfolio",
    "close_quote_sanity",
    "curve_mid",
    "day_trades_used",
    "limit_price",
    "live_gate_status",
    "live_size_cap",
    "market_snapshot",
    "next_earnings",
    "opened_today_symbols",
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


def account_baseline(
    conn: sqlite3.Connection, info: AccountInfo, now: _dt.datetime
) -> Baseline | None:
    """Start-of-day equity for the ET day of *now* (E5.9b, D43).

    Arc's prior-session close from ``pnl_snapshots``; the broker's ``last_equity``
    only when no Arc close exists (:func:`arc.reconcile.baseline.start_of_day_equity`).
    """
    from arc.reconcile.baseline import start_of_day_equity

    return start_of_day_equity(conn, now.astimezone(ET).date(), broker_last_equity=info.last_equity)


def account_snapshot(
    info: AccountInfo,
    now: _dt.datetime,
    *,
    baseline: Baseline | None,
    orders_used_today: int | None = None,
    day_trades_used: int | None = None,
    live_gate_met: bool | None = None,
) -> AccountSnapshot:
    """Gate view of the account. The halt flag is stamped later by ``HaltSwitch.apply``.

    *baseline* (E5.9b, D43) is :func:`account_baseline`: the start-of-day equity the
    loop root, monitor and tower also use. It becomes ``AccountSnapshot.last_equity``
    (the gate's daily-loss basis); ``None`` (unknown) becomes 0, so the rule fails
    closed. The broker's raw ``last_equity`` never reaches the gate directly.

    ``orders_used_today`` (D32) is the day's order count from
    :func:`arc.budget.current_budget`; live callers pass it so the gate's
    ``order_budget`` rule runs. ``None`` skips the rule (fixtures, dry runs).
    ``day_trades_used`` (E10.2) feeds the profile's day-trade rule on closes;
    ``None`` skips it.
    ``live_gate_met`` (D70) is :func:`live_gate_status`; ``None`` makes the gate's
    live size cap apply (fail closed). Ignored in paper.
    """
    return AccountSnapshot(
        equity=info.equity,
        last_equity=baseline.value if baseline is not None else Decimal(0),
        settled_cash=settled_cash(info),
        orders_used_today=orders_used_today,
        day_trades_used=day_trades_used,
        live_gate_met=live_gate_met,
        as_of=now,
    )


def live_gate_status(
    conn: sqlite3.Connection, settings: ArcSettings, *, now: _dt.datetime
) -> bool | None:
    """D70: is the live scorecard gate met? ``None`` in paper (the cap never applies).

    True once the sticky ``live.gate_met`` is on, or when the live readiness on
    this (live-stamped) store is met now. Any error → False (fail closed: capped).
    """
    if settings.env.value != "live":
        return None
    if settings.live_gate_met:
        return True
    from arc.journal.scorecard import env_readiness, live_gate_met

    try:
        return live_gate_met(env_readiness(conn, settings, now=now))
    except Exception:  # noqa: BLE001 - unknown gate status caps; never blocks the loop
        log.exception("pipeline.live_gate_status_failed")
        return False


def live_size_cap(settings: ArcSettings, account: AccountSnapshot) -> int | None:
    """D70: contracts a live open is clamped to, or ``None`` (paper / gate met)."""
    if settings.env.value != "live" or account.live_gate_met is True:
        return None
    return settings.live_max_contracts_until_gate


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
    spot_max_spread_pct: float = DEFAULT_SPOT_MAX_SPREAD_PCT,
    delta_breakdown: dict[str, dict[str, float | str]] | None = None,
) -> Portfolio:
    """Open option positions (max loss per underlying + net Greeks) plus recent closed lots.

    D57: ``dollar_delta`` sums each root's net Δ (share-eq) × that root's spot (the same
    spot its Greeks were priced at). D62: ``beta_dollar_delta`` sums the same terms ×
    the root's β used (:func:`arc.betas.store.betas_used`: stored 1y β vs SPY floored at
    1.0; missing or stale -> 1.0). *delta_breakdown* (when given) is filled per root with
    ``{dollar_delta, beta, beta_dollar_delta, beta_source, gamma, spot}`` for the monitor
    heartbeat (D87: Γ share-eq and the spot it was priced at, for the Tower's $Γ).

    Raises :class:`PortfolioError` when any open position cannot be valued
    (e.g. undefined risk, missing quotes): no new trade is proposed then.
    """
    today = now.astimezone(ET).date()
    open_positions: list[Position] = []
    greeks = Greeks()
    dollar_delta = Decimal(0)
    beta_dollar_delta = Decimal(0)
    groups = _group_legs(conn, positions)
    betas = betas_used(conn, sorted({root for root, _, _ in groups}), today)
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
            found = market_spot(market, root, today, max_spread_pct=spot_max_spread_pct)
            if found.price is None:  # E4.12: never a half-price spot from a one-sided quote
                msg = f"no usable spot for open {root} position (one-sided quote, no close)"
                raise PortfolioError(msg)
            spot = found.price
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
                leg_dollar_delta = Decimal(str(g.delta)) * Decimal(str(spot))
                b = betas[root.upper()]
                leg_beta_delta = leg_dollar_delta * Decimal(str(b.beta))
                dollar_delta += leg_dollar_delta
                beta_dollar_delta += leg_beta_delta
                if delta_breakdown is not None:
                    row = delta_breakdown.setdefault(
                        root,
                        {
                            "dollar_delta": 0.0,
                            "beta": b.beta,
                            "beta_dollar_delta": 0.0,
                            "beta_source": b.source,
                            "gamma": 0.0,
                            "spot": round(float(spot), 4),
                        },
                    )
                    # D87: the Tower's gamma advisory needs Γ per root at its own spot
                    row["gamma"] = float(row["gamma"]) + float(g.gamma)
                    row["dollar_delta"] = round(
                        float(row["dollar_delta"]) + float(leg_dollar_delta), 2
                    )
                    row["beta_dollar_delta"] = round(
                        float(row["beta_dollar_delta"]) + float(leg_beta_delta), 2
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
    return Portfolio(
        positions=open_positions,
        greeks=greeks,
        dollar_delta=dollar_delta,
        beta_dollar_delta=beta_dollar_delta,
        closed_lots=lots,
        legs=dict(held),
        opened_today=frozenset(opened_today_symbols(conn, today)),
    )


def _structure_symbols(structure_json: str) -> list[str]:
    try:
        legs = json.loads(structure_json).get("legs", [])
    except (ValueError, AttributeError):
        return []
    return [str(leg.get("occ_symbol")) for leg in legs if isinstance(leg, dict)]


def opened_today_symbols(conn: sqlite3.Connection, day: _dt.date) -> set[str]:
    """E10.2: OCC symbols of structures opened on ET *day* (a close of one is a day trade)."""
    out: set[str] = set()
    for r in conn.execute("SELECT structure_json, opened_at FROM open_structures"):
        if from_db(r[1]).astimezone(ET).date() == day:
            out.update(_structure_symbols(r[0]))
    return out


def day_trades_used(conn: sqlite3.Connection, day: _dt.date, window_sessions: int) -> int:
    """E10.2: structures opened and closed on the same ET day in the last *window_sessions*.

    Read from ``open_structures`` (``opened_at`` / ``closed_at``), so a store's count
    is its own account's: an experiment arm counts its own day trades.
    """
    from arc.utils.calendar import is_session, previous_session

    start = day
    if is_session(day):
        for _ in range(window_sessions - 1):
            start = previous_session(start)
    n = 0
    for r in conn.execute(
        "SELECT opened_at, closed_at FROM open_structures WHERE closed_at IS NOT NULL"
    ):
        opened = from_db(r[0]).astimezone(ET).date()
        closed = from_db(r[1]).astimezone(ET).date()
        if opened == closed and start <= closed <= day:
            n += 1
    return n


# ---------------------------------------------------------------------------
# Pricing a chosen structure against a fresh chain
# ---------------------------------------------------------------------------


class LegQuote(BaseModel):
    """One leg's quote evidence, as used to price a structure (E6.2a).

    ``curve_mid`` is the leg's fair mid read off the expiry's strike curve
    (:func:`curve_mid`), ``None`` when the chain has too few neighbours.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    side: LegIntent
    ratio: int
    bid: float | None
    ask: float | None
    mid: float | None
    bid_size: float | None = None
    ask_size: float | None = None
    quote_ts: _dt.datetime | None = None
    spread_pct: float | None = None
    curve_mid: float | None = None
    tape_lag_s: float | None = Field(
        None,
        description=(
            "E10.2d: seconds the paired control chain's clock was behind the arm's when "
            "this quote was replayed from the market tape; None = a live read (wall clock)"
        ),
    )

    @property
    def sign(self) -> int:
        """+1 for a leg bought, -1 for a leg sold (per-share net convention)."""
        return self.ratio if self.side == LegIntent.LONG else -self.ratio


class PricedStructure:
    """A structure re-priced at mid from fresh quotes, plus the gate's quote map.

    ``spot`` and ``atm_iv`` (the expiry's ATM IV, ``None`` if the chain has no IVs)
    feed the E2.4 exit model. ``curve`` holds each leg's strike-curve fair mid
    (E6.2a quote check). ``tape_lags`` (E10.2d): per leg replayed from a paired arm's
    market tape, how far the paired control chain's clock was behind the arm's
    (:meth:`arc.experiments.tape.TapeReplay.tape_lag`); a leg absent was read live.
    """

    def __init__(
        self,
        structure: Structure,
        contracts: dict[str, OptionContract],
        *,
        spot: float | None = None,
        atm_iv: float | None = None,
        spot_as_of: _dt.datetime | None = None,
        curve: dict[str, float | None] | None = None,
        tape_lags: dict[str, _dt.timedelta] | None = None,
    ) -> None:
        self.structure = structure
        self.contracts = contracts
        self.spot = spot
        self.atm_iv = atm_iv
        self.spot_as_of = spot_as_of
        self.curve = curve or {}
        self.tape_lags = tape_lags or {}

    def quote_ref_times(self, now: _dt.datetime) -> dict[str, _dt.datetime]:
        """E10.2d: per replayed leg, the paired chain's clock at *now* (``now - lag``)."""
        return {k: now - lag for k, lag in self.tape_lags.items()}

    def leg_spreads(self) -> dict[str, float]:
        """Quoted ask − bid per leg (per share)."""
        return {
            k: c.ask - c.bid
            for k, c in self.contracts.items()
            if c.ask is not None and c.bid is not None
        }

    def leg_quotes(self) -> list[LegQuote]:
        """Per-leg quote evidence (bid, ask, sizes, quote time, spread %, curve mid)."""
        out: list[LegQuote] = []
        for leg in self.structure.legs:
            c = self.contracts.get(leg.occ_symbol)
            mid = c.mid if c is not None else None
            spread_pct = (
                (c.ask - c.bid) / mid
                if c is not None and c.bid is not None and c.ask is not None and mid
                else None
            )
            out.append(
                LegQuote(
                    symbol=leg.occ_symbol,
                    side=leg.side,
                    ratio=leg.ratio,
                    bid=c.bid if c is not None else None,
                    ask=c.ask if c is not None else None,
                    mid=mid,
                    bid_size=c.bid_size if c is not None else None,
                    ask_size=c.ask_size if c is not None else None,
                    quote_ts=c.quote_timestamp if c is not None else None,
                    spread_pct=spread_pct,
                    curve_mid=self.curve.get(leg.occ_symbol),
                    tape_lag_s=(
                        lag.total_seconds()
                        if (lag := self.tape_lags.get(leg.occ_symbol)) is not None
                        else None
                    ),
                )
            )
        return out


def curve_mid(chain: Sequence[OptionContract], symbol: str, *, neighbours: int = 6) -> float | None:
    """Fair mid of *symbol* read off its expiry's strike curve, from its neighbours only.

    A least-squares quadratic in strike through the mids of the ``neighbours``
    nearest strikes of the same expiry and type (the leg itself excluded), so one
    jittery quote cannot vouch for itself. ``None`` unless there are at least 4
    usable neighbours with at least one strike on each side. Pure.
    """
    import numpy as np

    occ = parse_occ(symbol)
    strike = float(occ.strike)
    pts: list[tuple[float, float]] = []
    for c in chain:
        if c.mid is None or c.bid is None or c.ask is None or c.bid <= 0 or c.ask < c.bid:
            continue
        o = parse_occ(c.symbol)
        if o.expiration != occ.expiration or o.kind != occ.kind or float(o.strike) == strike:
            continue
        pts.append((float(o.strike), float(c.mid)))
    pts.sort(key=lambda p: (abs(p[0] - strike), p[0]))
    near = pts[:neighbours]
    if len(near) < 4 or not (min(k for k, _ in near) < strike < max(k for k, _ in near)):
        return None
    ks = np.array([k for k, _ in near]) - strike
    ms = np.array([m for _, m in near])
    return float(np.polyfit(ks, ms, 2)[-1])  # value of the fit at the strike itself


def close_quote_sanity(
    quotes: Sequence[LegQuote], now: _dt.datetime, cfg: ArcSettings
) -> list[str]:
    """Why a close must not be priced from these leg quotes; ``[]`` when they are usable.

    Deterministic and pure (E6.2a, PLAN §6.8 data quality fails closed). Every leg:

    * ``missing``: has a bid, an ask (bid <= ask) and a quote timestamp;
    * ``stale``: quote timestamp within ``close_quote_max_age_seconds`` of *now*
      (the quote's own time, not the fetch time; a future stamp fails too). E10.2d:
      a leg replayed from a paired arm's tape (``tape_lag_s``) is aged against the
      paired chain's clock, ``now - tape_lag_s``, the age control saw;
    * ``spread``: spread <= ``close_quote_max_spread_pct`` of mid, or
      <= ``close_quote_max_spread_abs`` (a cheap wing is never unclosable);

    across legs:

    * ``skew``: leg quote times within ``close_quote_max_skew_seconds`` of each other;
    * ``off_curve``: the combo mid is within ``close_quote_max_curve_dev`` ($/share)
      of the combo read off the strike curve. This is the observed failure: the
      indicative feed's per-leg quotes jitter by $0.10-0.30 read to read while each
      looks fresh and tight, so a band built from one read can sit wholly outside
      the market. Skipped when a leg has no curve mid (chain edge).

    Each problem is ``"<code>: <detail>"``.
    """
    out: list[str] = []
    stamps: list[_dt.datetime] = []
    max_age, max_skew = cfg.close_quote_max_age_seconds, cfg.close_quote_max_skew_seconds
    max_pct, max_abs = cfg.close_quote_max_spread_pct, cfg.close_quote_max_spread_abs
    for q in quotes:
        if q.bid is None or q.ask is None or q.mid is None or q.bid > q.ask or q.quote_ts is None:
            out.append(
                f"missing: {q.symbol} has no usable quote "
                f"(bid {q.bid}, ask {q.ask}, ts {q.quote_ts})"
            )
            continue
        stamps.append(q.quote_ts)
        ref = now if q.tape_lag_s is None else now - _dt.timedelta(seconds=q.tape_lag_s)
        age = (ref - q.quote_ts).total_seconds()
        if age < 0 or age > max_age:
            clock = "" if q.tape_lag_s is None else ", paired-chain clock"
            out.append(
                f"stale: {q.symbol} quote is {age:.0f}s old "
                f"(max {max_age}s, quote {q.quote_ts.isoformat()}{clock})"
            )
        spread = q.ask - q.bid
        pct = spread / q.mid if q.mid > 0 else float("inf")
        if pct > max_pct and spread > max_abs + 1e-9:
            out.append(
                f"spread: {q.symbol} {q.bid:.2f}/{q.ask:.2f} spread {pct:.1%} of mid "
                f"(max {max_pct:.0%} or ${max_abs:.2f})"
            )
    if len(stamps) > 1:
        skew = (max(stamps) - min(stamps)).total_seconds()
        if skew > max_skew:
            out.append(f"skew: leg quote times are {skew:.0f}s apart (max {max_skew}s)")
    if quotes and all(q.mid is not None and q.curve_mid is not None for q in quotes):
        combo = sum(q.sign * (q.mid or 0.0) for q in quotes)
        fair = sum(q.sign * (q.curve_mid or 0.0) for q in quotes)
        dev = combo - fair
        if abs(dev) > cfg.close_quote_max_curve_dev + 1e-9:
            out.append(
                f"off_curve: combo mid {combo:+.2f} is {dev:+.2f} from the strike-curve "
                f"value {fair:+.2f} (max ${cfg.close_quote_max_curve_dev:.2f})"
            )
    return out


def price_structure(
    market: MarketDataProvider,
    legs: Sequence[tuple[str, LegIntent, int]],
    *,
    as_of: _dt.date,
    r: float,
    require_iv: bool = True,
    spot_max_spread_pct: float = DEFAULT_SPOT_MAX_SPREAD_PCT,
) -> PricedStructure:
    """Fetch the chain for the legs' expiry and rebuild the structure at current mids.

    Spot comes from :func:`arc.data.base.market_spot` (E4.12): with no usable spot an
    entry raises ``LookupError``; an exit (``require_iv=False``) is priced at mids with
    ``spot=None`` and no Greeks.

    Raises ``LookupError`` if a leg is missing from the chain, has no usable quote,
    or (with ``require_iv``, the default) has no implied volatility (None, <= 0 or
    NaN). Without an IV on every leg the structure's Greeks cannot be computed, and
    all-zero Greeks would slip past the gate's Greek caps (PLAN §6.8: data quality
    fails the proposal closed).

    ``require_iv=False`` is for exits only: a close needs mids, not Greeks (the
    gate skips Greek caps on closing proposals), and a missing IV must never stop
    a position from being closed. Greeks are then left at zero when any IV is missing.
    """
    occs = [parse_occ(sym) for sym, _, _ in legs]
    root = occs[0].root
    exp = occs[0].expiration
    chain = {parse_occ(c.symbol).format(): c for c in market.option_chain(root, exp, exp)}
    uq = market.underlying_quote(root)
    found = market_spot(market, root, as_of, max_spread_pct=spot_max_spread_pct, quote=uq)
    spot = found.price
    if spot is None and require_iv:
        # E4.12: fail closed rather than price off a one-sided (half-price) mid.
        msg = f"no usable spot for {root} (one-sided quote and no recent close)"
        raise LookupError(msg)
    out_legs: list[Leg] = []
    used: dict[str, OptionContract] = {}
    for occ, (_, side, ratio) in zip(occs, legs, strict=True):
        key = occ.format()
        c = chain.get(key)
        if c is None or c.bid is None or c.ask is None or c.mid is None:
            msg = f"no usable quote for {key}"
            raise LookupError(msg)
        iv = c.implied_volatility
        if require_iv and (iv is None or not float(iv) > 0):
            msg = f"no implied volatility for {key} (iv={iv!r}); cannot compute Greeks"
            raise LookupError(msg)
        used[key] = c
        out_legs.append(
            Leg(
                occ_symbol=key,
                side=side,
                ratio=ratio,
                premium=Decimal(str(round(c.mid, 4))),
                penny_program=c.penny_program,
            )
        )
    ivs = {
        k: float(c.implied_volatility)
        for k, c in used.items()
        if c.implied_volatility and float(c.implied_volatility) > 0
    }
    market_inputs = (
        MarketInputs(spot=spot, r=r, ivs=ivs)
        if spot is not None and len(ivs) == len(used)
        else None
    )
    chain_list = list(chain.values())
    from arc.experiments.tape import tape_lag  # noqa: PLC0415 - E10.2d, paired arms only

    lags = {k: lag for k in used if (lag := tape_lag(market, k)) is not None}
    return PricedStructure(
        analyze(out_legs, as_of=as_of, market=market_inputs),
        used,
        spot=spot,
        atm_iv=atm_iv(chain_list, spot) if spot is not None else None,
        spot_as_of=uq.timestamp,
        curve={k: curve_mid(chain_list, k) for k in used},
        tape_lags=lags,
    )


def limit_price(net: Decimal, grid: TickGrid | Decimal) -> Decimal:
    """Round the mid net price onto the order's exchange grid, toward the marketable side.

    ``up`` (``ROUND_CEILING`` in signed terms) pays up by at most one increment
    on a debit and gives up at most one increment of credit (-1.6555 becomes
    -1.65 on a $0.01 grid). D66: *grid* is the order's grid
    (:func:`arc.gate.rules.grid_for`); a bare ``Decimal`` is a flat increment.
    The gate still checks the result against the combo NBBO and the grid.
    """
    return as_grid(grid).snap(net, "up")


def market_snapshot(
    contracts: dict[str, OptionContract],
    earnings: dict[str, _dt.date | None],
    spots: Mapping[str, float | None] | None = None,
    betas: Mapping[str, float] | None = None,
    *,
    quote_ref_time: Mapping[str, _dt.datetime] | None = None,
) -> MarketSnapshot:
    """Gate quotes for the legs. A contract without a quote timestamp is left out (fails closed).

    *spots* (D57): underlying spot per root, the re-pricing spot. A ``None`` or
    non-positive spot is left out, so the gate's dollar-delta cap fails closed.
    *betas* (D62): β used per root (from :func:`proposal_betas`; already floored).
    D66: ``penny_program`` carries each contract's ``ppind`` (None = unknown).
    *quote_ref_time* (E10.2d): per leg replayed from a paired arm's tape, the paired
    chain's clock (:meth:`PricedStructure.quote_ref_times`); the gate ages those
    quotes against it.
    """
    quotes: dict[str, Quote] = {}
    for sym, c in contracts.items():
        if c.bid is None or c.ask is None or c.quote_timestamp is None:
            continue
        quotes[sym] = Quote(
            bid=Decimal(str(c.bid)), ask=Decimal(str(c.ask)), as_of=c.quote_timestamp
        )
    spot_map = {
        root: Decimal(str(v)) for root, v in (spots or {}).items() if v is not None and v > 0
    }
    beta_map = {root: Decimal(str(v)) for root, v in (betas or {}).items()}
    return MarketSnapshot(
        quotes=quotes,
        next_earnings=earnings,
        underlying_spot=spot_map,
        underlying_beta=beta_map,
        penny_program={sym: c.penny_program for sym, c in contracts.items()},
        quote_ref_time={s: t for s, t in (quote_ref_time or {}).items() if s in quotes},
    )


def proposal_betas(
    conn: sqlite3.Connection | None, tickers: Sequence[str], today: _dt.date
) -> dict[str, float]:
    """D62: β used per ticker for a gate ``MarketSnapshot`` (floored; 1.0 by default)."""
    return {t: b.beta for t, b in betas_used(conn, tickers, today).items()}
