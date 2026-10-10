"""E17.3 (D77): build the regime v2 vs v1 report tables from ``arc backtest rank`` output.

Usage::

    python scripts/e173_regime_report.py --v1 <out>/run_v1 --v2 <out>/run_v2 \\
        --gate <out>/run_v2_gate --data-dir <data> --out <out>/compare

Reads each run's ``trades.csv`` / ``equity.csv`` (base cost, every ranker), the
split-adjusted closes in ``<data>/underlying_ohlc`` (the label history the runs used),
and writes CSVs plus ``summary.md`` with the three verdicts (regime model, vol gate as a
candidate, transitional guard at the reference setting run 3 / margin z 0.10).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from arc.backtest.entry_filter import OhlcStore, load_ohlc
from arc.backtest.experiments import load_runs
from arc.backtest.ranking import (
    challenge,
    labels_for,
    load_ranking_file,
    profile_allows_credit,
    summarize,
)
from arc.backtest.regime_compare import (
    guard_blocked_days,
    guard_split,
    guard_verdict,
    label_flips,
    reference_settings,
    regime_verdict,
)
from arc.scanner.rank import incumbent_for

PROFILES = ("margin", "cash_debit")


def _closes(data: Path, t: str, lo: dt.date, hi: dt.date) -> pd.Series:
    df = load_ohlc(OhlcStore(data), t, lo, hi, source=None)
    return pd.Series(df["close"].to_numpy(dtype=float), index=list(df.index), name=t)


def _md(df: pd.DataFrame) -> str:
    def cell(v: object) -> str:
        if isinstance(v, float):
            return f"{v:,.3f}" if abs(v) < 10 else f"{v:,.0f}"
        return str(v)

    head = "| " + " | ".join(df.columns) + " |\n|" + "---|" * len(df.columns) + "\n"
    return head + "\n".join(
        "| " + " | ".join(cell(v) for v in r) + " |" for r in df.itertuples(index=False)
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--v1", type=Path, required=True)
    p.add_argument("--v2", type=Path, required=True)
    p.add_argument("--gate", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--tickers", default="SPY,QQQ,IWM,AAPL,NVDA,TSLA")
    p.add_argument("--from", dest="start", type=dt.date.fromisoformat, default="2024-03-01")
    p.add_argument("--to", dest="end", type=dt.date.fromisoformat, default="2026-07-31")
    p.add_argument("--history-days", type=int, default=490)
    a = p.parse_args(argv)
    cfg = load_ranking_file()
    bt = cfg.backtest
    a.out.mkdir(parents=True, exist_ok=True)
    runs = {k: load_runs(d) for k, d in (("v1", a.v1), ("v2", a.v2), ("gate", a.gate))}
    inc = {pr: incumbent_for(allows_credit=profile_allows_credit(pr)).value for pr in PROFILES}
    out: list[str] = []

    # 1. incumbent v1 vs v2 (+ every ranker for context)
    v1i = {pr: runs["v1"][(pr, inc[pr])] for pr in PROFILES}
    v2i = {pr: runs["v2"][(pr, inc[pr])] for pr in PROFILES}
    table, verdict = regime_verdict(v1i, v2i, starting_equity=bt.starting_equity, boot=bt.bootstrap)
    table.to_csv(a.out / "verdict.csv", index=False)
    rows = []
    for key in sorted(runs["v1"].keys() & runs["v2"].keys()):
        s1 = summarize(runs["v1"][key], bt.starting_equity)
        s2 = summarize(runs["v2"][key], bt.starting_equity)
        ch = challenge(runs["v1"][key], runs["v2"][key], rule=bt.decision, boot=bt.bootstrap)
        rows.append(
            {
                "profile": key[0],
                "ranker": key[1],
                "trades_v1": s1["trades"],
                "trades_v2": s2["trades"],
                "net_pnl_v1": s1["net_pnl"],
                "net_pnl_v2": s2["net_pnl"],
                "max_dd_v1": s1["max_dd"],
                "max_dd_v2": s2["max_dd"],
                "pnl_diff": ch["pnl_diff"],
                "ci_lo": ch["ci_lo"],
                "ci_hi": ch["ci_hi"],
            }
        )
    pd.DataFrame(rows).to_csv(a.out / "all_rankers.csv", index=False)

    # 2. vol gate candidate: margin incumbent, v2 vs v2 + gate
    g_rows = []
    for key in sorted(runs["v2"].keys() & runs["gate"].keys()):
        if key[0] != "margin":
            continue
        s2 = summarize(runs["v2"][key], bt.starting_equity)
        sg = summarize(runs["gate"][key], bt.starting_equity)
        ch = challenge(runs["v2"][key], runs["gate"][key], rule=bt.decision, boot=bt.bootstrap)
        g_rows.append(
            {
                "ranker": key[1],
                "trades_v2": s2["trades"],
                "trades_gate": sg["trades"],
                "net_pnl_v2": s2["net_pnl"],
                "net_pnl_gate": sg["net_pnl"],
                "max_dd_v2": s2["max_dd"],
                "max_dd_gate": sg["max_dd"],
                "sharpe_v2": s2["sharpe"],
                "sharpe_gate": sg["sharpe"],
                **ch,
            }
        )
    pd.DataFrame(g_rows).to_csv(a.out / "vol_gate.csv", index=False)

    # 3. label flips per ticker
    tickers = a.tickers.split(",")
    lo = a.start - dt.timedelta(days=a.history_days)
    closes = {t: _closes(a.data_dir, t, lo, a.end) for t in tickers}
    labels = {t: {"v1": labels_for(c, "v1"), "v2": labels_for(c, "v2")} for t, c in closes.items()}
    flips = label_flips(labels, a.start, a.end)  # type: ignore[arg-type]
    flips.to_csv(a.out / "label_flips.csv", index=False)

    # 4. transitional guard at the reference setting (SPY v2 labels per decision day)
    blocked = guard_blocked_days(closes["SPY"], reference_settings())
    in_window = {d: b for d, b in blocked.items() if a.start <= d <= a.end}
    split_v2 = guard_split(v2i, blocked, bt.bootstrap)
    split_v2.to_csv(a.out / "guard_split.csv", index=False)
    gv = guard_verdict(split_v2, PROFILES)

    out.append(f"regime verdict: {verdict}")
    out.append(f"guard verdict: {gv}")
    out.append(
        f"guard blocked days in window: {sum(in_window.values())} / {len(in_window)} "
        f"({sum(in_window.values()) / max(len(in_window), 1):.1%})"
    )
    for name, df in (
        ("verdict", table),
        ("all_rankers", pd.DataFrame(rows)),
        ("vol_gate", pd.DataFrame(g_rows)),
        ("label_flips", flips),
        ("guard_split", split_v2),
    ):
        out.append(f"\n## {name}\n")
        out.append(_md(df))
    (a.out / "summary.md").write_text("\n".join(out) + "\n")
    sys.stdout.write("\n".join(out[:3]) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
