"""Deterministic paper position sizing (D18). Shared by E5.2 (entries) and E6.4 (swap opens).

    budget    = cap_pct × equity − existing max loss on the same underlying
    contracts = min(Risk.sizing_suggestion, floor(budget / max_loss_per_contract))

with ``cap_pct`` = the D116 tiered per-underlying cap (:func:`arc.gate.caps.alloc_tier`:
``max_alloc_pct`` 15% while deployed max loss < 60% of equity, else
``max_alloc_pct_reduced`` 10%; PLAN §5), and ``cap_label`` naming that tier in the
no-trade reason. At least one
contract is proposed when a single contract fits under the remaining budget;
otherwise there is no trade. The Risk persona's suggestion is advisory: it can
only lower the count, never raise it above the cap. A suggestion of 0 is Risk
declining the trade.

``existing_max_loss`` (Sentinel S-7) is the summed max loss of the open positions
on the same underlying (``Position.max_loss`` rows of
:func:`arc.pipeline.market.build_portfolio`). Sizing uses the budget that is
left, so a re-entry is sized down instead of being rejected by the gate's
per-underlying cap. The gate stays the final check; it is never relied on to size.

E22.3 (D97, D25): on a ``cash_settled`` profile the caller passes ``cash_available``
(settled cash minus what earlier opens in the same run reserved) and
``cash_per_contract`` (the gate's ``account_profile_settled_cash`` need for one unit:
worst band price x 100 + fees). Contracts are then also capped at
``floor(cash_available / cash_per_contract)`` (``cash_capped``; ``cash_zero`` when not
even one fits), so sizing stops proposing an order the gate's cash rule would reject.
``cash_available=None`` (margin profiles, backtests) leaves sizing as before.

Pure function: no I/O, no LLM, Decimal arithmetic throughout.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "SizingCode",
    "SizingResult",
    "apply_live_cap",
    "cash_after",
    "size_contracts",
]

# Stable outcome codes (the decision journal records them as ``sizing:<code>``).
SizingCode = Literal[
    "ok",
    "capped",
    "cap_zero",
    "budget_exhausted",
    "risk_zero",
    "unbounded",
    "invalid_input",
    "live_capped",
    "cash_capped",
    "cash_zero",
]


class SizingResult(BaseModel):
    """Outcome of :func:`size_contracts`. ``contracts == 0`` means no trade."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: int = Field(..., ge=0)
    cap_contracts: int = Field(..., ge=0, description="floor(remaining budget / max loss)")
    suggestion: int = Field(..., ge=0, description="Risk persona's advisory count")
    max_loss_total: Decimal = Field(..., ge=0, description="contracts × max loss per contract")
    pct_equity: float = Field(..., ge=0.0, description="max_loss_total / equity")
    existing_max_loss: Decimal = Field(
        Decimal(0), ge=0, description="Max loss already open on this underlying"
    )
    cash_contracts: int | None = Field(
        None,
        ge=0,
        description="E22.3: floor(cash available / cash per contract); None = no cash fit",
    )
    reason: str = Field("", description="Why there is no trade (contracts == 0)")
    code: SizingCode = Field("ok", description="Stable outcome code for the decision journal")

    @property
    def trade(self) -> bool:
        return self.contracts > 0


def size_contracts(
    *,
    suggestion: int,
    max_loss_per_contract: Decimal | None,
    equity: Decimal,
    cap_pct: float | Decimal,
    existing_max_loss: Decimal = Decimal(0),
    cash_available: Decimal | None = None,
    cash_per_contract: Decimal | None = None,
    cap_label: str | None = None,
) -> SizingResult:
    """Apply D18 against the per-underlying budget still free (see module doc).

    ``max_loss_per_contract`` is dollars per one structure unit. ``cash_available`` /
    ``cash_per_contract`` (E22.3, ``cash_settled`` profiles only) add the cash fit.
    """
    suggestion = max(int(suggestion), 0)
    existing = max(Decimal(existing_max_loss), Decimal(0))
    cash_cap: int | None = None

    def none(code: SizingCode, reason: str, cap: int = 0) -> SizingResult:
        return SizingResult(
            contracts=0,
            cap_contracts=cap,
            suggestion=suggestion,
            max_loss_total=Decimal(0),
            pct_equity=0.0,
            existing_max_loss=existing,
            cash_contracts=cash_cap,
            reason=reason,
            code=code,
        )

    if max_loss_per_contract is None:
        return none("unbounded", "max loss is unbounded")
    if max_loss_per_contract <= 0:
        return none(
            "invalid_input", f"max loss per contract {max_loss_per_contract} is not positive"
        )
    if equity <= 0:
        return none("invalid_input", f"equity {equity} is not positive")
    budget = Decimal(str(cap_pct)) * equity
    tier = f" ({cap_label})" if cap_label else ""
    remaining = max(budget - existing, Decimal(0))
    cap = int((remaining / max_loss_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if cap < 1:
        if existing > 0:
            return none(
                "budget_exhausted",
                f"existing max loss {existing} leaves {remaining} of the {budget} cap{tier}; "
                f"one contract needs {max_loss_per_contract}",
                cap,
            )
        return none(
            "cap_zero",
            f"one contract (max loss {max_loss_per_contract}) exceeds the {budget} cap{tier}",
            cap,
        )
    if suggestion < 1:
        return none("risk_zero", "Risk suggested 0 contracts", cap)
    contracts = min(suggestion, cap)
    code: SizingCode = "capped" if contracts < suggestion else "ok"
    if cash_available is not None:
        if cash_per_contract is None or cash_per_contract <= 0:
            return none(
                "invalid_input", f"cash per contract {cash_per_contract} is not positive", cap
            )
        free = max(Decimal(cash_available), Decimal(0))
        cash_cap = int((free / cash_per_contract).to_integral_value(rounding=ROUND_FLOOR))
        if cash_cap < 1:
            return none(
                "cash_zero",
                f"settled cash ${free} left does not cover one contract "
                f"(${cash_per_contract} incl. fees)",
                cap,
            )
        if cash_cap < contracts:
            contracts, code = cash_cap, "cash_capped"
    total = max_loss_per_contract * contracts
    return SizingResult(
        contracts=contracts,
        cap_contracts=cap,
        suggestion=suggestion,
        max_loss_total=total,
        pct_equity=float(total / equity),
        existing_max_loss=existing,
        cash_contracts=cash_cap,
        code=code,
    )


def apply_live_cap(size: SizingResult, cap: int | None, equity: Decimal) -> SizingResult:
    """D70: clamp a live open to *cap* contracts while the live scorecard gate is not met.

    ``cap=None`` (paper, or the live gate met) returns *size* unchanged, as does a
    no-trade result. Only ever lowers the count (a layer under D18, never above it).
    """
    if cap is None or not size.trade or size.contracts <= cap:
        return size
    per = size.max_loss_total / size.contracts
    total = per * cap
    return size.model_copy(
        update={
            "contracts": cap,
            "max_loss_total": total,
            "pct_equity": float(total / equity) if equity > 0 else 0.0,
            "code": "live_capped",
        }
    )


def cash_after(cash: Decimal, per_contract: Decimal, contracts: int) -> Decimal:
    """E22.3: settled cash left for the next open in the same run (floored at 0).

    A gate-passed open reserves ``per_contract x contracts``; settled cash is first
    come within a run, in Research rank order (no per-sleeve split, D97).
    """
    return max(cash - per_contract * max(contracts, 0), Decimal(0))
