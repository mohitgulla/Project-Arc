"""Managed-exit Monte Carlo model (PLAN D23): static vs managed PoP / EV for one structure.

Model ``gbm_flat_iv``
---------------------
- The underlying follows GBM with drift ``r`` and a single volatility (ATM IV by
  default), stepped **daily over calendar days** from today to expiry
  (``t = dte / 365``, the same clock as :mod:`arc.pricing.bs` and the scanner).
- Every day before expiry the structure is re-priced with the E2.1 BSM pricer
  (:func:`arc.pricing.bs.price_vectorized`) at the path's spot, the remaining time
  and the day's IV. The IV path is constant by default; ``iv_model:
  mean_reverting`` in ``config/exits.yaml`` decays it deterministically toward a
  long-run level (see :class:`arc.exits.policy.IvModel`).
- The exit policy is checked on the **mid** P&L each day in the order stop → take
  profit → DTE exit (:func:`arc.exits.policy.check_rules`). A position that fires
  closes that day; anything still open at expiry settles at intrinsic.

Costs (:class:`arc.backtest.costs.CostModel`, shared with the backtester)
-------------------------------------------------------------------------
- Entry: every leg fills at ``mid ± x·spread`` and pays the per-contract commission.
- Early close: every leg fills at ``mid ∓ x·spread`` (never below 0) and pays the
  commission again.
- Expiry: settlement at intrinsic, no slippage; commission only on ITM legs.
- The spread per leg is the quoted ``ask − bid`` when given, else the cost model's
  estimate; it is held constant over the life of the trade.

Outputs (:class:`ExitModelResult`), money in dollars per one unit of the structure:

- ``static``: hold to expiry, from the same paths' terminal prices, plus the
  analytic lognormal PoP (``pop_analytic``) as a cross-check.
- ``managed``: under the policy, with the probability of each exit reason, the
  expected holding period and net EV per dollar of buying power per day held.
- ``pop`` is the share of paths whose P&L **after costs** is > 0; ``pop_gross`` is
  the same at mid with no costs (comparable with the scanner's PoP).
- EVs are undiscounted.

Deterministic: fixed seed and path count (``config/exits.yaml``), pure numpy. No LLM,
no network.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence  # noqa: TC003 - pydantic fields
from dataclasses import dataclass
from typing import Literal

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict, Field
from scipy.special import ndtr

from arc.backtest.costs import CostModel
from arc.exits.policy import (
    HOLD_TO_EXPIRY,
    ExitModelConfig,
    ExitPolicy,
    ExitReason,
    IvModel,
    ResolvedRules,
    resolve_rules,
)
from arc.models import LegIntent, Structure, StructureKind
from arc.pricing.bs import OptionKind, price_vectorized
from arc.structures import CONTRACT_MULTIPLIER, parse_occ

__all__ = [
    "MODEL_NAME",
    "ExitModelResult",
    "ExitSummary",
    "ManagedStats",
    "SimLeg",
    "SimOutcome",
    "StaticStats",
    "TriggerLevels",
    "analytic_pop",
    "close_values",
    "iv_path",
    "model_exits",
    "realized_vol_forecast",
    "sim_legs",
    "simulate",
]

log = structlog.get_logger(__name__)

MODEL_NAME = "gbm_flat_iv"
MULT = float(CONTRACT_MULTIPLIER)
_REASON_CODE = {
    ExitReason.STOP: 0,
    ExitReason.TAKE_PROFIT: 1,
    ExitReason.DTE_EXIT: 2,
    ExitReason.EXPIRY: 3,
}
_FORBID = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Result contracts
# ---------------------------------------------------------------------------


class StaticStats(BaseModel):
    """Hold to expiry."""

    model_config = _FORBID

    pop: float = Field(..., ge=0.0, le=1.0, description="P(net P&L > 0) after costs")
    pop_gross: float = Field(..., ge=0.0, le=1.0, description="P(P&L > 0) at mid, no costs")
    pop_analytic: float = Field(..., ge=0.0, le=1.0, description="Lognormal PoP over breakevens")
    gross_ev: float = Field(..., description="$ per unit at mid, no costs")
    net_ev: float = Field(..., description="$ per unit after slippage and commissions")
    ev_per_bp_day: float | None = Field(None, description="net_ev / (buying power × days held)")


class ManagedStats(BaseModel):
    """Under the exit policy."""

    model_config = _FORBID

    pop: float = Field(..., ge=0.0, le=1.0)
    pop_gross: float = Field(..., ge=0.0, le=1.0)
    gross_ev: float
    net_ev: float
    p_take_profit: float = Field(..., ge=0.0, le=1.0)
    p_stop: float = Field(..., ge=0.0, le=1.0)
    p_dte_exit: float = Field(..., ge=0.0, le=1.0)
    p_expiry: float = Field(..., ge=0.0, le=1.0)
    expected_days_held: float = Field(..., ge=0.0)
    ev_per_bp_day: float | None = None


class TriggerLevels(BaseModel):
    """Where a rule fires: the close price per share, and the underlying when it fired.

    ``close_price`` is what the order pays (``close_side = "debit"``, e.g. $0.83 to buy
    back a $1.66 credit at 50%) or receives (``"credit"``). ``pnl`` is the P&L per
    share at that price. The underlying levels are the medians of the simulated spot
    on the paths where this rule fired, split below / above today's spot (``None``
    when no path fired on that side); ``median_day`` is the median day it fired.
    ``pnl``/``close_price`` for take profit use the base target (before any
    time-adjusted bucket).
    """

    model_config = _FORBID

    pnl: float
    close_price: float
    close_side: Literal["debit", "credit"]
    underlying_down: float | None = None
    underlying_up: float | None = None
    median_day: float | None = None
    probability: float = Field(..., ge=0.0, le=1.0)
    reachable: bool = Field(
        True,
        description="False when the threshold lies beyond max gain / max loss, so the rule "
        "can never fire (e.g. a 3x-credit stop on a condor whose max loss is < 2x credit)",
    )


class ExitModelResult(BaseModel):
    """Static vs managed numbers for one structure under one policy."""

    model_config = _FORBID

    policy: ExitPolicy
    structure_kind: StructureKind | None
    spot: float
    dte: int
    r: float
    iv_used: float = Field(..., description="Vol the marks are priced at (ATM IV)")
    path_vol: float = Field(..., description="Vol the underlying paths move at")
    path_vol_source: Literal["iv", "realized_forecast"] = "iv"
    vrp: float | None = Field(
        None, description="ATM IV − realised-vol forecast (None = no forecast)"
    )
    rorc_day: float | None = Field(
        None, description="managed.net_ev / (max_loss × expected_days_held): return on risk per day"
    )
    iv_model: IvModel
    entry_net: float = Field(..., description="Per-share entry price at mid, + debit / − credit")
    entry_costs: float = Field(..., description="$ per unit: entry slippage + commissions")
    buying_power: float | None = Field(None, description="$ per unit")
    static: StaticStats
    managed: ManagedStats
    take_profit: TriggerLevels | None = None
    stop: TriggerLevels | None = None
    dte_exit_day: int | None = Field(None, description="Days from today until the DTE exit")
    pop_std_error: float = Field(..., description="MC standard error of a PoP estimate")
    n_paths: int
    seed: int
    model: Literal["gbm_flat_iv"] = MODEL_NAME


class ExitSummary(BaseModel):
    """Compact static-vs-managed numbers for persona prompts and the Quant menu.

    Filled by the pipeline from :class:`ExitModelResult`; money is $ per unit.
    """

    model_config = _FORBID

    policy: str = Field(..., description="Exit rules applied, one line")
    static_pop: float = Field(..., description="Hold to expiry, after costs")
    static_net_ev: float
    managed_pop: float = Field(..., description="Under the exit policy, after costs")
    managed_net_ev: float
    p_take_profit: float
    p_stop: float
    p_dte_exit: float
    p_expiry: float
    expected_days_held: float
    rorc_day: float | None = Field(None, description="managed net EV / (max loss × days held)")
    vrp: float | None = Field(None, description="ATM IV − realised-vol forecast")
    path_vol: float | None = Field(None, description="Vol the simulated paths moved at")
    take_profit_close: float | None = Field(None, description="Per-share price to close at TP")
    stop_close: float | None = Field(None, description="Per-share price to close at the stop")

    @classmethod
    def from_result(cls, r: ExitModelResult) -> ExitSummary:
        return cls(
            policy=r.policy.summary(),
            static_pop=round(r.static.pop, 4),
            static_net_ev=r.static.net_ev,
            managed_pop=round(r.managed.pop, 4),
            managed_net_ev=r.managed.net_ev,
            p_take_profit=round(r.managed.p_take_profit, 4),
            p_stop=round(r.managed.p_stop, 4),
            p_dte_exit=round(r.managed.p_dte_exit, 4),
            p_expiry=round(r.managed.p_expiry, 4),
            expected_days_held=r.managed.expected_days_held,
            rorc_day=r.rorc_day,
            vrp=r.vrp,
            path_vol=round(r.path_vol, 4),
            take_profit_close=None if r.take_profit is None else r.take_profit.close_price,
            stop_close=None if r.stop is None else r.stop.close_price,
        )


# ---------------------------------------------------------------------------
# Simulation core (shared with evaluate_position)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimLeg:
    """One leg for the simulator. ``sign`` +1 long / −1 short; prices per share."""

    kind: OptionKind
    strike: float
    sign: int
    ratio: int
    spread: float


@dataclass(frozen=True)
class SimOutcome:
    """Per-path results. Values are per share; fees are dollars per unit."""

    exit_day: np.ndarray  # int, 1..dte
    reason: np.ndarray  # int codes, see _REASON_CODE
    exit_value_mid: np.ndarray  # position value at mid when closed/settled
    exit_proceeds: np.ndarray  # value realised after exit slippage
    exit_fees: np.ndarray  # $ per unit
    exit_spot: np.ndarray
    terminal_spot: np.ndarray  # spot at expiry on every path (hold-to-expiry view)

    def share(self, reason: ExitReason) -> float:
        return float(np.mean(self.reason == _REASON_CODE[reason]))


def sim_legs(
    structure: Structure,
    cost: CostModel,
    spreads: Mapping[str, float] | None = None,
) -> list[SimLeg]:
    """Simulator legs; spreads by OCC symbol (compact or padded), else the cost estimate."""
    quoted = {parse_occ(k).format(): v for k, v in (spreads or {}).items()}
    out: list[SimLeg] = []
    for leg in structure.legs:
        occ = parse_occ(leg.occ_symbol)
        mid = float(leg.premium or 0)
        spread = quoted.get(occ.format())
        out.append(
            SimLeg(
                kind=occ.kind,
                strike=float(occ.strike),
                sign=1 if leg.side == LegIntent.LONG else -1,
                ratio=leg.ratio,
                spread=cost.spread(mid) if spread is None else max(float(spread), 0.0),
            )
        )
    return out


def iv_path(iv0: float, days: int, model: IvModel) -> np.ndarray:
    """IV for days 0..days (inclusive), per the configured IV model."""
    d = np.arange(days + 1, dtype=float)
    if model.kind == "constant":
        return np.full(days + 1, iv0)
    assert model.long_run is not None and model.half_life_days is not None
    return model.long_run + (iv0 - model.long_run) * 0.5 ** (d / model.half_life_days)


def _value(legs: Sequence[SimLeg], s: np.ndarray, t: float, r: float, sigma: float) -> np.ndarray:
    """Position value per share at spots *s* with *t* years left (BSM, E2.1 pricer)."""
    v = np.zeros_like(s)
    for lg in legs:
        v += (
            lg.sign
            * lg.ratio
            * price_vectorized(np.asarray(lg.kind.value), s, lg.strike, t, r, sigma)
        )
    return v


def _leg_prices(lg: SimLeg, s: np.ndarray, t: float | None, r: float, sigma: float) -> np.ndarray:
    if t is None:  # expiry: intrinsic
        return (
            np.maximum(s - lg.strike, 0.0)
            if lg.kind == OptionKind.CALL
            else np.maximum(lg.strike - s, 0.0)
        )
    return price_vectorized(np.asarray(lg.kind.value), s, lg.strike, t, r, sigma)


def close_values(
    legs: Sequence[SimLeg], s: np.ndarray, t: float | None, r: float, sigma: float, cost: CostModel
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(value at mid, proceeds after slippage, fees $/unit) of closing at spots *s*.

    ``t is None`` = expiry settlement: intrinsic, no slippage, fees on ITM legs only.
    """
    value = np.zeros_like(s)
    proceeds = np.zeros_like(s)
    fees = np.zeros_like(s)
    x = cost.slippage_frac
    for lg in legs:
        p = _leg_prices(lg, s, t, r, sigma)
        value += lg.sign * lg.ratio * p
        if t is None:
            proceeds += lg.sign * lg.ratio * p
            fees += np.where(p > 0, cost.commission_per_contract * lg.ratio, 0.0)
        else:
            # closing a long sells at mid − x·spread; closing a short buys at mid + x·spread
            fill = np.maximum(p - lg.sign * x * lg.spread, 0.0)
            proceeds += lg.sign * lg.ratio * fill
            fees += cost.commission_per_contract * lg.ratio
    return value, proceeds, fees


def simulate(
    legs: Sequence[SimLeg],
    rules: ResolvedRules,
    *,
    spot: float,
    iv: float,
    r: float,
    dte: int,
    cost: CostModel,
    cfg: ExitModelConfig,
    path_vol: float | None = None,
) -> SimOutcome:
    """Run the policy over ``cfg.n_paths`` daily GBM paths from *spot* to expiry (*dte* days).

    Marks are always priced at *iv* (the IV path). The underlying paths move at
    *path_vol* (a realised-vol forecast) when given, else along the IV path. Priced
    and simulated at IV alone, EV is ≈ −costs by construction; a realised-vol
    forecast below IV is what makes short premium worth anything (and vice versa).
    """
    if dte < 1:
        msg = "the exit model needs dte >= 1"
        raise ValueError(msg)
    if spot <= 0 or iv <= 0 or (path_vol is not None and path_vol <= 0):
        msg = "spot, iv and path_vol must be > 0"
        raise ValueError(msg)
    n = cfg.n_paths
    rng = np.random.default_rng(cfg.seed)
    sig = iv_path(iv, dte, cfg.iv_model)
    dt = 1.0 / 365.0
    z = rng.standard_normal((n, dte))
    step_sig = sig[:dte] if path_vol is None else np.full(dte, path_vol)  # vol over day d → d+1
    log_ret = (r - 0.5 * step_sig**2) * dt + step_sig * math.sqrt(dt) * z
    spots = spot * np.exp(np.cumsum(log_ret, axis=1))  # spots[:, d-1] = spot at end of day d

    exit_day = np.full(n, dte, dtype=int)
    reason = np.full(n, _REASON_CODE[ExitReason.EXPIRY], dtype=int)
    value = np.zeros(n)
    proceeds = np.zeros(n)
    fees = np.zeros(n)
    exit_spot = spots[:, -1].copy()
    open_ = np.ones(n, dtype=bool)

    pol = rules.policy
    has_rules = (
        pol.stop is not None
        or pol.close_at_dte is not None
        or any(rules.tp_pnl(d) is not None for d in range(dte))
    )
    stop = rules.stop_pnl
    for d in range(1, dte):
        if not has_rules:
            break
        idx = np.flatnonzero(open_)
        if idx.size == 0:
            break
        rem = dte - d
        t = rem / 365.0
        s = spots[idx, d - 1]
        v = _value(legs, s, t, r, float(sig[d]))
        pnl = v - rules.entry_net
        fired = np.full(idx.size, -1, dtype=int)
        if stop is not None:
            fired[pnl <= stop] = _REASON_CODE[ExitReason.STOP]
        tp = rules.tp_pnl(rem)
        if tp is not None:
            fired[(fired < 0) & (pnl >= tp)] = _REASON_CODE[ExitReason.TAKE_PROFIT]
        if pol.close_at_dte is not None and rem <= pol.close_at_dte:
            fired[fired < 0] = _REASON_CODE[ExitReason.DTE_EXIT]
        hit = fired >= 0
        if not hit.any():
            continue
        rows = idx[hit]
        cv, cp, cf = close_values(legs, s[hit], t, r, float(sig[d]), cost)
        exit_day[rows] = d
        reason[rows] = fired[hit]
        value[rows] = cv
        proceeds[rows] = cp
        fees[rows] = cf
        exit_spot[rows] = s[hit]
        open_[rows] = False

    rows = np.flatnonzero(open_)
    if rows.size:
        cv, cp, cf = close_values(legs, spots[rows, -1], None, r, float(sig[-1]), cost)
        value[rows] = cv
        proceeds[rows] = cp
        fees[rows] = cf
    return SimOutcome(
        exit_day=exit_day,
        reason=reason,
        exit_value_mid=value,
        exit_proceeds=proceeds,
        exit_fees=fees,
        exit_spot=exit_spot,
        terminal_spot=spots[:, -1].copy(),
    )


# ---------------------------------------------------------------------------
# Analytic cross-check
# ---------------------------------------------------------------------------


def _payoff_mid(legs: Sequence[SimLeg], s: np.ndarray, entry_net: float) -> np.ndarray:
    v = np.zeros_like(s, dtype=float)
    for lg in legs:
        v += lg.sign * lg.ratio * _leg_prices(lg, s, None, 0.0, 1.0)
    return v - entry_net


def analytic_pop(
    structure: Structure, *, spot: float, iv: float, r: float, dte: int | None = None
) -> float:
    """Lognormal (drift r, vol *iv*) probability the expiry P&L at mid is > 0.

    Integrates the terminal density over the intervals between breakevens where the
    payoff is positive (same method as the scanner's PoP).
    """
    days = structure.dte if dte is None else dte
    t = max(days, 1e-9) / 365.0
    legs = sim_legs(structure, CostModel())
    entry = float(structure.net_debit_credit)
    bes = sorted(float(b) for b in structure.breakevens)
    edges = [0.0, *bes, math.inf]

    def cdf(x: float) -> float:
        if x <= 0:
            return 0.0
        if math.isinf(x):
            return 1.0
        d2 = (math.log(spot / x) + (r - 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
        return float(ndtr(-d2))

    p = 0.0
    for lo, hi in zip(edges, edges[1:], strict=False):
        probe = hi / 2 if lo == 0.0 else (lo * 2 if math.isinf(hi) else (lo + hi) / 2)
        if _payoff_mid(legs, np.array([probe]), entry)[0] > 0:
            p += cdf(hi) - cdf(lo)
    return min(max(p, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _entry(legs: Sequence[SimLeg], structure: Structure, cost: CostModel) -> tuple[float, float]:
    """(entry price per share after slippage, entry commissions $/unit)."""
    fill_net = 0.0
    for lg, leg in zip(legs, structure.legs, strict=True):
        fill_net += lg.sign * lg.ratio * cost.fill(float(leg.premium or 0), lg.spread, lg.sign)
    return fill_net, cost.fees(sum(lg.ratio for lg in legs))


def _per_bp_day(ev: float, bp: float | None, days: float) -> float | None:
    if bp is None or bp <= 0 or days <= 0:
        return None
    return ev / (bp * days)


def _median(a: np.ndarray) -> float | None:
    return float(np.median(a)) if a.size else None


def _trigger(
    out: SimOutcome, rules: ResolvedRules, code: int, pnl: float | None, spot: float
) -> TriggerLevels | None:
    if pnl is None:
        return None
    v = rules.value_at_pnl(pnl)
    mask = out.reason == code
    if pnl >= 0:
        reachable = rules.max_gain is None or pnl < rules.max_gain - 1e-9
    else:
        reachable = rules.max_loss is None or -pnl < rules.max_loss - 1e-9
    s = out.exit_spot[mask]
    return TriggerLevels(
        pnl=round(pnl, 4),
        close_price=round(abs(v), 4),
        close_side="debit" if v < 0 else "credit",
        underlying_down=_round(_median(s[s < spot])),
        underlying_up=_round(_median(s[s >= spot])),
        median_day=_median(out.exit_day[mask].astype(float)),
        probability=float(np.mean(mask)),
        reachable=reachable,
    )


def _rorc_day(net_ev: float, structure: Structure, days: float) -> float | None:
    """Return on risk per day: net EV / (max loss × days held); None if undefined."""
    ml = None if structure.max_loss is None else float(structure.max_loss)
    if ml is None or ml <= 0 or days <= 0:
        return None
    return round(net_ev / (ml * days), 6)


def realized_vol_forecast(hv20: float | None, hv60: float | None) -> float | None:
    """Realised-vol forecast for the paths: mean of HV20 and HV60 (either alone if one is
    missing; None if both are)."""
    vals = [v for v in (hv20, hv60) if v is not None and v > 0 and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _dte_exit_day(dte: int, close_at_dte: int | None) -> int | None:
    """First simulated day the DTE rule closes (days are checked 1..dte-1); None = never."""
    if close_at_dte is None:
        return None
    day = max(dte - close_at_dte, 1)
    return day if day < dte else None


def model_exits(
    structure: Structure,
    policy: ExitPolicy,
    *,
    spot: float,
    iv: float,
    r: float,
    cost: CostModel | None = None,
    cfg: ExitModelConfig | None = None,
    spreads: Mapping[str, float] | None = None,
    realized_vol: float | None = None,
) -> ExitModelResult:
    """Static (hold to expiry) vs managed (under *policy*) PoP and EV for *structure*.

    *structure* legs must carry ``premium`` (the entry mid); *iv* is the vol the marks
    are priced at (ATM IV); *spreads* are per-leg quoted spreads per share.
    *realized_vol* is the realised-vol forecast the paths move at (the pipeline
    passes the mean of HV20 and HV60, see :func:`realized_vol_forecast`); ``None``
    or ``cfg.path_vol == "iv"`` moves the paths at IV.
    """
    cost = cost or CostModel()
    cfg = cfg or ExitModelConfig()
    legs = sim_legs(structure, cost, spreads)
    rules = resolve_rules(structure, policy)
    dte = structure.dte
    use_rv = realized_vol is not None and cfg.path_vol == "realized_forecast"
    pvol = realized_vol if use_rv and realized_vol is not None else iv
    out = simulate(
        legs,
        rules,
        spot=spot,
        iv=iv,
        r=r,
        dte=dte,
        cost=cost,
        cfg=cfg,
        path_vol=pvol if use_rv else None,
    )

    entry_mid = rules.entry_net
    entry_fill, entry_fees = _entry(legs, structure, cost)
    entry_costs = (entry_fill - entry_mid) * MULT + entry_fees
    bp = None if structure.buying_power is None else float(structure.buying_power)

    # managed
    gross = (out.exit_value_mid - entry_mid) * MULT
    net = (out.exit_proceeds - entry_fill) * MULT - entry_fees - out.exit_fees
    days = float(np.mean(out.exit_day))
    managed = ManagedStats(
        pop=float(np.mean(net > 0)),
        pop_gross=float(np.mean(gross > 0)),
        gross_ev=round(float(np.mean(gross)), 2),
        net_ev=round(float(np.mean(net)), 2),
        p_take_profit=out.share(ExitReason.TAKE_PROFIT),
        p_stop=out.share(ExitReason.STOP),
        p_dte_exit=out.share(ExitReason.DTE_EXIT),
        p_expiry=out.share(ExitReason.EXPIRY),
        expected_days_held=round(days, 2),
        ev_per_bp_day=_per_bp_day(float(np.mean(net)), bp, days),
    )

    # static: same paths, settled at expiry
    hold = resolve_rules(structure, HOLD_TO_EXPIRY)
    term = out.terminal_spot
    t_value, _, t_fees = close_values(legs, term, None, r, iv, cost)
    s_gross = (t_value - hold.entry_net) * MULT
    s_net = (t_value - entry_fill) * MULT - entry_fees - t_fees
    static = StaticStats(
        pop=float(np.mean(s_net > 0)),
        pop_gross=float(np.mean(s_gross > 0)),
        pop_analytic=round(analytic_pop(structure, spot=spot, iv=pvol, r=r), 4),
        gross_ev=round(float(np.mean(s_gross)), 2),
        net_ev=round(float(np.mean(s_net)), 2),
        ev_per_bp_day=_per_bp_day(float(np.mean(s_net)), bp, float(dte)),
    )

    cad = policy.close_at_dte
    p_mid = max(min(static.pop_gross, 1 - 1e-9), 1e-9)
    result = ExitModelResult(
        policy=policy,
        structure_kind=structure.kind,
        spot=spot,
        dte=dte,
        r=r,
        iv_used=iv,
        path_vol=pvol,
        path_vol_source="realized_forecast" if use_rv else "iv",
        vrp=None if realized_vol is None else round(iv - realized_vol, 4),
        rorc_day=_rorc_day(managed.net_ev, structure, managed.expected_days_held),
        iv_model=cfg.iv_model,
        entry_net=entry_mid,
        entry_costs=round(entry_costs, 2),
        buying_power=bp,
        static=static,
        managed=managed,
        take_profit=_trigger(
            out, rules, _REASON_CODE[ExitReason.TAKE_PROFIT], rules.tp_pnl(dte), spot
        ),
        stop=_trigger(out, rules, _REASON_CODE[ExitReason.STOP], rules.stop_pnl, spot),
        dte_exit_day=_dte_exit_day(dte, cad),
        pop_std_error=math.sqrt(p_mid * (1 - p_mid) / cfg.n_paths),
        n_paths=cfg.n_paths,
        seed=cfg.seed,
    )
    log.debug(
        "exits.model",
        kind=structure.kind.value if structure.kind else None,
        dte=dte,
        static_pop=static.pop,
        managed_pop=managed.pop,
        static_net_ev=static.net_ev,
        managed_net_ev=managed.net_ev,
    )
    return result
