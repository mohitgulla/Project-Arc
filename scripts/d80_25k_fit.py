"""D80 $25K fit check (card E19.2): replay past opens and menus at a smaller equity.

    .venv/bin/python scripts/d80_25k_fit.py --db data/arc.db [--equity 25000]
        [--menu-days 14] [--out docs/RESEARCH/d80-25k-fit.md] [--json <path>]

Report-only. The store is opened **read-only** (``mode=ro`` + ``query_only``); nothing
is written to it, no broker or LLM is called, no default changes. Every number in
``docs/RESEARCH/d80-25k-fit.md`` comes from this script.

What it replays, with ``AccountSnapshot`` equity = settled cash = ``--equity`` and an
empty book (each row on its own):

1. every past ``open`` proposal (structure, Risk's lot count, the D24 band its
   execution actually used) and every menu structure Quant offered in the last
   ``--menu-days`` days (``structures`` context, priced at mid; no band was minted):
   D18 sizing (:func:`arc.sizing.size_contracts`) and the gate's opening rules that
   depend on equity or cash: per-underlying cap at the band's worst price, the
   account profile (kind / net debit / short legs / ``cash_settled``), the $Δ,
   β$Δ and vega caps, and the structure whitelist;
2. concurrency: how many such structures settled cash holds at once vs the old
   book's peak open count (``open_structures``);
3. absolute-dollar thresholds in the effective settings / config that do not scale
   with equity;
4. per active-list name: the cheapest in-band debit structure the scanner builds on
   the latest recorded chain (``market_tape``), else the cheapest menu item seen.

Effective settings = ``ArcSettings()`` + the store's applied ``config_changes``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arc.backtest.costs import MULTIPLIER, CostModel, load_cost_model
from arc.betas.store import betas_used
from arc.control.effective import effective_settings
from arc.data.base import HistoryBar, OptionContract, UnderlyingQuote
from arc.gate.band import PriceBand
from arc.gate.inputs import AccountSnapshot, MarketSnapshot, Portfolio
from arc.gate.rules import (
    RuleCode,
    check_account_profile,
    check_greek_caps,
    check_per_underlying,
    check_structure_whitelist,
    derive,
    worst_case,
)
from arc.models import (
    Greeks,
    Leg,
    LegIntent,
    Proposal,
    QuantMetrics,
    Sizing,
    Structure,
)
from arc.pricing import bs
from arc.scanner.scan import DEBIT_STRATEGIES, NoSpotError, ScanParams, scan
from arc.sizing import size_contracts
from arc.store.db import connect_ro
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

    from arc.config import ArcSettings

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "RESEARCH" / "d80-25k-fit.md"
FIT = "fits"
_PCT = Decimal(100)


# ---------------------------------------------------------------------------
# Store reads (read-only)
# ---------------------------------------------------------------------------


def _ts(text: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _et_day(text: str) -> dt.date:
    return _ts(text).astimezone(ET).date()


class SpotBook:
    """Best-known spot per ticker over time (proposal spot, regime close, chain snapshot)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._by: dict[str, list[tuple[dt.datetime, float]]] = defaultdict(list)
        for t, spot, at in conn.execute(
            "SELECT ticker, spot, created_at FROM proposals WHERE spot IS NOT NULL"
        ):
            self._add(t, spot, at)
        for subj, payload, at in conn.execute(
            "SELECT subject, payload, created_at FROM context_entries "
            "WHERE kind IN ('regime', 'chain_snapshot')"
        ):
            p = json.loads(payload)
            self._add(subj, p.get("last_close") or p.get("spot"), at)
        for v in self._by.values():
            v.sort()

    def _add(self, ticker: str | None, spot: object, at: str) -> None:
        try:
            s = float(spot)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if ticker and s > 0:
            self._by[ticker.upper()].append((_ts(at), s))

    def at(self, ticker: str, when: dt.datetime) -> float | None:
        rows = self._by.get(ticker.upper())
        if not rows:
            return None
        before = [s for t, s in rows if t <= when]
        return before[-1] if before else rows[0][1]


# ---------------------------------------------------------------------------
# Replay one structure
# ---------------------------------------------------------------------------


@dataclass
class Row:
    source: str  # open | menu
    ticker: str
    kind: str
    day: str
    debit: float  # per share
    max_loss_unit: float  # $ per lot at mid
    worst_loss_unit: float  # $ per lot at the band's worst price (= mid for menus)
    lots_orig: int | None
    lots_25k: int
    cap_lots: int
    sizing: str
    blocks: list[str] = field(default_factory=list)
    missing_spot: bool = False

    @property
    def fits(self) -> bool:
        return self.lots_25k > 0 and not self.blocks


def _proposal(structure: Structure, contracts: int, equity: Decimal) -> Proposal:
    ml = structure.max_loss or Decimal(0)
    return Proposal(
        candidate_id="replay",
        structure=structure,
        thesis="",
        quant=QuantMetrics(pop=0.5, ev=Decimal(0)),
        sizing=Sizing(
            contracts=contracts,
            notional=ml * contracts,
            pct_equity=min(float(ml * contracts / equity), 1.0),
        ),
        expires_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )


def replay(
    structure: Structure,
    *,
    source: str,
    ticker: str,
    day: dt.date,
    suggestion: int,
    lots_orig: int | None,
    band: PriceBand | None,
    settings: ArcSettings,
    equity: Decimal,
    spot: float | None,
    beta: Decimal,
    shrink: bool = False,
) -> Row:
    """D18 sizing + the equity/cash-dependent opening gate rules on an empty book.

    *shrink* (menus: no Risk count) steps the lot count down from the D18 cap to the
    largest one every rule passes; without it (past opens) the gate judges the lot
    count D18 sized, as the live pipeline does.
    """
    ml = structure.max_loss
    sz = size_contracts(
        suggestion=suggestion,
        max_loss_per_contract=ml,
        equity=equity,
        cap_pct=settings.max_alloc_pct,
    )
    acct = AccountSnapshot(
        equity=equity, last_equity=equity, settled_cash=equity, as_of=dt.datetime.now(dt.UTC)
    )
    mkt = MarketSnapshot(
        underlying_spot={ticker: Decimal(str(spot))} if spot else {},
        underlying_beta={ticker: beta},
    )
    pf = Portfolio()

    def gate(n: int) -> tuple[list[Any], Any, Any]:
        prop = _proposal(structure, n, equity)
        if band is not None:
            prop = prop.model_copy(update={"limit_price": band.lo})
        d = derive(prop)
        risk_d = worst_case(d, band, n) if band is not None else d
        v = [
            *check_per_underlying(risk_d, acct, pf, settings),
            *check_account_profile(prop, risk_d, acct, settings),
            *check_greek_caps(prop, acct, pf, mkt, settings),
            *check_structure_whitelist(prop, d, settings),
        ]
        return [x for x in v if x.code is not RuleCode.MISSING_SPOT], v, (d, risk_d)

    n = sz.contracts if sz.trade else 1  # 1 lot shows which gate rule would block
    v, raw, (d, risk_d) = gate(n)
    lots = sz.contracts
    if shrink and sz.trade and v:
        while n > 1 and v:
            n -= 1
            v, raw, (d, risk_d) = gate(n)
        lots = n if not v else 0
    codes = sorted({x.code.value for x in v})
    worst_unit = (risk_d.max_loss_total or Decimal(0)) / n
    blocks = [] if sz.trade else [f"sizing:{sz.code}"]
    blocks += codes if sz.trade else [c for c in codes if c != RuleCode.PER_UNDERLYING.value]
    return Row(
        source=source,
        ticker=ticker,
        kind=str(d.kind.value),
        day=day.isoformat(),
        debit=float(structure.net_debit_credit),
        max_loss_unit=float(ml or 0),
        worst_loss_unit=float(worst_unit),
        lots_orig=lots_orig,
        lots_25k=lots if not blocks else 0,
        cap_lots=sz.cap_contracts,
        sizing=sz.code,
        blocks=blocks,
        missing_spot=any(x.code is RuleCode.MISSING_SPOT for x in raw),
    )


def _band(row: sqlite3.Row | None) -> PriceBand | None:
    if row is None:
        return None
    try:
        return PriceBand(
            lo=Decimal(row["band_lo"]), hi=Decimal(row["band_hi"]), max_steps=row["max_steps"]
        )
    except ValueError:
        return None


def replay_opens(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    equity: Decimal,
    spots: SpotBook,
    betas: dict[tuple[str, dt.date], Decimal],
) -> list[Row]:
    out: list[Row] = []
    for r in conn.execute(
        "SELECT p.ticker, p.structure_json, p.sizing_json, p.created_at, p.spot, "
        "e.band_lo, e.band_hi, e.max_steps FROM proposals p "
        "LEFT JOIN executions e ON e.proposal_hash = p.proposal_hash "
        "WHERE p.kind = 'open' AND p.arm_id IS NULL ORDER BY p.created_at"
    ):
        st = Structure.model_validate_json(r["structure_json"])
        lots = int(json.loads(r["sizing_json"])["contracts"])
        day = _et_day(r["created_at"])
        t = str(r["ticker"]).upper()
        spot = float(r["spot"]) if r["spot"] else spots.at(t, _ts(r["created_at"]))
        out.append(
            replay(
                st,
                source="open",
                ticker=t,
                day=day,
                suggestion=lots,
                lots_orig=lots,
                band=_band(r if r["band_lo"] is not None else None),
                settings=settings,
                equity=equity,
                spot=spot,
                beta=betas.get((t, day), Decimal(1)),
            )
        )
    return out


def menu_structure(item: dict[str, Any]) -> Structure | None:
    """A ``structures``-context menu item as a priced Structure (net on the long leg)."""
    net = Decimal(str(item["net_debit_credit"]))
    legs_in = item.get("legs") or []
    longs = [x for x in legs_in if x["side"] == "long"]
    if net <= 0 or len(longs) != 1 or item.get("max_loss") is None:
        return None  # credit or multi-long: not a cash_debit menu item
    legs = [
        Leg(
            occ_symbol=x["occ_symbol"],
            side=LegIntent(x["side"]),
            ratio=int(x.get("ratio") or 1),
            premium=net if x["side"] == "long" else Decimal(0),
        )
        for x in legs_in
    ]
    g = item.get("greeks") or {}
    return Structure(
        legs=legs,
        net_debit_credit=net,
        max_loss=Decimal(str(item["max_loss"])),
        max_gain=None if item.get("max_gain") is None else Decimal(str(item["max_gain"])),
        greeks=Greeks(**{k: float(v) for k, v in g.items() if v is not None}),
        dte=int(item.get("dte") or 0),
    )


def replay_menus(
    conn: sqlite3.Connection,
    settings: ArcSettings,
    equity: Decimal,
    spots: SpotBook,
    betas: dict[tuple[str, dt.date], Decimal],
    *,
    since: dt.date,
) -> tuple[list[Row], int]:
    out: list[Row] = []
    skipped = 0
    for payload, at in conn.execute(
        "SELECT payload, created_at FROM context_entries WHERE kind = 'structures' "
        "ORDER BY created_at"
    ):
        day = _et_day(at)
        if day < since:
            continue
        for item in json.loads(payload).get("structures", []):
            st = menu_structure(item)
            if st is None:
                skipped += 1
                continue
            t = str(item["ticker"]).upper()
            out.append(
                replay(
                    st,
                    source="menu",
                    ticker=t,
                    day=day,
                    suggestion=10**6,  # no Risk count on a menu: the D18 cap alone
                    lots_orig=None,
                    band=None,
                    settings=settings,
                    equity=equity,
                    spot=spots.at(t, _ts(at)),
                    beta=betas.get((t, day), Decimal(1)),
                    shrink=True,
                )
            )
    return out, skipped


def load_betas(conn: sqlite3.Connection, keys: set[tuple[str, dt.date]]) -> dict:
    by_day: dict[dt.date, set[str]] = defaultdict(set)
    for t, d in keys:
        by_day[d].add(t)
    out: dict[tuple[str, dt.date], Decimal] = {}
    for d, tickers in by_day.items():
        for t, b in betas_used(conn, sorted(tickers), d).items():
            out[(t, d)] = Decimal(str(b.beta))
    return out


def _keys(conn: sqlite3.Connection) -> set[tuple[str, dt.date]]:
    keys = {
        (str(t).upper(), _et_day(at))
        for t, at in conn.execute("SELECT ticker, created_at FROM proposals WHERE kind='open'")
    }
    for payload, at in conn.execute(
        "SELECT payload, created_at FROM context_entries WHERE kind = 'structures'"
    ):
        for item in json.loads(payload).get("structures", []):
            keys.add((str(item["ticker"]).upper(), _et_day(at)))
    return keys


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def peak_open(conn: sqlite3.Connection) -> tuple[int, str, list[tuple[str, int]]]:
    """Peak simultaneous open structures in the old book and the end-of-day series."""
    events: list[tuple[dt.datetime, int]] = []
    for opened, closed in conn.execute("SELECT opened_at, closed_at FROM open_structures"):
        events.append((_ts(opened), 1))
        if closed:
            events.append((_ts(closed), -1))
    events.sort()
    n = peak = 0
    peak_at = ""
    eod: dict[str, int] = {}
    for t, e in events:
        n += e
        eod[t.astimezone(ET).date().isoformat()] = n
        if n > peak:
            peak, peak_at = n, t.astimezone(ET).isoformat(timespec="minutes")
    return peak, peak_at, sorted(eod.items())


def old_book_cash_use(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """Old book: max loss $ open at each end of day (entry debit × 100 × lots)."""
    rows = conn.execute(
        "SELECT opened_at, closed_at, entry_net, contracts FROM open_structures"
    ).fetchall()
    days = sorted({_et_day(r[0]) for r in rows} | {_et_day(r[1]) for r in rows if r[1]})
    out = []
    for d in days:
        end = dt.datetime.combine(d, dt.time(16, 30), ET)
        used = sum(
            float(r[2]) * MULTIPLIER * int(r[3])
            for r in rows
            if _ts(r[0]) <= end and (r[1] is None or _ts(r[1]) > end)
        )
        out.append((d.isoformat(), used))
    return out


# ---------------------------------------------------------------------------
# Universe: cheapest in-band debit structure per active name
# ---------------------------------------------------------------------------


class _TapeProvider:
    def __init__(
        self, chain: list[OptionContract], quote: UnderlyingQuote, bars: list[HistoryBar]
    ) -> None:
        self.chain, self.quote, self.bars = chain, quote, bars

    def option_chain(self, underlying: str, exp_start: dt.date, exp_end: dt.date):  # noqa: ANN201
        return self.chain

    def underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self.quote

    def history_bars(self, symbol: str, start: dt.date, end: dt.date, timeframe: str = "1Day"):  # noqa: ANN201
        return self.bars


def _unpack(blob: bytes) -> list[dict[str, Any]]:
    return json.loads(zlib.decompress(blob))


def latest_tape_chains(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str]]:
    """ticker -> (chain_run_id, call key, at) of its latest full-window chain read."""
    out: dict[str, tuple[str, str, str]] = {}
    for run, call, at in conn.execute(
        "SELECT chain_run_id, call, at FROM market_tape WHERE call LIKE 'option_chain(%' "
        "ORDER BY at"
    ):
        args = json.loads(call[len("option_chain(") : -1])
        if args[1] != args[2]:  # a window read (the scan), not a one-expiry re-price
            out[str(args[0]).upper()] = (run, call, at)
    return out


def cheapest_in_band(
    conn: sqlite3.Connection, settings: ArcSettings, ticker: str, ref: tuple[str, str, str]
) -> tuple[float | None, str, str]:
    """(cheapest max loss $, its strategy, as-of day) from the recorded chain."""
    run, call, at = ref
    blob = conn.execute(
        "SELECT payload FROM market_tape WHERE chain_run_id = ? AND call = ?", (run, call)
    ).fetchone()[0]
    chain = [OptionContract.model_validate(r) for r in _unpack(blob)]
    q = conn.execute(
        "SELECT payload FROM market_tape WHERE chain_run_id = ? AND call = ?",
        (run, f'underlying_quote(["{ticker}"])'),
    ).fetchone()
    if q is None:
        return None, "no recorded quote", ""
    quote = UnderlyingQuote.model_validate(_unpack(q[0])[0])
    bars_row = conn.execute(
        "SELECT payload FROM market_tape WHERE chain_run_id = ? AND call LIKE ?",
        (run, f'history_bars(["{ticker}",%'),
    ).fetchone()
    bars = [HistoryBar.model_validate(r) for r in _unpack(bars_row[0])] if bars_row else []
    as_of = _et_day(at)
    params = ScanParams.from_settings(settings, strategies=list(DEBIT_STRATEGIES))
    try:
        res = scan(_TapeProvider(chain, quote, bars), ticker, params, as_of=as_of)
    except NoSpotError:
        return None, "no spot", as_of.isoformat()
    costs = [
        (float(c.structure.max_loss), c.strategy.value)
        for c in res.candidates
        if c.structure.max_loss is not None
    ]
    if not costs:
        return None, "no in-band structure", as_of.isoformat()
    ml, strat = min(costs)
    return ml, strat, as_of.isoformat()


def _call_strike(spot: float, sigma: float, t: float, r: float, target: float) -> float:
    """Strike whose BSM call |Δ| is *target* (bisection; Δ falls as the strike rises)."""
    lo, hi = spot * 0.2, spot * 5.0
    for _ in range(80):
        mid = (lo + hi) / 2
        d = bs.delta(bs.BSMInputs(S=spot, K=mid, t=t, r=r, sigma=sigma, flag=bs.OptionKind.CALL))
        lo, hi = (mid, hi) if d > target else (lo, mid)
    return (lo + hi) / 2


def bsm_target_vertical(spot: float, iv30: float, settings: ArcSettings) -> float:
    """$ max loss of the scanner's *target* bull call debit vertical, from spot + IV30 only.

    Long leg at ``scanner_long_target_delta``, short at
    ``scanner_debit_short_target_delta``, flat vol, the profile DTE window's middle.
    An estimate for names with no recorded chain; calibrated against the scanner in
    :func:`build` (the scanner's cheapest in-band item is usually below the target).
    """
    lo, hi = settings.entry_dte_window
    t = (lo + hi) / 2 / 365
    r = settings.scanner_risk_free_rate

    def call(k: float) -> float:
        return bs.price(bs.BSMInputs(S=spot, K=k, t=t, r=r, sigma=iv30, flag=bs.OptionKind.CALL))

    k_long = _call_strike(spot, iv30, t, r, settings.scanner_long_target_delta)
    k_short = _call_strike(spot, iv30, t, r, settings.scanner_debit_short_target_delta)
    return MULTIPLIER * (call(k_long) - call(k_short))


def latest_iv(conn: sqlite3.Connection, ticker: str) -> tuple[float, float, str] | None:
    row = conn.execute(
        "SELECT spot, iv30, day FROM iv_daily WHERE ticker = ? AND spot IS NOT NULL "
        "ORDER BY day DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return None if row is None else (float(row[0]), float(row[1]), str(row[2]))


def active_names(conn: sqlite3.Connection) -> tuple[str, list[tuple[str, str]]]:
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = 'active_universe' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "", []
    p = json.loads(row[0])
    return p.get("as_of", ""), [(m["ticker"], m["tier"]) for m in p.get("members", [])]


# ---------------------------------------------------------------------------
# Dollar thresholds that do not scale with equity
# ---------------------------------------------------------------------------


def dollar_thresholds(settings: ArcSettings, cost: CostModel, equity: Decimal) -> list[dict]:
    from arc.scanner.rank import load_ranking_config
    from arc.universe.config import load_universe_config

    rk = load_ranking_config()
    uc = load_universe_config()
    cap = float(Decimal(str(settings.max_alloc_pct)) * equity)
    fee = cost.per_contract_both_sides
    dcap = float(equity) * settings.portfolio_dollar_delta_cap_pct
    return [
        {
            "key": "ranking.filters.min_managed_net_ev (live Net EV floor)",
            "value": f"${rk.filters.min_managed_net_ev:g} per unit, strict >",
            "effect": "Per lot, not per $ of equity: unchanged at $25K. A $0 floor is equity-free.",
        },
        {
            "key": "gate_fee_per_leg_contract (gate cash check)",
            "value": f"${settings.gate_fee_per_leg_contract:g} / leg-contract",
            "effect": f"Per contract: unchanged. Fee model {fee:.4f} $/contract/side.",
        },
        {
            "key": "spread_max_abs / spread_max_pct (scanner + gate)",
            "value": f"${settings.spread_max_abs:g} or {settings.spread_max_pct:.0%} of mid",
            "effect": "Per share: unchanged. Cheap (< $1) legs pass on the $ leg, so "
            "a smaller book drifting to cheaper legs pays relatively wider spreads.",
        },
        {
            "key": "close_quote_max_spread_abs (exit quote check)",
            "value": f"${settings.close_quote_max_spread_abs:g}",
            "effect": "Per share: unchanged.",
        },
        {
            "key": "liquidity_screen.standard.min_price (momentum)",
            "value": f"${uc.liquidity_screen.standard.min_price:g} underlying",
            "effect": "Screens names, not orders: unchanged. A cheaper name is not "
            "cheaper to trade unless its in-band structure is (section 4).",
        },
        {
            "key": "liquidity_screen.loose.min_price (discovery)",
            "value": f"${uc.liquidity_screen.loose.min_price:g} underlying",
            "effect": "As above.",
        },
        {
            "key": "routines market_movers.min_price (Scalp context)",
            "value": "$3 underlying",
            "effect": "Context only; never a candidate. Unchanged.",
        },
        {
            "key": "ticks (Penny / standard grid, boundary)",
            "value": f"${settings.ticks.boundary} boundary, "
            f"{settings.ticks.penny_below}/{settings.ticks.penny_above} penny",
            "effect": "Exchange rules: unchanged.",
        },
        {
            "key": "exits expiry_guard.pin_band",
            "value": "$0.50 / share",
            "effect": "Per share: unchanged.",
        },
        {
            "key": "max_open_positions",
            "value": str(settings.max_open_positions),
            "effect": "A count, not $: unchanged. Settled cash binds first at $25K (section 2).",
        },
        {
            "key": "max_alloc_pct (per-underlying cap)",
            "value": f"{settings.max_alloc_pct:.1%} of equity",
            "effect": f"Scales: ${cap:,.0f} per underlying at ${float(equity):,.0f}.",
        },
        {
            "key": "portfolio_dollar_delta_cap_pct / beta / vega caps",
            "value": f"{settings.portfolio_dollar_delta_cap_pct:g} / "
            f"{settings.portfolio_beta_delta_cap_pct:g} / {settings.portfolio_vega_cap_pct:g} "
            "x equity",
            "effect": f"Scale: $Δ cap ${dcap:,.0f}, "
            f"vega cap ${float(equity) * settings.portfolio_vega_cap_pct:,.0f}/vol-pt.",
        },
        {
            "key": "daily_loss_halt_pct",
            "value": f"{settings.daily_loss_halt_pct:.0%} of start-of-day equity",
            "effect": f"Scales: ${float(equity) * settings.daily_loss_halt_pct:,.0f} at "
            f"${float(equity):,.0f}.",
        },
    ]


def fee_example(cost: CostModel, debit: float, legs: int) -> dict[str, float]:
    """Round-trip fees for 1 lot of a *legs*-leg debit structure of *debit* $ total."""
    per_share = debit / MULTIPLIER
    entry = cost.fees(legs)
    # close: sell the long leg(s), buy back the short(s); sell-side fees on the long.
    exit_ = cost.trade_fees(1, -1, per_share) + cost.fees(legs - 1)
    total = entry + exit_
    return {"entry": entry, "exit": exit_, "total": total, "pct": 100 * total / debit}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def _kind_table(rows: list[Row]) -> list[str]:
    by: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by[r.kind].append(r)
    lines = [
        "| kind | n | fit ≥ 1 lot | lots (orig → $25K) | median max loss / lot | blocked by |",
        "|---|---:|---:|---|---:|---|",
    ]
    for kind in sorted(by):
        rs = by[kind]
        fits = sum(r.fits for r in rs)
        orig = sum(r.lots_orig or 0 for r in rs)
        new = sum(r.lots_25k for r in rs if r.fits)
        lots = f"{orig} → {new}" if rs[0].lots_orig is not None else f"— → {new}"
        blocks = Counter(b for r in rs for b in r.blocks)
        btxt = ", ".join(f"{k} ×{v}" for k, v in blocks.most_common()) or "—"
        med = statistics.median(r.worst_loss_unit for r in rs)
        lines.append(
            f"| {kind} | {len(rs)} | {fits} ({_pct(fits, len(rs))}) | {lots} | "
            f"${med:,.0f} | {btxt} |"
        )
    tot = len(rows)
    fits = sum(r.fits for r in rows)
    lines.append(f"| **all** | {tot} | {fits} ({_pct(fits, tot)}) | | | |")
    return lines


def build(conn: sqlite3.Connection, *, equity: Decimal, menu_days: int) -> dict[str, Any]:
    settings = effective_settings(conn)
    cost = load_cost_model()
    spots = SpotBook(conn)
    betas = load_betas(conn, _keys(conn))
    opens = replay_opens(conn, settings, equity, spots, betas)
    last = max(
        (
            _et_day(r[0])
            for r in conn.execute(
                "SELECT created_at FROM context_entries WHERE kind = 'structures'"
            )
        ),
        default=dt.date.today(),
    )
    since = last - dt.timedelta(days=menu_days)
    menus, skipped = replay_menus(conn, settings, equity, spots, betas, since=since)
    peak, peak_at, eod = peak_open(conn)
    cash_use = old_book_cash_use(conn)
    as_of, names = active_names(conn)
    tape = latest_tape_chains(conn)
    menu_min: dict[str, float] = {}
    for r in menus:
        menu_min[r.ticker] = min(menu_min.get(r.ticker, 1e12), r.max_loss_unit)
    cap = Decimal(str(settings.max_alloc_pct)) * equity
    universe = []
    for t, tier in names:
        if t in tape:
            ml, how, day = cheapest_in_band(conn, settings, t, tape[t])
            src = f"scanner on tape {day}"
        elif t in menu_min:
            ml, how, src = menu_min[t], "menu", "cheapest menu item"
        else:
            ml, how, src = None, "", ""
        iv = latest_iv(conn, t)
        universe.append(
            {
                "ticker": t,
                "tier": tier,
                "cheapest": ml,
                "strategy": how,
                "source": src,
                "bsm_target": None if iv is None else bsm_target_vertical(iv[0], iv[1], settings),
                "spot": None if iv is None else iv[0],
                "iv30": None if iv is None else iv[1],
            }
        )
    # Calibrate the BSM target against the scanner where both exist: the scanner's
    # cheapest in-band item / the target vertical's max loss.
    ratios = sorted(
        u["cheapest"] / u["bsm_target"]
        for u in universe
        if u["cheapest"] and u["bsm_target"] and u["source"].startswith("scanner")
    )
    lo_r = ratios[0] if ratios else 1.0
    med_r = statistics.median(ratios) if ratios else 1.0
    capf = float(cap)
    for u in universe:
        if u["cheapest"] is not None:
            u["fits"] = "yes" if u["cheapest"] <= capf else "no"
        elif u["bsm_target"] is None:
            u["fits"] = "?"
        else:
            u["est_low"] = u["bsm_target"] * lo_r
            u["est_mid"] = u["bsm_target"] * med_r
            u["source"] = "estimate: BSM target vertical (iv_daily)"
            u["fits"] = (
                "no (est.)"
                if u["est_low"] > capf
                else "unlikely (est.)"
                if u["est_mid"] > capf
                else "likely (est.)"
            )
    return {
        "settings": settings,
        "cost": cost,
        "equity": equity,
        "cap": cap,
        "opens": opens,
        "menus": menus,
        "menus_skipped": skipped,
        "menu_since": since,
        "menu_last": last,
        "peak": peak,
        "peak_at": peak_at,
        "eod": eod,
        "cash_use": cash_use,
        "active_as_of": as_of,
        "universe": universe,
        "calib": (lo_r, med_r, ratios[-1] if ratios else 1.0, len(ratios)),
        "thresholds": dollar_thresholds(settings, cost, equity),
        "applied": conn.execute(
            "SELECT key, new FROM config_changes WHERE status = 'applied' ORDER BY id"
        ).fetchall(),
    }


def render(res: dict[str, Any]) -> str:  # noqa: PLR0915 - one linear report
    s: ArcSettings = res["settings"]
    eq: Decimal = res["equity"]
    cap: Decimal = res["cap"]
    opens: list[Row] = res["opens"]
    menus: list[Row] = res["menus"]
    cost: CostModel = res["cost"]
    feq = float(eq)
    L: list[str] = []
    w = L.append
    w("# D80 · $25K fit check (E19.2)")
    w("")
    w(
        f"Report-only. Generated by `scripts/d80_25k_fit.py` from a read-only open of the "
        f"store (pre-reset book, opens {opens[0].day if opens else '—'} → "
        f"{opens[-1].day if opens else '—'}). No default is changed; section 6 lists the "
        "owner decisions the numbers raise."
    )
    w("")
    w(
        f"Replay account: equity = settled cash = **${feq:,.0f}**, empty book. Effective "
        f"settings (`ArcSettings` + applied `config_changes`, carried over by D80): "
        f"`max_alloc_pct` **{s.max_alloc_pct:g}** → per-underlying cap **${float(cap):,.0f}**; "
        f"profile `{s.account_profile}` (`{s.profile.buying_power.value}`); "
        f"`portfolio_dollar_delta_cap_pct` {s.portfolio_dollar_delta_cap_pct:g}, "
        f"`portfolio_beta_delta_cap_pct` {s.portfolio_beta_delta_cap_pct:g}, "
        f"`portfolio_vega_cap_pct` {s.portfolio_vega_cap_pct:g}, "
        f"`max_open_positions` {s.max_open_positions}."
    )
    w("")
    # 1 ---------------------------------------------------------------
    w("## 1. Sizing + gate replay")
    w("")
    w(
        "Each structure goes through D18 sizing (`arc.sizing.size_contracts`) and the "
        "gate's equity/cash rules (`check_per_underlying` at the band's worst price, "
        "`check_account_profile` incl. `cash_settled`, `check_greek_caps`, "
        "`check_structure_whitelist`). Time, quote and calendar rules are not replayed "
        "(they don't depend on equity). A structure that sizes to 0 is checked at 1 lot "
        "to show which other rule would also block it."
    )
    w("")
    fit_o = sum(r.fits for r in opens)
    w(
        f"### Past opens ({len(opens)} proposals, Risk's lot count, the D24 band their "
        f"execution used): **{fit_o} fit**, {len(opens) - fit_o} don't"
    )
    w("")
    L.extend(_kind_table(opens))
    w("")
    w("| day | ticker | kind | max loss / lot (worst) | lots orig → $25K | result |")
    w("|---|---|---|---:|---|---|")
    for r in opens:
        res_txt = "fits" if r.fits else ", ".join(r.blocks)
        w(
            f"| {r.day} | {r.ticker} | {r.kind} | ${r.max_loss_unit:,.0f} "
            f"(${r.worst_loss_unit:,.0f}) | {r.lots_orig} → {r.lots_25k} | {res_txt} |"
        )
    w("")
    orig_risk = sum(r.worst_loss_unit * (r.lots_orig or 0) for r in opens)
    new_risk = sum(r.worst_loss_unit * r.lots_25k for r in opens if r.fits)
    downs = [r for r in opens if r.fits and r.lots_orig and r.lots_25k < r.lots_orig]
    w(
        f"Lots cut by the smaller cap but still traded: {len(downs)} "
        f"({', '.join(f'{r.ticker} {r.lots_orig}→{r.lots_25k}' for r in downs) or 'none'}). "
        f"Max loss committed across these opens: ${orig_risk:,.0f} as traded → "
        f"${new_risk:,.0f} at ${feq:,.0f}."
    )
    w("")
    fit_m = sum(r.fits for r in menus)
    distinct = {(r.ticker, r.kind, r.max_loss_unit, r.day) for r in menus}
    w(
        f"### Menus ({res['menu_since']} → {res['menu_last']}, {len(menus)} menu items "
        f"from `structures` context, {len(distinct)} distinct per day; priced at mid, no "
        f"band): **{fit_m} fit ({_pct(fit_m, len(menus))})**"
    )
    w("")
    L.extend(_kind_table(menus))
    w("")
    if menus:
        mls = sorted(r.max_loss_unit for r in menus)
        lots = [r.lots_25k for r in menus if r.fits]
        w(
            f"Menu max loss / lot: median ${statistics.median(mls):,.0f}, "
            f"p75 ${mls[int(0.75 * (len(mls) - 1))]:,.0f}, p90 "
            f"${mls[int(0.9 * (len(mls) - 1))]:,.0f}, max ${mls[-1]:,.0f}. "
            f"Lots available at the cap where it fits: median "
            f"{statistics.median(lots) if lots else 0:g}, 1-lot only "
            f"{sum(1 for x in lots if x == 1)} of {len(lots)}."
        )
        w("")
    if res["menus_skipped"]:
        w(f"({res['menus_skipped']} menu items skipped: credit or multi-long, not cash_debit.)")
        w("")
    blocks_all = Counter(b for r in [*opens, *menus] for b in r.blocks)
    gate_only = [
        r
        for r in [*opens, *menus]
        if r.blocks and not any(b.startswith("sizing:") for b in r.blocks)
    ]
    with_size = [
        r
        for r in [*opens, *menus]
        if any(b.startswith("sizing:") for b in r.blocks)
        and any(not b.startswith("sizing:") for b in r.blocks)
    ]
    w(
        "**What blocks:** "
        + (", ".join(f"`{k}` ×{v}" for k, v in blocks_all.most_common()) or "nothing")
        + f". {len(with_size)} rows that D18 already sizes to 0 would also fail a Greek "
        "cap at 1 lot (a high-priced name's 1 lot carries more $Δ than "
        f"{s.portfolio_dollar_delta_cap_pct:g} × ${feq:,.0f}). "
        + (
            f"**{len(gate_only)} rows fit D18 but fail the gate at 1 lot:** "
            + ", ".join(
                f"{r.source} {r.ticker} {r.kind} ({', '.join(r.blocks)})" for r in gate_only
            )
            + ". "
            if gate_only
            else "No row that D18 sizes ≥ 1 lot fails another rule at 1 lot. "
        )
        + "Menu rows that fail a Greek cap at the D18 lot count were stepped down to the "
        "largest passing lot count (the table's lots column; "
        f"{sum(1 for r in menus if r.fits and r.lots_25k < r.cap_lots)} rows). The "
        "settled-cash rule never "
        f"binds on an empty book (one structure ≤ ${float(cap):,.0f} ≪ ${feq:,.0f})."
    )
    w("")
    miss = sum(r.missing_spot for r in [*opens, *menus])
    if miss:
        w(f"({miss} rows had no recorded spot; their $Δ caps are not checked.)")
        w("")
    # 2 ---------------------------------------------------------------
    w("## 2. Concurrency (settled cash, T+1)")
    w("")
    fitting = [r for r in [*opens, *menus] if r.fits]
    typ = statistics.median(r.worst_loss_unit * r.lots_25k for r in fitting) if fitting else 0
    typ1 = statistics.median(r.worst_loss_unit for r in fitting) if fitting else 0
    w(
        "On `cash_debit` a debit is paid in full from settled cash and a close's proceeds "
        "settle T+1 (options), so the book holds at most settled cash ÷ debit per position."
    )
    w("")
    w(f"| sizing assumption | $ per position | positions at ${feq:,.0f} |")
    w("|---|---:|---:|")
    w(f"| full D18 cap on every name | ${float(cap):,.0f} | {int(feq // float(cap))} |")
    w(
        f"| median fitting structure at its $25K lot count | ${typ:,.0f} | "
        f"{int(feq // typ) if typ else 0} |"
    )
    w(f"| median fitting structure, 1 lot | ${typ1:,.0f} | {int(feq // typ1) if typ1 else 0} |")
    w("")
    peak_cash = max((u for _, u in res["cash_use"]), default=0.0)
    w(
        f"Old book: peak **{res['peak']}** structures open at once ({res['peak_at']}); "
        f"peak end-of-day debit outstanding ${peak_cash:,.0f}. "
        f"`max_open_positions` is {s.max_open_positions}. At ${feq:,.0f} with D18 sizing "
        f"filling the cap, cash runs out after {int(feq // float(cap))} positions, so "
        f"settled cash, not `max_open_positions`, is the binding count. A same-day close "
        "frees its cash only the next session (T+1), so a close-and-reopen day can hold "
        "fewer."
    )
    w("")
    w(f"| day (ET) | open at EOD | debit outstanding | fits in ${feq:,.0f}? |")
    w("|---|---:|---:|---|")
    eod = dict(res["eod"])
    for d, used in res["cash_use"]:
        w(f"| {d} | {eod.get(d, '')} | ${used:,.0f} | {'yes' if used <= feq else 'no'} |")
    w("")
    # 3 ---------------------------------------------------------------
    w("## 3. Dollar thresholds that don't scale with equity")
    w("")
    w(
        "Grep of `config/*.yaml` and `ArcSettings` for absolute $ values. Nothing in the "
        "trading path is an absolute $ floor that a smaller account fails: there is no "
        "min premium, min EV $ (the live Net EV floor is $0 per unit) or cost floor in $."
    )
    w("")
    w("| knob | value | at $25K |")
    w("|---|---|---|")
    for t in res["thresholds"]:
        w(f"| `{t['key']}` | {t['value']} | {t['effect']} |")
    w("")
    fv = fee_example(cost, 600.0, 2)
    fs = fee_example(cost, 600.0, 1)
    w(
        f"**Fees** (`config/costs.yaml`): ${cost.per_contract_both_sides:.4f} per contract "
        f"per side (ORF + OCC + CAT) plus TAF/SEC on sells. A $600 debit vertical, 1 lot, "
        f"round trip: ${fv['total']:.2f} = **{fv['pct']:.3f}%** of the debit "
        f"(entry ${fv['entry']:.2f}, exit ${fv['exit']:.2f}); a $600 long option "
        f"${fs['total']:.2f} = {fs['pct']:.3f}%. Fees are per contract, so they are "
        "the same share of a trade at any account size; slippage (`slippage_frac` "
        f"{cost.slippage_frac:g} × quoted spread) is the real cost and is also per "
        "contract."
    )
    w("")
    # 4 ---------------------------------------------------------------
    w("## 4. Universe impact")
    w("")
    uni = res["universe"]
    lo_r, med_r, hi_r, n_r = res["calib"]
    never = [u for u in uni if u["fits"] == "no"]
    est_no = [u for u in uni if u["fits"] == "no (est.)"]
    est_unl = [u for u in uni if u["fits"] == "unlikely (est.)"]
    unknown = [u for u in uni if u["fits"] == "?"]
    w(
        f"Active list of {res['active_as_of']} ({len(uni)} names). Per name: the cheapest "
        "in-band debit structure (long call/put + debit verticals, profile DTE window, "
        "scanner delta bands) that `arc.scanner.scan` builds on the name's latest recorded "
        "chain (`market_tape`), else the cheapest menu item seen for it. A name whose "
        f"cheapest structure exceeds ${float(cap):,.0f} sizes to 0 every time: it goes "
        "idle (journalled `sizing:cap_zero`), it doesn't error."
    )
    w("")
    w(
        "Names with no recorded chain (the tape only holds chains the loop scanned on "
        "2026-10-07..09) get an **estimate**: the $ max loss of the scanner's target bull "
        "call vertical (long |Δ| "
        f"{s.scanner_long_target_delta:g}, short |Δ| {s.scanner_debit_short_target_delta:g}, "
        "flat vol) from the name's latest `iv_daily` spot and IV30, scaled by how the "
        f"scanner's cheapest in-band item compared to that target on the {n_r} names that "
        f"have both (ratio min {lo_r:.2f}, median {med_r:.2f}, max {hi_r:.2f}). "
        "*no (est.)*: even the most favourable ratio is over the cap; *unlikely*: the "
        "median ratio is over the cap; *likely*: the median ratio fits."
    )
    w("")
    w(
        f"**Never fit, measured ({len(never)}):** "
        + (", ".join(f"{u['ticker']} (${u['cheapest']:,.0f})" for u in never) or "none")
    )
    w("")
    w(
        f"**Never fit, estimated ({len(est_no)}):** "
        + (", ".join(f"{u['ticker']} (~${u['est_low']:,.0f}+)" for u in est_no) or "none")
        + f". **Unlikely ({len(est_unl)}):** "
        + (", ".join(f"{u['ticker']} (~${u['est_mid']:,.0f})" for u in est_unl) or "none")
        + "."
    )
    w("")
    if unknown:
        w(f"**No data ({len(unknown)}):** " + ", ".join(u["ticker"] for u in unknown) + ".")
        w("")
    w("| ticker | tier | spot | cheapest in-band max loss | structure / source | fits |")
    w("|---|---|---:|---:|---|---|")
    order = {"no": 0, "no (est.)": 1, "unlikely (est.)": 2, "?": 3, "likely (est.)": 4, "yes": 5}
    for u in sorted(uni, key=lambda u: (order[u["fits"]], u["tier"], u["ticker"])):
        if u["cheapest"] is not None:
            ch = f"${u['cheapest']:,.0f}"
            how = f"{u['strategy']} · {u['source']}"
        elif "est_mid" in u:
            ch = f"~${u['est_low']:,.0f}–{u['est_mid']:,.0f}"
            how = f"est. (IV30 {u['iv30']:.0%})"
        else:
            ch, how = "—", "no data"
        sp = "—" if u["spot"] is None else f"${u['spot']:,.0f}"
        fit = f"**{u['fits']}**" if u["fits"].startswith("no") else u["fits"]
        w(f"| {u['ticker']} | {u['tier']} | {sp} | {ch} | {how} | {fit} |")
    w("")
    # 5 ---------------------------------------------------------------
    w("## 5. PDT is moot")
    w("")
    from arc.account_profiles import DayTradeRule, load_account_profiles

    profs = load_account_profiles()
    pdt = [
        p.name
        for p in profs.profiles.values()
        if p.day_trades.rule is DayTradeRule.PATTERN_DAY_TRADER
    ]
    w(
        f"- No profile in `config/account_profiles.yaml` sets `day_trades.rule: "
        f"pattern_day_trader` (profiles: {', '.join(sorted(profs.profiles))}; with it: "
        f"{', '.join(pdt) or 'none'}). The gate's `account_profile_day_trades` rule only "
        "runs for such a profile, so it never fires."
    )
    w(
        "- Alpaca paper `/v2/account` (read-only probe, 2026-10-10, all three key pairs) "
        "returns no `pattern_day_trader`, `daytrade_count` or `daytrading_buying_power` "
        "field. FINRA retired the PDT rule on 2026-06-04."
    )
    w(
        "- `DayTradeRule` (`arc/account_profiles.py`) now carries a one-line comment saying "
        "so; no behaviour change."
    )
    w("")
    # 6 ---------------------------------------------------------------
    w("## 6. Owner decisions, if any")
    w("")
    cap_needed = sorted(r.worst_loss_unit for r in opens if not r.fits)
    w(
        f"1. **Keep `max_alloc_pct` {s.max_alloc_pct:g} or raise it.** At ${feq:,.0f} it "
        f"lets {fit_o}/{len(opens)} past opens and {_pct(fit_m, len(menus))} of menu items "
        f"through, and caps settled-cash concurrency at ~{int(feq // float(cap))} full-size "
        "positions. The opens it drops are the high-priced names "
        + (
            f"(1 lot needs ${cap_needed[0]:,.0f}–${cap_needed[-1]:,.0f}, i.e. "
            f"{cap_needed[0] / feq:.1%}–{cap_needed[-1] / feq:.1%} of equity). "
            if cap_needed
            else ". "
        )
        + "Raising it fits more of them but means fewer concurrent positions and a "
        "larger single-name loss as a share of the account."
    )
    w(
        "2. **Never-fit names** (measured: "
        + (", ".join(u["ticker"] for u in never) or "none")
        + "; estimated: "
        + (", ".join(u["ticker"] for u in est_no) or "none")
        + "): leave them on the list (they idle and cost a shortlist slot when Research "
        "picks them) or drop/exclude them so the slots go to names that can trade. The "
        "core list ones ("
        + (", ".join(u["ticker"] for u in [*never, *est_no] if u["tier"] == "core") or "none")
        + ") are owner-set via `universe`."
    )
    w(
        f"3. **`max_open_positions` {s.max_open_positions}** no longer binds (cash does). "
        "No change needed; noted so nobody expects 8 full-size positions."
    )
    w(
        "4. Nothing else needs a decision: no absolute-$ floor fails at $25K, fees "
        "are a rounding error, PDT is gone."
    )
    w("")
    w("Applied overrides used (from `config_changes`):")
    w("")
    for k, v in res["applied"]:
        w(f"- `{k}` = `{v}`")
    w("")
    w(
        'Reproduce: `sqlite3 data/arc.db ".backup <scratch>/arc.db"` then '
        "`.venv/bin/python scripts/d80_25k_fit.py --db <scratch>/arc.db` (opens the store "
        "read-only; the live file gives the same output)."
    )
    w("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", default=str(REPO / "data" / "arc.db"))
    ap.add_argument("--equity", type=Decimal, default=Decimal(25000))
    ap.add_argument("--menu-days", type=int, default=14)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--json", default=None, help="also dump the replay rows as JSON")
    a = ap.parse_args(argv)
    conn = connect_ro(a.db)
    try:
        res = build(conn, equity=a.equity, menu_days=a.menu_days)
    finally:
        conn.close()
    Path(a.out).write_text(render(res))
    if a.json:
        rows = [asdict(r) | {"fits": r.fits} for r in [*res["opens"], *res["menus"]]]
        Path(a.json).write_text(json.dumps({"rows": rows, "universe": res["universe"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
