"""E7.17 sanity check: re-price sampled backtest trades by hand from the raw NBBO rows.

For each profile, the 15 worst trades plus 15 random ones (seed 17) of a run's
``trades.csv`` are re-priced independently of the simulator's chain/mark pipeline:

* entry: each leg's mid and spread from that session's raw bid/ask, filled at
  mid ± x·spread (x = 0.25), net = Σ side·fill, against ``entry_net``;
* path: every later session where all legs have a two-sided quote (any width), the
  position's P&L at mid, checked against the resolved exit rules
  (``arc.exits.policy``), giving the session the exit should fire and its reason;
* exit: an early exit re-priced from the exit session's quotes, expiry settled at
  intrinsic on the underlying's raw close (split-adjusted onto the original strikes
  when a split falls inside the hold);
* fees: 0.0403 $/contract/leg each side (ORF + OCC + CAT), sell fees ignored like
  the simulator; sizing: contracts x max_loss_unit as % of the entry session's
  marked equity (cap: max_alloc_pct, 15 % on both profiles).

Output: one row per sampled trade (simulated vs re-priced), as CSV and markdown.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from arc.backtest.costs import load_cost_model
from arc.backtest.engine import MULT, STRUCTURE_KIND
from arc.backtest.entry_filter import OhlcStore
from arc.backtest.ranking import _legs
from arc.backtest.sim import split_ratios, spread_ok
from arc.backtest.strategies import StrategyKind
from arc.backtest.underlying import UnderlyingStore
from arc.config import ArcSettings
from arc.data.history.store import ParquetHistoryStore
from arc.exits.policy import check_rules, load_exit_config, resolve_rules
from arc.structures import analyze
from arc.structures.occ import parse_occ

if TYPE_CHECKING:
    from pathlib import Path

X = 0.25
SEED = 17


def _legs_of(s: str) -> list[tuple[int, str]]:
    return [(1 if tok[0] == "+" else -1, tok[1:]) for tok in s.split()]


class _Picks:
    """Minimal LegPick stand-in for :func:`arc.backtest.ranking._legs`."""

    def __init__(self, side: int, sym: str, mid: float) -> None:
        o = parse_occ(sym)
        self.side = side
        self.symbol = sym
        self.expiration = o.expiration
        self.right = "call" if str(o.kind).lower().startswith("c") else "put"
        self.strike = float(o.strike)
        self.mid = mid


def reprice(
    trades: pd.DataFrame,
    data_dir: Path,
    equity: pd.Series,
    realised_book: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Re-price *trades*; sizing is checked against *equity* (marked, the live-shaped
    walk) or, with *realised_book* (the legacy E7.14 walk), 25,000 + realised P&L of
    the book's trades closed by the entry session."""
    store = ParquetHistoryStore(data_dir)
    und = UnderlyingStore(data_dir)
    ohlc = OhlcStore(data_dir)
    exits = load_exit_config()
    cost = load_cost_model()
    per_leg_fee = cost.fees(1)
    settings = ArcSettings()
    rows = []
    for tk, g in trades.groupby("underlying"):
        lo = g["entry_date"].min()
        hi = g["expiration"].max()
        raw = store.read("thetadata", tk, lo, hi)
        raw = raw[["date", "symbol", "bid", "ask"]].copy()
        raw = raw[(raw["ask"] > 0) & (raw["bid"] >= 0) & (raw["bid"] <= raw["ask"])]
        q = {(d, s): (b, a) for d, s, b, a in raw.itertuples(index=False)}
        days = sorted(set(raw["date"]))
        closes = und.read(str(tk))
        adj = ohlc.read(tk)
        splits = split_ratios(closes, pd.Series(adj["close"])) if len(adj) else {}
        for t in g.itertuples():
            legs = _legs_of(t.legs)
            ed, xd, exp = t.entry_date, t.exit_date, t.expiration
            ent = [q.get((ed, s)) for _, s in legs]
            r: dict[str, object] = {
                "profile": t.profile,
                "underlying": tk,
                "kind": t.kind,
                "entry_date": ed,
                "exit_date": xd,
                "expiration": exp,
                "contracts": t.contracts,
                "sim_entry_net": round(t.entry_net, 4),
                "sim_exit_reason": t.exit_reason,
                "sim_pnl_unit": round(t.pnl_unit, 2),
                "sample": getattr(t, "sample", ""),
                "sim_exit_delayed": t.exit_delayed,
            }
            if any(e is None for e in ent):
                r["note"] = "entry leg unquoted"
                rows.append(r)
                continue
            mids = [(b + a) / 2 for b, a in ent]
            sprs = [a - b for b, a in ent]
            fills = [
                max(m + sd * X * s, 0.0) for (sd, _), m, s in zip(legs, mids, sprs, strict=True)
            ]
            entry_net = sum(sd * f for (sd, _), f in zip(legs, fills, strict=True))
            entry_mid = sum(sd * m for (sd, _), m in zip(legs, mids, strict=True))
            r["rp_entry_net"] = round(entry_net, 4)
            r["d_entry"] = round(entry_net - t.entry_net, 4)
            picks = [_Picks(sd, s, m) for (sd, s), m in zip(legs, mids, strict=True)]
            structure = analyze(_legs(picks, tk), as_of=ed)
            kind = STRUCTURE_KIND[StrategyKind(t.kind)]
            rules = resolve_rules(structure, exits.policy_for(kind))
            # path: first session after entry where every leg is two-sided and a rule fires
            inside = sorted((d, f) for d, f in splits.items() if ed < d <= exp)
            fire_day = fire_reason = signal_day = None
            peak = None
            n_q = n_all = 0
            st = settings.with_profile(t.profile)
            for d in days:
                if d <= ed:
                    continue
                if d >= exp:
                    break
                n_all += 1
                f = math.prod(v for dd, v in inside if dd <= d)
                syms = [s if f == 1 else _restrike(s, f) for _, s in legs]
                qs = [q.get((d, s)) for s in syms]
                if any(x is None for x in qs):
                    continue
                n_q += 1
                if signal_day is None:
                    val = sum(
                        sd * (b + a) / 2 * f for (sd, _), (b, a) in zip(legs, qs, strict=True)
                    )
                    pnl = val - rules.entry_net
                    why = check_rules(rules, pnl=pnl, dte=(exp - d).days, peak_pnl=peak)
                    peak = pnl if peak is None else max(peak, pnl)
                    if why is not None:
                        signal_day, fire_reason = d, why.value
                # live closes obey the per-leg spread rule too: wait for a session
                # where every leg passes it
                if signal_day is not None and all(spread_ok((b + a) / 2, a - b, st) for b, a in qs):
                    fire_day = d
                    break
            r["quoted_sessions"] = f"{n_q}/{n_all}"
            r["rp_signal_day"] = signal_day
            r["rp_exit_day"] = fire_day or exp
            r["rp_exit_reason"] = fire_reason if fire_day is not None else "expiry"
            total = math.prod(v for _, v in inside)
            if fire_day is not None:
                f = math.prod(v for dd, v in inside if dd <= fire_day)
                syms = [s if f == 1 else _restrike(s, f) for _, s in legs]
                qs = [q[(fire_day, s)] for s in syms]
                value = sum(
                    sd * max((b + a) / 2 * f - sd * X * (a - b) * f, 0.0)
                    for (sd, _), (b, a) in zip(legs, qs, strict=True)
                )
                fees = per_leg_fee * len(legs) * 2
            else:
                past = closes[pd.Index(closes.index) <= exp]
                spot = float(past.iloc[-1]) * total
                vals = []
                for sd, s in legs:
                    p = _Picks(sd, s, 0.0)
                    iv = (
                        max(spot - p.strike, 0.0)
                        if p.right == "call"
                        else max(p.strike - spot, 0.0)
                    )
                    vals.append((sd, iv))
                value = sum(sd * v for sd, v in vals)
                fees = per_leg_fee * (len(legs) + sum(1 for _, v in vals if v > 0))
                r["rp_settle_spot"] = round(spot, 2)
            pnl_unit = (value - entry_net) * MULT - fees
            r["rp_pnl_unit"] = round(pnl_unit, 2)
            r["d_pnl_unit"] = round(pnl_unit - t.pnl_unit, 2)
            r["rp_fees_unit"] = round(fees, 4)
            r["sim_fees_unit"] = round(t.fees / t.contracts, 4)
            if realised_book is None:  # marked equity of the entry session
                eq = float(equity[equity.index <= ed].iloc[-1])
            else:
                done = [x <= ed for x in realised_book["exit_day"]]
                eq = 25_000.0 + float(realised_book.loc[done, "pnl"].sum())
            r["risk_pct_equity"] = round(t.contracts * t.max_loss_unit / eq * 100, 2)
            r["splits_in_hold"] = ",".join(f"{d}:{v:g}" for d, v in inside)
            same = (r["rp_exit_reason"] == t.exit_reason) and (
                r["rp_exit_day"] == (xd if isinstance(xd, dt.date) else exp)
            )
            r["exit_match"] = bool(same)
            r["entry_mid_check"] = round(entry_mid - t.entry_net_mid, 4)
            rows.append(r)
    return pd.DataFrame(rows)


def _restrike(sym: str, f: float) -> str:
    from arc.structures import format_occ

    o = parse_occ(sym)
    return format_occ(o.root, o.expiration, o.kind, Decimal(str(round(float(o.strike) / f, 3))))


def sample(trades: pd.DataFrame, n_worst: int = 15, n_rand: int = 15) -> pd.DataFrame:
    worst = trades.nsmallest(n_worst, "pnl")
    rest = trades.drop(worst.index)
    rnd = rest.sample(n=min(n_rand, len(rest)), random_state=SEED)
    return pd.concat([worst.assign(sample="worst"), rnd.assign(sample="random")])


def load_trades(path: Path) -> pd.DataFrame:
    t = pd.read_csv(path)
    for c in ("entry_date", "exit_date", "expiration"):
        t[c] = pd.to_datetime(t[c]).dt.date
    t["exit_date"] = [None if (isinstance(x, float) or x is pd.NaT) else x for x in t["exit_date"]]
    return t


def load_equity(path: Path) -> pd.Series:
    e = pd.read_csv(path)
    return pd.Series(e["equity"].to_numpy(), index=pd.to_datetime(e["day"]).dt.date)


__all__ = ["load_equity", "load_trades", "reprice", "sample", "np"]
