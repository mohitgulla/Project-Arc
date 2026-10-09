"""Risk Proxy Gate rules engine (PLAN §2.1, §5; card E3.1).

Every rule is a pure function of its arguments: no clock reads, no I/O, no
network, no LLM. The caller passes ``now`` explicitly. :func:`evaluate` runs
*every* rule and enumerates every violation (no short-circuit); a rule that
raises is itself reported as a violation, so the gate fails closed.

Money is ``Decimal`` dollars; option prices are per share; Greeks are
share-equivalents (see :mod:`arc.structures`). The gate re-derives the
structure's facts (underlying, expiry, kind, max loss) from the legs instead
of trusting persona-supplied numbers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Sequence
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel, ConfigDict

from arc.account_profiles import BuyingPower, DayTradeRule, ShortLegPolicy
from arc.config import ArcSettings, StructureKind
from arc.gate.band import PriceBand, as_grid, band_from_nbbo
from arc.gate.ticks import TickGrid, order_grid
from arc.models import GateDecision, Leg, LegIntent, Proposal
from arc.models import StructureKind as ModelKind
from arc.structures import (
    classify,
    is_defined_risk,
    max_gain_loss,
    net_debit_credit,
    parse_occ,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from arc.gate.inputs import AccountSnapshot, MarketSnapshot, Portfolio

__all__ = [
    "CAPACITY_CODES",
    "CapacityRejection",
    "Derived",
    "RuleCode",
    "Violation",
    "capacity_rejection",
    "check_account_profile",
    "check_approval_ttl",
    "check_band",
    "check_closing",
    "check_daily_loss",
    "check_data_freshness",
    "check_day_trades",
    "check_dte_window",
    "check_earnings_blackout",
    "check_greek_caps",
    "check_greeks_present",
    "check_halt",
    "check_max_open_positions",
    "check_max_gain",
    "check_per_underlying",
    "check_spread_tick",
    "check_structure_whitelist",
    "check_wash_sale",
    "combo_nbbo",
    "derive",
    "evaluate",
    "max_gain_cap",
    "price_band",
    "price_ceiling",
    "proposal_band",
    "proposal_hash",
]

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")
_ONE = Decimal(1)


class RuleCode(StrEnum):
    """Stable machine-readable violation codes."""

    STRUCTURE_INVALID = "structure_invalid"
    PER_UNDERLYING = "per_underlying_limit"
    DAILY_LOSS = "daily_loss_halt"
    HALTED = "halted"
    SPREAD = "spread_too_wide"
    LIMIT_OUTSIDE_NBBO = "limit_outside_nbbo"
    TICK = "limit_off_tick"
    WASH_SALE = "wash_sale"
    DELTA_CAP = "portfolio_delta_cap"
    BETA_DELTA_CAP = "portfolio_beta_delta_cap"  # D62 (E3.6)
    VEGA_CAP = "portfolio_vega_cap"
    STRUCTURE_NOT_ALLOWED = "structure_not_allowed"
    DTE_WINDOW = "dte_window"
    EARNINGS_BLACKOUT = "earnings_blackout"
    MAX_POSITIONS = "max_open_positions"
    APPROVAL_TTL = "approval_ttl"
    STALE_DATA = "stale_data"
    NO_MAX_GAIN = "limit_no_max_gain"
    BAND = "price_band"
    CLOSE_MISMATCH = "close_mismatch"
    MISSING_GREEKS = "missing_greeks"
    MISSING_SPOT = "missing_spot"
    # D25 account profile (one code per sub-check; E7.4 reason codes reuse them)
    ACCOUNT_KIND = "account_profile_kind"
    ACCOUNT_NET_DEBIT = "account_profile_net_debit"
    ACCOUNT_SHORT_LEG = "account_profile_short_leg"
    ACCOUNT_CASH = "account_profile_settled_cash"
    # D32 daily options order budget (E6.5)
    ORDER_BUDGET = "order_budget"
    # E10.2 day-trade limit of the account profile (PDT / good-faith parity)
    DAY_TRADES = "account_profile_day_trades"
    RULE_ERROR = "rule_error"


class Violation(BaseModel):
    """One failed rule. ``str(v)`` is the form stored in ``GateDecision.violations``."""

    model_config = ConfigDict(frozen=True)

    code: RuleCode
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class Derived(NamedTuple):
    """Facts the gate derives from the proposal's legs (never persona-supplied)."""

    underlying: str
    expiration: dt.date
    kind: ModelKind
    defined_risk: bool
    max_loss_total: Decimal | None  # dollars for sizing.contracts units; None = unbounded
    net_price: Decimal  # per-share net: + debit / - credit (structure's own mid)
    limit_price: Decimal  # per-share limit the order will carry
    # per-share price at which max gain hits 0 (the most the structure can be worth
    # at expiry, net of the other legs); a limit must stay strictly below it.
    # None = unbounded max gain.
    price_ceiling: Decimal | None = None


Rule = Callable[[], list[Violation]]


def _v(code: RuleCode, detail: str) -> list[Violation]:
    return [Violation(code=code, detail=detail)]


def _d(x: float | int) -> Decimal:
    return Decimal(str(x))


# ---------------------------------------------------------------------------
# Hash + derivation
# ---------------------------------------------------------------------------


def proposal_hash(proposal: Proposal) -> str:
    """SHA-256 over the canonical JSON of the proposal (sorted keys, no spaces)."""
    payload = json.dumps(
        proposal.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def derive(proposal: Proposal) -> Derived:
    """Re-derive structure facts from the legs. Raises ``ValueError`` if malformed."""
    legs = proposal.structure.legs
    kind = classify(legs)  # validates: legs present, premiums set, single root/expiry
    occ = parse_occ(legs[0].occ_symbol)
    max_gain, max_loss = max_gain_loss(legs)
    net = net_debit_credit(legs)
    contracts = Decimal(proposal.sizing.contracts)
    return Derived(
        underlying=occ.root,
        expiration=occ.expiration,
        kind=kind,
        defined_risk=is_defined_risk(legs),
        max_loss_total=None if max_loss is None else max_loss * contracts,
        net_price=net,
        limit_price=net if proposal.limit_price is None else proposal.limit_price,
        price_ceiling=None if max_gain is None else net + max_gain / _HUNDRED,
    )


def price_ceiling(legs: Sequence[Leg]) -> Decimal | None:
    """Per-share price at which the structure's max gain is 0; ``None`` = unbounded gain.

    Max gain at a limit ``P`` is ``max_gain(mid) − (P − mid)·100`` per unit, so it
    is positive iff ``P < mid + max_gain(mid)/100``. The ceiling depends only on
    the strikes (e.g. the width of a debit vertical; ``0`` for a credit vertical,
    whose credit must stay positive), not on the quotes.
    """
    max_gain, _ = max_gain_loss(legs)
    return None if max_gain is None else net_debit_credit(legs) + max_gain / _HUNDRED


def max_gain_cap(legs: Sequence[Leg], grid: TickGrid | Decimal) -> Decimal | None:
    """Worst on-grid limit that still leaves max gain > 0: the last grid price below the ceiling."""
    ceiling = price_ceiling(legs)
    if ceiling is None:
        return None
    below = (ceiling / _CENT).to_integral_value(rounding=ROUND_CEILING) * _CENT - _CENT
    return as_grid(grid).snap(below, "down")


def grid_for(legs: Sequence[Leg], market: MarketSnapshot, config: ArcSettings) -> TickGrid:
    """D66: the exchange price grid for an order of *legs* (``ppind`` from *market*)."""
    return order_grid(legs, config.ticks, market.penny_program)


def check_max_gain(d: Derived) -> list[Violation]:
    """The limit must leave max gain > 0: never pay the structure's full value or more
    (a debit vertical above its width, a credit vertical for no credit)."""
    if d.price_ceiling is not None and d.limit_price >= d.price_ceiling:
        return _v(
            RuleCode.NO_MAX_GAIN,
            f"limit {d.limit_price} leaves max gain <= 0 (must stay below {d.price_ceiling})",
        )
    return []


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def check_per_underlying(
    d: Derived, account: AccountSnapshot, portfolio: Portfolio, config: ArcSettings
) -> list[Violation]:
    """Existing + new max loss on one underlying ≤ ``max_alloc_pct`` × equity."""
    if d.max_loss_total is None:
        return _v(RuleCode.PER_UNDERLYING, "max loss is unbounded")
    existing = sum((p.max_loss for p in portfolio.positions if p.underlying == d.underlying), _ZERO)
    total = existing + d.max_loss_total
    limit = _d(config.max_alloc_pct) * account.equity
    if total > limit:
        return _v(
            RuleCode.PER_UNDERLYING,
            f"{d.underlying} max loss {total} (existing {existing} + new {d.max_loss_total}) "
            f"> {limit} ({config.max_alloc_pct:.2%} of equity {account.equity})",
        )
    return []


def check_daily_loss(account: AccountSnapshot, config: ArcSettings) -> list[Violation]:
    """Day loss vs previous close must stay below ``daily_loss_halt_pct``."""
    if account.last_equity <= 0:
        return _v(RuleCode.DAILY_LOSS, f"invalid start-of-day equity {account.last_equity}")
    loss_pct = (account.last_equity - account.equity) / account.last_equity
    if loss_pct >= _d(config.daily_loss_halt_pct):
        return _v(
            RuleCode.DAILY_LOSS,
            f"day loss {loss_pct:.4%} >= halt threshold {config.daily_loss_halt_pct:.2%}",
        )
    return []


def check_halt(account: AccountSnapshot) -> list[Violation]:
    """Kill switch / daily halt must not be active."""
    if account.halted:
        return _v(RuleCode.HALTED, "trading is halted")
    return []


def check_order_budget(
    account: AccountSnapshot, config: ArcSettings, *, attempts: int, closing: bool
) -> list[Violation]:
    """D32: ``used + worst-case attempts`` must fit the daily options order budget.

    An open must fit under ``order_budget_daily_max - order_budget_close_reserve``;
    a close under ``order_budget_daily_max``. *attempts* is the ladder's worst
    case (the band's attempts; the caller passes the tier-adjusted band). Skipped
    when the snapshot carries no count (``orders_used_today is None``).
    """
    used = account.orders_used_today
    if used is None:
        return []
    cap = config.order_budget_daily_max
    if not closing:
        cap -= config.order_budget_close_reserve
    if used + attempts > cap:
        what = "close" if closing else "open"
        return _v(
            RuleCode.ORDER_BUDGET,
            f"{what} needs {attempts} order(s) but {used} of {cap} used today "
            f"(daily max {config.order_budget_daily_max}"
            + ("" if closing else f", close reserve {config.order_budget_close_reserve}")
            + ")",
        )
    return []


def check_day_trades(
    proposal: Proposal, account: AccountSnapshot, portfolio: Portfolio, config: ArcSettings
) -> list[Violation]:
    """E10.2: a same-day close must fit the profile's day-trade limit.

    Applies only to closes (the caller runs it with ``closing=True``) under a
    ``pattern_day_trader`` profile while equity is below ``min_equity``. A close
    is a day trade when any of its legs was opened this ET day
    (``portfolio.opened_today``); it fails when ``day_trades_used + 1`` exceeds
    ``max_day_trades``. Skipped when the count is unknown (``None``).
    """
    rule = config.profile.day_trades
    if rule.rule is not DayTradeRule.PATTERN_DAY_TRADER or account.day_trades_used is None:
        return []
    if account.equity >= rule.min_equity:
        return []
    if not any(leg.occ_symbol in portfolio.opened_today for leg in proposal.structure.legs):
        return []
    if account.day_trades_used + 1 > rule.max_day_trades:
        return _v(
            RuleCode.DAY_TRADES,
            f"same-day close would be day trade {account.day_trades_used + 1} in "
            f"{rule.window_sessions} sessions (max {rule.max_day_trades} below equity "
            f"${rule.min_equity})",
        )
    return []


def check_spread_tick(
    proposal: Proposal, d: Derived, market: MarketSnapshot, config: ArcSettings
) -> list[Violation]:
    """Each leg's spread ≤ 10% of mid or ≤ $0.10; limit inside the combo NBBO; on tick.

    Combo NBBO (per share, + = debit): ``low = Σ long·bid − Σ short·ask`` and
    ``high = Σ long·ask − Σ short·bid``; the limit must satisfy low ≤ limit ≤ high.
    """
    out: list[Violation] = []
    low = high = _ZERO
    complete = True
    for leg in proposal.structure.legs:
        q = market.quotes.get(leg.occ_symbol)
        if q is None:
            out += _v(RuleCode.SPREAD, f"no quote for {leg.occ_symbol}")
            complete = False
            continue
        if q.bid > q.ask:
            out += _v(RuleCode.SPREAD, f"crossed quote for {leg.occ_symbol}: {q.bid} > {q.ask}")
            complete = False
            continue
        spread = q.ask - q.bid
        mid = (q.ask + q.bid) / 2
        pct_ok = mid > 0 and spread <= _d(config.spread_max_pct) * mid
        if not (pct_ok or spread <= _d(config.spread_max_abs)):
            out += _v(
                RuleCode.SPREAD,
                f"{leg.occ_symbol} spread {spread} > max({config.spread_max_pct:.0%} of mid "
                f"{mid}, ${config.spread_max_abs})",
            )
        r = Decimal(leg.ratio)
        if leg.side == LegIntent.LONG:
            low += r * q.bid
            high += r * q.ask
        else:
            low -= r * q.ask
            high -= r * q.bid
    if complete and not low <= d.limit_price <= high:
        out += _v(
            RuleCode.LIMIT_OUTSIDE_NBBO,
            f"limit {d.limit_price} outside combo NBBO [{low}, {high}]",
        )
    grid = grid_for(proposal.structure.legs, market, config)
    tick = grid.tick_at(d.limit_price)
    if not grid.on_grid(d.limit_price):
        out += _v(
            RuleCode.TICK,
            f"limit {d.limit_price} is not a multiple of tick {tick} "
            f"({grid.describe(d.limit_price)})",
        )
    return out


def combo_nbbo(legs: Sequence[Leg], market: MarketSnapshot) -> tuple[Decimal, Decimal] | None:
    """Combo NBBO ``(low, high)`` per share (+ = debit); ``None`` if a leg quote is missing/crossed.

    ``high`` is the far touch for the buyer of the combo: the worst limit a band may reach.
    """
    low = high = _ZERO
    for leg in legs:
        q = market.quotes.get(leg.occ_symbol)
        if q is None or q.bid > q.ask:
            return None
        r = Decimal(leg.ratio)
        if leg.side == LegIntent.LONG:
            low, high = low + r * q.bid, high + r * q.ask
        else:
            low, high = low - r * q.ask, high - r * q.bid
    return low, high


def price_band(
    legs: Sequence[Leg], limit: Decimal, market: MarketSnapshot, config: ArcSettings
) -> PriceBand:
    """D24 band from *limit* toward the far touch of the legs' combo NBBO.

    ``max_steps = config.execution_improvement_steps``; the worst price is
    ``execution_band_reach`` of the way to the far touch, capped at
    :func:`max_gain_cap` so no step can leave max gain <= 0 (a debit vertical
    never priced at or above its width, a credit never at or below zero).
    Without a complete NBBO (or with an off-grid limit) there is no room: a
    one-attempt band at the limit (the gate's own spread/tick checks then
    decide). Every band price is on the order's exchange grid (D66,
    :func:`grid_for`), so the ``arc2`` token is minted on grid-valid prices. Pure.
    """
    nbbo = combo_nbbo(legs, market)
    grid = grid_for(legs, market, config)
    if limit % _CENT:
        msg = f"limit {limit} is not whole cents: round it to the tick before banding"
        raise ValueError(msg)
    if nbbo is None or not grid.on_grid(limit):
        return PriceBand(lo=limit, hi=limit, max_steps=0)
    return band_from_nbbo(
        limit,
        nbbo[1],
        max_steps=config.execution_improvement_steps,
        reach=_d(config.execution_band_reach),
        grid=grid,
        cap=max_gain_cap(legs, grid),
    )


def proposal_band(proposal: Proposal, market: MarketSnapshot, config: ArcSettings) -> PriceBand:
    """:func:`price_band` at the proposal's limit (default: the structure's mid)."""
    st = proposal.structure
    limit = st.net_debit_credit if proposal.limit_price is None else proposal.limit_price
    return price_band(st.legs, limit, market, config)


def worst_case(d: Derived, band: PriceBand, contracts: int) -> Derived:
    """``d`` re-derived at the band's worst limit: paying ``hi − net`` more per share adds
    exactly that (× 100 × contracts) to a defined-risk structure's max loss."""
    extra = (band.hi - d.net_price) * _HUNDRED * contracts
    worst = None if d.max_loss_total is None else max(d.max_loss_total + extra, _ZERO)
    return d._replace(limit_price=band.hi, max_loss_total=worst)


def check_band(
    proposal: Proposal,
    d: Derived,
    band: PriceBand,
    market: MarketSnapshot,
    config: ArcSettings,
) -> list[Violation]:
    """D24: the band starts at the proposal's limit, its steps are within the configured
    maximum, and its worst price passes the combo-NBBO and tick checks too and still
    leaves max gain > 0 (e.g. a $1-wide debit vertical is never worked at >= 1.00).

    (The 5% per-underlying cap at the worst price is checked by
    :func:`check_per_underlying` on :func:`worst_case`, see :func:`evaluate`.)
    """
    out: list[Violation] = []
    if band.lo != d.limit_price:
        out += _v(RuleCode.BAND, f"band starts at {band.lo}, not at the limit {d.limit_price}")
    if band.max_steps > config.execution_improvement_steps:
        out += _v(
            RuleCode.BAND,
            f"{band.max_steps} steps > max {config.execution_improvement_steps}",
        )
    worst = d._replace(limit_price=band.hi)
    for v in check_spread_tick(proposal, worst, market, config):
        if v.code in (RuleCode.LIMIT_OUTSIDE_NBBO, RuleCode.TICK):
            out += _v(RuleCode.BAND, f"worst price: {v.detail}")
    for v in check_max_gain(worst):
        out += _v(RuleCode.BAND, f"worst price: {v.detail}")
    return out


def check_closing(proposal: Proposal, portfolio: Portfolio) -> list[Violation]:
    """A closing order may only reduce held legs: sell ≤ held long, buy ≤ held short."""
    out: list[Violation] = []
    n = proposal.sizing.contracts
    for leg in proposal.structure.legs:
        held = portfolio.legs.get(leg.occ_symbol, 0)
        qty = leg.ratio * n
        closes = held <= -qty if leg.side == LegIntent.LONG else held >= qty
        if not closes:
            verb = "buy" if leg.side == LegIntent.LONG else "sell"
            out += _v(
                RuleCode.CLOSE_MISMATCH,
                f"{verb} {qty} {leg.occ_symbol} does not close a held position ({held:+d})",
            )
    return out


def check_wash_sale(
    d: Derived, portfolio: Portfolio, config: ArcSettings, now: dt.datetime
) -> list[Violation]:
    """No re-open within ``wash_sale_days`` of a loss close on the same underlying."""
    window = dt.timedelta(days=config.wash_sale_days)
    hits = [
        lot
        for lot in portfolio.closed_lots
        if lot.underlying == d.underlying and lot.realized_pnl < 0 and now - lot.closed_at <= window
    ]
    if hits:
        last = max(lot.closed_at for lot in hits)
        return _v(
            RuleCode.WASH_SALE,
            f"{d.underlying} closed at a loss {last.isoformat()} (within {config.wash_sale_days}d)",
        )
    return []


def check_greek_caps(
    proposal: Proposal,
    account: AccountSnapshot,
    portfolio: Portfolio,
    market: MarketSnapshot,
    config: ArcSettings,
) -> list[Violation]:
    """Post-trade |$Δ| ≤ pct × equity; |β$Δ| ≤ pct × equity; |ν| ≤ pct × equity ($/vol-pt).

    Dollar delta (D57) = the book's ``dollar_delta`` + the proposal's Δ (share-eq) ×
    contracts × its underlying's spot. Beta-weighted dollar delta (D62) = the book's
    ``beta_dollar_delta`` + the same proposal term × β used, where β used is
    ``market.underlying_beta[root]`` floored at 1.0 (a missing β is 1.0, never a
    rejection). A missing or non-positive spot fails closed (``missing_spot``) and
    skips both delta caps; the vega check still runs.
    """
    out: list[Violation] = []
    n = proposal.sizing.contracts
    g = proposal.structure.greeks
    root = parse_occ(proposal.structure.legs[0].occ_symbol).root
    spot = market.underlying_spot.get(root)
    if spot is None or spot <= 0:
        out += _v(RuleCode.MISSING_SPOT, f"no spot for {root}: dollar delta unknown")
    else:
        added = _d(g.delta) * n * spot
        dollar_delta = portfolio.dollar_delta + added
        delta_cap = _d(config.portfolio_dollar_delta_cap_pct) * account.equity
        if abs(dollar_delta) > delta_cap:
            out += _v(
                RuleCode.DELTA_CAP,
                f"post-trade |$Δ| ${abs(dollar_delta):,.2f} > cap ${delta_cap:,.2f}",
            )
        beta = max(market.underlying_beta.get(root) or _ONE, _ONE)
        beta_delta = portfolio.beta_dollar_delta + added * beta
        beta_cap = _d(config.portfolio_beta_delta_cap_pct) * account.equity
        if abs(beta_delta) > beta_cap:
            out += _v(
                RuleCode.BETA_DELTA_CAP,
                f"post-trade |β$Δ| ${abs(beta_delta):,.2f} > cap ${beta_cap:,.2f} "
                f"({root} β {beta:.2f})",
            )
    # vega is per 1.00 of sigma in share-equivalents -> /100 = dollars per vol point
    vega_usd = _d(portfolio.greeks.vega + g.vega * n) / _HUNDRED
    vega_cap = _d(config.portfolio_vega_cap_pct) * account.equity
    if abs(vega_usd) > vega_cap:
        out += _v(
            RuleCode.VEGA_CAP, f"post-trade |ν| ${abs(vega_usd):.2f}/vol-pt > cap ${vega_cap:.2f}"
        )
    return out


def check_greeks_present(proposal: Proposal) -> list[Violation]:
    """A multi-leg opening structure must carry computed Greeks (PLAN §6.8).

    All-zero Δ/Γ/ν/Θ means re-pricing had no implied volatility, not a flat
    position: the Greek caps would see no contribution and pass. Fail closed.
    """
    legs = proposal.structure.legs
    g = proposal.structure.greeks
    if len(legs) > 1 and g.delta == g.gamma == g.vega == g.theta == 0:
        return _v(
            RuleCode.MISSING_GREEKS,
            f"{len(legs)}-leg structure has all-zero Greeks (no IV at re-pricing)",
        )
    return []


_KIND_MAP: dict[ModelKind, StructureKind] = {
    ModelKind.LONG_CALL: StructureKind.LONG_CALL,
    ModelKind.LONG_PUT: StructureKind.LONG_PUT,
    ModelKind.VERTICAL_DEBIT: StructureKind.VERTICAL_DEBIT,
    ModelKind.VERTICAL_CREDIT: StructureKind.VERTICAL_CREDIT,
    ModelKind.IRON_CONDOR: StructureKind.IRON_CONDOR,
}


def _uncovered_shorts(legs: Sequence[Leg]) -> list[str]:
    """Short legs not covered by a long leg of the same type and expiry worth at least as
    much at any underlying price (call: long strike <= short; put: long strike >= short).

    Ratios count as units. Shorts are matched hardest-first (calls by ascending strike,
    puts by descending), each to any still-free long that covers it; a free long that
    covers the hardest short also covers every easier one, so the greedy match is exact.
    """
    groups: dict[tuple[str, dt.date], tuple[list[tuple[Decimal, str]], list[Decimal]]] = {}
    for leg in legs:
        occ = parse_occ(leg.occ_symbol)
        # Normalise puts onto the call axis (negate strikes): cover = long <= short.
        k = occ.strike if occ.kind.value == "c" else -occ.strike
        shorts, longs = groups.setdefault((occ.kind.value, occ.expiration), ([], []))
        if leg.side == LegIntent.SHORT:
            shorts.extend([(k, leg.occ_symbol)] * leg.ratio)
        else:
            longs.extend([k] * leg.ratio)
    out: list[str] = []
    for shorts, longs in groups.values():
        free = sorted(longs)
        for k, sym in sorted(shorts):
            if free and free[0] <= k:
                free.pop(0)
            else:
                out.append(sym)
    return out


def check_account_profile(
    proposal: Proposal, d: Derived, account: AccountSnapshot, config: ArcSettings
) -> list[Violation]:
    """D25: the structure is one the active account profile can open.

    * kind in ``allowed_kinds``;
    * ``require_net_debit``: the structure and its limit are a net debit (> 0);
    * ``allow_short_legs``: ``none`` = no short leg; ``covered_only`` = every short
      covered (:func:`_uncovered_shorts`); ``any`` = no check;
    * ``cash_settled``: ``limit × 100 × contracts + fees <= settled cash``. Pass the
      band's worst case (:func:`worst_case`) so every ladder step fits. Fees are the
      conservative ``gate_fee_per_leg_contract`` × legs × contracts. Unknown settled
      cash fails closed.

    A settings object whose profile is unresolved raises, so :func:`evaluate`
    reports ``rule_error`` (fail closed).
    """
    prof = config.profile
    out: list[Violation] = []
    if d.kind not in prof.allowed_kinds:
        allowed = ", ".join(k.value for k in prof.allowed_kinds)
        out += _v(
            RuleCode.ACCOUNT_KIND,
            f"{d.kind} is not allowed under profile {prof.name} (allowed: {allowed})",
        )
    if prof.require_net_debit and (d.net_price <= 0 or d.limit_price <= 0):
        out += _v(
            RuleCode.ACCOUNT_NET_DEBIT,
            f"profile {prof.name} requires a net debit; net {d.net_price}, limit {d.limit_price}",
        )
    legs = proposal.structure.legs
    if prof.allow_short_legs is ShortLegPolicy.NONE:
        shorts = [leg.occ_symbol for leg in legs if leg.side == LegIntent.SHORT]
        if shorts:
            out += _v(
                RuleCode.ACCOUNT_SHORT_LEG,
                f"profile {prof.name} allows no short legs: {', '.join(shorts)}",
            )
    elif prof.allow_short_legs is ShortLegPolicy.COVERED_ONLY:
        bare = _uncovered_shorts(legs)
        if bare:
            out += _v(
                RuleCode.ACCOUNT_SHORT_LEG,
                f"uncovered short leg(s) under profile {prof.name}: {', '.join(bare)}",
            )
    if prof.buying_power is BuyingPower.CASH_SETTLED:
        n = proposal.sizing.contracts
        units = sum(leg.ratio for leg in legs) * n
        need = max(d.limit_price, _ZERO) * _HUNDRED * n + _d(
            config.gate_fee_per_leg_contract
        ) * Decimal(units)
        if account.settled_cash is None:
            out += _v(RuleCode.ACCOUNT_CASH, "settled cash unknown (cash_settled profile)")
        elif need > account.settled_cash:
            out += _v(
                RuleCode.ACCOUNT_CASH,
                f"needs ${need} (limit {d.limit_price} × 100 × {n} + fees) > settled cash "
                f"${account.settled_cash}",
            )
    return out


def check_structure_whitelist(
    proposal: Proposal, d: Derived, config: ArcSettings
) -> list[Violation]:
    """Leg geometry must classify into the whitelist, be defined-risk, and match any label."""
    out: list[Violation] = []
    allowed = _KIND_MAP.get(d.kind)
    if allowed is None or allowed not in config.structure_whitelist:
        out += _v(RuleCode.STRUCTURE_NOT_ALLOWED, f"structure kind {d.kind} is not whitelisted")
    if not d.defined_risk:
        out += _v(RuleCode.STRUCTURE_NOT_ALLOWED, "structure is not defined-risk")
    labelled = proposal.structure.kind
    if labelled is not None and labelled != d.kind:
        out += _v(
            RuleCode.STRUCTURE_NOT_ALLOWED,
            f"labelled kind {labelled} does not match leg geometry {d.kind}",
        )
    return out


def check_dte_window(d: Derived, config: ArcSettings, now: dt.datetime) -> list[Violation]:
    """Calendar DTE (from the legs, as of ``now``) inside the entry window; never 0DTE.

    The window is the account profile's ``dte_min/dte_max`` override when it sets
    one (D25), else ``dte_min/dte_max`` (:attr:`ArcSettings.entry_dte_window`).
    """
    lo, hi = config.entry_dte_window
    dte = (d.expiration - now.astimezone(ET).date()).days
    if dte < 1 or not lo <= dte <= hi:
        return _v(RuleCode.DTE_WINDOW, f"DTE {dte} outside [{max(lo, 1)}, {hi}]")
    return []


def check_earnings_blackout(
    proposal: Proposal,
    d: Derived,
    market: MarketSnapshot,
    config: ArcSettings,
    now: dt.datetime,
) -> list[Violation]:
    """No net-credit (short premium) structure held through earnings.

    Exempt only if Research flagged an earnings play *and* Risk concurs.
    Unknown earnings date (underlying missing from the snapshot) fails closed.
    """
    if not config.earnings_blackout or d.net_price >= 0:
        return []
    if proposal.earnings_play and proposal.risk_concurs:
        return []
    if d.underlying not in market.next_earnings:
        return _v(RuleCode.EARNINGS_BLACKOUT, f"earnings date unknown for {d.underlying}")
    earnings = market.next_earnings[d.underlying]
    if earnings is not None and now.astimezone(ET).date() <= earnings <= d.expiration:
        return _v(
            RuleCode.EARNINGS_BLACKOUT,
            f"short premium through {d.underlying} earnings on {earnings.isoformat()}",
        )
    return []


def check_max_open_positions(portfolio: Portfolio, config: ArcSettings) -> list[Violation]:
    """Open positions + this one ≤ ``max_open_positions``."""
    n = len(portfolio.positions) + 1
    if n > config.max_open_positions:
        return _v(RuleCode.MAX_POSITIONS, f"{n} positions > max {config.max_open_positions}")
    return []


def check_approval_ttl(
    proposal: Proposal, config: ArcSettings, now: dt.datetime
) -> list[Violation]:
    """Proposal not expired, and its expiry is no further out than the approval TTL."""
    remaining = proposal.expires_at - now
    if remaining <= dt.timedelta(0):
        return _v(RuleCode.APPROVAL_TTL, f"proposal expired at {proposal.expires_at.isoformat()}")
    if remaining > dt.timedelta(seconds=config.approval_ttl_seconds):
        return _v(
            RuleCode.APPROVAL_TTL,
            f"expiry {proposal.expires_at.isoformat()} exceeds TTL {config.approval_ttl_seconds}s",
        )
    return []


def _age_violation(
    label: str, as_of: dt.datetime, max_age: int, now: dt.datetime
) -> list[Violation]:
    age = (now - as_of).total_seconds()
    if age < 0:
        return _v(RuleCode.STALE_DATA, f"{label} timestamp {as_of.isoformat()} is in the future")
    if age > max_age:
        return _v(RuleCode.STALE_DATA, f"{label} is {age:.0f}s old (max {max_age}s)")
    return []


def check_data_freshness(
    proposal: Proposal,
    account: AccountSnapshot,
    market: MarketSnapshot,
    config: ArcSettings,
    now: dt.datetime,
) -> list[Violation]:
    """Account snapshot and every leg quote are present and recent."""
    out = _age_violation("account snapshot", account.as_of, config.account_max_age_seconds, now)
    for leg in proposal.structure.legs:
        q = market.quotes.get(leg.occ_symbol)
        if q is None:
            out += _v(RuleCode.STALE_DATA, f"no quote for {leg.occ_symbol}")
        else:
            out += _age_violation(
                f"quote {leg.occ_symbol}", q.as_of, config.quote_max_age_seconds, now
            )
    return out


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def _run(name: str, rule: Rule) -> list[Violation]:
    try:
        return rule()
    except Exception as exc:  # noqa: BLE001 — fail closed: any rule error is a violation
        return _v(RuleCode.RULE_ERROR, f"{name}: {type(exc).__name__}: {exc}")


# Capacity codes (E6.4, D19): a proposal failing ONLY on these would pass if an open
# position were closed to free room. Everything else (halt, band, profile, data) is
# not fixable by reallocating, so it never qualifies.
CAPACITY_CODES: frozenset[RuleCode] = frozenset(
    {RuleCode.PER_UNDERLYING, RuleCode.MAX_POSITIONS, RuleCode.ACCOUNT_CASH}
)


class CapacityRejection(StrEnum):
    """Typed ``rejected_for`` reason of a capacity-only gate failure (E6.4)."""

    BUYING_POWER = "buying_power"  # settled cash (D25 cash_debit) or per-underlying budget
    PORTFOLIO_CAP = "portfolio_cap"  # max open positions
    # D32: the daily order budget is used up. Typed like the capacity reasons so the
    # audit rows carry it, but closing a position frees no orders (it costs some), so
    # a close-to-reallocate swap never pairs it (:func:`arc.positions.reallocate._frees`).
    ORDER_BUDGET = "order_budget"


def capacity_rejection(violations: Sequence[str]) -> CapacityRejection | None:
    """``rejected_for`` for a failed decision, or ``None`` when a non-capacity rule failed.

    *violations* are the stored ``"<code>: <detail>"`` strings of a
    :class:`GateDecision`. Pure: reads nothing but its argument. An unbounded max
    loss also reports ``per_underlying_limit``; callers only pair bounded structures.
    """
    codes: set[str] = {v.split(":", 1)[0].strip() for v in violations}
    if codes == {RuleCode.ORDER_BUDGET.value}:
        return CapacityRejection.ORDER_BUDGET
    if not codes or not codes <= {c.value for c in CAPACITY_CODES}:
        return None
    if codes == {RuleCode.MAX_POSITIONS.value}:
        return CapacityRejection.PORTFOLIO_CAP
    return CapacityRejection.BUYING_POWER


def evaluate(
    proposal: Proposal,
    account_snapshot: AccountSnapshot,
    portfolio: Portfolio,
    config: ArcSettings,
    *,
    market: MarketSnapshot,
    now: dt.datetime,
    band: PriceBand | None = None,
    closing: bool = False,
) -> GateDecision:
    """Run every rule and return a :class:`GateDecision` listing *all* violations.

    ``now`` must be timezone-aware (``arc.utils.calendar.now_et()``); it is an
    argument so the gate stays pure. ``token`` is left ``None`` — minting is E3.2.

    ``band`` (D24): the whole price band is checked — the start at the limit, the
    worst price inside the combo NBBO and on tick, and the per-underlying cap at
    the worst price's max loss.

    ``order_budget`` (D32): when the account snapshot carries ``orders_used_today``,
    the proposal's worst-case attempts (``band.attempts``, else
    ``1 + execution_improvement_steps``) must fit the daily options order budget:
    opens under ``daily_max - close_reserve``, closes under ``daily_max``.

    ``closing``: the proposal closes an open position (E6.2 exits). Every leg must
    reduce a held leg (:func:`check_closing`); rules that only limit *opening*
    risk are skipped — daily-loss entry block, max open positions, Greek caps,
    per-underlying cap, structure whitelist, account profile (D25), wash sale, entry DTE window and
    earnings blackout. The halt, TTL, data freshness, spread/NBBO/tick and band
    checks still run (fail closed).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "evaluate() requires a timezone-aware `now` (use arc.utils.calendar.now_et())"
        raise ValueError(msg)
    p, a, pf, c, m = proposal, account_snapshot, portfolio, config, market
    violations: list[Violation] = []
    violations += _run("halt", lambda: check_halt(a))
    if closing:
        violations += _run("closing", lambda: check_closing(p, pf))
        violations += _run("day_trades", lambda: check_day_trades(p, a, pf, c))
    else:
        violations += _run("daily_loss", lambda: check_daily_loss(a, c))
        violations += _run("max_open_positions", lambda: check_max_open_positions(pf, c))
        violations += _run("greeks_present", lambda: check_greeks_present(p))
        violations += _run("greek_caps", lambda: check_greek_caps(p, a, pf, m, c))
    violations += _run("approval_ttl", lambda: check_approval_ttl(p, c, now))
    violations += _run("data_freshness", lambda: check_data_freshness(p, a, m, c, now))
    attempts = band.attempts if band is not None else 1 + c.execution_improvement_steps
    violations += _run(
        "order_budget", lambda: check_order_budget(a, c, attempts=attempts, closing=closing)
    )

    try:
        d: Derived | None = derive(p)
    except Exception as exc:  # noqa: BLE001 — malformed legs: report, run the rest
        d = None
        violations += _v(RuleCode.STRUCTURE_INVALID, f"{type(exc).__name__}: {exc}")

    if d is not None:
        dd = d
        risk_d = dd if band is None else worst_case(dd, band, p.sizing.contracts)
        if not closing:
            violations += _run("per_underlying", lambda: check_per_underlying(risk_d, a, pf, c))
        violations += _run("spread_tick", lambda: check_spread_tick(p, dd, m, c))
        violations += _run("max_gain", lambda: check_max_gain(dd))
        if band is not None:
            bb = band
            violations += _run("band", lambda: check_band(p, dd, bb, m, c))
        if not closing:
            violations += _run("structure", lambda: check_structure_whitelist(p, dd, c))
            violations += _run("account_profile", lambda: check_account_profile(p, risk_d, a, c))
            violations += _run("wash_sale", lambda: check_wash_sale(dd, pf, c, now))
            violations += _run("dte_window", lambda: check_dte_window(dd, c, now))
            violations += _run("earnings", lambda: check_earnings_blackout(p, dd, m, c, now))

    return GateDecision(
        proposal_hash=proposal_hash(p),
        passed=not violations,
        violations=[str(v) for v in violations],
        token=None,
        account_snapshot=a.model_dump(mode="json"),
    )
