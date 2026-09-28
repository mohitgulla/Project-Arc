"""Exit policy (PLAN D19/D23): one set of rules per structure kind, from ``config/exits.yaml``.

The same :class:`ExitPolicy` drives three consumers, which must not define their
own exit config:

- the managed-exit Monte Carlo model (:mod:`arc.exits.model`), which puts numbers on
  the proposal card (E6.1a);
- the Investor's live exit decisions (E6.2 base exits, E6.4 profit-taking and
  close-to-reallocate) through :func:`arc.exits.evaluate_position`;
- the backtester's ``exit_policy="policy"`` mode (E7.2).

Rule semantics (all P&L per share, marked at mid, relative to the entry price)
-----------------------------------------------------------------------------
Let ``N`` = the entry net price (positive debit paid, negative credit received),
``V`` = the position's current value per share (long legs +, short legs −), and
``pnl = V − N``.

*Credit structures* (``N < 0``, credit ``C = −N``):

- take profit when ``pnl ≥ p · max_gain`` with ``p = take_profit_pct_of_max_gain``.
  For a credit vertical or iron condor ``max_gain = C``, so 50% of a $1.66
  credit closes at a **$0.83 debit to close**.

*Debit structures* (``N > 0``, debit ``D = N``):

- take profit when ``pnl ≥ q · D`` with ``q = take_profit_pct_of_debit``
  (1.00 = the position is worth twice what was paid).

*Stops* (``stop.basis``), a loss threshold ``L`` such that the stop fires when
``pnl ≤ −L``:

- ``credit_multiple``: ``L = value · C``. **2.0 means a loss of twice the credit**
  (debit to close = 3 × credit). Credit structures only.
- ``pct_max_loss``: ``L = value · max_loss``. Any structure.
- ``pct_debit``: ``L = value · D`` (0.5 = the position lost half its debit). Debit
  structures only.

*Time*: ``close_at_dte`` closes the position once remaining DTE ≤ that value.
``time_adjusted_targets`` (D19 decay-adjusted target) replace the take-profit
percentage once remaining DTE ≤ ``dte_lte``; the tightest matching bucket
(smallest ``dte_lte``) wins.

Rules are checked each day in the order **stop → take profit → DTE exit**.

Defaults (``config/exits.yaml``): take profit 50% of max gain (credit) / 100% of
debit (debit), close at 7 DTE. The stop default is the **owner's open call**; until
it is answered the file carries E6.2's proposal: ``credit_multiple 2.0`` for
credit structures and ``pct_debit 0.5`` for debit structures.

Everything here is deterministic and pure (no LLM, no network).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.models import StructureKind

if TYPE_CHECKING:
    from arc.models import Structure

__all__ = [
    "CREDIT_KINDS",
    "DEBIT_KINDS",
    "DEFAULT_EXITS_PATH",
    "ExitConfig",
    "ExitModelConfig",
    "ExitPolicy",
    "ExitReason",
    "IvModel",
    "PipelineExitConfig",
    "ResolvedRules",
    "StopBasis",
    "StopRule",
    "TimeAdjustedTarget",
    "check_rules",
    "load_exit_config",
    "resolve_rules",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EXITS_PATH = REPO_ROOT / "config" / "exits.yaml"

CREDIT_KINDS = frozenset({StructureKind.VERTICAL_CREDIT, StructureKind.IRON_CONDOR})
DEBIT_KINDS = frozenset(
    {StructureKind.LONG_CALL, StructureKind.LONG_PUT, StructureKind.VERTICAL_DEBIT}
)

_FORBID = ConfigDict(extra="forbid", frozen=True)


class StopBasis(StrEnum):
    """What the stop's ``value`` multiplies (see module doc)."""

    CREDIT_MULTIPLE = "credit_multiple"
    PCT_MAX_LOSS = "pct_max_loss"
    PCT_DEBIT = "pct_debit"


class ExitReason(StrEnum):
    """Why a position was (or would be) closed."""

    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    DTE_EXIT = "dte_exit"
    EXPIRY = "expiry"


class StopRule(BaseModel):
    model_config = _FORBID

    basis: StopBasis
    value: float = Field(..., gt=0.0)

    @model_validator(mode="after")
    def _range(self) -> StopRule:
        if self.basis is not StopBasis.CREDIT_MULTIPLE and self.value > 1.0:
            msg = f"stop {self.basis.value} is a fraction in (0, 1]; got {self.value}"
            raise ValueError(msg)
        return self


class TimeAdjustedTarget(BaseModel):
    """Take-profit percentage that applies once remaining DTE ≤ ``dte_lte`` (D19)."""

    model_config = _FORBID

    dte_lte: int = Field(..., ge=0)
    take_profit_pct: float = Field(..., gt=0.0)


class ExitPolicy(BaseModel):
    """Exit rules for one structure kind (see module doc for exact semantics)."""

    model_config = _FORBID

    take_profit_pct_of_max_gain: float | None = Field(
        0.50, gt=0.0, le=1.0, description="Credit structures: close at this share of max gain"
    )
    take_profit_pct_of_debit: float | None = Field(
        1.00, gt=0.0, description="Debit structures: close when pnl reaches this × debit"
    )
    stop: StopRule | None = None
    close_at_dte: int | None = Field(7, ge=0)
    time_adjusted_targets: list[TimeAdjustedTarget] = Field(default_factory=list)

    @field_validator("time_adjusted_targets")
    @classmethod
    def _unique_buckets(cls, v: list[TimeAdjustedTarget]) -> list[TimeAdjustedTarget]:
        seen = [t.dte_lte for t in v]
        if len(seen) != len(set(seen)):
            msg = "time_adjusted_targets: dte_lte values must be unique"
            raise ValueError(msg)
        return sorted(v, key=lambda t: t.dte_lte)

    def take_profit_pct(self, *, credit: bool, dte: int) -> float | None:
        """Take-profit percentage in force at *dte* for a credit or debit structure."""
        for t in self.time_adjusted_targets:  # ascending: tightest bucket first
            if dte <= t.dte_lte:
                return t.take_profit_pct
        return self.take_profit_pct_of_max_gain if credit else self.take_profit_pct_of_debit

    def summary(self) -> str:
        """One-line human description (used in prompts / cards)."""
        parts: list[str] = []
        if self.take_profit_pct_of_max_gain is not None:
            parts.append(f"take profit {self.take_profit_pct_of_max_gain:.0%} of max gain (credit)")
        if self.take_profit_pct_of_debit is not None:
            parts.append(f"take profit {self.take_profit_pct_of_debit:.0%} of debit (debit)")
        parts.append(
            "no stop" if self.stop is None else f"stop {self.stop.basis.value} {self.stop.value:g}"
        )
        if self.close_at_dte is not None:
            parts.append(f"close at {self.close_at_dte} DTE")
        parts.extend(
            f"take profit {t.take_profit_pct:.0%} at ≤{t.dte_lte} DTE"
            for t in self.time_adjusted_targets
        )
        return "; ".join(parts)


HOLD_TO_EXPIRY = ExitPolicy(
    take_profit_pct_of_max_gain=None, take_profit_pct_of_debit=None, stop=None, close_at_dte=None
)


class IvModel(BaseModel):
    """IV path for the model. ``constant`` (default) or deterministic mean reversion.

    ``mean_reverting``: ``iv_d = long_run + (iv_0 − long_run) · 0.5 ** (d / half_life_days)``.
    """

    model_config = _FORBID

    kind: Literal["constant", "mean_reverting"] = "constant"
    long_run: float | None = Field(None, gt=0.0)
    half_life_days: float | None = Field(None, gt=0.0)

    @model_validator(mode="after")
    def _params(self) -> IvModel:
        if self.kind == "mean_reverting" and (self.long_run is None or self.half_life_days is None):
            msg = "iv_model mean_reverting needs long_run and half_life_days"
            raise ValueError(msg)
        return self


class ExitModelConfig(BaseModel):
    """Monte Carlo knobs."""

    model_config = _FORBID

    n_paths: int = Field(20_000, ge=100)
    seed: int = Field(20260927, ge=0)
    iv_model: IvModel = Field(default_factory=IvModel)


class PipelineExitConfig(BaseModel):
    """How the E5.2 pipeline uses the model."""

    model_config = _FORBID

    rank_menu_by_managed_net_ev: bool = Field(
        False, description="Rank the Quant menu by managed net EV (off until the owner decides)"
    )


class ExitConfig(BaseModel):
    """Validated ``config/exits.yaml``."""

    model_config = _FORBID

    model: ExitModelConfig = Field(default_factory=ExitModelConfig)
    pipeline: PipelineExitConfig = Field(default_factory=PipelineExitConfig)
    default: ExitPolicy = Field(default_factory=ExitPolicy)
    kinds: dict[StructureKind, ExitPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stops_match_direction(self) -> ExitConfig:
        for kind, pol in self.kinds.items():
            if pol.stop is None:
                continue
            if pol.stop.basis is StopBasis.CREDIT_MULTIPLE and kind not in CREDIT_KINDS:
                msg = f"{kind.value}: credit_multiple stop applies to credit structures only"
                raise ValueError(msg)
            if pol.stop.basis is StopBasis.PCT_DEBIT and kind not in DEBIT_KINDS:
                msg = f"{kind.value}: pct_debit stop applies to debit structures only"
                raise ValueError(msg)
        return self

    def policy_for(self, kind: StructureKind | None) -> ExitPolicy:
        """The policy for *kind*, else ``default``."""
        if kind is not None and kind in self.kinds:
            return self.kinds[kind]
        return self.default


def load_exit_config(path: Path | str | None = None) -> ExitConfig:
    """Load and validate an exits YAML file (default: ``config/exits.yaml``)."""
    p = Path(path) if path is not None else DEFAULT_EXITS_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return ExitConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Rule resolution (shared by the model, evaluate_position and the backtester)
# ---------------------------------------------------------------------------


class ResolvedRules(BaseModel):
    """A policy bound to one structure's entry economics. All values per share.

    ``tp_pnl(dte)`` / ``stop_pnl`` are P&L thresholds (``pnl ≥ tp`` takes profit,
    ``pnl ≤ stop`` stops out).
    """

    model_config = _FORBID

    credit: bool
    entry_net: float = Field(..., description="Per-share entry price, + debit / − credit")
    max_gain: float | None = Field(..., description="Per share; None = unbounded")
    max_loss: float | None = Field(..., description="Per share, positive; None = unbounded")
    policy: ExitPolicy

    @property
    def stop_pnl(self) -> float | None:
        s = self.policy.stop
        if s is None:
            return None
        if s.basis is StopBasis.CREDIT_MULTIPLE:
            if not self.credit:
                msg = "credit_multiple stop on a debit structure"
                raise ValueError(msg)
            return -s.value * -self.entry_net
        if s.basis is StopBasis.PCT_DEBIT:
            if self.credit:
                msg = "pct_debit stop on a credit structure"
                raise ValueError(msg)
            return -s.value * self.entry_net
        if self.max_loss is None:
            msg = "pct_max_loss stop on an unbounded-loss structure"
            raise ValueError(msg)
        return -s.value * self.max_loss

    def tp_pct(self, dte: int) -> float | None:
        return self.policy.take_profit_pct(credit=self.credit, dte=dte)

    def tp_pnl(self, dte: int) -> float | None:
        pct = self.tp_pct(dte)
        if pct is None:
            return None
        if self.credit:
            return None if self.max_gain is None else pct * self.max_gain
        return pct * self.entry_net

    def value_at_pnl(self, pnl: float) -> float:
        """Position value per share at which P&L = *pnl* (``V = N + pnl``)."""
        return self.entry_net + pnl


def resolve_rules(
    structure: Structure, policy: ExitPolicy, *, entry_net: float | None = None
) -> ResolvedRules:
    """Bind *policy* to *structure*'s entry price, max gain and max loss.

    *entry_net* overrides the structure's mid net price (e.g. the actual fill); max
    gain / max loss shift by the difference, since the expiry payoff is unchanged.
    """
    mid_net = float(structure.net_debit_credit)
    net = mid_net if entry_net is None else float(entry_net)
    shift = mid_net - net  # per share: a better fill raises max gain, lowers max loss
    return ResolvedRules(
        credit=net < 0,
        entry_net=net,
        max_gain=None if structure.max_gain is None else float(structure.max_gain) / 100.0 + shift,
        max_loss=None if structure.max_loss is None else float(structure.max_loss) / 100.0 - shift,
        policy=policy,
    )


def check_rules(rules: ResolvedRules, *, pnl: float, dte: int) -> ExitReason | None:
    """Which rule fires for a position with per-share *pnl* at mid and *dte* left.

    Order: stop → take profit → DTE exit. ``None`` = keep holding. Expiry is the
    caller's business (``dte == 0`` settles; it is not a rule).
    """
    stop = rules.stop_pnl
    if stop is not None and pnl <= stop:
        return ExitReason.STOP
    tp = rules.tp_pnl(dte)
    if tp is not None and pnl >= tp:
        return ExitReason.TAKE_PROFIT
    cad = rules.policy.close_at_dte
    if cad is not None and dte <= cad:
        return ExitReason.DTE_EXIT
    return None
