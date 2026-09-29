"""Deterministic paper position sizing (D18). Shared by E5.2 (entries) and E6.4 (swap opens).

    budget    = cap_pct × equity − existing max loss on the same underlying
    contracts = min(Risk.sizing_suggestion, floor(budget / max_loss_per_contract))

with ``cap_pct`` = ``ArcSettings.max_alloc_pct`` (5%, PLAN §5). At least one
contract is proposed when a single contract fits under the remaining budget;
otherwise there is no trade. The Risk persona's suggestion is advisory: it can
only lower the count, never raise it above the cap. A suggestion of 0 is Risk
declining the trade.

``existing_max_loss`` (Sentinel S-7) is the summed max loss of the open positions
on the same underlying (``Position.max_loss`` rows of
:func:`arc.pipeline.market.build_portfolio`). Sizing uses the budget that is
left, so a re-entry is sized down instead of being rejected by the gate's
per-underlying cap. The gate stays the final check; it is never relied on to size.

Pure function: no I/O, no LLM, Decimal arithmetic throughout.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SizingCode", "SizingResult", "size_contracts"]

# Stable outcome codes (the decision journal records them as ``sizing:<code>``).
SizingCode = Literal[
    "ok", "capped", "cap_zero", "budget_exhausted", "risk_zero", "unbounded", "invalid_input"
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
) -> SizingResult:
    """Apply D18 against the per-underlying budget still free (see module doc).

    ``max_loss_per_contract`` is dollars per one structure unit.
    """
    suggestion = max(int(suggestion), 0)
    existing = max(Decimal(existing_max_loss), Decimal(0))

    def none(code: SizingCode, reason: str, cap: int = 0) -> SizingResult:
        return SizingResult(
            contracts=0,
            cap_contracts=cap,
            suggestion=suggestion,
            max_loss_total=Decimal(0),
            pct_equity=0.0,
            existing_max_loss=existing,
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
    remaining = max(budget - existing, Decimal(0))
    cap = int((remaining / max_loss_per_contract).to_integral_value(rounding=ROUND_FLOOR))
    if cap < 1:
        if existing > 0:
            return none(
                "budget_exhausted",
                f"existing max loss {existing} leaves {remaining} of the {budget} cap; "
                f"one contract needs {max_loss_per_contract}",
                cap,
            )
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
        existing_max_loss=existing,
        code="capped" if contracts < suggestion else "ok",
    )
