"""Chain scanner: liquid, delta-targeted, defined-risk credit and debit structures.

Pipeline for one underlying (:func:`scan`):

1. Pull the quote and the option chain for expirations in the DTE window
   (default 30–45, PLAN D4) through a :class:`~arc.data.base.MarketDataProvider`.
2. Drop illiquid / bad-data contracts (:mod:`arc.scanner.filters`).
3. ATM IV of the expiration nearest 30 DTE → IV rank / percentile against the
   stored history (:mod:`arc.scanner.iv`).
4. For each expiration, pick short strikes whose |Δ| lies in the D4 band
   (16–30Δ) closest to the target delta, pair each with a long wing about
   ``wing_width`` further OTM, and build bull put / bear call credit verticals
   and iron condors via :mod:`arc.structures` (every leg must pass the filters).
5. Debit strategies (D25, E3.4; for the no-margin ``cash_debit`` /
   ``cash_long_only`` account profiles): long legs whose |Δ| lies in the long
   band (default 0.40–0.70, target 0.55) become long calls / long puts; each is
   paired with a further-OTM short of the same type and expiry whose |Δ| lies in
   the debit short band (default 0.20–0.35) into a bull call / bear put debit
   vertical (:func:`~arc.structures.debit_vertical`). Every leg must pass the
   filters.
6. Score and rank.

Scores (per one unit of the structure):

- ``credit`` — mid-price net credit per share; ``natural_credit`` is the
  worst-case fill (sell at bid, buy at ask).
- ``credit_width`` — ``credit / max wing width``; the primary rank key for
  credit structures (``None`` for debit structures).
- ``ev_ratio`` — ``ev_proxy / max loss``; the primary key for debit structures
  under ``credit_width`` ranking (a debit has no credit to rank by). For a debit
  structure, max loss is the debit paid (× 100).
- ``pop`` — probability of finishing at a profit at expiry, lognormal with
  drift ``r`` and vol = ATM IV, over the structure's breakevens.
- ``ev_proxy`` — dollars: flat-vol (ATM IV) BSM value of the position minus
  what it costs at mid, minus ``cost``: the **entry** cost under the shared
  :class:`~arc.backtest.costs.CostModel` (``config/costs.yaml``): every leg's
  slippage from mid (``slippage_frac`` × its quoted spread) plus the entry fees
  (commission, ORF, OCC, CAT; TAF + SEC on the legs sold). Positive means the
  chain's skew pays more for the short strikes than a flat-vol model says they
  are worth, after costs. It is a ranking proxy, not a forecast; the card's Net
  EV (E2.4 exit model) uses the same cost model for entry *and* exit.

Nothing here submits orders or calls an LLM.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field, model_validator
from scipy.stats import norm

from arc.backtest.costs import CostModel, load_cost_model
from arc.models import Liquidity, Structure
from arc.pricing.bs import BSMInputs, price
from arc.scanner.filters import FilterReport, LiquidityRules, apply_filters
from arc.scanner.iv import IvStats, atm_iv, iv_stats
from arc.structures import (
    CONTRACT_MULTIPLIER,
    MarketInputs,
    credit_vertical,
    debit_vertical,
    iron_condor,
    long_call,
    long_put,
    parse_occ,
    payoff_at,
)
from arc.utils.calendar import dte_calendar

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider, OptionContract

__all__ = [
    "CREDIT_STRATEGIES",
    "DEBIT_STRATEGIES",
    "RankBy",
    "ScanCandidate",
    "ScanParams",
    "ScanResult",
    "ScanStrategy",
    "profile_strategy_set",
    "scan",
    "select_debit_short",
    "select_shorts",
    "select_wing",
]

log = structlog.get_logger(__name__)

_ATM_TARGET_DTE = 30


class ScanStrategy(StrEnum):
    """Structures the scanner builds (PLAN D4 whitelist; debit set D25)."""

    BULL_PUT = "bull_put"
    BEAR_CALL = "bear_call"
    IRON_CONDOR = "iron_condor"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_DEBIT = "bull_call_debit"
    BEAR_PUT_DEBIT = "bear_put_debit"


CREDIT_STRATEGIES: tuple[ScanStrategy, ...] = (
    ScanStrategy.BULL_PUT,
    ScanStrategy.BEAR_CALL,
    ScanStrategy.IRON_CONDOR,
)
DEBIT_STRATEGIES: tuple[ScanStrategy, ...] = (
    ScanStrategy.LONG_CALL,
    ScanStrategy.LONG_PUT,
    ScanStrategy.BULL_CALL_DEBIT,
    ScanStrategy.BEAR_PUT_DEBIT,
)


class RankBy(StrEnum):
    """Primary ranking key; the other one breaks ties."""

    CREDIT_WIDTH = "credit_width"
    EV = "ev"


class ScanParams(BaseModel):
    """Scanner knobs; :meth:`from_settings` fills defaults from config."""

    dte_min: int = Field(30, ge=1)
    dte_max: int = Field(45, ge=1)
    target_delta: float = Field(0.20, gt=0.0, lt=1.0)
    delta_min: float = Field(0.16, gt=0.0, lt=1.0)
    delta_max: float = Field(0.30, gt=0.0, lt=1.0)
    wing_width: float = Field(5.0, gt=0.0)
    shorts_per_side: int = Field(3, ge=1, description="Short strikes tried per side/expiry")
    # Debit strategies (D25): long leg band / target, debit-vertical short band.
    long_target_delta: float = Field(0.55, gt=0.0, lt=1.0)
    long_delta_min: float = Field(0.40, gt=0.0, lt=1.0)
    long_delta_max: float = Field(0.70, gt=0.0, lt=1.0)
    debit_short_target_delta: float = Field(0.30, gt=0.0, lt=1.0)
    debit_short_delta_min: float = Field(0.20, gt=0.0, lt=1.0)
    debit_short_delta_max: float = Field(0.35, gt=0.0, lt=1.0)
    debit_width: float | None = Field(
        None,
        gt=0.0,
        description="Target debit-vertical width ($); None = pick the short by delta only",
    )
    longs_per_side: int = Field(3, ge=1, description="Long strikes tried per side/expiry")
    debit_shorts_per_long: int = Field(2, ge=1, description="Short legs paired with each long")
    # Default: the credit set, as before D25. Profile-aware callers pass strategies
    # (ScanParams.from_settings uses the account profile's strategies).
    strategies: list[ScanStrategy] = Field(default_factory=lambda: list(CREDIT_STRATEGIES))
    rank_by: RankBy = RankBy.CREDIT_WIDTH
    top: int | None = Field(None, ge=1, description="Keep only the best N candidates")
    risk_free_rate: float = 0.04
    rules: LiquidityRules = Field(default_factory=LiquidityRules)
    iv_lookback: int = Field(252, ge=2)
    iv_min_obs: int = Field(120, ge=2)
    spot_max_spread_pct: float = Field(0.05, gt=0.0, le=1.0)
    cost: CostModel = Field(
        default_factory=load_cost_model,
        description="Entry cost model for ev_proxy / cost (config/costs.yaml, shared, D23)",
    )

    @model_validator(mode="after")
    def _check(self) -> ScanParams:
        if self.dte_max < self.dte_min:
            msg = f"dte_max ({self.dte_max}) must be >= dte_min ({self.dte_min})"
            raise ValueError(msg)
        if self.delta_max < self.delta_min:
            msg = f"delta_max ({self.delta_max}) must be >= delta_min ({self.delta_min})"
            raise ValueError(msg)
        if not self.delta_min <= self.target_delta <= self.delta_max:
            msg = (
                f"target delta {self.target_delta} is outside the short-strike band "
                f"[{self.delta_min}, {self.delta_max}]"
            )
            raise ValueError(msg)
        for name, lo, tgt, hi in (
            ("long", self.long_delta_min, self.long_target_delta, self.long_delta_max),
            (
                "debit short",
                self.debit_short_delta_min,
                self.debit_short_target_delta,
                self.debit_short_delta_max,
            ),
        ):
            if not lo <= tgt <= hi:
                msg = f"{name} target delta {tgt} is outside its band [{lo}, {hi}]"
                raise ValueError(msg)
        if not self.strategies:
            msg = "at least one strategy is required"
            raise ValueError(msg)
        return self

    @classmethod
    def from_settings(cls, settings: ArcSettings, **overrides: object) -> ScanParams:
        """Defaults from :class:`arc.config.ArcSettings`, then *overrides*.

        The DTE window is the account profile's entry window (D25) and the default
        strategies are every strategy the profile maps a stance to.
        """
        dte_min, dte_max = settings.entry_dte_window
        profile_strategies = profile_strategy_set(settings)
        base: dict[str, object] = {
            "dte_min": dte_min,
            "dte_max": dte_max,
            "long_target_delta": settings.scanner_long_target_delta,
            "long_delta_min": settings.scanner_long_delta_min,
            "long_delta_max": settings.scanner_long_delta_max,
            "debit_short_target_delta": settings.scanner_debit_short_target_delta,
            "debit_short_delta_min": settings.scanner_debit_short_delta_min,
            "debit_short_delta_max": settings.scanner_debit_short_delta_max,
            "debit_width": settings.scanner_debit_width,
            "target_delta": settings.scanner_target_delta,
            "delta_min": settings.scanner_short_delta_min,
            "delta_max": settings.scanner_short_delta_max,
            "wing_width": settings.scanner_wing_width,
            "risk_free_rate": settings.scanner_risk_free_rate,
            "rules": LiquidityRules.from_settings(settings),
            "iv_lookback": settings.scanner_iv_lookback,
            "iv_min_obs": settings.iv_min_obs_rank,
            "spot_max_spread_pct": settings.spot_max_spread_pct,
        }
        if profile_strategies:
            base["strategies"] = profile_strategies
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls.model_validate(base)


class ScanCandidate(BaseModel):
    """One ranked structure candidate."""

    rank: int = Field(0, ge=0, description="1 = best; 0 before ranking")
    ticker: str
    strategy: ScanStrategy
    expiration: dt.date
    dte: int
    structure: Structure
    short_deltas: list[float] = Field(..., description="|Δ| of each short leg (put first)")
    long_deltas: list[float] = Field(
        default_factory=list, description="|Δ| of each long leg (put first)"
    )
    width: float | None = Field(
        ..., gt=0, description="Widest wing, dollars per share; None for a single long leg"
    )
    credit: float = Field(
        ..., description="Mid net credit per share (> 0 credit; < 0 = a debit of -credit)"
    )
    natural_credit: float = Field(..., description="Sell-at-bid / buy-at-ask credit per share")
    credit_width: float | None = Field(
        ..., description="credit / width for credit structures; None for debit structures"
    )
    ev_ratio: float | None = Field(
        None, description="ev_proxy / max loss ($ EV per $ at risk); None without a max loss"
    )
    pop: float = Field(..., ge=0.0, le=1.0)
    ev_proxy: float = Field(..., description="Dollars per unit, after cost (see module doc)")
    cost: float = Field(
        ..., ge=0.0, description="Entry slippage + fees (shared CostModel), dollars per unit"
    )
    atm_iv: float | None = Field(None, description="ATM IV of this expiration (model vol)")
    leg_spreads: dict[str, float] = Field(
        default_factory=dict, description="Quoted ask − bid per share, by leg OCC symbol"
    )


class NoSpotError(LookupError):
    """E4.12: the underlying has no usable spot (fail the ticker closed)."""


class ScanResult(BaseModel):
    """Everything :func:`scan` found for one underlying."""

    ticker: str
    as_of: dt.date
    spot: float
    spot_basis: str = Field("mid", description="E4.12: mid | last_close (see market_spot)")
    params: ScanParams
    expirations: list[dt.date]
    iv: IvStats
    filter_report: FilterReport
    candidates: list[ScanCandidate]


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


def _abs_delta(c: OptionContract) -> float:
    assert c.greeks is not None and c.greeks.delta is not None  # guaranteed by filters
    return abs(c.greeks.delta)


def select_shorts(
    contracts: Sequence[OptionContract],
    *,
    target: float,
    delta_min: float,
    delta_max: float,
    n: int,
) -> list[OptionContract]:
    """Up to *n* contracts with |Δ| in ``[delta_min, delta_max]``, nearest *target* first.

    Ties on distance go to the lower |Δ| (further OTM), then the lower strike.
    """
    band = [c for c in contracts if delta_min <= _abs_delta(c) <= delta_max]
    # Round the distance so float noise (|0.19-0.20| vs |0.21-0.20|) does not break ties.
    band.sort(key=lambda c: (round(abs(_abs_delta(c) - target), 9), _abs_delta(c), c.strike))
    return band[:n]


def select_debit_short(
    long: OptionContract,
    contracts: Sequence[OptionContract],
    *,
    target: float,
    delta_min: float,
    delta_max: float,
    width: float | None,
    n: int,
) -> list[OptionContract]:
    """Up to *n* shorts for a debit vertical on *long*: same type and expiry, further
    OTM (calls above, puts below), |Δ| in ``[delta_min, delta_max]``.

    Nearest *width* first when a width is set (only ``[width / 2, 2 * width]``
    qualifies), else nearest *target* |Δ|; ties go to the narrower spread.
    """
    sign = -1.0 if long.option_type == "put" else 1.0
    pool: list[tuple[float, float, float, OptionContract]] = []
    for c in contracts:
        if c.option_type != long.option_type or c.expiration != long.expiration:
            continue
        dist = sign * (c.strike - long.strike)
        if dist <= 0 or not delta_min <= _abs_delta(c) <= delta_max:
            continue
        if width is not None and not width / 2 <= dist <= 2 * width:
            continue
        fit = abs(dist - width) if width is not None else abs(_abs_delta(c) - target)
        pool.append((round(fit, 9), dist, c.strike, c))
    pool.sort(key=lambda x: x[:3])
    return [x[3] for x in pool[:n]]


def select_wing(
    short: OptionContract, contracts: Sequence[OptionContract], width: float
) -> OptionContract | None:
    """Long wing further OTM than *short* whose distance is closest to *width*.

    Puts look below the short strike, calls above. Ties go to the narrower wing.
    Only strikes within ``[width / 2, 2 * width]`` of the short qualify.
    """
    sign = -1.0 if short.option_type == "put" else 1.0
    best: tuple[float, float, OptionContract] | None = None
    for c in contracts:
        if c.option_type != short.option_type or c.expiration != short.expiration:
            continue
        dist = sign * (c.strike - short.strike)
        if not width / 2 <= dist <= 2 * width:
            continue
        key = (abs(dist - width), dist)
        if best is None or key < best[:2]:
            best = (*key, c)
    return None if best is None else best[2]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _lognormal_cdf(x: float, spot: float, sigma: float, t: float, r: float) -> float:
    """P(S_T <= x) under GBM with drift r."""
    if x <= 0:
        return 0.0
    if math.isinf(x):
        return 1.0
    d2 = (math.log(spot / x) + (r - 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return float(norm.cdf(-d2))


def _pop(structure: Structure, spot: float, sigma: float, t: float, r: float) -> float:
    """Probability the expiry payoff is > 0, integrating over breakeven intervals."""
    bes = sorted(float(b) for b in structure.breakevens)
    edges = [0.0, *bes, math.inf]
    p = 0.0
    for lo, hi in zip(edges, edges[1:], strict=False):
        probe = hi / 2 if lo == 0.0 else (lo * 2 if math.isinf(hi) else (lo + hi) / 2)
        if payoff_at(structure.legs, Decimal(str(probe))) > 0:
            p += _lognormal_cdf(hi, spot, sigma, t, r) - _lognormal_cdf(lo, spot, sigma, t, r)
    return min(max(p, 0.0), 1.0)


def _flat_vol_value(structure: Structure, spot: float, sigma: float, t: float, r: float) -> float:
    """Per-share BSM value of the position (long +, short -) at a single vol."""
    total = 0.0
    for leg in structure.legs:
        occ = parse_occ(leg.occ_symbol)
        sign = 1 if leg.side.value == "long" else -1
        total += (
            sign
            * leg.ratio
            * price(BSMInputs(S=spot, K=float(occ.strike), t=t, r=r, sigma=sigma, flag=occ.kind))
        )
    return total


def profile_strategy_set(settings: ArcSettings) -> list[ScanStrategy]:
    """Every strategy the active account profile maps a stance to (profile order)."""
    st = settings.profile.stance_strategies
    out: list[ScanStrategy] = []
    for name in (*st.bullish, *st.bearish, *st.neutral):
        s = ScanStrategy(name)
        if s not in out:
            out.append(s)
    return out


def _candidate(
    ticker: str,
    strategy: ScanStrategy,
    structure: Structure,
    quotes: Mapping[str, OptionContract],
    *,
    spot: float,
    sigma: float,
    r: float,
    cost_model: CostModel | None = None,
) -> ScanCandidate:
    cm = cost_model or load_cost_model()
    legs = structure.legs
    contracts = [quotes[leg.occ_symbol] for leg in legs]
    credit = -float(structure.net_debit_credit)
    natural = 0.0
    entry_cost = 0.0  # $ per unit: slippage from mid + entry fees
    for leg, c in zip(legs, contracts, strict=True):
        assert c.bid is not None and c.ask is not None  # guaranteed by filters
        natural += (c.bid if leg.side.value == "short" else -c.ask) * leg.ratio
        side = -1 if leg.side.value == "short" else 1
        mid = (c.bid + c.ask) / 2
        fill = cm.fill(mid, c.ask - c.bid, side)
        entry_cost += abs(fill - mid) * leg.ratio * float(CONTRACT_MULTIPLIER)
        entry_cost += cm.trade_fees(leg.ratio, side, fill)
    shorts = [c for leg, c in zip(legs, contracts, strict=True) if leg.side.value == "short"]
    longs = [c for leg, c in zip(legs, contracts, strict=True) if leg.side.value == "long"]
    strikes: dict[str, list[float]] = defaultdict(list)
    for c in contracts:
        strikes[c.option_type].append(c.strike)
    width = max(max(v) - min(v) for v in strikes.values()) or None
    mult = float(CONTRACT_MULTIPLIER)
    t = structure.dte / 365.0
    model_value = _flat_vol_value(structure, spot, sigma, t, r)
    cost = entry_cost
    ev = (model_value + credit) * mult - cost
    max_loss = float(structure.max_loss) if structure.max_loss else None
    spreads = [(c.ask - c.bid) / ((c.ask + c.bid) / 2) for c in contracts]  # type: ignore[operator]
    structure = structure.model_copy(
        update={
            "liquidity": Liquidity(
                spread_pct=max(spreads),
                open_interest=min(c.open_interest or 0 for c in contracts),
                volume=min(c.volume or 0 for c in contracts),
            )
        }
    )
    return ScanCandidate(
        ticker=ticker,
        strategy=strategy,
        expiration=contracts[0].expiration,
        dte=structure.dte,
        structure=structure,
        short_deltas=[round(_abs_delta(c), 4) for c in sorted(shorts, key=_put_first)],
        long_deltas=[round(_abs_delta(c), 4) for c in sorted(longs, key=_put_first)],
        width=width,
        credit=round(credit, 4),
        natural_credit=round(natural, 4),
        credit_width=round(credit / width, 4) if credit > 0 and width else None,
        ev_ratio=round(ev / max_loss, 4) if max_loss else None,
        pop=round(_pop(structure, spot, sigma, t, r), 4),
        ev_proxy=round(ev, 2),
        cost=round(cost, 2),
        atm_iv=sigma,
        leg_spreads={
            leg.occ_symbol: round(c.ask - c.bid, 4)  # type: ignore[operator]
            for leg, c in zip(legs, contracts, strict=True)
        },
    )


def _put_first(c: OptionContract) -> tuple[bool, float]:
    return (c.option_type != "put", c.strike)


def _rank(cands: list[ScanCandidate], by: RankBy) -> list[ScanCandidate]:
    """Credit structures rank by credit/width (or EV); debit structures, which have
    no credit, rank by ``ev_ratio`` under ``credit_width``. In a mixed list the
    credit structures come first under ``credit_width``; ``ev`` ranks all by EV."""

    def key(c: ScanCandidate) -> tuple[int, float, float, str]:
        ratio = c.ev_ratio if c.ev_ratio is not None else -math.inf
        if c.credit_width is None:  # debit structure
            group, primary, secondary = 1, ratio, c.ev_proxy
            if by is RankBy.EV:
                group, primary, secondary = 0, c.ev_proxy, ratio
        elif by is RankBy.CREDIT_WIDTH:
            group, primary, secondary = 0, c.credit_width, c.ev_proxy
        else:
            group, primary, secondary = 0, c.ev_proxy, c.credit_width
        legs = ",".join(leg.occ_symbol for leg in c.structure.legs)
        return (group, -primary, -secondary, legs)

    ranked = sorted(cands, key=key)
    return [c.model_copy(update={"rank": i}) for i, c in enumerate(ranked, start=1)]


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _verticals(
    ticker: str,
    exp: dt.date,
    side: list[OptionContract],
    params: ScanParams,
    as_of: dt.date,
    market: MarketInputs,
) -> list[tuple[OptionContract, OptionContract, Structure]]:
    """(short, long, structure) credit verticals for one option type."""
    out: list[tuple[OptionContract, OptionContract, Structure]] = []
    shorts = select_shorts(
        side,
        target=params.target_delta,
        delta_min=params.delta_min,
        delta_max=params.delta_max,
        n=params.shorts_per_side,
    )
    for s in shorts:
        wing = select_wing(s, side, params.wing_width)
        if wing is None:
            continue
        assert s.mid is not None and wing.mid is not None
        try:
            st = credit_vertical(
                s.option_type,
                ticker,
                exp,
                short_strike=s.strike,
                short_premium=round(s.mid, 4),
                long_strike=wing.strike,
                long_premium=round(wing.mid, 4),
                as_of=as_of,
                market=market,
            )
        except ValueError as exc:  # e.g. crossed quotes price it at a non-credit
            log.debug("scanner.vertical_skipped", short=s.symbol, long=wing.symbol, err=str(exc))
            continue
        out.append((s, wing, st))
    return out


def _debit_structures(
    ticker: str,
    exp: dt.date,
    side: list[OptionContract],
    params: ScanParams,
    as_of: dt.date,
    market: MarketInputs,
    *,
    singles: bool,
    verticals: bool,
) -> tuple[list[Structure], list[Structure]]:
    """(long singles, debit verticals) for one option type and expiry."""
    longs = select_shorts(  # same band/target selection, applied to the long leg
        side,
        target=params.long_target_delta,
        delta_min=params.long_delta_min,
        delta_max=params.long_delta_max,
        n=params.longs_per_side,
    )
    one: list[Structure] = []
    two: list[Structure] = []
    for lg in longs:
        assert lg.mid is not None
        if singles:
            build = long_call if lg.option_type == "call" else long_put
            try:
                one.append(
                    build(ticker, exp, lg.strike, round(lg.mid, 4), as_of=as_of, market=market)
                )
            except ValueError as exc:
                log.debug("scanner.long_skipped", long=lg.symbol, err=str(exc))
        if not verticals:
            continue
        for sh in select_debit_short(
            lg,
            side,
            target=params.debit_short_target_delta,
            delta_min=params.debit_short_delta_min,
            delta_max=params.debit_short_delta_max,
            width=params.debit_width,
            n=params.debit_shorts_per_long,
        ):
            assert sh.mid is not None
            try:
                two.append(
                    debit_vertical(
                        lg.option_type,
                        ticker,
                        exp,
                        long_strike=lg.strike,
                        long_premium=round(lg.mid, 4),
                        short_strike=sh.strike,
                        short_premium=round(sh.mid, 4),
                        as_of=as_of,
                        market=market,
                    )
                )
            except ValueError as exc:
                log.debug("scanner.debit_skipped", long=lg.symbol, short=sh.symbol, err=str(exc))
    return one, two


def scan(
    provider: MarketDataProvider,
    ticker: str,
    params: ScanParams,
    *,
    as_of: dt.date,
    iv_history: Mapping[dt.date, float] | None = None,
) -> ScanResult:
    """Scan *ticker*'s chain and return ranked candidates for ``params.strategies``."""
    from arc.data.base import market_spot

    ticker = ticker.upper()
    found = market_spot(provider, ticker, as_of, max_spread_pct=params.spot_max_spread_pct)
    if found.price is None:  # E4.12: never price off a one-sided / missing quote
        msg = f"{ticker}: no usable spot (one-sided quote and no recent daily close)"
        raise NoSpotError(msg)
    spot = found.price
    exp_start = as_of + dt.timedelta(days=params.dte_min)
    exp_end = as_of + dt.timedelta(days=params.dte_max)
    chain = [
        c
        for c in provider.option_chain(ticker, exp_start, exp_end)
        if exp_start <= c.expiration <= exp_end
    ]
    expirations = sorted({c.expiration for c in chain})

    # IV context from the full (unfiltered) chain of the expiry nearest 30 DTE.
    iv_exp = min(
        expirations, key=lambda e: (abs(dte_calendar(as_of, e) - _ATM_TARGET_DTE), e), default=None
    )
    today_iv = atm_iv([c for c in chain if c.expiration == iv_exp], spot) if iv_exp else None
    iv = iv_stats(
        iv_history or {},
        as_of,
        today_iv,
        expiration=iv_exp,
        lookback=params.iv_lookback,
        min_obs=params.iv_min_obs,
    )

    liquid, report = apply_filters(chain, params.rules)
    # Builders emit compact OCC symbols; normalise chain symbols (padded or compact) to match.
    quotes = {parse_occ(c.symbol).format(): c for c in liquid}
    by_exp: dict[tuple[dt.date, str], list[OptionContract]] = defaultdict(list)
    for c in liquid:
        by_exp[(c.expiration, c.option_type)].append(c)
    # Per-leg IVs for net Greeks (liquid legs always carry an IV).
    market = MarketInputs(
        spot=spot,
        r=params.risk_free_rate,
        ivs={sym: c.implied_volatility for sym, c in quotes.items() if c.implied_volatility},
    )

    cands: list[ScanCandidate] = []
    for exp in expirations:
        exp_chain = [c for c in chain if c.expiration == exp]
        sigma = atm_iv(exp_chain, spot)
        if sigma is None:
            continue
        puts = _verticals(ticker, exp, by_exp[(exp, "put")], params, as_of, market)
        calls = _verticals(ticker, exp, by_exp[(exp, "call")], params, as_of, market)

        def add(strategy: ScanStrategy, st: Structure, sig: float = sigma) -> None:
            cands.append(
                _candidate(
                    ticker,
                    strategy,
                    st,
                    quotes,
                    spot=spot,
                    sigma=sig,
                    r=params.risk_free_rate,
                    cost_model=params.cost,
                )
            )

        if ScanStrategy.BULL_PUT in params.strategies:
            for _, _, st in puts:
                add(ScanStrategy.BULL_PUT, st)
        if ScanStrategy.BEAR_CALL in params.strategies:
            for _, _, st in calls:
                add(ScanStrategy.BEAR_CALL, st)
        if ScanStrategy.IRON_CONDOR in params.strategies:
            for ps, pl, _ in puts:
                for cs, cl, _ in calls:
                    if ps.strike >= cs.strike:
                        continue
                    try:
                        st = iron_condor(
                            ticker,
                            exp,
                            long_put_strike=pl.strike,
                            long_put_premium=round(pl.mid or 0, 4),
                            short_put_strike=ps.strike,
                            short_put_premium=round(ps.mid or 0, 4),
                            short_call_strike=cs.strike,
                            short_call_premium=round(cs.mid or 0, 4),
                            long_call_strike=cl.strike,
                            long_call_premium=round(cl.mid or 0, 4),
                            as_of=as_of,
                            market=market,
                        )
                    except ValueError as exc:
                        log.debug("scanner.condor_skipped", err=str(exc))
                        continue
                    add(ScanStrategy.IRON_CONDOR, st)
        for opt, single, vert in (
            ("call", ScanStrategy.LONG_CALL, ScanStrategy.BULL_CALL_DEBIT),
            ("put", ScanStrategy.LONG_PUT, ScanStrategy.BEAR_PUT_DEBIT),
        ):
            want_one, want_two = single in params.strategies, vert in params.strategies
            if not (want_one or want_two):
                continue
            ones, twos = _debit_structures(
                ticker,
                exp,
                by_exp[(exp, opt)],
                params,
                as_of,
                market,
                singles=want_one,
                verticals=want_two,
            )
            for st in ones:
                add(single, st)
            for st in twos:
                add(vert, st)

    ranked = _rank(cands, params.rank_by)
    if params.top is not None:
        ranked = ranked[: params.top]
    log.info(
        "scanner.scan",
        ticker=ticker,
        as_of=as_of.isoformat(),
        contracts=report.total,
        liquid=report.kept,
        candidates=len(cands),
        atm_iv=iv.atm_iv,
        iv_rank=iv.iv_rank,
    )
    return ScanResult(
        ticker=ticker,
        as_of=as_of,
        spot=spot,
        spot_basis=found.basis or "mid",
        params=params,
        expirations=expirations,
        iv=iv,
        filter_report=report,
        candidates=ranked,
    )
