"""Account profiles (PLAN D25; card E3.4): what the trading account can actually do.

The real-money account is expected to have **no margin**, so paper trading runs
the same constraint by default (``cash_debit``). Every profile is declared in
``config/account_profiles.yaml`` and selected by ``ARC_ACCOUNT_PROFILE``;
switching profiles needs no code change.

One profile is read by everything that decides what may be traded:

* the gate (rule ``account_profile``, :mod:`arc.gate.rules`) enforces it;
* the scanner builds only the profile's strategies for a stance;
* Research / Quant / Risk prompts state it;
* the entry DTE window comes from it when it overrides the global one.

Profile fields:

``allowed_kinds``
    Structure kinds (:class:`arc.models.StructureKind` values) the account may open.
``require_net_debit``
    The order must be a net debit (limit > 0).
``buying_power``
    ``cash_settled``: the debit (at the band's worst price) x 100 x contracts plus
    entry fees must fit the account's settled cash. ``margin``: the broker's
    margin buying power applies (the per-underlying cap still binds).
``allow_short_legs``
    ``none``: long legs only. ``covered_only``: every short leg is covered by a
    long leg of the same type and expiry that is worth at least as much at any
    price (long call strike <= short call strike; long put strike >= short put
    strike), i.e. a debit spread. ``any``: no coverage check (margin).
``dte_min`` / ``dte_max``
    Optional per-profile entry DTE window; ``None`` falls back to the global
    ``ARC_DTE_MIN`` / ``ARC_DTE_MAX``. A profile with one window behaves as one
    implicit sleeve named ``default``.
``sleeves``
    E22.1 (D97): several named entry-DTE sleeves instead of one window, written in
    YAML as a mapping ``name -> {dte_min, dte_max, max_positions, exit_policy}``.
    Windows never overlap; ``sleeves`` and ``dte_min/dte_max`` are mutually
    exclusive. ``max_positions`` caps open positions whose *entry* DTE is in the
    sleeve (gate rule ``sleeve_max_positions``; ``0`` closes the sleeve, ``None`` = no cap).
    ``exit_policy`` names a block in ``config/exits.yaml`` ``sleeves:`` (``None`` =
    the kind policy).
``stance_strategies``
    Scanner strategies per Research stance (bullish / bearish / neutral). An empty
    list means the profile has no structure for that stance: no trade, journaled.
``day_trades``
    E10.2: the day-trade limit (``rule: none | pattern_day_trader``, ``min_equity``,
    ``max_day_trades``, ``window_sessions``). The gate rejects a same-day close that
    would exceed it while equity is below ``min_equity``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arc.models import StructureKind

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from arc.models import Structure

__all__ = [
    "DEFAULT_ACCOUNT_PROFILE",
    "DEFAULT_PROFILES_PATH",
    "STRATEGY_NAMES",
    "AccountProfile",
    "AccountProfiles",
    "BuyingPower",
    "DayTradeRule",
    "DayTrades",
    "DteSleeve",
    "ShortLegPolicy",
    "entry_dte_of",
    "load_account_profiles",
    "sleeve_for_dte",
    "sleeve_of",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_PATH = REPO_ROOT / "config" / "account_profiles.yaml"
# D25: paper mimics the expected no-margin real-money account first.
DEFAULT_ACCOUNT_PROFILE = "cash_debit"

StrategyName = Literal[
    "bull_put",
    "bear_call",
    "iron_condor",
    "long_call",
    "long_put",
    "bull_call_debit",
    "bear_put_debit",
]
# Must equal arc.scanner.ScanStrategy's values (a test pins it).
STRATEGY_NAMES: frozenset[str] = frozenset(StrategyName.__args__)  # type: ignore[attr-defined]


class BuyingPower(StrEnum):
    CASH_SETTLED = "cash_settled"
    MARGIN = "margin"


class ShortLegPolicy(StrEnum):
    NONE = "none"
    COVERED_ONLY = "covered_only"
    ANY = "any"


# FINRA retired the PDT rule on 2026-06-04 and Alpaca dropped the fields; no profile sets it (D80).
class DayTradeRule(StrEnum):
    """E10.2 (D44): which day-trade limit the account faces.

    ``none``: no limit. ``pattern_day_trader``: below ``min_equity`` an account
    may make at most ``max_day_trades`` day trades (an option opened and closed
    the same ET day) in a rolling ``window_sessions`` window; a close that would
    be one more is rejected (FINRA PDT; also how a cash account's good-faith
    rule is approximated). The paper account does not enforce it, so an
    experiment arm mirroring a small control account gets it from here.
    """

    NONE = "none"
    PATTERN_DAY_TRADER = "pattern_day_trader"


_FORBID = ConfigDict(extra="forbid", frozen=True)


class StanceStrategies(BaseModel):
    model_config = _FORBID

    bullish: list[StrategyName] = Field(default_factory=list)
    bearish: list[StrategyName] = Field(default_factory=list)
    neutral: list[StrategyName] = Field(default_factory=list)

    def for_stance(self, stance: str) -> list[str]:
        return list(getattr(self, stance.strip().lower(), []))


class DayTrades(BaseModel):
    """Day-trade limit of a profile (E10.2): see :class:`DayTradeRule`."""

    model_config = _FORBID

    rule: DayTradeRule = DayTradeRule.NONE
    min_equity: Decimal = Field(Decimal(25000), ge=0, description="limit applies below this")
    max_day_trades: int = Field(3, ge=0, description="allowed in the rolling window")
    window_sessions: int = Field(5, ge=1, le=20)


#: Name of the one implicit sleeve of a single-window profile.
DEFAULT_SLEEVE = "default"


class DteSleeve(BaseModel):
    """E22.1 (D97): one named entry-DTE sleeve of an account profile."""

    model_config = _FORBID

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    dte_min: int = Field(..., ge=1)
    dte_max: int = Field(..., ge=1)
    max_positions: int | None = Field(
        None,
        ge=0,
        description="open positions in this sleeve (by entry DTE); None = no cap, "
        "0 = sleeve closed to new opens (the D97 registry rollback)",
    )
    exit_policy: str | None = Field(
        None, description="block in config/exits.yaml `sleeves:`; None = the kind policy"
    )

    @model_validator(mode="after")
    def _window(self) -> DteSleeve:
        if self.dte_max < self.dte_min:
            msg = f"sleeve {self.name}: dte_max {self.dte_max} < dte_min {self.dte_min}"
            raise ValueError(msg)
        return self

    def contains(self, dte: int) -> bool:
        return self.dte_min <= dte <= self.dte_max

    @property
    def label(self) -> str:
        """``short[10-29]``: how gate messages and summaries name the sleeve."""
        return f"{self.name}[{self.dte_min}-{self.dte_max}]"


def sleeve_for_dte(sleeves: Sequence[DteSleeve], dte: int | None) -> DteSleeve | None:
    """The sleeve whose window contains *dte*, else ``None`` (windows never overlap)."""
    if dte is None:
        return None
    return next((s for s in sleeves if s.contains(dte)), None)


def entry_dte_of(structure: Structure, opened_at: dt.date | dt.datetime | str | None) -> int:
    """A position's *entry* DTE: the structure's stored ``dte`` (as priced at the open).

    A stored ``dte`` of 0 (the pipeline never opens one) falls back to the nearest
    leg expiry minus the ``opened_at`` ET date; without ``opened_at`` it stays 0.
    """
    if structure.dte > 0 or opened_at is None or not structure.legs:
        return structure.dte
    from arc.structures.occ import parse_occ
    from arc.utils.calendar import ET

    when: dt.date | dt.datetime = (
        dt.datetime.fromisoformat(opened_at) if isinstance(opened_at, str) else opened_at
    )
    if isinstance(when, dt.datetime):
        aware = when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)
        when = aware.astimezone(ET).date()
    expiry = min(parse_occ(leg.occ_symbol).expiration for leg in structure.legs)
    return (expiry - when).days


def sleeve_of(
    sleeves: Sequence[DteSleeve],
    structure: Structure,
    opened_at: dt.date | dt.datetime | str | None = None,
) -> DteSleeve | None:
    """E22.1 (D97): the sleeve of a held or proposed structure, by its *entry* DTE.

    ``None`` when the entry DTE fits no current sleeve (e.g. a window changed after
    the open); exits then use the kind policy. Pure; never raises on a valid structure.
    """
    return sleeve_for_dte(sleeves, entry_dte_of(structure, opened_at))


class AccountProfile(BaseModel):
    """One account profile (see the module doc for field semantics)."""

    model_config = _FORBID

    name: str
    description: str = ""
    allowed_kinds: list[StructureKind] = Field(..., min_length=1)
    require_net_debit: bool
    buying_power: BuyingPower
    allow_short_legs: ShortLegPolicy
    dte_min: int | None = Field(None, ge=1)
    dte_max: int | None = Field(None, ge=1)
    sleeves: list[DteSleeve] | None = Field(
        None, description="E22.1 (D97): named entry-DTE sleeves; excludes dte_min/dte_max"
    )
    stance_strategies: StanceStrategies
    day_trades: DayTrades = Field(default_factory=lambda: DayTrades())

    @model_validator(mode="before")
    @classmethod
    def _sleeve_names(cls, data: Any) -> Any:
        """YAML writes sleeves as ``name -> spec`` so D26 overrides can address them."""
        if isinstance(data, dict) and isinstance(data.get("sleeves"), dict):
            data = dict(data)
            data["sleeves"] = [
                {"name": name, **body} if isinstance(body, dict) else body
                for name, body in data["sleeves"].items()
            ]
        return data

    @model_validator(mode="after")
    def _check(self) -> AccountProfile:
        if StructureKind.OTHER in self.allowed_kinds:
            msg = f"profile {self.name}: 'other' is never an allowed kind"
            raise ValueError(msg)
        if (self.dte_min is None) != (self.dte_max is None):
            msg = f"profile {self.name}: set both dte_min and dte_max, or neither"
            raise ValueError(msg)
        if self.dte_min is not None and self.dte_max is not None and self.dte_max < self.dte_min:
            msg = f"profile {self.name}: dte_max {self.dte_max} < dte_min {self.dte_min}"
            raise ValueError(msg)
        if self.sleeves is not None:
            self._check_sleeves(self.sleeves)
        return self

    def _check_sleeves(self, sleeves: list[DteSleeve]) -> None:
        if self.dte_min is not None:
            msg = (
                f"profile {self.name} declares sleeves, so dte_min/dte_max must be unset "
                f"(tune profiles.{self.name}.sleeves.<sleeve>.dte_min|dte_max instead)"
            )
            raise ValueError(msg)
        if not sleeves:
            msg = f"profile {self.name}: sleeves must list at least one sleeve (or be omitted)"
            raise ValueError(msg)
        names = [s.name for s in sleeves]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            msg = f"profile {self.name}: duplicate sleeve name(s) {', '.join(dupes)}"
            raise ValueError(msg)
        ordered = sorted(sleeves, key=lambda s: (s.dte_min, s.dte_max))
        for a, b in zip(ordered, ordered[1:], strict=False):
            if b.dte_min <= a.dte_max:
                msg = f"profile {self.name}: sleeves {a.label} and {b.label} overlap"
                raise ValueError(msg)

    @property
    def has_sleeves(self) -> bool:
        """True when the profile declares named sleeves (not one implicit window)."""
        return bool(self.sleeves)

    def entry_sleeves(self, fallback: tuple[int, int] | None = None) -> list[DteSleeve]:
        """The profile's sleeves, ordered by window.

        A single-window profile is one implicit ``default`` sleeve (no slot cap, the
        kind exit policy). A profile with neither uses *fallback* (the global
        ``dte_min/dte_max``; a 0 floor becomes 1, 0DTE is never allowed) or, without
        one, has no sleeve.
        """
        if self.sleeves:
            return sorted(self.sleeves, key=lambda s: s.dte_min)
        if self.dte_min is not None and self.dte_max is not None:
            return [DteSleeve(name=DEFAULT_SLEEVE, dte_min=self.dte_min, dte_max=self.dte_max)]
        if fallback is not None:
            lo, hi = max(fallback[0], 1), max(fallback[1], 1)
            return [DteSleeve(name=DEFAULT_SLEEVE, dte_min=lo, dte_max=max(hi, lo))]
        return []

    def sleeve_for_dte(
        self, dte: int | None, fallback: tuple[int, int] | None = None
    ) -> DteSleeve | None:
        """The sleeve containing entry DTE *dte*, else ``None``."""
        return sleeve_for_dte(self.entry_sleeves(fallback), dte)

    def strategies_for(self, stance: str) -> list[str]:
        """Scanner strategy names for a Research stance; ``[]`` = no trade."""
        return self.stance_strategies.for_stance(stance)

    def summary(self) -> str:
        """One line for persona prompts."""
        kinds = ", ".join(k.value for k in self.allowed_kinds)
        parts = [f"account profile `{self.name}`: allowed structures {kinds}"]
        if self.require_net_debit:
            parts.append("net debit only (no credit structures)")
        if self.allow_short_legs is ShortLegPolicy.NONE:
            parts.append("no short legs")
        elif self.allow_short_legs is ShortLegPolicy.COVERED_ONLY:
            parts.append("every short leg covered by a long leg of the same type and expiry")
        if self.buying_power is BuyingPower.CASH_SETTLED:
            parts.append("the debit plus fees must fit settled cash (no margin)")
        return "; ".join(parts) + "."


class AccountProfiles(BaseModel):
    """All declared profiles, by name."""

    model_config = _FORBID

    profiles: dict[str, AccountProfile]

    @model_validator(mode="before")
    @classmethod
    def _names(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
            data = dict(data)
            data["profiles"] = {
                name: ({"name": name, **body} if isinstance(body, dict) else body)
                for name, body in data["profiles"].items()
            }
        return data

    def get(self, name: str) -> AccountProfile:
        try:
            return self.profiles[name]
        except KeyError:
            msg = f"unknown account profile {name!r}; declared: {', '.join(sorted(self.profiles))}"
            raise KeyError(msg) from None


def load_account_profiles(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> AccountProfiles:
    """Load and validate ``config/account_profiles.yaml`` (or *path*).

    *overrides* (D26, ``path -> value`` into the file, e.g. a profile's DTE window)
    patch it before validation.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_PROFILES_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return AccountProfiles.model_validate(apply_overrides(data, overrides))
