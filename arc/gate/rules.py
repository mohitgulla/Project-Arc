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
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel, ConfigDict

from arc.config import ArcSettings, StructureKind
from arc.models import GateDecision, LegIntent, Proposal
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
    "Derived",
    "RuleCode",
    "Violation",
    "check_approval_ttl",
    "check_daily_loss",
    "check_data_freshness",
    "check_dte_window",
    "check_earnings_blackout",
    "check_greek_caps",
    "check_halt",
    "check_max_open_positions",
    "check_per_underlying",
    "check_spread_tick",
    "check_structure_whitelist",
    "check_wash_sale",
    "derive",
    "evaluate",
    "proposal_hash",
]

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


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
    VEGA_CAP = "portfolio_vega_cap"
    STRUCTURE_NOT_ALLOWED = "structure_not_allowed"
    DTE_WINDOW = "dte_window"
    EARNINGS_BLACKOUT = "earnings_blackout"
    MAX_POSITIONS = "max_open_positions"
    APPROVAL_TTL = "approval_ttl"
    STALE_DATA = "stale_data"
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
    _, max_loss = max_gain_loss(legs)
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
    )


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
    tick = _d(config.limit_tick)
    if d.limit_price % tick != 0:
        out += _v(RuleCode.TICK, f"limit {d.limit_price} is not a multiple of tick {tick}")
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
    proposal: Proposal, account: AccountSnapshot, portfolio: Portfolio, config: ArcSettings
) -> list[Violation]:
    """Post-trade |net Δ| ≤ cap × equity/100 (share-eq); |ν| ≤ pct × equity ($/vol-pt)."""
    out: list[Violation] = []
    n = proposal.sizing.contracts
    g = proposal.structure.greeks
    delta = _d(portfolio.greeks.delta + g.delta * n)
    delta_cap = _d(config.portfolio_delta_cap) * account.equity / _HUNDRED
    if abs(delta) > delta_cap:
        out += _v(RuleCode.DELTA_CAP, f"post-trade |Δ| {abs(delta):.2f} > cap {delta_cap:.2f}")
    # vega is per 1.00 of sigma in share-equivalents -> /100 = dollars per vol point
    vega_usd = _d(portfolio.greeks.vega + g.vega * n) / _HUNDRED
    vega_cap = _d(config.portfolio_vega_cap_pct) * account.equity
    if abs(vega_usd) > vega_cap:
        out += _v(
            RuleCode.VEGA_CAP, f"post-trade |ν| ${abs(vega_usd):.2f}/vol-pt > cap ${vega_cap:.2f}"
        )
    return out


_KIND_MAP: dict[ModelKind, StructureKind] = {
    ModelKind.LONG_CALL: StructureKind.LONG_CALL,
    ModelKind.LONG_PUT: StructureKind.LONG_PUT,
    ModelKind.VERTICAL_DEBIT: StructureKind.VERTICAL,
    ModelKind.VERTICAL_CREDIT: StructureKind.VERTICAL,
    ModelKind.IRON_CONDOR: StructureKind.IRON_CONDOR,
}


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
    """Calendar DTE (from the legs, as of ``now``) in [dte_min, dte_max]; never 0DTE."""
    dte = (d.expiration - now.astimezone(ET).date()).days
    if dte < 1 or not config.dte_min <= dte <= config.dte_max:
        return _v(
            RuleCode.DTE_WINDOW,
            f"DTE {dte} outside [{max(config.dte_min, 1)}, {config.dte_max}]",
        )
    return []


def check_earnings_blackout(
    proposal: Proposal,
    d: Derived,
    market: MarketSnapshot,
    config: ArcSettings,
    now: dt.datetime,
) -> list[Violation]:
    """No net-credit (short premium) structure held through earnings.

    Exempt only if the Director flagged an earnings play *and* Risk concurs.
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


def evaluate(
    proposal: Proposal,
    account_snapshot: AccountSnapshot,
    portfolio: Portfolio,
    config: ArcSettings,
    *,
    market: MarketSnapshot,
    now: dt.datetime,
) -> GateDecision:
    """Run every rule and return a :class:`GateDecision` listing *all* violations.

    ``now`` must be timezone-aware (``arc.utils.calendar.now_et()``); it is an
    argument so the gate stays pure. ``token`` is left ``None`` — minting is E3.2.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "evaluate() requires a timezone-aware `now` (use arc.utils.calendar.now_et())"
        raise ValueError(msg)
    p, a, pf, c, m = proposal, account_snapshot, portfolio, config, market
    violations: list[Violation] = []
    violations += _run("halt", lambda: check_halt(a))
    violations += _run("daily_loss", lambda: check_daily_loss(a, c))
    violations += _run("max_open_positions", lambda: check_max_open_positions(pf, c))
    violations += _run("approval_ttl", lambda: check_approval_ttl(p, c, now))
    violations += _run("data_freshness", lambda: check_data_freshness(p, a, m, c, now))
    violations += _run("greek_caps", lambda: check_greek_caps(p, a, pf, c))

    try:
        d: Derived | None = derive(p)
    except Exception as exc:  # noqa: BLE001 — malformed legs: report, run the rest
        d = None
        violations += _v(RuleCode.STRUCTURE_INVALID, f"{type(exc).__name__}: {exc}")

    if d is not None:
        dd = d
        violations += _run("per_underlying", lambda: check_per_underlying(dd, a, pf, c))
        violations += _run("spread_tick", lambda: check_spread_tick(p, dd, m, c))
        violations += _run("wash_sale", lambda: check_wash_sale(dd, pf, c, now))
        violations += _run("structure", lambda: check_structure_whitelist(p, dd, c))
        violations += _run("dte_window", lambda: check_dte_window(dd, c, now))
        violations += _run("earnings", lambda: check_earnings_blackout(p, dd, m, c, now))

    return GateDecision(
        proposal_hash=proposal_hash(p),
        passed=not violations,
        violations=[str(v) for v in violations],
        token=None,
        account_snapshot=a.model_dump(mode="json"),
    )
