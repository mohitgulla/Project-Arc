"""Ranking backtest (PLAN D25; card E7.5): which candidate should the scanner pick?

The only variable between runs is the **ranker** (:mod:`arc.scanner.rank`). For
each ticker and each daily decision session the same *menu* of candidate
structures is built once; every ranker then picks its top-1 from that menu
(after the same hard filters), and the picked trades go through the same
sizing (D18), cost model (``config/costs.yaml``), exit policy
(``config/exits.yaml``, E2.4 relaxed stops) and simulated portfolio caps.

No look-ahead
-------------
:func:`build_menu` sees only the session's EOD chain and the underlying's closes
up to and including that session (it truncates the series it is given). ATM IV
comes from that chain; the realised-vol forecast is mean(HV20, HV60) of those
closes, the same forecast the live pipeline passes to the exit model. Future
sessions are only read by :func:`simulate_outcome`, which plays a trade that has
already been chosen forward to its exit.

Menu
----
Per account profile (``config/ranking.yaml`` → ``backtest.menus``): each strategy
kind × anchor |Δ| in the profile's list, on the one expiration nearest the middle
of the profile's DTE window (:class:`arc.backtest.strategies.StrategySpec`), every
leg with session volume ≥ ``min_volume``. Each candidate gets the scanner-style
``ev_proxy`` (flat ATM-IV BSM value after entry costs) and the E2.4 managed-exit
Monte Carlo (Net EV, PoP, expected days held, ``rorc_day``, ``vrp``).

Portfolio
---------
Sessions are processed in date order; on each session positions whose exit date
has come are closed first, then tickers are visited in a fixed order. Contracts =
D18 ``min(suggestion, floor(max_alloc_pct × equity / max loss))`` with no Risk
persona, so the suggestion is the cap. The pure gate rules
:func:`arc.gate.rules.check_per_underlying` and
:func:`arc.gate.rules.check_max_open_positions` are applied, and a cash-settled
profile must fit the debit (+ fees) in cash not already tied up in open debits.
Equity is marked to market daily at mid.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import norm

from arc.account_profiles import BuyingPower, load_account_profiles
from arc.backtest.engine import (
    MULT,
    STRUCTURE_KIND,
    Trade,
    close_early,
    open_trade,
    settle,
)
from arc.backtest.regime import label_trend, label_vol
from arc.backtest.strategies import (
    LegPick,
    StrategyKind,
    StrategySpec,
    pick_expirations,
    select_legs,
)
from arc.exits.model import model_exits, realized_vol_forecast
from arc.exits.policy import ExitReason, check_rules, resolve_rules
from arc.gate.inputs import AccountSnapshot, Portfolio, Position
from arc.gate.rules import Derived, check_max_open_positions, check_per_underlying
from arc.models import Leg, LegIntent
from arc.pricing.bs import OptionKind, price_vectorized
from arc.scanner.rank import (
    Ranker,
    RankFilters,
    RankingConfig,
    RankInputs,
    applicable,
    incumbent_for,
    rank,
)
from arc.sizing import size_contracts
from arc.structures import analyze, format_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.backtest.costs import CostModel
    from arc.config import ArcSettings
    from arc.exits.policy import ExitConfig, ExitModelConfig

log = structlog.get_logger()

__all__ = [
    "BacktestSettings",
    "Candidate",
    "DecisionRule",
    "MenuSpec",
    "Outcome",
    "RankRun",
    "RankingFile",
    "atm_iv",
    "block_bootstrap_ci",
    "build_menu",
    "build_menus",
    "decide",
    "load_ranking_file",
    "run_portfolio",
    "simulate_outcome",
    "summarize",
]

DEFAULT_RANKING_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "ranking.yaml"
_FORBID = ConfigDict(extra="forbid", frozen=True)
_HV_SHORT = 20
_HV_LONG = 60
_TRADING_DAYS = 252.0


# ---------------------------------------------------------------------------
# Config (config/ranking.yaml)
# ---------------------------------------------------------------------------


class MenuSpec(BaseModel):
    """Which structures one account profile's menu offers each session."""

    model_config = _FORBID

    kinds: list[StrategyKind]
    deltas: list[float] = Field(..., min_length=1, description="Anchor |Δ| values tried")
    width_pct: float = Field(0.02, gt=0.0, description="Vertical width as a fraction of spot")
    delta_tol: float = Field(0.04, ge=0.0)
    min_volume: float = Field(1.0, ge=0.0)

    def specs(self, dte_min: int, dte_max: int) -> list[StrategySpec]:
        return [
            StrategySpec(
                kind=k,
                dte_min=dte_min,
                dte_max=dte_max,
                delta=d,
                delta_tol=self.delta_tol,
                width_pct=self.width_pct,
                min_volume=self.min_volume,
            )
            for k in self.kinds
            for d in self.deltas
        ]


class BootstrapSpec(BaseModel):
    model_config = _FORBID

    resamples: int = Field(2000, ge=100)
    block_days: int = Field(20, ge=1, description="Moving-block length in sessions")
    ci: float = Field(0.90, gt=0.0, lt=1.0)
    seed: int = 7


class DecisionRule(BaseModel):
    """D25 switch rule, fixed before the run."""

    model_config = _FORBID

    min_subperiod_wins: int = Field(2, ge=1, description="Sub-periods won on P&L and max DD")
    subperiods: list[str] = Field(
        default_factory=lambda: ["bear", "sideways", "bull"],
        description="Trend-regime labels at entry (E4.3 rule)",
    )

    def text(self, ci: float) -> str:
        return (
            f"Switch the default to a challenger only if it beats the incumbent on net P&L "
            f"(higher) **and** max drawdown (not larger) in ≥ {self.min_subperiod_wins} of "
            f"{len(self.subperiods)} sub-periods ({', '.join(self.subperiods)} trend regime at "
            f"entry), **and** the {ci:.0%} block-bootstrap CI of its daily P&L difference vs the "
            f"incumbent excludes 0 (lower bound > 0). Otherwise keep the incumbent and report "
            f"the closest challenger."
        )


class BacktestSettings(BaseModel):
    model_config = _FORBID

    starting_equity: float = Field(100_000.0, gt=0.0)
    risk_free_rate: float = 0.045
    n_paths: int = Field(5000, ge=100, description="Managed-exit MC paths per candidate")
    max_leg_iv_dev: float | None = Field(
        0.15,
        gt=0.0,
        description="Drop a candidate when a leg's IV is off the median IV of its neighbouring "
        "strikes by more than this fraction (stale trade-close filter); None = off",
    )
    smile_window: int = Field(5, ge=3, description="Strikes in the centred neighbour window")
    marks: Literal["smile", "close"] = Field(
        "smile",
        description="smile = price every leg off a same-session fitted IV smile (default); "
        "close = raw last-trade closes",
    )
    slippage_grid: list[float] = Field(default_factory=lambda: [0.0, 0.25, 0.5])
    tickers: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    bootstrap: BootstrapSpec = Field(default_factory=lambda: BootstrapSpec())
    decision: DecisionRule = Field(default_factory=lambda: DecisionRule())
    menus: dict[str, list[MenuSpec]]

    def specs_for(self, profile: str, dte_min: int, dte_max: int) -> list[StrategySpec]:
        if profile not in self.menus:
            msg = f"no backtest menu for profile {profile!r} in config/ranking.yaml"
            raise KeyError(msg)
        return [s for m in self.menus[profile] for s in m.specs(dte_min, dte_max)]


class RankingFile(BaseModel):
    model_config = _FORBID

    ranking: RankingConfig = Field(default_factory=lambda: RankingConfig())
    backtest: BacktestSettings


def load_ranking_file(path: Path | str | None = None) -> RankingFile:
    p = Path(path) if path is not None else DEFAULT_RANKING_PATH
    return RankingFile.model_validate(yaml.safe_load(p.read_text()) or {})


# ---------------------------------------------------------------------------
# Menu (decision-time only)
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """One menu entry: the legs, the ranker inputs, and what the models said."""

    model_config = _FORBID

    underlying: str
    day: dt.date
    spec: str
    kind: str
    picks: list[LegPick]
    inputs: RankInputs
    static_pop: float
    static_net_ev: float
    expected_days_held: float
    max_loss_mid: float = Field(..., description="$ per unit at mid")
    atm_iv: float
    rv_forecast: float | None


def atm_iv(chain: pd.DataFrame, expiration: dt.date, spot: float) -> float | None:
    """Mean call/put IV at the strike nearest *spot* on *expiration* (None if unsolved)."""
    c = chain[(chain["expiration"] == expiration) & np.isfinite(chain["iv"])]
    if c.empty:
        return None
    k = c.loc[(c["strike"] - spot).abs().idxmin(), "strike"]
    ivs = c[c["strike"] == k]["iv"].astype(float)
    v = float(ivs.mean())
    return v if v > 0 and math.isfinite(v) else None


def _hv(closes: pd.Series, window: int) -> float | None:
    s = closes.astype(float)
    if len(s) < window + 1 or (s <= 0).any():
        return None
    r = np.log(s / s.shift(1)).dropna().iloc[-window:]
    return float(r.std(ddof=1) * math.sqrt(_TRADING_DAYS))


def _legs(picks: Sequence[LegPick], underlying: str, price: str = "mid") -> list[Leg]:
    return [
        Leg(
            occ_symbol=format_occ(
                underlying,
                p.expiration,
                OptionKind.CALL if p.right == "call" else OptionKind.PUT,
                Decimal(str(p.strike)),
            ),
            side=LegIntent.LONG if p.side > 0 else LegIntent.SHORT,
            premium=Decimal(str(round(getattr(p, price), 6))),
        )
        for p in picks
    ]


def _width(picks: Sequence[LegPick]) -> float | None:
    by: dict[str, list[float]] = {}
    for p in picks:
        by.setdefault(p.right, []).append(p.strike)
    w = max((max(v) - min(v) for v in by.values()), default=0.0)
    return w or None


def _ev_proxy(
    picks: Sequence[LegPick], spot: float, sigma: float, r: float, dte: int, cost: CostModel
) -> float:
    """Scanner-style ``ev_proxy``: flat-vol BSM value at *sigma* − mid entry, − entry costs."""
    t = dte / 365.0
    value = 0.0
    entry_cost = 0.0
    for p in picks:
        flag = np.asarray("c" if p.right == "call" else "p")
        value += p.side * float(price_vectorized(flag, spot, p.strike, t, r, sigma))
        fill = cost.fill(p.mid, p.spread, p.side)
        entry_cost += abs(fill - p.mid) * MULT + cost.trade_fees(1, p.side, fill)
    entry_mid = sum(p.side * p.mid for p in picks)
    return (value - entry_mid) * MULT - entry_cost


def smile_deviation(chain: pd.DataFrame, window: int = 5) -> pd.Series:
    """Per row: ``iv / median(iv of the *window* nearest strikes, same expiry and right) − 1``.

    Alpaca history has trade closes only; a contract that last traded hours before
    the close has a stale price and an IV off its neighbours. NaN IV → NaN.
    """
    out = pd.Series(np.nan, index=chain.index, dtype=float)
    ok = chain[np.isfinite(chain["iv"])]
    for _, g in ok.groupby(["expiration", "right"], sort=False):
        g = g.sort_values("strike")
        med = g["iv"].rolling(window, center=True, min_periods=3).median()
        out.loc[g.index] = g["iv"] / med - 1.0
    return out


def smile_marks(
    ch: pd.DataFrame,
    spot: float,
    *,
    r: float,
    cost: CostModel,
    min_points: int = 5,
    iterations: int = 2,
) -> pd.DataFrame:
    """Re-mark one session's chain from a same-session fitted IV smile.

    Alpaca EOD closes are last trades printed at different times, so neighbouring
    strikes routinely violate monotonicity and put-call parity. Per expiration, a
    volume-weighted quadratic in log-moneyness ``log(K/F)`` is fitted to the OTM
    options' IVs (puts below the forward, calls above), with points more than
    3 MADs off the fit dropped and the fit repeated *iterations* times. Every row
    in the fitted strike range is then priced by BSM at the fitted IV: ``mid``,
    ``iv``, ``delta`` and the estimated ``spread`` are replaced. Rows outside the
    range (no extrapolation) and expiries with < *min_points* OTM points are dropped.
    Uses the session's own rows only, so it cannot look ahead.
    """
    keep: list[pd.DataFrame] = []
    base = ch[np.isfinite(ch["iv"]) & (ch["iv"] > 0)]
    for exp, g in ch.groupby("expiration", sort=True):
        t = float(g["dte"].iloc[0]) / 365.0
        fwd = spot * math.exp(r * t)
        gb = base[base["expiration"] == exp]
        otm = gb[
            ((gb["right"] == "put") & (gb["strike"] < fwd))
            | ((gb["right"] == "call") & (gb["strike"] >= fwd))
        ]
        if len(otm) < min_points:
            continue
        x = np.log(otm["strike"].to_numpy(dtype=float) / fwd)
        y = otm["iv"].to_numpy(dtype=float)
        w = np.sqrt(np.nan_to_num(otm["volume"].to_numpy(dtype=float)) + 1.0)
        mask = np.ones(len(x), dtype=bool)
        coef = np.polyfit(x, y, 2, w=w)
        for _ in range(iterations):
            res = y - np.polyval(coef, x)
            mad = float(np.median(np.abs(res - np.median(res)))) or 1e-9
            mask = np.abs(res) <= 3.0 * 1.4826 * mad
            if mask.sum() < min_points:
                break
            coef = np.polyfit(x[mask], y[mask], 2, w=w[mask])
        lo, hi = float(x[mask].min()), float(x[mask].max())
        gx = np.log(g["strike"].to_numpy(dtype=float) / fwd)
        inside = (gx >= lo) & (gx <= hi)
        if not inside.any():
            continue
        g = g[inside].copy()
        sig = np.clip(np.polyval(coef, gx[inside]), 0.03, 3.0)
        k = g["strike"].to_numpy(dtype=float)
        is_call = (g["right"].astype(str) == "call").to_numpy()
        flag = np.where(is_call, "c", "p")
        n = len(g)
        mid = price_vectorized(flag, np.full(n, spot), k, np.full(n, t), np.full(n, r), sig)
        g["mid"] = np.round(np.maximum(mid, 0.0), 4)
        g["iv"] = sig
        with np.errstate(invalid="ignore", divide="ignore"):
            d1 = (np.log(spot / k) + (r + 0.5 * sig**2) * t) / (sig * math.sqrt(t))
        g["delta"] = np.where(is_call, norm.cdf(d1), norm.cdf(d1) - 1.0)
        g["spread"] = [cost.spread(float(m)) for m in g["mid"]]
        keep.append(g[g["mid"] > 0])
    if not keep:
        return ch.iloc[0:0]
    return pd.concat(keep, ignore_index=True)


def remark_chains(
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    *,
    r: float,
    cost: CostModel,
) -> dict[dt.date, pd.DataFrame]:
    """:func:`smile_marks` for every session (session spot from *closes* at that date)."""
    out: dict[dt.date, pd.DataFrame] = {}
    for d, ch in chains.items():
        if d in closes.index and len(ch):
            out[d] = smile_marks(ch, float(closes[d]), r=r, cost=cost)
    return out


def clean_chains(
    chains: Mapping[dt.date, pd.DataFrame], *, max_dev: float | None, window: int = 5
) -> dict[dt.date, pd.DataFrame]:
    """Drop rows whose IV is more than *max_dev* off their strike neighbours' median.

    Each session is filtered on its own rows only (no look-ahead). The cleaned chains
    are used for the menus *and* the exit marks, so a stale close can neither make a
    structure look cheap at entry nor fire a spurious stop / take profit later.
    """
    if max_dev is None:
        return dict(chains)
    out: dict[dt.date, pd.DataFrame] = {}
    for d, ch in chains.items():
        dev = smile_deviation(ch, window)
        out[d] = ch[dev.abs() <= max_dev].reset_index(drop=True)
    return out


def build_menu(
    chain: pd.DataFrame,
    closes: pd.Series,
    *,
    day: dt.date,
    underlying: str,
    specs: Sequence[StrategySpec],
    cost: CostModel,
    exits: ExitConfig,
    mc: ExitModelConfig,
    r: float,
) -> list[Candidate]:
    """Every candidate for one session from information known at its close.

    *closes* is truncated to ``<= day`` here, so a caller cannot leak a future
    close into IV, the realised-vol forecast or the model.
    """
    past = closes[pd.Index(closes.index) <= day]
    if day not in past.index:
        return []
    spot = float(past[day])
    rv = realized_vol_forecast(_hv(past, _HV_SHORT), _hv(past, _HV_LONG))
    out: list[Candidate] = []
    seen: set[str] = set()
    for spec in specs:
        for exp in pick_expirations(chain, spec):
            picks = select_legs(chain, spec, spot, exp)
            if picks is None:
                continue
            key = f"{spec.kind}|" + ",".join(f"{p.side:+d}{p.symbol}" for p in picks)
            if key in seen:  # two deltas can land on the same strikes
                continue
            seen.add(key)
            iv = atm_iv(chain, exp, spot)
            if iv is None:
                continue
            try:
                structure = analyze(_legs(picks, underlying), as_of=day)
            except ValueError:
                continue
            if structure.max_loss is None or structure.max_loss <= 0:
                continue
            policy = exits.policy_for(STRUCTURE_KIND[spec.kind])
            res = model_exits(
                structure,
                policy,
                spot=spot,
                iv=iv,
                r=r,
                cost=cost,
                cfg=mc,
                spreads={_legs([p], underlying)[0].occ_symbol: p.spread for p in picks},
                realized_vol=rv,
            )
            entry_mid = sum(p.side * p.mid for p in picks)
            width = _width(picks)
            ml = float(structure.max_loss)
            evp = _ev_proxy(picks, spot, iv, r, structure.dte, cost)
            credit = entry_mid < 0
            out.append(
                Candidate(
                    underlying=underlying,
                    day=day,
                    spec=spec.label,
                    kind=str(spec.kind),
                    picks=list(picks),
                    inputs=RankInputs(
                        key=key,
                        credit=credit,
                        vertical=len(picks) == 2,
                        credit_width=(-entry_mid / width) if credit and width else None,
                        debit_width=(width / entry_mid)
                        if (not credit) and width and len(picks) == 2 and entry_mid > 0
                        else None,
                        ev_proxy=round(evp, 4),
                        ev_ratio=round(evp / ml, 6),
                        managed_net_ev=res.managed.net_ev,
                        managed_pop=res.managed.pop,
                        rorc_day=res.rorc_day,
                        vrp=res.vrp,
                    ),
                    static_pop=res.static.pop,
                    static_net_ev=res.static.net_ev,
                    expected_days_held=res.managed.expected_days_held,
                    max_loss_mid=ml,
                    atm_iv=iv,
                    rv_forecast=rv,
                )
            )
    return out


def build_menus(
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    *,
    days: Sequence[dt.date],
    underlying: str,
    specs: Sequence[StrategySpec],
    cost: CostModel,
    exits: ExitConfig,
    mc: ExitModelConfig,
    r: float,
) -> dict[dt.date, list[Candidate]]:
    closes = closes.sort_index()
    return {
        d: build_menu(
            chains[d],
            closes,
            day=d,
            underlying=underlying,
            specs=specs,
            cost=cost,
            exits=exits,
            mc=mc,
            r=r,
        )
        for d in days
        if d in chains
    }


# ---------------------------------------------------------------------------
# Outcome of one chosen candidate (reads the future: only after the pick)
# ---------------------------------------------------------------------------


class Outcome(BaseModel):
    """What one unit of a picked candidate did. Money in $ per unit."""

    model_config = ConfigDict(frozen=True)

    trade: Trade
    exit_day: dt.date
    marks: dict[dt.date, float] = Field(
        default_factory=dict, description="Unrealised net P&L at each session's mid, while open"
    )
    hold_to_expiry_pnl: float
    entry_debit: float = Field(..., description="Per-share entry fill, + debit / − credit")
    open_fees: float


def _spot_at_or_before(closes: pd.Series, day: dt.date) -> float | None:
    s = closes[pd.Index(closes.index) <= day]
    return None if s.empty else float(s.iloc[-1])


def simulate_outcome(
    c: Candidate,
    *,
    chains: Mapping[dt.date, pd.DataFrame],
    days: Sequence[dt.date],
    closes: pd.Series,
    cost: CostModel,
    exits: ExitConfig,
) -> Outcome | None:
    """Play *c* forward under the exit policy; ``None`` if it cannot be filled or settled."""
    spec_kind = StrategyKind(c.kind)
    o = open_trade(
        c.picks,
        spec=StrategySpec(kind=spec_kind),
        underlying=c.underlying,
        entry_date=c.day,
        spot=float(closes[c.day]),
        cost=cost,
    )
    if o is None:
        return None
    last_close = max(closes.index)
    if o.expiration > last_close:
        return None
    structure = analyze(_legs(c.picks, c.underlying), as_of=c.day)
    rules = resolve_rules(structure, exits.policy_for(STRUCTURE_KIND[spec_kind]))
    entry_fill = sum(lg.side * lg.fill for lg in o.legs)
    symbols = [lg.symbol for lg in o.legs]
    marks: dict[dt.date, float] = {}
    trade: Trade | None = None
    for day in days:
        if day <= c.day:
            continue
        if day >= o.expiration:
            break
        ch = chains.get(day)
        if ch is None:
            continue
        rows = ch[ch["symbol"].isin(symbols)]
        m = {str(x.symbol): (float(x.mid), float(x.spread)) for x in rows.itertuples()}
        if len(m) != len(set(symbols)):
            continue
        value_mid = sum(lg.side * m[lg.symbol][0] for lg in o.legs)
        marks[day] = (value_mid - entry_fill) * MULT - o.open_fees
        reason = check_rules(rules, pnl=value_mid - rules.entry_net, dte=(o.expiration - day).days)
        if reason is not None:
            trade = close_early(
                o, day=day, marks=m, spot=float(closes[day]), reason=reason, cost=cost
            )
            break
    spot_exp = _spot_at_or_before(closes, o.expiration)
    if spot_exp is None:
        return None
    held = settle(o, spot_exp, cost)
    if trade is None:
        trade = held
    exit_day = trade.exit_date or trade.expiration
    marks = {d: v for d, v in marks.items() if d < exit_day}
    return Outcome(
        trade=trade,
        exit_day=exit_day,
        marks=marks,
        hold_to_expiry_pnl=held.pnl,
        entry_debit=entry_fill,
        open_fees=o.open_fees,
    )


# ---------------------------------------------------------------------------
# Portfolio simulation, one ranker
# ---------------------------------------------------------------------------


class RankRun(BaseModel):
    """One ranker × profile × cost run."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ranker: str
    profile: str
    slippage: float
    trades: pd.DataFrame
    equity: pd.Series
    skipped: dict[str, int]


class _Pos(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying: str
    exit_day: dt.date
    contracts: int
    max_loss: Decimal
    cash_used: float
    outcome: Outcome


def _derived(c: Candidate, contracts: int, max_loss_unit: float) -> Derived:
    structure = analyze(_legs(c.picks, c.underlying), as_of=c.day)
    assert structure.kind is not None
    return Derived(
        underlying=c.underlying,
        expiration=c.picks[0].expiration,
        kind=structure.kind,
        defined_risk=True,
        max_loss_total=Decimal(str(round(max_loss_unit, 2))) * contracts,
        net_price=structure.net_debit_credit,
        limit_price=structure.net_debit_credit,
    )


def picked_outcomes(
    menus: Mapping[dt.date, list[Candidate]],
    *,
    rankers: Sequence[Ranker],
    filters: RankFilters,
    vrp_threshold: float,
    chains: Mapping[dt.date, pd.DataFrame],
    closes: pd.Series,
    cost: CostModel,
    exits: ExitConfig,
) -> dict[tuple[str, dt.date, str], Outcome | None]:
    """Outcomes of every candidate some ranker would pick (top-1 does not depend on the
    portfolio, so this is exactly the set :func:`run_portfolio` can ask for)."""
    closes = closes.sort_index()
    days = sorted(chains)
    out: dict[tuple[str, dt.date, str], Outcome | None] = {}
    for day, menu in sorted(menus.items()):
        if not menu:
            continue
        inputs = [c.inputs for c in menu]
        keys = set()
        for rk in rankers:
            ranked = rank(inputs, rk, filters=filters, vrp_threshold=vrp_threshold)
            if ranked:
                keys.add(ranked[0].key)
        for c in menu:
            if c.inputs.key in keys:
                out[(c.underlying, day, c.inputs.key)] = simulate_outcome(
                    c, chains=chains, days=days, closes=closes, cost=cost, exits=exits
                )
    return out


def run_portfolio(
    menus: Mapping[str, Mapping[dt.date, list[Candidate]]],
    outcomes: Mapping[tuple[str, dt.date, str], Outcome | None],
    *,
    ranker: Ranker,
    profile: str,
    settings: ArcSettings,
    starting_equity: float,
    filters: RankFilters,
    vrp_threshold: float,
    sessions: Sequence[dt.date],
    slippage: float,
    trend: Mapping[str, pd.Series],
    vol: Mapping[str, pd.Series],
) -> RankRun:
    """Walk *sessions*; each ticker's top-ranked candidate is sized, gated and opened.

    *outcomes* maps ``(underlying, day, candidate key)`` → the pick's
    :class:`Outcome` (computed once, so every ranker sees the same outcome for the
    same pick). A missing key is treated like ``None`` (cannot be settled).
    """
    cash_settled = settings.profile.buying_power is BuyingPower.CASH_SETTLED
    realised = 0.0
    open_: list[_Pos] = []
    rows: list[dict[str, object]] = []
    eq: dict[dt.date, float] = {}
    skipped = {"no_candidate": 0, "unsettled": 0, "sizing": 0, "gate": 0, "cash": 0}
    tickers = sorted(menus)
    for day in sessions:
        still: list[_Pos] = []
        for p in open_:
            if p.exit_day <= day:
                realised += p.outcome.trade.pnl * p.contracts
            else:
                still.append(p)
        open_ = still
        equity = starting_equity + realised
        for t in tickers:
            menu = menus[t].get(day)
            if not menu:
                continue
            ranked = rank(
                [c.inputs for c in menu], ranker, filters=filters, vrp_threshold=vrp_threshold
            )
            if not ranked:
                skipped["no_candidate"] += 1
                continue
            best = next(c for c in menu if c.inputs.key == ranked[0].key)
            out = outcomes.get((t, day, best.inputs.key))
            if out is None:
                skipped["unsettled"] += 1
                continue
            ml_unit = out.trade.max_loss
            size = size_contracts(
                suggestion=10**6,
                max_loss_per_contract=Decimal(str(round(ml_unit, 2))),
                equity=Decimal(str(round(equity, 2))),
                cap_pct=settings.max_alloc_pct,
            )
            if not size.trade:
                skipped["sizing"] += 1
                continue
            n = size.contracts
            d = _derived(best, n, ml_unit)
            account = AccountSnapshot(
                equity=Decimal(str(round(equity, 2))),
                last_equity=Decimal(str(round(equity, 2))),
                as_of=dt.datetime.combine(day, dt.time(16, 0), tzinfo=ET),
            )
            portfolio = Portfolio(
                positions=[Position(underlying=p.underlying, max_loss=p.max_loss) for p in open_]
            )
            if check_per_underlying(d, account, portfolio, settings) or check_max_open_positions(
                portfolio, settings
            ):
                skipped["gate"] += 1
                continue
            cash_used = max(out.entry_debit, 0.0) * MULT * n + out.open_fees * n
            if cash_settled and cash_used > equity - sum(p.cash_used for p in open_):
                skipped["cash"] += 1
                continue
            open_.append(
                _Pos(
                    underlying=t,
                    exit_day=out.exit_day,
                    contracts=n,
                    max_loss=Decimal(str(round(ml_unit, 2))) * n,
                    cash_used=cash_used,
                    outcome=out,
                )
            )
            tr = out.trade
            rows.append(
                {
                    "ranker": ranker.value,
                    "profile": profile,
                    "underlying": t,
                    "kind": tr.kind,
                    "spec": best.spec,
                    "entry_date": day,
                    "exit_date": out.exit_day,
                    "expiration": tr.expiration,
                    "contracts": n,
                    "pnl": tr.pnl * n,
                    "pnl_mid": tr.pnl_mid * n,
                    "pnl_unit": tr.pnl,
                    "max_loss_unit": ml_unit,
                    "entry_net": tr.entry_net,
                    "entry_net_mid": tr.entry_net_mid,
                    "fees": tr.fees * n,
                    "exit_reason": tr.exit_reason,
                    "days_held": (out.exit_day - day).days,
                    "hold_to_expiry_pnl": out.hold_to_expiry_pnl * n,
                    "managed_net_ev": (best.inputs.managed_net_ev or 0.0) * n,
                    "managed_net_ev_unit": best.inputs.managed_net_ev,
                    "managed_pop": best.inputs.managed_pop,
                    "static_pop": best.static_pop,
                    "static_net_ev_unit": best.static_net_ev,
                    "expected_days_held": best.expected_days_held,
                    "ev_proxy_unit": best.inputs.ev_proxy,
                    "rorc_day": best.inputs.rorc_day,
                    "vrp": best.inputs.vrp,
                    "atm_iv": best.atm_iv,
                    "rv_forecast": best.rv_forecast,
                    "credit_width": best.inputs.credit_width,
                    "debit_width": best.inputs.debit_width,
                    "spot_entry": tr.spot_entry,
                    "spot_exit": tr.spot_exit,
                    "trend": str(trend[t].get(day, "unknown")),
                    "vol": str(vol[t].get(day, "unknown")),
                    "legs": " ".join(f"{'+' if lg.side > 0 else '-'}{lg.symbol}" for lg in tr.legs),
                }
            )
        unreal = 0.0
        for p in open_:
            marks = p.outcome.marks
            if marks:
                past = [v for d, v in marks.items() if d <= day]
                if past:
                    unreal += past[-1] * p.contracts
        eq[day] = starting_equity + realised + unreal
    # Positions still open after the last session are booked at their exit, so the
    # caller should pass sessions that run past the last entry's exit.
    for p in open_:
        realised += p.outcome.trade.pnl * p.contracts
    trades = pd.DataFrame(rows)
    equity = pd.Series(eq, name="equity", dtype=float).sort_index()
    if open_ and len(equity):
        equity.iloc[-1] = starting_equity + realised
    log.info(
        "backtest.rank_run",
        ranker=ranker.value,
        profile=profile,
        slippage=slippage,
        trades=len(trades),
        **skipped,
    )
    return RankRun(
        ranker=ranker.value,
        profile=profile,
        slippage=slippage,
        trades=trades,
        equity=equity,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# Metrics, bootstrap, decision
# ---------------------------------------------------------------------------


def _max_dd(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((peak - equity).max())


def _dd_of_pnl(pnl_by_exit: pd.DataFrame) -> float:
    if pnl_by_exit.empty:
        return 0.0
    s = pnl_by_exit.sort_values(["exit_date", "entry_date"])["pnl"].cumsum()
    s = pd.concat([pd.Series([0.0]), s], ignore_index=True)
    return float((s.cummax() - s).max())


def summarize(run: RankRun, starting_equity: float) -> dict[str, object]:
    """One row of report metrics for *run*."""
    t, eq = run.trades, run.equity
    n = len(t)
    rets = eq.pct_change().dropna() if len(eq) > 1 else pd.Series(dtype=float)
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else math.nan
    dn = rets[rets < 0]
    sdd = float(np.sqrt((dn**2).mean())) if len(dn) else math.nan
    years = max((eq.index.max() - eq.index.min()).days / 365.25, 1e-9) if len(eq) else math.nan
    final = float(eq.iloc[-1]) if len(eq) else starting_equity
    gross = float(t["pnl_mid"].sum()) if n else 0.0
    pnl = float(t["pnl"].sum()) if n else 0.0
    return {
        "ranker": run.ranker,
        "profile": run.profile,
        "slippage": run.slippage,
        "trades": n,
        "net_pnl": pnl,
        "cagr": (final / starting_equity) ** (1 / years) - 1 if final > 0 and n else math.nan,
        "max_dd": _max_dd(eq),
        "max_dd_pct": float(((eq.cummax() - eq) / eq.cummax()).max()) if len(eq) else 0.0,
        "sharpe": float(rets.mean() / sd * math.sqrt(_TRADING_DAYS)) if sd and sd > 0 else math.nan,
        "sortino": float(rets.mean() / sdd * math.sqrt(_TRADING_DAYS))
        if sdd and sdd > 0
        else math.nan,
        "win_rate": float((t["pnl"] > 0).mean()) if n else math.nan,
        "mean_managed_pop": float(t["managed_pop"].mean()) if n else math.nan,
        "realised_net_ev_unit": float(t["pnl_unit"].mean()) if n else math.nan,
        "modelled_net_ev_unit": float(t["managed_net_ev_unit"].mean()) if n else math.nan,
        "avg_days_held": float(t["days_held"].mean()) if n else math.nan,
        "turnover_per_month": n / max(years * 12, 1e-9) if n else 0.0,
        "cost_share_of_gross": (gross - pnl) / abs(gross) if n and gross else math.nan,
        "hold_to_expiry_pnl": float(t["hold_to_expiry_pnl"].sum()) if n else 0.0,
    }


def block_bootstrap_ci(
    diff: np.ndarray, *, resamples: int, block: int, ci: float, seed: int
) -> tuple[float, float, float]:
    """(point, lo, hi) for Σ *diff* by a moving-block bootstrap (seeded, deterministic)."""
    x = np.asarray(diff, dtype=float)
    n = len(x)
    point = float(x.sum())
    if n == 0:
        return 0.0, 0.0, 0.0
    b = min(block, n)
    k = math.ceil(n / b)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - b + 1, size=(resamples, k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(resamples, -1)[:, :n]
    sums = x[idx].sum(axis=1)
    a = (1 - ci) / 2
    return point, float(np.quantile(sums, a)), float(np.quantile(sums, 1 - a))


def subperiod_stats(run: RankRun, labels: Sequence[str]) -> dict[str, tuple[float, float, int]]:
    """label → (net P&L, max DD of that label's trades booked at exit, trades)."""
    out: dict[str, tuple[float, float, int]] = {}
    for lab in labels:
        t = run.trades[run.trades["trend"] == lab] if len(run.trades) else run.trades
        out[lab] = (float(t["pnl"].sum()) if len(t) else 0.0, _dd_of_pnl(t), len(t))
    return out


def decide(
    runs: Mapping[str, RankRun],
    *,
    allows_credit: bool,
    rule: DecisionRule,
    boot: BootstrapSpec,
) -> tuple[pd.DataFrame, str]:
    """Apply the pre-registered rule; return the per-challenger table and the verdict."""
    inc = incumbent_for(allows_credit=allows_credit).value
    if inc not in runs:
        return pd.DataFrame(), f"incumbent {inc} was not run; no decision"
    base = runs[inc]
    base_sub = subperiod_stats(base, rule.subperiods)
    rows = []
    for name, run in runs.items():
        if name == inc:
            continue
        sub = subperiod_stats(run, rule.subperiods)
        wins = [
            lab
            for lab in rule.subperiods
            if sub[lab][0] > base_sub[lab][0] and sub[lab][1] <= base_sub[lab][1]
        ]
        idx = base.equity.index.union(run.equity.index)
        a = run.equity.reindex(idx).ffill().bfill()
        b = base.equity.reindex(idx).ffill().bfill()
        diff = (a.diff().fillna(0.0) - b.diff().fillna(0.0)).to_numpy()
        point, lo, hi = block_bootstrap_ci(
            diff, resamples=boot.resamples, block=boot.block_days, ci=boot.ci, seed=boot.seed
        )
        switch = len(wins) >= rule.min_subperiod_wins and lo > 0
        rows.append(
            {
                "challenger": name,
                "subperiods_won": len(wins),
                "won": ",".join(wins) or "–",
                "pnl_diff": point,
                "ci_lo": lo,
                "ci_hi": hi,
                "switch": switch,
            }
        )
    table = pd.DataFrame(rows)
    if table.empty:
        return table, f"keep {inc}: no challengers"
    winners = table[table["switch"]]
    if len(winners):
        best = winners.sort_values("pnl_diff", ascending=False).iloc[0]
        return table, (
            f"switch candidate: {best['challenger']} (won {best['subperiods_won']} sub-periods, "
            f"CI [{best['ci_lo']:,.0f}, {best['ci_hi']:,.0f}])"
        )
    closest = table.sort_values(["subperiods_won", "ci_lo"], ascending=False).iloc[0]
    return table, (
        f"keep {inc}. Closest: {closest['challenger']} (won {closest['subperiods_won']} "
        f"sub-periods, P&L diff {closest['pnl_diff']:,.0f}, CI [{closest['ci_lo']:,.0f}, "
        f"{closest['ci_hi']:,.0f}])"
    )


def profile_allows_credit(profile: str, path: Path | str | None = None) -> bool:
    p = load_account_profiles(path).get(profile)
    return not p.require_net_debit


def rankers_for(requested: Sequence[Ranker], *, allows_credit: bool) -> list[Ranker]:
    """Requested rankers that apply to the profile, incumbent always included (first)."""
    inc = incumbent_for(allows_credit=allows_credit)
    out = [inc]
    out += [r for r in requested if r is not inc and applicable(r, allows_credit=allows_credit)]
    return out


def labels_for(closes: pd.Series) -> tuple[pd.Series, pd.Series]:
    c = closes.sort_index()
    return label_trend(c), label_vol(c)


def exit_reason_share(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {}
    s = trades["exit_reason"].value_counts(normalize=True)
    return {str(k): float(v) for k, v in s.items() if k in {e.value for e in ExitReason}}
