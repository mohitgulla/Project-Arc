"""Deterministic paper position sizing (D18). Shared by E5.2 (entries) and E6.4.

    contracts = min(Risk.sizing_suggestion, floor(cap_pct × equity / max_loss_per_contract))

with ``cap_pct`` = ``ArcSettings.max_alloc_pct`` (5%, PLAN §5). At least one
contract is proposed when a single contract fits under the cap; otherwise there
is no trade. The Risk persona's suggestion is advisory: it can only lower the
count, never raise it above the cap. A suggestion of 0 is Risk declining the
trade. The gate still enforces every portfolio cap afterwards.

Pure function: no I/O, no LLM, Decimal arithmetic throughout.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SizingCode", "SizingResult", "size_contracts"]

# Stable outcome codes (the decision journal records them as ``sizing:<code>``).
SizingCode = Literal["ok", "capped", "cap_zero", "risk_zero", "unbounded", "invalid_input"]


class SizingResult(BaseModel):
    """Outcome of :func:`size_contracts`. ``contracts == 0`` means no trade."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: int = Field(..., ge=0)
    cap_contracts: int = Field(..., ge=0, description="floor(cap × equity / max loss)")
    suggestion: int = Field(..., ge=0, description="Risk persona's advisory count")
    max_loss_total: Decimal = Field(..., ge=0, description="contracts × max loss per contract")
    pct_equity: float = Field(..., ge=0.0, description="max_loss_total / equity")
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
) -> SizingResult:
    """Apply D18. ``max_loss_per_contract`` is dollars per one structure unit."""
    suggestion = max(int(suggestion), 0)

    def none(code: SizingCode, reason: str, cap: int = 0) -> SizingResult:
        return SizingResult(
            contracts=0,
            cap_contracts=cap,
            suggestion=suggestion,
            max_loss_total=Decimal(0),
            pct_equity=0.0,
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
    cap = int((budget / max_loss_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if cap < 1:
        return none(
            "cap_zero",
            f"one contract (max loss {max_loss_per_contract}) exceeds the {budget} cap",
            cap,
        )
    if suggestion < 1:
        return none("risk_zero", "Risk suggested 0 contracts", cap)
    contracts = min(suggestion, cap)
    total = max_loss_per_contract * contracts
    return SizingResult(
        contracts=contracts,
        cap_contracts=cap,
        suggestion=suggestion,
        max_loss_total=total,
        pct_equity=float(total / equity),
        code="capped" if contracts < suggestion else "ok",
    )
