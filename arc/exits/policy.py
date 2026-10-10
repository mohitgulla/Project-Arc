"""Exit policy (PLAN D19/D23): one set of rules per structure kind, from ``config/exits.yaml``.

The same :class:`ExitPolicy` drives three consumers, which must not define their
own exit config:

- the managed-exit Monte Carlo model (:mod:`arc.exits.model`), which puts numbers on
  the proposal card (E6.1a);
- the Quant exit step's live decisions (E6.2 base exits, E6.4 profit-taking and
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

- ``credit_multiple``: close when the **debit to close reaches value × credit**, i.e.
  ``L = (value − 1) · C`` (the usual "2x credit stop": a $1.66 credit is stopped at a
  $3.32 debit, a $1.66 loss). ``value > 1``. Credit structures only.
- ``pct_max_loss``: ``L = value · max_loss``. Any structure.
- ``pct_debit``: ``L = value · D`` (0.5 = the position lost half its debit). Debit
  structures only.

*Profit lock* (E18.1, D78; ``profit_lock: {arm_pct, floor_pct, eod_only}``, null =
off), a trailing take profit on the take-profit basis ``B`` (max gain for credit
structures, the debit for debit structures): once the position's **peak** P&L since
entry reached ``arm_pct · B``, close when the current P&L falls to ``floor_pct · B``
or below (``0.50 / 0.20`` on a $2.00 debit: once up $1.00, close if it falls back to
+$0.40). The peak comes from the stored marks (``peak_pnl``); without any stored
mark the lock is not armed. ``0 < floor_pct < arm_pct < take-profit pct``.

*Time*: ``close_at_dte`` closes the position once remaining DTE ≤ that value.
``time_adjusted_targets`` (D19 decay-adjusted target) replace the take-profit
percentage once remaining DTE ≤ ``dte_lte``; the tightest matching bucket
(smallest ``dte_lte``) wins.

Rules are checked each day in the order **stop → profit lock → take profit → DTE
exit**.

*Stop timing*: with ``stop_eod_only`` (default) the stop is only evaluated on
end-of-day marks (``check_rules(..., eod=True)``); take profit and the DTE exit may
fire on any mark. The Monte Carlo model and the backtester step on daily closes, so
every step is end of day for them.

Defaults (``config/exits.yaml``, D23 owner decision 2026-09-27): take profit 50% of
max gain (credit), close at 7 DTE, and **relaxed stops** so a trade can play out:
``pct_max_loss 0.75`` for credit structures and ``pct_debit 0.75`` for debit
structures, on end-of-day marks only. D78 (2026-10-10): debit take profit 60% of
the debit (was 100%) and a ``0.50 → 0.20`` profit lock on the debit kinds.

Everything here is deterministic and pure (no LLM, no network).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arc.exits.expiry import ExpiryGuard
from arc.models import StructureKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from arc.models import Structure

__all__ = [
    "MenuRank",
    "CREDIT_KINDS",
    "DEBIT_KINDS",
    "DEFAULT_EXITS_PATH",
    "ExitConfig",
    "ExitModelConfig",
    "ExitPolicy",
    "ExitReason",
    "IvModel",
    "PipelineExitConfig",
    "ProfitLock",
    "ResolvedRules",
    "StopBasis",
    "StopRule",
    "TimeAdjustedTarget",
    "check_rules",
    "load_exit_config",
    "lock_fires",
    "peak_pnl",
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
    PROFIT_LOCK = "profit_lock"  # E18.1 (D78)


class StopRule(BaseModel):
    model_config = _FORBID

    basis: StopBasis
    value: float = Field(..., gt=0.0)

    @model_validator(mode="after")
    def _range(self) -> StopRule:
        if self.basis is StopBasis.CREDIT_MULTIPLE:
            if self.value <= 1.0:
                msg = f"credit_multiple is a debit-to-close multiple, > 1; got {self.value}"
                raise ValueError(msg)
        elif self.value > 1.0:
            msg = f"stop {self.basis.value} is a fraction in (0, 1]; got {self.value}"
            raise ValueError(msg)
        return self


class TimeAdjustedTarget(BaseModel):
    """Take-profit percentage that applies once remaining DTE ≤ ``dte_lte`` (D19)."""

    model_config = _FORBID

    dte_lte: int = Field(..., ge=0)
    take_profit_pct: float = Field(..., gt=0.0)


class ProfitLock(BaseModel):
    """E18.1 (D78) trailing take profit, in units of the take-profit basis.

    Armed once the peak P&L since entry ≥ ``arm_pct · B``; then closes when the
    current P&L ≤ ``floor_pct · B`` (``B`` = max gain for credit, debit for debit).
    ``eod_only``: evaluated on end-of-day marks only (default: every mark).
    """

    model_config = _FORBID

    arm_pct: float = Field(..., gt=0.0, le=3.0)
    floor_pct: float = Field(..., gt=0.0, le=3.0)
    eod_only: bool = False

    @model_validator(mode="after")
    def _order(self) -> ProfitLock:
        if not self.floor_pct < self.arm_pct:
            msg = f"profit_lock: floor_pct {self.floor_pct} must be < arm_pct {self.arm_pct}"
            raise ValueError(msg)
        return self


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
    stop_eod_only: bool = Field(True, description="Evaluate the stop on end-of-day marks only")
    close_at_dte: int | None = Field(7, ge=0)
    time_adjusted_targets: list[TimeAdjustedTarget] = Field(default_factory=list)
    profit_lock: ProfitLock | None = Field(
        None, description="E18.1 (D78) trailing take profit; null = off"
    )

    @model_validator(mode="after")
    def _lock_below_take_profit(self) -> ExitPolicy:
        """``arm_pct`` must sit below every base take-profit pct (else it never acts)."""
        lock = self.profit_lock
        if lock is None:
            return self
        for name in ("take_profit_pct_of_max_gain", "take_profit_pct_of_debit"):
            tp = getattr(self, name)
            if tp is not None and not lock.arm_pct < tp:
                msg = f"profit_lock: arm_pct {lock.arm_pct} must be < {name} {tp}"
                raise ValueError(msg)
        return self

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
        if self.stop is None:
            parts.append("no stop")
        else:
            eod = " (end of day)" if self.stop_eod_only else ""
            parts.append(f"stop {self.stop.basis.value} {self.stop.value:g}{eod}")
        if self.profit_lock is not None:
            lk = self.profit_lock
            eod = " (end of day)" if lk.eod_only else ""
            parts.append(f"profit lock {lk.arm_pct:.0%} -> {lk.floor_pct:.0%}{eod}")
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
    path_vol: Literal["realized_forecast", "iv"] = Field(
        "realized_forecast",
        description="Vol the paths move at: the realised-vol forecast (mean HV20/HV60) when "
        "the caller has one, else IV; 'iv' always uses IV. Marks are always priced at IV.",
    )


MenuRank = Literal["scanner", "managed_net_ev", "rorc_day", "vrp"]


class PipelineExitConfig(BaseModel):
    """How the E5.2 pipeline uses the model."""

    model_config = _FORBID

    rank_menu_by: MenuRank = Field(
        "scanner",
        description="Quant menu order: 'scanner' keeps the scanner's rank_by (credit_width); "
        "or managed_net_ev / rorc_day / vrp, highest first (owner decision pending)",
    )


class PositionsConfig(BaseModel):
    """E6.4 position manager (``positions:`` in exits.yaml).

    ``remaining_ev_floor_per_bp``: suggest closing a position once its remaining
    net EV (hold under the policy vs close now, after costs) per $ of buying power
    it holds falls below this (``-0.01`` = expected to lose > 1% of that BP vs
    closing now); ``null`` = off. ``kinds`` overrides per structure kind.

    ``remaining_ev_floor_eod_only`` (E6.4a, default on): the floor is only evaluated
    on end-of-day marks (the chain's ``eod_marks_from`` window), like the D23 stop,
    so it never fires on the fill day's intraday evaluations.
    """

    model_config = _FORBID

    remaining_ev_floor_per_bp: float | None = Field(-0.01, ge=-1.0, le=1.0)
    remaining_ev_floor_eod_only: bool = Field(
        True, description="Evaluate the remaining-EV floor on end-of-day marks only"
    )
    kinds: dict[StructureKind, float | None] = Field(default_factory=dict)
    expiry_guard: ExpiryGuard = Field(
        default_factory=ExpiryGuard,
        description="E11.4 (D73): flat by DTE, closing-window retries, expiry-day cutoff, DNE",
    )

    def floor_for(self, kind: StructureKind | None) -> float | None:
        if kind is not None and kind in self.kinds:
            return self.kinds[kind]
        return self.remaining_ev_floor_per_bp


class ExitConfig(BaseModel):
    """Validated ``config/exits.yaml``."""

    model_config = _FORBID

    model: ExitModelConfig = Field(default_factory=ExitModelConfig)
    pipeline: PipelineExitConfig = Field(default_factory=PipelineExitConfig)
    positions: PositionsConfig = Field(default_factory=PositionsConfig)
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


def load_exit_config(
    path: Path | str | None = None, *, overrides: Mapping[tuple[str, ...], object] | None = None
) -> ExitConfig:
    """Load and validate an exits YAML file (default: ``config/exits.yaml``).

    *overrides* (D26 control panel, ``path -> value``) patch the YAML before
    validation; :func:`arc.control.exit_config` returns the effective config.
    """
    from arc.utils.yamlpatch import apply_overrides

    p = Path(path) if path is not None else DEFAULT_EXITS_PATH
    data = yaml.safe_load(p.read_text()) or {}
    return ExitConfig.model_validate(apply_overrides(data, overrides))


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
            return -(s.value - 1.0) * -self.entry_net
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

    @property
    def tp_basis(self) -> float | None:
        """Per-share basis of the take profit and the profit lock (max gain / debit)."""
        if self.credit:
            return self.max_gain
        return self.entry_net if self.entry_net > 0 else None

    @property
    def lock_arm_pnl(self) -> float | None:
        """Per-share peak P&L that arms the profit lock (``None`` = no lock)."""
        lock, basis = self.policy.profit_lock, self.tp_basis
        return None if lock is None or basis is None else lock.arm_pct * basis

    @property
    def lock_floor_pnl(self) -> float | None:
        """Per-share P&L at or below which an armed profit lock closes."""
        lock, basis = self.policy.profit_lock, self.tp_basis
        return None if lock is None or basis is None else lock.floor_pct * basis

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


def peak_pnl(marks: Iterable[float | Decimal | None]) -> Decimal | None:
    """Peak per-share P&L over stored *marks* (E18.1); ``None`` = no usable mark.

    Non-finite and missing marks are skipped; a peak is never made up.
    """
    vals = [d for d in (Decimal(str(m)) for m in marks if m is not None) if d.is_finite()]
    return max(vals) if vals else None


def lock_fires(rules: ResolvedRules, *, pnl: float, peak: float | None, eod: bool = True) -> bool:
    """The profit lock closes: armed (peak ≥ arm) and the current P&L ≤ floor."""
    lock = rules.policy.profit_lock
    arm, floor = rules.lock_arm_pnl, rules.lock_floor_pnl
    if lock is None or arm is None or floor is None or peak is None:
        return False
    if lock.eod_only and not eod:
        return False
    return peak >= arm and pnl <= floor


def check_rules(
    rules: ResolvedRules,
    *,
    pnl: float,
    dte: int,
    eod: bool = True,
    peak_pnl: float | None = None,
) -> ExitReason | None:
    """Which rule fires for a position with per-share *pnl* at mid and *dte* left.

    Order: stop → profit lock → take profit → DTE exit. ``None`` = keep holding.
    Expiry is the caller's business (``dte == 0`` settles; it is not a rule). *eod*
    says whether the mark is an end-of-day mark; with ``stop_eod_only`` the stop is
    skipped otherwise. *peak_pnl* is the peak per-share P&L of the stored marks since
    entry (:func:`peak_pnl`); ``None`` leaves the profit lock unarmed.
    """
    stop = rules.stop_pnl
    stop_live = eod or not rules.policy.stop_eod_only
    if stop is not None and stop_live and pnl <= stop:
        return ExitReason.STOP
    if lock_fires(rules, pnl=pnl, peak=peak_pnl, eod=eod):
        return ExitReason.PROFIT_LOCK
    tp = rules.tp_pnl(dte)
    if tp is not None and pnl >= tp:
        return ExitReason.TAKE_PROFIT
    cad = rules.policy.close_at_dte
    if cad is not None and dte <= cad:
        return ExitReason.DTE_EXIT
    return None
