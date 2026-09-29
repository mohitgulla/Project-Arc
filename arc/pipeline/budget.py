"""Pipeline glue for the D32 daily options order budget (E6.5).

One helper reads the budget (local ``orders`` rows, the broker's option-order
list when the env carries a broker, and reserved working ladders), records it on
the run manifest and returns the tier-adjusted settings the step should run
under. The rules themselves live in :mod:`arc.budget.orders` (pure) and
:mod:`arc.gate.rules` (gate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from arc.budget.orders import (
    OrderBudget,
    OrderBudgetConfig,
    Tier,
    current_budget,
    tier_settings,
    worst_case_attempts,
)
from arc.routines.runs import RoutineStateRepo

if TYPE_CHECKING:
    import datetime as _dt

    from arc.config import ArcSettings
    from arc.pipeline.env import PipelineEnv
    from arc.routines.handlers import JobContext

__all__ = ["BudgetView", "NOTICE_KEY", "budget_notice", "read_budget"]

log = structlog.get_logger(__name__)

#: ``routine_state`` key holding the last tier a notice was posted for (per day).
NOTICE_KEY = "order_budget:notice"

_NOTICE_TEXT: dict[Tier, str] = {
    Tier.RESTRICTIVE: "order budget: restrictive tier on ({used}/{limit} orders used today)",
    Tier.OPENS_EXHAUSTED: (
        "order budget: opens stopped at {open_limit} ({used}/{limit}); closes may still run"
    ),
    Tier.EXHAUSTED: (
        "order budget EXHAUSTED ({used}/{limit}): no more orders today, closes included. "
        "Close positions in the Alpaca dashboard if needed."
    ),
}


class BudgetView:
    """What one step knows about the budget: the state and the settings to run under."""

    def __init__(self, budget: OrderBudget, settings: ArcSettings) -> None:
        self.budget = budget
        self.settings = tier_settings(settings, budget.tier)
        self.cfg = OrderBudgetConfig.from_settings(settings)

    @property
    def tier(self) -> Tier:
        return self.budget.tier

    @property
    def attempts(self) -> int:
        """Worst-case orders one proposal may cost under the current tier."""
        return worst_case_attempts(self.settings, self.tier)

    def metrics(self) -> dict[str, Any]:
        return {"order_budget": self.budget.brief()}


def read_budget(
    ctx: JobContext, env: PipelineEnv, settings: ArcSettings, *, now: _dt.datetime
) -> BudgetView:
    """Count today's orders (recorded on the manifest) and return the tier view.

    Fixture/dry runs have no broker, so the count is local-only (their fixture DB
    holds no orders, so the tier is ``normal`` unless a test seeds rows).
    """
    budget = current_budget(ctx.conn, env.broker, settings, now=now)
    ctx.record_input(
        "order_budget",
        "db" if env.broker is None else "db+broker",
        budget,
        as_of=now,
        count=budget.used,
    )
    log.info("order_budget.state", **budget.brief(), day=budget.day.isoformat())
    return BudgetView(budget, settings)


def budget_notice(ctx: JobContext, budget: OrderBudget) -> str:
    """The day-thread notice to post when the tier crossed a threshold, else ``""``.

    Fires once per (day, tier) as the count climbs: restrictive at
    ``restrict_at``, opens at the open limit, exhausted at the cap.
    """
    if budget.tier is Tier.NORMAL:
        return ""
    state = RoutineStateRepo(ctx.conn)
    marker = f"{budget.day.isoformat()}|{budget.tier.value}"
    last = state.get(NOTICE_KEY) or ""
    if last == marker:
        return ""
    if last.startswith(budget.day.isoformat()):
        order = list(Tier)
        if order.index(Tier(last.split("|", 1)[1])) >= order.index(budget.tier):
            return ""  # already at this tier or worse today
    state.set(NOTICE_KEY, marker, now=ctx.now)
    return _NOTICE_TEXT[budget.tier].format(
        used=budget.used, limit=budget.limit, open_limit=budget.open_limit
    )
