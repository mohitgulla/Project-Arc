"""E7.9 (D84): ThetaData history coverage + data-quality report (aggregates only).

Reads the cached ``options_eod/provider=thetadata`` parquet for an E7.7 universe's pull
list and writes ``docs/RESEARCH/theta-history-coverage.md``. Sections:

* pull summary per checkpoint (names, sessions, GB, rows) + ``checkpoints.json`` state;
* sessions covered per ticker, late starts (IPO/listing) and gaps (missing sessions inside
  the name's own listed span);
* rows per session over time (yearly median);
* quote quality: % two-sided, % crossed/locked, % zero-bid, % stale last trade;
* median quoted spread % of mid by moneyness x DTE bucket and by liquidity bucket, vs the
  ``config/costs.yaml`` estimate ``max(spread_min, spread_pct x mid)`` (recommendation only);
* Theta vs Alpaca on the overlap (2024-02 ->): matching contracts, Alpaca close vs Theta mid,
  % of Alpaca closes outside Theta's bid/ask;
* option-liquidity score (``<version>.optliq.parquet``): rows/quarters, % score > 0 per
  quarter and checkpoint, score distribution, and the would-have-moved list.

Sampling: quality statistics use every ``--sample-every``-th session per ticker (default 5)
so a C500 run stays minutes, not hours; coverage counts use every session (file names).
ThetaData retail terms: no raw rows, symbols with prices, or contract-level output.

    .venv/bin/python scripts/theta_history_report.py --version u500-20261009-s1-e52ff301 \
        --end 2026-10-09 [--checkpoint c125] [--out docs/RESEARCH/theta-history-coverage.md]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from arc.backtest.costs import load_cost_model
from arc.backtest.pull import optliq_path, pull_status, read_checkpoints, would_move
from arc.backtest.underlying import UnderlyingStore
from arc.backtest.universe import checkpoint_symbols, load_universe
from arc.data.history.store import ParquetHistoryStore
from arc.utils.calendar import ET, sessions_between

COLS = [
    "date",
    "symbol",
    "expiration",
    "strike",
    "right",
    "close",
    "volume",
    "bid",
    "ask",
    "last_trade",
    "open_interest",
]
MONEY = [
    (-1.0, -0.10, "OTM >10%"),
    (-0.10, -0.03, "OTM 3-10%"),
    (-0.03, 0.03, "ATM ±3%"),
    (0.03, 0.10, "ITM 3-10%"),
    (0.10, 9.0, "ITM >10%"),
]
DTE = [(0, 7, "0-7"), (8, 21, "8-21"), (22, 45, "22-45"), (46, 60, "46-60")]
ALPACA_START = dt.date(2024, 2, 1)


def _bucket(x: float, edges: list[tuple[float, float, str]]) -> str | None:
    for lo, hi, name in edges:
        if lo <= x < hi or (hi == edges[-1][1] and x == hi):
            return name
    return None


def load_day(store: ParquetHistoryStore, prov: str, sym: str, d: dt.date) -> pd.DataFrame | None:
    p = store.path_for(prov, sym, d)
    if not p.is_file():
        return None
    cols = [c for c in COLS if c in pq.read_schema(p).names]
    return pq.read_table(p, columns=cols).to_pandas()


def enrich(df: pd.DataFrame, spot: float | None, d: dt.date) -> pd.DataFrame:
    df = df.copy()
    bid, ask = df["bid"].astype(float), df["ask"].astype(float)
    df["two_sided"] = (bid > 0) & (ask > 0)
    df["crossed_locked"] = df["two_sided"] & (bid >= ask)
    df["zero_bid"] = bid.fillna(0) <= 0
    mid = (bid + ask) / 2
    df["mid"] = mid
    ok = df["two_sided"] & ~df["crossed_locked"]
    df["spread_pct"] = np.where(ok, (ask - bid) / mid, np.nan)
    # Theta EOD stamps last_trade with the session date even without a trade, so a
    # "stale" print = no volume on the session (the close is carried, not traded).
    df["stale"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0) <= 0
    exp = pd.to_datetime(df["expiration"]).dt.date
    df["dte"] = [(e - d).days for e in exp]
    if spot and spot > 0:
        k = df["strike"].astype(float)
        sign = np.where(df["right"].astype(str) == "call", 1.0, -1.0)
        df["money"] = sign * (spot - k) / spot  # >0 ITM
    else:
        df["money"] = np.nan
    return df


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - a linear report
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--data-dir", type=Path, default=Path("data"))
    ap.add_argument("--provider", default="thetadata")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2020, 1, 1))
    ap.add_argument("--end", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--checkpoint", default=None, help="Restrict quality stats to a checkpoint")
    ap.add_argument("--sample-every", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path("docs/RESEARCH/theta-history-coverage.md"))
    ap.add_argument(
        "--coverage-summary",
        type=Path,
        default=None,
        help="Text of `arc history coverage --provider thetadata` to embed",
    )
    args = ap.parse_args(argv)

    manifest, rows, membership = load_universe(args.data_dir, args.version)
    sessions = sessions_between(args.start, args.end)
    store = ParquetHistoryStore(args.data_dir)
    under = UnderlyingStore(args.data_dir)
    st = pull_status(
        args.data_dir, rows, version=manifest.version, provider=args.provider, sessions=sessions
    )
    recs = read_checkpoints(args.data_dir)
    pos = {r.symbol: r.position for r in rows}
    cp_of = {r.symbol: r.checkpoint for r in rows}
    names = (
        checkpoint_symbols(rows, args.checkpoint) if args.checkpoint else [r.symbol for r in rows]
    )
    started = [s for s in names if st.done_by_name.get(s, 0) > 0]
    L: list[str] = []
    w = L.append
    now = dt.datetime.now(tz=ET)
    w("# ThetaData history coverage and data quality (E7.9, D84)\n")
    w(
        f"Generated {now:%Y-%m-%d %H:%M %Z} by `scripts/theta_history_report.py` from "
        f"`data/options_eod/provider={args.provider}/` (universe `{manifest.version}`, "
        f"{args.start} → {args.end}, {len(sessions)} sessions, ≤60 DTE, closing OPRA NBBO + OI). "
        "Aggregates only: ThetaData's retail terms forbid redistributing raw rows.\n"
    )
    w(
        "Pull list: the stage-1 universe (underlying $-volume, point-in-time per quarter). "
        "Stage 2 (option OI/volume re-rank) was skipped by the owner on 2026-10-11 "
        "(≈42k probes at 50–85 s would outrun the paid month); the same score is computed "
        "post-pull below and does not re-rank the list.\n"
    )

    # -- pull summary -------------------------------------------------------------
    w("## Pull summary by checkpoint\n")
    w(
        "| checkpoint | names | names complete | sessions done / total | GB on disk | "
        "complete at (checkpoints.json) | rows |"
    )
    w("|---|---:|---:|---:|---:|---|---:|")
    for c in st.checkpoints:
        r = recs.get(c.checkpoint)
        w(
            f"| {c.checkpoint} | {c.names} | {c.names_complete} | {c.sessions_done:,} / "
            f"{c.sessions_total:,} | {c.gb:.1f} | "
            f"{(r.complete_at.strftime('%Y-%m-%d %H:%M') if r and r.complete_at else '—')} | "
            f"{(f'{r.rows:,}' if r and r.rows else '—')} |"
        )
    w("")
    if args.coverage_summary and args.coverage_summary.is_file():
        w("`arc history coverage --provider thetadata` (final summary):\n")
        w("```\n" + args.coverage_summary.read_text().strip() + "\n```\n")

    # -- per ticker coverage ------------------------------------------------------------
    w("## Sessions covered per ticker\n")
    late: list[str] = []
    vendor_late: list[str] = []
    gaps: list[str] = []
    empty_inner: list[str] = []
    empty_names: list[str] = []
    empty_days: dict[dt.date, int] = {}
    lead_sessions = sessions[min(5, len(sessions) - 1)]
    for s in names:
        p = store.path_for(args.provider, s, sessions[0]).parent
        have = (
            sorted(dt.date.fromisoformat(f.stem) for f in p.glob("*.parquet")) if p.is_dir() else []
        )
        have = [d for d in have if args.start <= d <= args.end]
        if not have:
            continue
        # first session with rows = listing start (zero-row files before it are pre-listing)
        nonempty = [
            d for d in have if pq.read_metadata(store.path_for(args.provider, s, d)).num_rows
        ]
        if not nonempty:
            empty_names.append(s)
            continue
        first, last = nonempty[0], nonempty[-1]
        if first > lead_sessions:
            # Listed before the options start? Then the late start is Theta's, not a listing:
            # a ticker change (history under the old root) or a vendor hole.
            bars = [d for d in under.read(s).index if args.start <= d < first]
            if len(bars) > 20:  # noqa: PLR2004 - options list days to weeks after an IPO
                vendor_late.append(f"{s} {first} ({len(bars)} sessions)")
            else:
                late.append(f"{s} {first}")
        span = [d for d in sessions if first <= d <= last]
        miss = sorted(set(span) - set(have))
        if miss:
            gaps.append(f"{s}: {len(miss)} ({miss[0]}…{miss[-1]})")
        ne = set(nonempty)
        inner = [d for d in have if first <= d <= last and d not in ne]
        if inner:
            empty_inner.append(f"{s} {len(inner)}")
            for d in inner:
                empty_days[d] = empty_days.get(d, 0) + 1
    total_done = sum(1 for s in names if st.done_by_name.get(s, 0) >= len(sessions))
    w(
        f"- names with every session cached: **{total_done} / {len(names)}**; names started: "
        f"{len(started)}"
    )
    w(
        f"- names whose option history starts after {args.start} because the underlying "
        f"listed later (IPO/spin-off, not a gap): {len(late)}"
        + (f" — {', '.join(late)}" if late else "")
    )
    w(
        f"- **known gap:** names listed (Alpaca bars) before their first Theta option session, "
        f"i.e. history filed under a former root (ticker change) or a vendor hole: "
        f"{len(vendor_late)} (first Theta session, listed sessions before it)"
        + (f" — {', '.join(vendor_late)}" if vendor_late else "")
    )
    w(
        f"- names with every cached session empty (no listed options in the window): "
        f"{len(empty_names)}" + (f" — {', '.join(empty_names)}" if empty_names else "")
    )
    w(
        f"- names with missing sessions inside their own span (not cached): {len(gaps)}"
        + ("".join(f"\n  - {g}" for g in gaps) if gaps else "")
    )
    w(
        f"- **known gap:** names with empty sessions (Theta returned no rows) inside their own "
        f"span: {len(empty_inner)}, {sum(empty_days.values()):,} ticker-sessions"
        + (f" — {', '.join(empty_inner)}" if empty_inner else "")
    )
    if empty_days:
        top = sorted(empty_days.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        w(
            "  - sessions with the most empty names: "
            + ", ".join(f"{d} ({n})" for d, n in sorted(top))
        )
    w("")

    # -- quality sample -------------------------------------------------------------------
    sample_days = sessions[:: max(1, args.sample_every)]
    frames: list[pd.DataFrame] = []
    rows_by_day: dict[dt.date, list[int]] = {}
    closes = {}
    for s in started:
        cs = under.read(s)
        closes[s] = {k: float(v) for k, v in cs.items()}
        for d in sample_days:
            df = load_day(store, args.provider, s, d)
            if df is None:
                continue
            rows_by_day.setdefault(d, []).append(len(df))
            if df.empty:
                continue
            e = enrich(df, closes[s].get(d), d)
            e["ticker"] = s
            e["cp"] = cp_of.get(s)
            frames.append(
                e[
                    [
                        "ticker",
                        "cp",
                        "date",
                        "two_sided",
                        "crossed_locked",
                        "zero_bid",
                        "stale",
                        "spread_pct",
                        "mid",
                        "dte",
                        "money",
                        "volume",
                        "open_interest",
                    ]
                ]
            )
    q = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    w(f"## Rows per session over time (sampled every {args.sample_every}th session)\n")
    if rows_by_day:
        yr: dict[int, list[int]] = {}
        for d, v in rows_by_day.items():
            yr.setdefault(d.year, []).extend(v)
        w("| year | ticker-sessions | median rows / ticker-session | p90 | total rows (sample) |")
        w("|---|---:|---:|---:|---:|")
        for y in sorted(yr):
            v = np.array(yr[y])
            w(
                f"| {y} | {len(v):,} | {np.median(v):,.0f} | {np.percentile(v, 90):,.0f} | "
                f"{int(v.sum()):,} |"
            )
        w("")
    if q.empty:
        w("_no rows cached yet_\n")
    else:
        n = len(q)
        w("## Quote quality (sampled contract-sessions)\n")
        w(f"- contract-sessions sampled: {n:,} across {q['ticker'].nunique()} names")
        w(f"- two-sided quote (bid > 0 and ask > 0): **{100 * q['two_sided'].mean():.1f}%**")
        w(f"- crossed or locked (bid ≥ ask, two-sided): {100 * q['crossed_locked'].mean():.2f}%")
        w(f"- zero bid: {100 * q['zero_bid'].mean():.1f}%")
        w(
            f"- no trade on the session (volume 0; close is a carried/stale print): "
            f"{100 * q['stale'].mean():.1f}%"
        )
        w(f"- open interest present: {100 * q['open_interest'].notna().mean():.1f}%\n")

        cost = load_cost_model()
        v = q[q["spread_pct"].notna() & (q["mid"] > 0)].copy()
        v["est_pct"] = np.maximum(cost.spread_min / v["mid"], cost.spread_pct)
        v["mb"] = [(_bucket(x, MONEY) if not math.isnan(x) else None) for x in v["money"]]
        v["db"] = [_bucket(x, [(a, b, c) for a, b, c in DTE]) for x in v["dte"]]
        w("## Quoted spread vs the costs.yaml estimate\n")
        w(
            f"Estimate = max({cost.spread_min}, {cost.spread_pct} × mid) as % of mid. Measured = "
            "(ask − bid) / mid on two-sided, uncrossed closing quotes. Medians.\n"
        )
        w("By moneyness × DTE (measured % / estimate %, n):\n")
        w("| moneyness | " + " | ".join(f"DTE {d[2]}" for d in DTE) + " |")
        w("|---|" + "---:|" * len(DTE))
        for _, _, mb in MONEY:
            cells = []
            for _, _, db in DTE:
                g = v[(v["mb"] == mb) & (v["db"] == db)]
                cells.append(
                    "—"
                    if g.empty
                    else f"{100 * g['spread_pct'].median():.1f} / "
                    f"{100 * g['est_pct'].median():.1f} ({len(g):,})"
                )
            w(f"| {mb} | " + " | ".join(cells) + " |")
        w("")
        # liquidity buckets: pull-list position terciles of the sample
        v["liq"] = pd.cut(
            [pos.get(t, 999) for t in v["ticker"]],
            [0, 50, 125, 250, 500],
            labels=["top 50", "51-125", "126-250", "251-500"],
        )
        w("By liquidity bucket (pull-list position) and by mid price:\n")
        w(
            "| bucket | n | measured median % | estimate median % | measured p75 % | "
            "measured ≤ estimate |"
        )
        w("|---|---:|---:|---:|---:|---:|")
        for b, g in v.groupby("liq", observed=True):
            w(
                f"| {b} | {len(g):,} | {100 * g['spread_pct'].median():.1f} | "
                f"{100 * g['est_pct'].median():.1f} | {100 * g['spread_pct'].quantile(0.75):.1f} | "
                f"{100 * (g['spread_pct'] <= g['est_pct']).mean():.0f}% |"
            )
        w("")
        w(
            "| mid price | n | measured median % | estimate median % | measured p75 % | "
            "measured ≤ estimate |"
        )
        w("|---|---:|---:|---:|---:|---:|")
        v["px"] = pd.cut(
            v["mid"], [0, 0.5, 1, 2, 5, 1e9], labels=["< $0.50", "$0.50-1", "$1-2", "$2-5", "> $5"]
        )
        for b, g in v.groupby("px", observed=True):
            w(
                f"| {b} | {len(g):,} | {100 * g['spread_pct'].median():.1f} | "
                f"{100 * g['est_pct'].median():.1f} | {100 * g['spread_pct'].quantile(0.75):.1f} | "
                f"{100 * (g['spread_pct'] <= g['est_pct']).mean():.0f}% |"
            )
        w("")
        trade = v[(v["db"].isin(["22-45", "46-60"])) & (v["mb"].isin(["OTM 3-10%", "ATM ±3%"]))]
        if len(trade):
            med = trade["spread_pct"].median()
            w(
                f"Recommendation (no change made): on the strikes the strategies trade "
                f"(22–60 DTE, ATM to 10% OTM) the measured median spread is "
                f"{100 * med:.1f}% of mid vs the {100 * cost.spread_pct:.0f}% estimate"
                + (
                    " — the flat estimate is optimistic there; replace it with the quoted spread "
                    "(E7.10 quoted marks) rather than a new constant."
                    if med > cost.spread_pct
                    else " — the flat estimate is conservative there."
                )
                + " `config/costs.yaml` is unchanged.\n"
            )

    # -- Theta vs Alpaca overlap ------------------------------------------------------------
    w("## Theta vs Alpaca on the overlap (2024-02 →)\n")
    alp = [s for s in started if store.cached_dates("alpaca", s)]
    m_rows = []
    for s in alp:
        for d in sorted(store.cached_dates("alpaca", s)):
            if d < ALPACA_START or d not in set(sample_days):
                continue
            a = load_day(store, "alpaca", s, d)
            t = load_day(store, args.provider, s, d)
            if a is None or t is None or a.empty or t.empty:
                continue
            j = a[["symbol", "close"]].merge(t[["symbol", "bid", "ask"]], on="symbol")
            j = j[(j["bid"] > 0) & (j["ask"] > j["bid"]) & j["close"].notna() & (j["close"] > 0)]
            if j.empty:
                continue
            mid = (j["bid"] + j["ask"]) / 2
            m_rows.append(
                pd.DataFrame(
                    {
                        "ticker": s,
                        "a_n": len(a),
                        "gap": (j["close"] - mid).abs() / mid,
                        "outside": (j["close"] < j["bid"]) | (j["close"] > j["ask"]),
                    }
                )
            )
    if m_rows:
        mm = pd.concat(m_rows, ignore_index=True)
        w(
            f"- names on both partitions: {len(alp)} ({', '.join(alp)}); matched contract-sessions "
            f"(sampled, Theta two-sided): {len(mm):,}"
        )
        w(
            f"- Alpaca close vs Theta mid, |close − mid| / mid: median "
            f"{100 * mm['gap'].median():.1f}%, p75 {100 * mm['gap'].quantile(0.75):.1f}%, "
            f"p90 {100 * mm['gap'].quantile(0.9):.1f}%"
        )
        w(
            f"- Alpaca closes outside Theta's closing bid/ask: "
            f"**{100 * mm['outside'].mean():.1f}%**"
            " — that share of the old trade-close marks could not have been filled at the close.\n"
        )
    else:
        w("_no overlapping Alpaca/Theta sessions cached yet_\n")

    # -- option-liquidity score --------------------------------------------------------------
    w("## Option-liquidity score (`<version>.optliq.parquet`, replaces Stage 2)\n")
    op = optliq_path(args.data_dir, manifest.version)
    if not op.is_file():
        w(
            "_not computed yet (runs per checkpoint as each completes: "
            "`arc history universe optliq --checkpoint c125`)_\n"
        )
    else:
        o = pd.read_parquet(op)
        o["quarter"] = pd.to_datetime(o["quarter"]).dt.date
        o["cp"] = o["symbol"].map(cp_of)
        w(
            f"- rows {len(o):,} · quarters {o['quarter'].nunique()} · "
            f"symbols {o['symbol'].nunique()}"
        )
        w("| checkpoint | rows | % score > 0 | p10 | median | p90 |")
        w("|---|---:|---:|---:|---:|---:|")
        for c in ("c125", "c250", "c500"):
            size = int(c[1:])
            g = o[o["symbol"].map(lambda s, k=size: pos.get(s, 10**6) <= k)]
            if g.empty:
                continue
            sc = g["score"]
            w(
                f"| {c} | {len(g):,} | {100 * (sc > 0).mean():.1f} | {sc.quantile(0.1):,.0f} | "
                f"{sc.median():,.0f} | {sc.quantile(0.9):,.0f} |"
            )
        w("")
        w("% of symbols with score > 0, per quarter:\n")
        per_q = o.groupby("quarter")["score"].apply(lambda s: 100 * (s > 0).mean())
        w(" · ".join(f"{q:%Y-%m} {v:.0f}%" for q, v in per_q.items()) + "\n")
        moved_in, moved_out = would_move(o, membership, top=500)
        w(
            "Report only: names a Stage-2 (score) ordering would have moved **into** a quarter's "
            "stage-1 top set (quarters): "
            + (", ".join(f"{s} ({n})" for s, n in moved_in[:40]) or "none")
        )
        w(
            "\n…and **out of** it: "
            + (", ".join(f"{s} ({n})" for s, n in moved_out[:40]) or "none")
            + "\n"
        )
        w(
            "Scored only among names on the pull list (the score needs pulled parquet), so "
            '"into" means a pull-list name below the quarter\'s stage-1 cut.\n'
        )

    w("## Survivorship\n")
    uj = args.data_dir / "e79" / "underlying.json"
    if uj.is_file():
        u = json.loads(uj.read_text())
        miss = [x["symbol"] for x in u if not x.get("closes")]
        w(
            f"- names with no Alpaca bars (excluded; delisted, survivorship gap): {len(miss)}"
            + (f" — {', '.join(miss)}" if miss else "")
        )
    w(
        f"- universe candidate source: {manifest.candidate_source} "
        f"({manifest.candidates_by_source}); missing underlying bars at build: "
        f"{len(manifest.missing_underlying)}"
    )
    for note in manifest.notes:
        w(f"- note: {note}")
    w("")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(L) + "\n")
    sys.stdout.write(f"wrote {args.out} ({len(started)} names with data)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
