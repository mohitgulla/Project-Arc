"""``arc positions review``: the E6.4 position review + reallocation dry run (read-only).

Prints each open position's review (P&L, % of max gain / debit, DTE, theta,
remaining net EV per $ BP, remaining PoP, exit signal), the closes the Quant exit step
would propose, and every scored close-to-reallocate pair with its D19 outcome.
Writes nothing and proposes nothing: the real closes come from ``exits.mandatory``
and the Research exit path (``quant.exit → risk.exit → quant.propose``, D56), where
every close goes through the gate and approval.

``--fixtures`` uses the bundled SPY recording and ``arc/positions/fixtures/book.json``;
otherwise the open structures and today's capacity-blocked entries come from ``--db``
and marks from Alpaca market data (no broker call).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    import sqlite3

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.exits import ExitConfig
    from arc.positions.evaluate import PositionReview
    from arc.positions.reallocate import CapacityCandidate, ScoredPair, SwapSuggestion

__all__ = ["BOOK_FIXTURE", "add_positions_parser", "fixture_book", "run_positions"]

BOOK_FIXTURE = Path(__file__).parent / "fixtures" / "book.json"


def add_positions_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("positions", help="Position manager (E6.4): reviews, exits, swaps")
    psub = p.add_subparsers(dest="positions_command", required=True)
    r = psub.add_parser("review", help="Dry run: reviews + suggested closes/swaps (read-only)")
    r.add_argument(
        "--fixtures", action="store_true", help="Bundled SPY recording + fixture book (offline)"
    )
    r.add_argument("--book", default=None, help="Fixture book JSON (default: the bundled one)")
    r.add_argument("--db", default=None, help="Audit DB (live mode; default data/arc.db)")
    r.add_argument("--config", default=None, help="exits.yaml path (default config/exits.yaml)")
    r.add_argument("--json", action="store_true", help="Emit reviews + pairs as JSON")


def _price(market: MarketDataProvider, legs: list[list[Any]], as_of: dt.date, r: float) -> Any:
    from arc.models import LegIntent
    from arc.pipeline.market import price_structure

    return price_structure(
        market, [(str(o), LegIntent(s), int(n)) for o, s, n in legs], as_of=as_of, r=r
    )


def _review(
    *,
    sid: str,
    ticker: str,
    structure: Any,
    entry_net: float,
    contracts: int,
    market: MarketDataProvider,
    exits: ExitConfig,
    settings: ArcSettings,
    as_of: dt.date,
    eod: bool,
    peak_pnl: float | None = None,
) -> PositionReview:
    from arc.execution.exits import price_close
    from arc.exits.position import OpenPosition, PositionMarks
    from arc.positions.evaluate import review_position
    from arc.positions.steps import _theta

    r = settings.scanner_risk_free_rate
    priced = price_close(market, structure, as_of=as_of, r=r)
    mids = {k: float(c.mid) for k, c in priced.contracts.items() if c.mid is not None}
    return review_position(
        structure_id=sid,
        ticker=ticker,
        position=OpenPosition(structure=structure, entry_net=entry_net, contracts=contracts),
        marks=PositionMarks(
            as_of=as_of,
            leg_mids=mids,
            leg_spreads=priced.leg_spreads(),
            spot=priced.spot,
            iv=priced.atm_iv,
            r=r,
            end_of_day=eod,
        ),
        exits=exits,
        theta_per_day=_theta(priced, structure, as_of, r),
        peak_pnl=peak_pnl,
    )


def fixture_book(
    settings: ArcSettings, exits: ExitConfig, book: Path | None = None
) -> tuple[list[PositionReview], list[CapacityCandidate]]:
    """Reviews + capacity candidates for the fixture book on the SPY recording."""
    from arc.gate.rules import CapacityRejection
    from arc.pipeline.env import FIXTURE_NOW, PipelineEnv
    from arc.pipeline.steps import _proposal_exit_model
    from arc.positions.reallocate import CapacityCandidate

    data = json.loads((book or BOOK_FIXTURE).read_text())
    market = PipelineEnv.fixtures().market
    as_of = FIXTURE_NOW.date()
    r = settings.scanner_risk_free_rate
    reviews = [
        _review(
            sid=p["id"],
            ticker=p["ticker"],
            structure=_price(market, p["legs"], as_of, r).structure,
            entry_net=float(p["entry_net"]),
            contracts=int(p["contracts"]),
            market=market,
            exits=exits,
            settings=settings,
            as_of=as_of,
            eod=True,
        )
        for p in data["positions"]
    ]
    cands: list[CapacityCandidate] = []
    for c in data.get("candidates", []):
        priced = _price(market, c["legs"], as_of, r)
        model = _proposal_exit_model(priced, exits, r, None, None)
        st = priced.structure
        bp = st.buying_power if st.buying_power is not None else st.max_loss
        if model is None or not bp:
            continue
        cands.append(
            CapacityCandidate(
                source_ref=c["ref"],
                ticker=c["ticker"],
                kind=st.kind.value if st.kind else None,
                rejected_for=CapacityRejection(c["rejected_for"]),
                violation_codes=list(c["violation_codes"]),
                net_ev=model.managed.net_ev,
                pop=model.managed.pop,
                buying_power=float(bp),
            )
        )
    return reviews, cands


def _float(x: object) -> float | None:
    return None if x is None else float(x)  # type: ignore[arg-type]


def _live_book(
    conn: sqlite3.Connection, settings: ArcSettings, exits: ExitConfig
) -> tuple[list[PositionReview], list[CapacityCandidate]]:
    from arc.data.alpaca import AlpacaMarketData
    from arc.models import Structure
    from arc.positions.marks import stored_peak_pnl
    from arc.positions.steps import capacity_candidates
    from arc.store.execution import OpenStructureRepo
    from arc.store.swaps import SwapRepo
    from arc.utils.calendar import ET

    now = dt.datetime.now(ET)
    market = AlpacaMarketData()
    reviews: list[PositionReview] = []
    for row in OpenStructureRepo(conn).list_open():
        try:
            reviews.append(
                _review(
                    sid=str(row["id"]),
                    ticker=str(row["ticker"]),
                    structure=Structure.model_validate_json(row["structure_json"]),
                    entry_net=float(row["entry_net"]),
                    contracts=int(row["contracts"]),
                    market=market,
                    exits=exits,
                    settings=settings,
                    as_of=now.date(),
                    eod=now.time() >= dt.time(15, 30),
                    peak_pnl=_float(
                        stored_peak_pnl(conn, str(row["id"]), opened_at=row.get("opened_at"))
                    ),
                )
            )
        except (LookupError, ValueError) as exc:
            sys.stderr.write(f"{row['ticker']} {row['id']}: cannot review ({exc})\n")
    cands, _ = capacity_candidates(conn, now.date().isoformat(), taken=SwapRepo(conn).sources())
    return reviews, cands


def _fmt_review(r: PositionReview) -> str:
    prog = (
        f"{r.pct_of_max_gain:+.0%} of max gain" if r.credit and r.pct_of_max_gain is not None
        else f"{r.pct_of_debit:+.0%} of debit" if r.pct_of_debit is not None
        else "n/a"
    )  # fmt: skip
    rem = "n/a" if r.remaining_ev is None else f"${r.remaining_ev:+,.2f}"
    per = "n/a" if r.remaining_ev_per_bp is None else f"{r.remaining_ev_per_bp:+.4f}"
    pop = "n/a" if r.remaining_pop is None else f"{r.remaining_pop:.0%}"
    theta = "n/a" if r.theta_per_day is None else f"${r.theta_per_day:+,.2f}/day"
    sig = r.signal
    return (
        f"  {r.ticker:<5} {r.structure_id:<22} {r.kind or '?':<16} x{r.contracts} "
        f"{r.dte:>3} DTE  P&L ${r.pnl_total:+,.0f} ({prog})  theta {theta}\n"
        f"        remaining EV {rem}/unit on ${r.buying_power or 0:,.0f} BP ({per}/$BP), "
        f"remaining PoP {pop}  -> {sig.kind.value + ': ' + sig.detail if sig else 'hold'}"
    )


def format_report(
    reviews: list[PositionReview],
    cands: list[CapacityCandidate],
    suggestions: list[SwapSuggestion],
    pairs: list[ScoredPair],
) -> str:
    out = [f"Position reviews ({len(reviews)})"]
    out += [_fmt_review(r) for r in reviews] or ["  (no open positions)"]
    closes = [r for r in reviews if r.signal is not None]
    out.append(f"\nSuggested closes ({len(closes)}; each is a proposal: gate + approval)")
    out += [
        f"  close {r.ticker} {r.structure_id}: {r.signal.kind.value}"
        for r in closes
        if r.signal is not None
    ] or ["  (none)"]
    out.append(f"\nCapacity-blocked entries ({len(cands)})")
    out += [
        f"  {c.source_ref}: {c.ticker} {c.kind or '?'} rejected for {c.rejected_for.value} "
        f"({', '.join(c.violation_codes)}); net EV ${c.net_ev:+,.2f} on ${c.buying_power:,.0f} "
        f"BP ({c.ev_per_bp:+.4f}/$BP), PoP {c.pop:.0%}"
        for c in cands
    ] or ["  (none)"]
    out.append(
        f"\nSuggested swaps ({len(suggestions)}; Risk may veto; close first, open after fill)"
    )
    out += [f"  {s.detail}" for s in suggestions] or ["  (none)"]
    rest = [p for p in pairs if p.outcome != "suggested"]
    if rest:
        out.append(f"\nPairs not suggested ({len(rest)})")
        out += [
            f"  {p.close_ticker} {p.close_structure_id} -> {p.source_ref}: {p.outcome}"
            + (f" ({p.detail})" if p.detail else "")
            for p in rest
        ]
    return "\n".join(out)


def run_positions(args: argparse.Namespace, settings: ArcSettings | None = None) -> int:
    from arc.config import ArcSettings
    from arc.exits import load_exit_config
    from arc.positions.reallocate import ReallocRules, score_swaps

    settings = settings or ArcSettings()
    exits = load_exit_config(Path(args.config) if args.config else None)
    if args.fixtures or args.book:
        reviews, cands = fixture_book(settings, exits, Path(args.book) if args.book else None)
    else:
        from arc.pipeline.runner import open_db

        conn = open_db(args.db, copy=True)  # read-only: an in-memory copy
        reviews, cands = _live_book(conn, settings, exits)
    rules = ReallocRules(
        min_edge=settings.realloc_min_edge,
        pop_tolerance=settings.realloc_pop_tolerance,
        max_per_day=settings.realloc_max_swaps_per_day,
        max_per_ticker_per_day=settings.realloc_max_swaps_per_ticker_per_day,
    )
    suggestions, pairs = score_swaps([r for r in reviews if r.signal is None], cands, rules)
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "reviews": [r.model_dump(mode="json", exclude={"structure"}) for r in reviews],
                    "candidates": [c.model_dump(mode="json") for c in cands],
                    "suggestions": [s.model_dump(mode="json") for s in suggestions],
                    "pairs": [p.model_dump(mode="json") for p in pairs],
                },
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(format_report(reviews, cands, suggestions, pairs) + "\n")
    return 0
