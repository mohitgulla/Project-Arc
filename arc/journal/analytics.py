"""Proposal analytics: the numbers a trader reads before approving (E6.1a, D22/D23).

:class:`ProposalAnalytics` is computed once by the propose step from the same
chain snapshot the proposal was priced with, and stored as part of E7.4's
:class:`~arc.journal.models.MarketContext` (``market_contexts`` row, one copy).
The proposal card reads it back through :mod:`arc.approvals.trail` and only
formats it: nothing on the card is recomputed from a live quote.

It is deliberately **not** part of :class:`~arc.models.Proposal`, so the gate's
proposal hash is unchanged.

The math helpers here are pure and deterministic (no I/O, no LLM):

- moneyness ``(K − S) / S``
- σ-distance ``ln(K / S) / (IV · √t)`` with ``t = DTE / 365``
- 1σ expected move to expiry ``S · IV · √t``
"""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - pydantic fields
import math
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from arc.backtest.costs import CostModel, FeeBreakdown  # noqa: TC001 - pydantic fields
from arc.exits.model import ExitModelResult  # noqa: TC001 - pydantic fields

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ANALYTICS_VERSION",
    "LEVEL_NAMES",
    "BreakevenStat",
    "LegAnalytics",
    "LevelCrossing",
    "ProposalAnalytics",
    "VolStats",
    "be_atr_multiple",
    "debit_direction",
    "directional_breakeven",
    "expected_move",
    "level_crossings",
    "levels_text",
    "moneyness_pct",
    "otm",
    "sigma_distance",
    "sigma_t",
    "tech_levels",
]

ANALYTICS_VERSION = 1
_FORBID = ConfigDict(extra="forbid", frozen=True)


def sigma_t(iv: float | None, dte: int) -> float | None:
    """IV × √(DTE/365): one standard deviation of log-return to expiry."""
    if iv is None or not math.isfinite(iv) or iv <= 0 or dte <= 0:
        return None
    return iv * math.sqrt(dte / 365.0)


def moneyness_pct(strike: float, spot: float) -> float:
    """Signed distance of *strike* from *spot*: ``(K − S) / S``."""
    if spot <= 0:
        msg = "spot must be positive"
        raise ValueError(msg)
    return (strike - spot) / spot


def sigma_distance(level: float, spot: float, iv: float | None, dte: int) -> float | None:
    """Signed distance of *level* from *spot* in σ to expiry: ``ln(K/S) / (IV·√t)``."""
    s = sigma_t(iv, dte)
    if s is None or level <= 0 or spot <= 0:
        return None
    return math.log(level / spot) / s


def expected_move(spot: float, iv: float | None, dte: int) -> float | None:
    """±1σ move of the underlying to expiry, in dollars: ``S · IV · √t``."""
    s = sigma_t(iv, dte)
    return None if s is None else spot * s


def otm(kind: Literal["call", "put"], strike: float, spot: float) -> bool:
    """True when the option is out of the money at *spot* (ATM counts as OTM)."""
    return strike >= spot if kind == "call" else strike <= spot


def be_atr_multiple(breakeven: float, spot: float, atr14: float | None, dte: int) -> float | None:
    """E16.5 (D76): ``|BE − S| / (ATR14 · √DTE)``, the breakeven in realised-move terms.

    ATR14 is the stock's average daily true range ($); √DTE scales it to the days
    left (calendar DTE, the same days the σ-distance uses). Example: spot 100, BE
    105, ATR 2, 25 DTE → 5 / (2 · 5) = 0.5. ``None`` without a positive ATR or DTE.
    """
    if atr14 is None or not math.isfinite(atr14) or atr14 <= 0 or dte <= 0 or spot <= 0:
        return None
    return abs(breakeven - spot) / (atr14 * math.sqrt(dte))


def directional_breakeven(breakevens: Sequence[float], direction: int) -> float | None:
    """E16.5: the breakeven a debit structure must cross in its own direction.

    *direction* ``+1`` (long call, bull call debit) → the highest breakeven; ``-1``
    (long put, bear put debit) → the lowest; ``0`` (credit / neutral) → ``None``.
    """
    if not breakevens or direction == 0:
        return None
    return max(breakevens) if direction > 0 else min(breakevens)


def debit_direction(net_debit_credit: float, long_kinds: Sequence[str]) -> int:
    """E16.5: ``+1`` / ``-1`` for a debit structure bought on calls / puts, else ``0``.

    *net_debit_credit* is per share (> 0 = a debit); *long_kinds* are the option
    types (``call`` / ``put``) of the bought legs. Credit structures, and a debit
    with long legs of both types (not on any profile today), return 0: no single
    direction, so no directional breakeven.
    """
    kinds = set(long_kinds)
    if net_debit_credit <= 0 or len(kinds) != 1:
        return 0
    return 1 if kinds == {"call"} else -1 if kinds == {"put"} else 0


# ---------------------------------------------------------------------------
# E23.3 (D98): strike vs level context
# ---------------------------------------------------------------------------

LevelKind = Literal["poc", "vah", "val", "hvn", "call_wall", "put_wall"]
#: Display names of the level kinds, in tie-break priority order (same price: first wins).
LEVEL_NAMES: dict[str, str] = {
    "call_wall": "call wall",
    "put_wall": "put wall",
    "poc": "POC",
    "vah": "VAH",
    "val": "VAL",
    "hvn": "HVN",
}
#: A target within this many ATR14 of a level (or $0.01 without ATR14) sits ``at`` it.
AT_LEVEL_ATR = 0.1
_AT_LEVEL_TICK = 0.01

# regime-technicals field -> level kind (E23.0 fields; the HVNs are the nearest ones)
_TECH_LEVELS: tuple[tuple[str, LevelKind], ...] = (
    ("oi_call_wall", "call_wall"),
    ("oi_put_wall", "put_wall"),
    ("vp_poc", "poc"),
    ("vp_vah", "vah"),
    ("vp_val", "val"),
    ("vp_hvn_above", "hvn"),
    ("vp_hvn_below", "hvn"),
)


class LevelCrossing(BaseModel):
    """E23.3 (D98): where one target (short strike / breakeven) sits vs the levels.

    The levels are the E23.0 volume-profile (POC, VAH, VAL, nearest HVNs) and OI-wall
    prices on the ticker's regime technicals. Walking from spot toward the target:

    * ``at``: the target is within :data:`AT_LEVEL_ATR` ATR14 of a level (that level);
    * ``beyond``: the move must push through at least one level first (the first one
      it meets from spot; ``crossed`` counts them all);
    * ``before``: no level between spot and the target (the first level past it).

    ``beyond_first`` (the replay split): the target sits beyond the first level, i.e.
    ``crossed >= 1`` (an ``at`` target with a level crossed on the way counts too).

    ``dist_atr`` = (target − level) / ATR14 measured in the direction of travel
    (> 0 beyond, < 0 before); ``None`` without ATR14. Context only, never a gate input.
    """

    model_config = _FORBID

    which: Literal["short_strike", "breakeven"]
    target: float
    direction: Literal[1, -1] = Field(..., description="+1 = above spot, -1 = below")
    level_kind: LevelKind
    price: float
    side: Literal["before", "at", "beyond"]
    dist_atr: float | None = None
    crossed: int = Field(0, ge=0, description="Levels strictly between spot and the target")
    label: str = Field(..., description="Rendered name, e.g. `BE`, `upper BE`, `short call`")

    @property
    def beyond_first(self) -> bool:
        """The target sits beyond the first level toward it (the replay split)."""
        return self.crossed >= 1


def tech_levels(tech: dict[str, object] | None) -> list[tuple[LevelKind, float]]:
    """The E23.0 levels on a stored ``technicals`` dump, ``(kind, price)``, deduplicated
    by price (the first kind in :data:`LEVEL_NAMES` order wins a tie)."""
    if not isinstance(tech, dict):
        return []
    out: dict[float, LevelKind] = {}
    for field, kind in _TECH_LEVELS:
        v = tech.get(field)
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            out.setdefault(round(float(v), 4), kind)
    return [(k, p) for p, k in out.items()]


def _crossing(
    which: Literal["short_strike", "breakeven"],
    target: float,
    spot: float,
    levels: Sequence[tuple[LevelKind, float]],
    atr14: float | None,
    label: str,
) -> LevelCrossing | None:
    d = 1 if target > spot else -1 if target < spot else 0
    if d == 0:
        return None
    # levels on the target's side of spot, nearest to spot first
    side_levels = sorted(
        ((k, p) for k, p in levels if d * (p - spot) > 0), key=lambda kp: d * (kp[1] - spot)
    )
    if not side_levels:
        return None
    tol = AT_LEVEL_ATR * atr14 if atr14 else _AT_LEVEL_TICK
    near = min(side_levels, key=lambda kp: abs(kp[1] - target))
    crossed = [kp for kp in side_levels if d * (target - kp[1]) > tol]
    kind: LevelKind
    side: Literal["before", "at", "beyond"]
    if abs(near[1] - target) <= tol:
        (kind, price), side = near, "at"
    elif crossed:
        (kind, price), side = crossed[0], "beyond"  # the first level the move meets
    else:
        (kind, price), side = side_levels[0], "before"
    dist = None if not atr14 else round(d * (target - price) / atr14 + 0.0, 4)
    return LevelCrossing(
        which=which,
        target=round(target, 4),
        direction=d,
        level_kind=kind,
        price=price,
        side=side,
        dist_atr=dist,
        crossed=len(crossed),
        label=label,
    )


def level_crossings(
    *,
    spot: float,
    net_debit_credit: float,
    legs: Sequence[tuple[Literal["long", "short"], Literal["call", "put"], float]],
    breakevens: Sequence[float],
    tech: dict[str, object] | None,
) -> list[LevelCrossing]:
    """E23.3 (D98): each target of a structure vs the first level(s) toward it.

    *legs* are ``(side, kind, strike)``. A **debit** bought on one option type (long
    call/put, debit vertical) has one direction: its directional breakeven and the
    short strike (verticals) on that side. Anything else (credit verticals, condors)
    checks every breakeven and short strike on its own side of spot. Labels carry a
    side word (``upper BE`` / ``short call``) only when targets sit on both sides.
    ``[]`` without levels in *tech*, or for a target with no level on its side.
    """
    levels = tech_levels(tech)
    if not levels or spot <= 0:
        return []
    atr = tech.get("atr14") if isinstance(tech, dict) else None
    atr14 = float(atr) if isinstance(atr, int | float) and atr > 0 else None
    longs = [k for s, k, _ in legs if s == "long"]
    direction = debit_direction(net_debit_credit, longs)
    shorts = [(k, float(x)) for s, k, x in legs if s == "short"]
    bes = [float(b) for b in breakevens]
    if direction != 0:
        be = directional_breakeven(bes, direction)
        bes = [be] if be is not None and direction * (be - spot) > 0 else []
        shorts = [(k, x) for k, x in shorts if direction * (x - spot) > 0]
    targets = [("short_strike", k, x) for k, x in shorts] + [("breakeven", None, b) for b in bes]
    sides = {1 if x > spot else -1 for _, _, x in targets if x != spot}
    two = len(sides) == 2
    out: list[LevelCrossing] = []
    for which, kind, x in targets:
        up = x > spot
        if which == "breakeven":
            label = ("upper BE" if up else "lower BE") if two else "BE"
        else:
            label = f"short {kind}" if two else "short strike"
        c = _crossing(which, x, spot, levels, atr14, label)  # type: ignore[arg-type]
        if c is not None:
            out.append(c)
    return out


def _price(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def levels_text(crossings: Sequence[LevelCrossing]) -> str:
    """``BE beyond POC 186.1 (+0.4 ATR) · short strike at call wall 190``; ``""`` if none.

    Breakevens first, then short strikes, upper side before lower.
    """
    order = sorted(crossings, key=lambda c: (c.which != "breakeven", -c.direction))
    parts = []
    for c in order:
        s = f"{c.label} {c.side} {LEVEL_NAMES[c.level_kind]} {_price(c.price)}"
        if c.side != "at" and c.dist_atr is not None:
            s += f" ({round(c.dist_atr, 1) + 0.0:+.1f} ATR)"
        parts.append(s)
    return " · ".join(parts)


class LegAnalytics(BaseModel):
    """One leg's quote, liquidity, moneyness and entry cost, frozen at proposal time."""

    model_config = _FORBID

    occ_symbol: str
    side: Literal["long", "short"]
    ratio: int = Field(..., ge=1)
    kind: Literal["call", "put"]
    strike: float
    expiration: _dt.date
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    bid_size: float | None = Field(None, description="Top of book; None when the feed omits it")
    ask_size: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    iv: float | None = None
    delta: float | None = None
    quote_ts: _dt.datetime | None = None
    spread: float | None = Field(None, description="ask − bid, $/share")
    spread_pct: float | None = Field(None, description="spread / mid")
    moneyness_pct: float | None = Field(None, description="(K − S) / S")
    sigma_distance: float | None = Field(None, description="ln(K/S) / (ATM IV · √t)")
    otm: bool | None = None
    entry_fill: float | None = Field(None, description="Modelled fill, $/share (mid ± x·spread)")
    entry_slippage: float = Field(0.0, ge=0.0, description="$ per unit: |fill − mid| × ratio × 100")
    entry_fees: FeeBreakdown = Field(default_factory=FeeBreakdown)


class BreakevenStat(BaseModel):
    model_config = _FORBID

    price: float
    pct: float = Field(..., description="(BE − S) / S")
    sigma: float | None = Field(None, description="ln(BE/S) / (ATM IV · √t)")
    atr_multiple: float | None = Field(
        None,
        description="E16.5 (D76): |BE − S| / (ATR14 · √DTE); None without the ticker's ATR14",
    )


class VolStats(BaseModel):
    """Vol context: scan-time ATM IV plus the regime FeatureSnapshot's rank/HV fields."""

    model_config = _FORBID

    atm_iv: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    hv20: float | None = None
    hv60: float | None = None
    iv_hv20: float | None = Field(None, description="ATM IV / HV20")
    features_as_of: _dt.date | None = None


class ProposalAnalytics(BaseModel):
    """Everything the v2 proposal card shows beyond the Proposal itself."""

    model_config = _FORBID

    version: Literal[1] = ANALYTICS_VERSION
    spot: float
    spot_as_of: _dt.datetime | None = None
    prev_close: float | None = Field(None, description="Last close in the regime snapshot")
    prev_close_as_of: _dt.date | None = None
    day_change_pct: float | None = Field(None, description="spot / prev_close − 1")
    dte: int = Field(..., ge=0)
    sigma_t: float | None = Field(None, description="ATM IV · √(DTE/365)")
    expected_move: float | None = Field(None, description="±1σ to expiry, $")
    breakevens: list[BreakevenStat] = Field(default_factory=list)
    legs: list[LegAnalytics]
    vol: VolStats = Field(default_factory=lambda: VolStats())
    cost_model: CostModel = Field(..., description="The costs.yaml values this was priced with")
    entry_slippage: float = Field(..., ge=0.0, description="$ per unit, all legs")
    entry_fees: FeeBreakdown = Field(default_factory=FeeBreakdown)
    exit_model: ExitModelResult | None = Field(None, description="E2.4 managed vs static")
    account_profile: str | None = Field(None, description="D25 account profile, when configured")
    sleeve_window: str | None = Field(
        None, description="E22.2 (D97): the proposal's DTE sleeve window, e.g. `10–29 DTE`"
    )
    levels: list[LevelCrossing] | None = Field(
        None,
        exclude_if=lambda v: v is None,  # off: the stored payload is byte-identical
        description="E23.3 (D98): short strike / breakeven vs the volume-profile and OI-wall "
        "levels; None = strike_levels off (or analytics stored before E23.3)",
    )
    depth: Literal["top_of_book"] = Field(
        "top_of_book", description="Alpaca gives top-of-book only; no order-book depth"
    )
