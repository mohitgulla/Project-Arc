"""E7.21 (D125, D126): candidate outcome labels on shared leg-level marks.

A candidate's exit path does not depend on the fill model: the exit rules read the
position's P&L at **mid** (``ResolvedRules.entry_net`` is the structure's mid net
price), the spread rule reads quoted spreads, and the intraday range proxy re-prices
legs at mid. So one forward walk per candidate (:func:`walk`) fixes the exit day,
reason and exit marks, and each slippage ``x`` only re-prices the entry and exit
fills (:func:`price_labels`, through :mod:`arc.backtest.engine`'s ``open_trade`` /
``close_early`` / ``settle`` themselves). This reproduces
:func:`arc.backtest.sim.sim_outcome` at that ``x`` (pinned in ``tests/test_ml_ledger.py``);
a hold with a split inside falls back to ``sim_outcome`` itself (:func:`labels_via_sim`).

Leg-level sharing (:class:`LegBook`): each contract's quoted mark per session is
looked up once per ticker, and the range proxy's implied vols and re-prices at the
session's high and low are solved once per (session, contract) for every quoted menu
contract, in one vectorised call per session, whatever the number of candidates
sharing a leg. ``implied_vol_vec`` is a fixed-iteration element-wise bisection, so a
leg's value is bitwise the one :func:`arc.backtest.sim.proxy_marks` computes per
candidate.

Everything here reads sessions after the entry day only (labels); nothing is a
feature. Pure: no I/O, no clock.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic resolves the field annotations at runtime
import math
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.chain import implied_vol_vec
from arc.backtest.engine import MULT, STRUCTURE_KIND, close_early, open_trade, settle
from arc.backtest.ranking import _legs
from arc.backtest.sim import PROXY_EXITS, pick_proxy_rule, proxy_rules_on, sim_outcome, spread_ok
from arc.backtest.strategies import StrategyKind, StrategySpec
from arc.exits.policy import ExitReason, check_rules, resolve_rules
from arc.pricing.bs import price_vectorized
from arc.structures import analyze

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pandas as pd

    from arc.backtest.costs import CostModel
    from arc.backtest.ranking import Candidate
    from arc.config import ArcSettings
    from arc.exits.policy import ExitConfig, ResolvedRules

Mark = tuple[float, float]
NAN_PAIR = (math.nan, math.nan)


class LegBook:
    """Per-ticker exit book shared by every candidate: ``day -> symbol -> (mid, spread)``.

    *book* is the sim's exit book (``quote_marks``, or the entry chains when the sim
    marks on them), restricted to *legs* (``symbol -> (right, strike, expiration)``):
    the contracts the ticker's menus use. *ranges* / *rate* feed the range proxy.
    """

    def __init__(
        self,
        book: Mapping[dt.date, pd.DataFrame],
        legs: Mapping[str, tuple[str, float, dt.date]],
        closes: pd.Series,
        *,
        ranges: Mapping[dt.date, tuple[float, float]] | None = None,
        rate: Callable[[dt.date], float] | None = None,
    ) -> None:
        self.days: list[dt.date] = sorted(book)
        self.legs = dict(legs)
        want = set(self.legs)
        self.marks: dict[dt.date, dict[str, Mark]] = {}
        for d in self.days:
            df = book[d]
            if df.empty:
                self.marks[d] = {}
                continue
            sub = df[df["symbol"].isin(want)]
            self.marks[d] = {
                str(s): (float(m), float(sp))
                for s, m, sp in zip(sub["symbol"], sub["mid"], sub["spread"], strict=True)
            }
        self.closes = closes
        self.ranges = ranges
        self.rate = rate
        self._proxy: dict[dt.date, dict[str, tuple[float, float]]] = {}
        self.iv_solves = 0

    def proxy(self, day: dt.date) -> dict[str, tuple[float, float]]:
        """``symbol -> (price at the session high, price at the low)`` at EOD IV.

        NaN where the IV cannot be solved or the contract expires on/before *day*
        (``proxy_marks`` then gives up on the whole candidate). Solved once per session
        for every quoted menu contract.
        """
        got = self._proxy.get(day)
        if got is not None:
            return got
        out: dict[str, tuple[float, float]] = {}
        rng = None if self.ranges is None else self.ranges.get(day)
        dm = self.marks.get(day, {})
        if rng is None or not dm or day not in self.closes.index or self.rate is None:
            self._proxy[day] = out
            return out
        spot = float(self.closes[day])
        syms = sorted(dm)
        t = np.array([max((self.legs[s][2] - day).days, 0) / 365.0 for s in syms], dtype=float)
        k = np.array([self.legs[s][1] for s in syms], dtype=float)
        flag = np.array(["c" if self.legs[s][0] == "call" else "p" for s in syms])
        mids = np.array([dm[s][0] for s in syms], dtype=float)
        r = float(self.rate(day))
        iv = implied_vol_vec(mids, spot, k, t, r, 0.0, flag)
        self.iv_solves += len(syms)
        with np.errstate(invalid="ignore", divide="ignore"):
            hi = price_vectorized(flag, np.full(len(syms), rng[0]), k, t, r, iv)
            lo = price_vectorized(flag, np.full(len(syms), rng[1]), k, t, r, iv)
        bad = ~np.isfinite(iv) | (t <= 0)
        for i, s in enumerate(syms):
            out[s] = NAN_PAIR if bad[i] else (float(hi[i]), float(lo[i]))
        self._proxy[day] = out
        return out


class Walk(BaseModel):
    """The fill-independent exit path of one candidate."""

    model_config = ConfigDict(frozen=True)

    exit_day: dt.date | None = Field(None, description="None = held to expiry")
    rule: str | None = Field(None, description="ExitReason value of the exit")
    proxy: bool = Field(False, description="Closed by the intraday range proxy")
    exit_marks: dict[str, Mark] = Field(default_factory=dict)
    exit_spot: float = Field(math.nan, description="Spot at exit (the extreme on a proxy exit)")
    delays: int = Field(0, description="Sessions a fired exit waited for a tradeable quote")
    #: (session, position value at mid per share) of each marked session
    path: list[tuple[dt.date, float]] = Field(default_factory=list)


def candidate_rules(c: Candidate, settings: ArcSettings, exits: ExitConfig) -> ResolvedRules:
    """The exit policy resolved at entry (E7.19: the entry DTE's sleeve block)."""
    structure = analyze(_legs(c.picks, c.underlying), as_of=c.day)
    dte = (c.picks[0].expiration - c.day).days
    block = settings.exit_block_for_dte(dte)
    return resolve_rules(structure, exits.policy_for(STRUCTURE_KIND[StrategyKind(c.kind)], block))


def walk(
    c: Candidate,
    rules: ResolvedRules,
    lb: LegBook,
    *,
    settings: ArcSettings,
    spread_rule: bool,
) -> Walk:
    """:func:`arc.backtest.sim.sim_outcome`'s session loop for a hold without a split."""
    legs = c.picks
    syms = [p.symbol for p in legs]
    expiration = legs[0].expiration
    proxy_on = lb.ranges is not None and proxy_rules_on(rules)
    peak: float | None = None
    pending: ExitReason | None = None
    delays = 0
    path: list[tuple[dt.date, float]] = []
    closes = lb.closes
    for day in lb.days:
        if day <= c.day:
            continue
        if day >= expiration:
            break
        dm = lb.marks[day]
        if not all(s in dm for s in syms):
            if pending is not None:
                delays += 1
            continue
        value_mid = sum(p.side * dm[p.symbol][0] for p in legs)
        spot = float(closes[day]) if day in closes.index else math.nan
        rng = lb.ranges.get(day) if proxy_on and lb.ranges is not None else None
        if pending is None and rng is not None and math.isfinite(spot):
            pxs = lb.proxy(day)
            vals = [pxs.get(s, NAN_PAIR) for s in syms]
            if all(math.isfinite(v[0]) and math.isfinite(v[1]) for v in vals):
                priced = []
                for j, x in enumerate(rng):
                    m = {s: (vals[i][j], dm[s][1]) for i, s in enumerate(syms)}
                    pnl = sum(p.side * m[p.symbol][0] for p in legs) - rules.entry_net
                    priced.append((pnl, m, x))
                hit = pick_proxy_rule(rules, priced, peak=peak)
                if hit is not None and (
                    not spread_rule or all(spread_ok(*dm[s], settings) for s in syms)
                ):
                    rule, pm, xx = hit
                    return Walk(
                        exit_day=day,
                        rule=rule,
                        proxy=True,
                        exit_marks=pm,
                        exit_spot=float(xx),
                        delays=delays,
                        path=path,
                    )
        path.append((day, value_mid))
        if pending is None:
            pnl = value_mid - rules.entry_net
            pending = check_rules(rules, pnl=pnl, dte=(expiration - day).days, peak_pnl=peak)
            peak = pnl if peak is None else max(peak, pnl)
        if pending is not None:
            if spread_rule and not all(spread_ok(*dm[s], settings) for s in syms):
                delays += 1
                continue
            return Walk(
                exit_day=day,
                rule=pending.value,
                exit_marks={s: dm[s] for s in syms},
                exit_spot=spot,
                delays=delays,
                path=path,
            )
    return Walk(delays=delays, path=path)


class Labels(BaseModel):
    """One candidate's outcome at one slippage x ($ per unit)."""

    model_config = ConfigDict(frozen=True)

    pnl: float
    pnl_mid: float
    max_loss: float
    hold_pnl: float
    hold_max_loss: float
    exit_reason: str
    exit_day: dt.date
    entry_net: float
    entry_net_mid: float
    open_fees: float
    mae: float = Field(..., description="Worst EOD mark P&L in the hold, incl. the exit")
    mfe: float = Field(..., description="Best EOD mark P&L in the hold, incl. the exit")


def settle_spot(c: Candidate, closes: pd.Series) -> float | None:
    """The last close on or before expiry; ``None`` if the hold can't settle in data."""
    exp = c.picks[0].expiration
    if len(closes) == 0 or exp > max(closes.index):
        return None
    past = closes[closes.index <= exp]
    return None if past.empty else float(past.iloc[-1])


def price_labels(
    c: Candidate, w: Walk, *, spot_entry: float, spot_settle: float, cost: CostModel
) -> Labels | None:
    """The walk's trade at *cost*'s slippage: ``sim_outcome``'s trade, via the engine.

    ``None`` where ``open_trade`` refuses (post-cost max loss not positive).
    """
    o = open_trade(
        c.picks,
        spec=StrategySpec(kind=StrategyKind(c.kind)),
        underlying=c.underlying,
        entry_date=c.day,
        spot=spot_entry,
        cost=cost,
    )
    if o is None:
        return None
    held = settle(o, spot_settle, cost)
    if w.exit_day is None:
        trade = held
    else:
        trade = close_early(
            o,
            day=w.exit_day,
            marks=w.exit_marks,
            spot=w.exit_spot,
            reason=ExitReason(str(w.rule)),
            cost=cost,
        )
        if w.proxy:
            trade = trade.model_copy(update={"exit_reason": PROXY_EXITS[str(w.rule)]})
    exit_day = trade.exit_date or trade.expiration
    entry_fill = sum(lg.side * lg.fill for lg in o.legs)
    path = [(v - entry_fill) * MULT - o.open_fees for d, v in w.path if d < exit_day]
    seen = [*path, trade.pnl]
    return Labels(
        pnl=trade.pnl,
        pnl_mid=trade.pnl_mid,
        max_loss=trade.max_loss,
        hold_pnl=held.pnl,
        hold_max_loss=held.max_loss,
        exit_reason=str(trade.exit_reason),
        exit_day=exit_day,
        entry_net=trade.entry_net,
        entry_net_mid=trade.entry_net_mid,
        open_fees=o.open_fees,
        mae=min(seen),
        mfe=max(seen),
    )


def labels_via_sim(
    c: Candidate,
    *,
    cost: CostModel,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    exits: ExitConfig,
    settings: ArcSettings,
    spread_rule: bool,
    marks: Mapping[dt.date, pd.DataFrame] | None,
    splits: Mapping[dt.date, float] | None,
    ranges: Mapping[dt.date, tuple[float, float]] | None,
    rate: Callable[[dt.date], float],
) -> Labels | None:
    """A hold with a split inside: ``sim_outcome`` itself at *cost*."""
    res = sim_outcome(
        c,
        chains=chains,
        closes=closes,
        cost=cost,
        exits=exits,
        settings=settings,
        spread_rule=spread_rule,
        marks=marks,
        splits=splits,
        ranges=ranges,
        rate=rate,
    )
    if res is None:
        return None
    out = res.outcome
    tr = out.trade
    seen = [*out.marks.values(), tr.pnl]
    return Labels(
        pnl=tr.pnl,
        pnl_mid=tr.pnl_mid,
        max_loss=tr.max_loss,
        hold_pnl=out.hold_to_expiry_pnl,
        hold_max_loss=tr.max_loss,
        exit_reason=str(tr.exit_reason),
        exit_day=out.exit_day,
        entry_net=tr.entry_net,
        entry_net_mid=tr.entry_net_mid,
        open_fees=out.open_fees,
        mae=min(seen),
        mfe=max(seen),
    )


def splits_inside(c: Candidate, splits: Mapping[dt.date, float] | None) -> bool:
    exp = c.picks[0].expiration
    return any(c.day < d <= exp for d in (splits or {}))
