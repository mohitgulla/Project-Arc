"""``arc backtest data check`` and ``arc backtest cache stats|clear`` (E7.10, D84).

``data check`` loads every checkpoint name that has option history through
:class:`arc.backtest.data.HistoryView` and prints, per ticker: the partition and mark
mode, decision sessions, % of contracts dropped by each quoted-mark rule, IV30 and
IV-rank coverage, detected earnings events, eligible sessions and the D9 screen pass
rate. With ``--db`` (opened read-only) it adds detected-vs-Finnhub earnings
precision/recall and the IV30 cross-check against ``iv_daily`` ``alpaca_cm30``.
Aggregates only: no raw rows are printed (ThetaData retail terms).
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from arc.backtest.marks import DROP_REASONS, DropCounts

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    from arc.backtest.data import HistoryView

__all__ = ["CheckRow", "add_cache_parser", "add_data_parser", "run_cache_cli", "run_data_cli"]


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def add_data_parser(bs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = bs.add_parser("data", help="E7.10: point-in-time backtest data layer")
    ds = p.add_subparsers(dest="data_command", required=True)
    ck = ds.add_parser("check", help="Per-ticker coverage of the data layer (aggregates only)")
    ck.add_argument("--universe", default="c125", choices=["c125", "c250", "c500"])
    ck.add_argument("--from", dest="start", type=_date, required=True)
    ck.add_argument("--to", dest="end", type=_date, required=True)
    ck.add_argument("--data-dir", type=Path, default=Path("data"))
    ck.add_argument("--config", type=Path, default=None, help="Ranking config file")
    ck.add_argument("--tickers", default=None, help="Comma list (default: the checkpoint)")
    ck.add_argument("--provider", default=None, help="Override backtest.data.provider")
    ck.add_argument(
        "--db", type=Path, default=None, help="Audit DB (read-only): Finnhub + iv_daily checks"
    )
    ck.add_argument("--json", type=Path, default=None, help="Also write the rows as JSON")


def add_cache_parser(bs: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = bs.add_parser("cache", help="E7.10: the backtest menu cache")
    cs = p.add_subparsers(dest="cache_command", required=True)
    for name, hlp in (("stats", "Files / bytes / tickers"), ("clear", "Delete cached menus")):
        c = cs.add_parser(name, help=hlp)
        c.add_argument("--data-dir", type=Path, default=Path("data"))
        if name == "clear":
            c.add_argument("--ticker", default=None, help="Only this ticker")


class CheckRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    provider: str
    marks: str
    sessions: int
    drops: DropCounts
    iv30_days: int
    iv_rank_days: int
    earnings: int
    eligible_days: int
    screen_pass_days: int
    etf: bool


def check_ticker(view: HistoryView, ticker: str) -> CheckRow | None:
    h = view.load(ticker)
    days = view.sessions(ticker)
    if not days:
        return None
    drops = DropCounts()
    for d in days:
        drops = drops.add(h.drops.get(d, DropCounts()))
    elig = [view.eligibility(ticker, d) for d in days]
    return CheckRow(
        ticker=h.ticker,
        provider=h.provider,
        marks=h.marks,
        sessions=len(days),
        drops=drops,
        iv30_days=sum(1 for d in days if (p := h.iv.get(d)) and p.iv30 is not None),
        iv_rank_days=sum(1 for d in days if (p := h.iv.get(d)) and p.iv_rank is not None),
        earnings=sum(1 for e in h.events if view.start <= e.day <= view.end),
        eligible_days=sum(e.eligible for e in elig),
        screen_pass_days=sum(e.screen_pass for e in elig),
        etf=view.is_etf(ticker),
    )


def format_rows(rows: Sequence[CheckRow]) -> str:
    drop_cols = " ".join(f"{r[:10]:>10}" for r in DROP_REASONS)
    head = (
        f"{'ticker':<6} {'prov':<9} {'marks':<6} {'sess':>5} {'contracts':>10} {drop_cols} "
        f"{'iv30%':>6} {'ivr%':>6} {'earn':>4} {'elig%':>6} {'scr%':>5}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        pct = " ".join(f"{r.drops.pct(x):>9.1f}%" for x in DROP_REASONS)
        n = max(r.sessions, 1)
        lines.append(
            f"{r.ticker:<6} {r.provider:<9} {r.marks:<6} {r.sessions:>5} "
            f"{r.drops.contracts:>10} {pct} {100 * r.iv30_days / n:>5.1f}% "
            f"{100 * r.iv_rank_days / n:>5.1f}% {'etf' if r.etf else r.earnings:>4} "
            f"{100 * r.eligible_days / n:>5.1f}% {100 * r.screen_pass_days / n:>4.0f}%"
        )
    return "\n".join(lines)


def _ro(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        msg = f"audit store not found: {db}"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def finnhub_dates(conn: sqlite3.Connection) -> dict[str, list[dt.date]]:
    """Finnhub calendar earnings dates per ticker from ``raw_docs`` (read-only)."""
    out: dict[str, list[dt.date]] = {}
    for row in conn.execute("SELECT url, tickers_hint FROM raw_docs WHERE source = 'earnings'"):
        parts = str(row["url"]).rstrip("/").split("/")
        try:
            day = dt.date.fromisoformat(parts[-1])
        except ValueError:
            continue
        for sym in json.loads(row["tickers_hint"] or "[]") or [parts[-2]]:
            out.setdefault(str(sym).upper(), []).append(day)
    return out


def iv_crosscheck(
    conn: sqlite3.Connection, view: HistoryView, tickers: Sequence[str]
) -> list[tuple[str, dt.date, float, float]]:
    """(ticker, day, backtest IV30, live alpaca_cm30) on every day both exist."""
    out: list[tuple[str, dt.date, float, float]] = []
    for t in tickers:
        h = view.load(t)
        for row in conn.execute(
            "SELECT day, iv30 FROM iv_daily WHERE source = 'alpaca_cm30' AND ticker = ? "
            "ORDER BY day",
            (t,),
        ):
            d = dt.date.fromisoformat(row["day"])
            p = h.iv.get(d)
            if p is not None and p.iv30 is not None and view.start <= d <= view.end:
                out.append((t, d, p.iv30, float(row["iv30"])))
    return out


def run_data_cli(args: argparse.Namespace) -> int:
    from arc.backtest.costs import load_cost_model
    from arc.backtest.data import HistoryView
    from arc.backtest.earnings_hist import match_events
    from arc.backtest.ranking import load_ranking_file
    from arc.config import ArcSettings

    cfg = load_ranking_file(args.config)
    ds = cfg.backtest.data
    if args.provider:
        ds = ds.model_copy(update={"provider": args.provider})
    view = HistoryView(
        args.data_dir,
        settings=ds,
        universe=args.universe,
        start=args.start,
        end=args.end,
        cost=load_cost_model(),
        flat_rate=cfg.backtest.risk_free_rate,
        min_iv_obs=ArcSettings().iv_min_obs_rank,
    )
    names = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else list(view.symbols)
    )
    with_data = [t for t in names if view.provider_for(t) is not None]
    out = sys.stdout
    out.write(
        f"universe {args.universe} ({view.version}): {len(names)} names, "
        f"{len(with_data)} with option history; primary partition {ds.provider} "
        f"({ds.marks} marks), fallback {ds.fallback_provider} ({ds.alpaca_marks}); "
        f"rate {view.rates.source}; window {args.start} -> {args.end}\n"
    )
    rows = [r for t in with_data if (r := check_ticker(view, t)) is not None]
    out.write(format_rows(rows) + "\n")
    out.write(
        "drop %: share of contracts in the 1-75 DTE window dropped by each quoted-mark rule "
        "(0 on a smile/close partition: those modes have no quotes); iv30% / ivr%: sessions "
        f"with an IV30 / an IV rank (>= {view.min_iv_obs} obs in {ds.iv_lookback}); earn: "
        "detected events in the window; elig%: in checkpoint + PIT + close; scr%: live D9 "
        "screen pass (flag only)\n"
    )
    if args.db is not None:
        conn = _ro(args.db)
        try:
            ref = finnhub_dates(conn)
            every = [d for ds_ in ref.values() for d in ds_]
            lo, hi = (min(every), max(every)) if every else (None, None)
            out.write(
                f"\nearnings vs Finnhub raw_docs (calendar {lo} -> {hi}; only sessions inside "
                "both it and the cache count; +/-1 session):\n"
            )
            for r in rows:
                if r.etf:
                    continue
                h = view.load(r.ticker)
                both = [d for d in view.sessions(r.ticker) if lo and hi and lo <= d <= hi]
                st = match_events([e.day for e in h.events], ref.get(r.ticker, []), both)
                if not both or (st.reference == 0 and st.detected == 0):
                    out.write(
                        f"  {r.ticker:<6} no Finnhub or detected date in the {len(both)} "
                        "overlapping sessions\n"
                    )
                    continue
                out.write(
                    f"  {r.ticker:<6} reference {st.reference} detected {st.detected} "
                    f"matched {st.matched} precision {_p(st.precision)} recall {_p(st.recall)}\n"
                )
            pairs = iv_crosscheck(conn, view, [r.ticker for r in rows])
        finally:
            conn.close()
        out.write("\nIV30 cross-check vs iv_daily alpaca_cm30 (vol points):\n")
        for t, d, ours, live in pairs:
            out.write(
                f"  {t:<6} {d}  backtest {ours * 100:6.2f}  live {live * 100:6.2f}  "
                f"diff {(ours - live) * 100:+6.2f}\n"
            )
        if pairs:
            worst = max(abs(a - b) for _, _, a, b in pairs) * 100
            out.write(f"  pairs {len(pairs)}  max |diff| {worst:.2f} pts\n")
    if args.json is not None:
        args.json.write_text(json.dumps([r.model_dump(mode="json") for r in rows], indent=1))
    return 0


def _p(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def run_cache_cli(args: argparse.Namespace) -> int:
    from arc.backtest.menucache import MenuCache

    cache = MenuCache(args.data_dir)
    if args.cache_command == "stats":
        st = cache.stats()
        sys.stdout.write(
            f"menu cache {st.root}: {st.files} files, {st.bytes / 1e6:.1f} MB, "
            f"{st.tickers} tickers\n"
        )
        return 0
    n = cache.clear(args.ticker)
    sys.stdout.write(f"removed {n} cached menus\n")
    return 0
