"""E12.5 golden helper: a Director prompt with a non-empty book, plus its rules.

Imported by tests/test_director_diversification.py and run stand-alone against an
origin/main checkout (``python tests/diversification_golden.py <repo>``) to pin the
strict-mode hashes. Uses only APIs that exist on main before E12.5.
"""

from __future__ import annotations

import hashlib
import sys
from typing import Any

PORTFOLIO_BLOCK = (
    "Equity $100,000.00 (day P&L +120, open P&L +80). Cash $95,000, buying power "
    "$190,000. 2 of 8 positions open; 8% of equity max loss per underlying.\n\n"
    "Open positions (largest max loss first; 2 of 2 shown):\n"
    "- os-1 NVDA vertical_debit bullish x2 30 DTE (expiry bucket 3-6w); sector "
    "technology; entry +2.10; mark P&L $+40 (10% of max gain, 5% of max loss); max "
    "loss $420 (0.4% of equity); Δ +30.0 ν +4.0 Θ -2.0\n  Thesis: AI capex\n"
    "- os-2 MU vertical_debit bullish x1 30 DTE (expiry bucket 3-6w); sector "
    "technology; entry +1.80; mark P&L $+40 (10% of max gain, 5% of max loss); max "
    "loss $180 (0.2% of equity); Δ +15.0 ν +2.0 Θ -1.0\n  Thesis: HBM pricing\n\n"
    "Allocation of open max loss ($600): by underlying NVDA 70%, MU 30%; by sector "
    "technology 100%; by stance bullish 100%; by expiry bucket 3-6w 100%. HHI 0.58.\n"
    "Flags: over_concentrated_sector, stance_skew (sectors technology) (stances bullish)."
)


def director_prompt(snap: Any, **extra: Any) -> str:
    from arc.personas.builders import build_director_prompt, director_input_from_context

    inp = director_input_from_context(
        snap,
        portfolio_summary="2 open",
        scan_date="2026-10-06",
        portfolio_block=PORTFOLIO_BLOCK,
        **extra,
    )
    return build_director_prompt(inp)


def portfolio_context(
    positions: list[tuple[str, str, str, float]],
    *,
    flagged_sectors: list[str],
    flagged_stances: list[str],
) -> Any:
    """A PortfolioContext from ``(ticker, sector, stance, max_loss)`` rows."""
    import datetime as dt

    from arc.models import Stance
    from arc.positions.portfolio import (
        GreekUsage,
        PortfolioAccount,
        PortfolioAggregates,
        PortfolioContext,
        PortfolioPosition,
    )
    from arc.utils.calendar import ET

    now = dt.datetime(2026, 10, 6, 10, 15, tzinfo=ET)
    total = sum(p[3] for p in positions) or 1.0
    by_under: dict[str, float] = {}
    for t, _, _, ml in positions:
        by_under[t] = by_under.get(t, 0.0) + ml / total
    return PortfolioContext(
        as_of=now,
        empty=False,
        account=PortfolioAccount(
            equity=100_000.0,
            cash=95_000.0,
            buying_power=190_000.0,
            halted=False,
            order_budget_tier="normal",
        ),
        positions=[
            PortfolioPosition(
                structure_id=f"os-{i}",
                ticker=t,
                sector=sec,
                kind="vertical_debit",
                stance=Stance(stance),
                dte=30,
                expiry_bucket="22-45",
                contracts=1,
                entry_net=2.0,
                max_loss_total=ml,
                max_loss_pct_equity=ml / 100_000.0,
                opened_at=now.isoformat(),
            )
            for i, (t, sec, stance, ml) in enumerate(positions, start=1)
        ],
        aggregates=PortfolioAggregates(
            total_max_loss=total,
            by_underlying=by_under,
            delta=GreekUsage(net=0.0, cap=1.0),
            vega=GreekUsage(net=0.0, cap=1.0),
            gamma=0.0,
            theta=0.0,
            positions=len(positions),
            max_positions=8,
            flagged_sectors=flagged_sectors,
            flagged_stances=flagged_stances,
        ),
    )


def strict_rules() -> list[str]:
    from arc.config import ArcSettings
    from arc.models import Stance
    from arc.pipeline.steps import _director_rules

    pctx = portfolio_context(
        [("NVDA", "technology", "bullish", 420.0), ("MU", "technology", "bullish", 180.0)],
        flagged_sectors=["technology"],
        flagged_stances=["bullish"],
    )
    settings = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    return _director_rules({"AMD": Stance.BULLISH}, settings, None, pctx)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


if __name__ == "__main__":  # pragma: no cover - run by hand against origin/main
    sys.path.insert(0, sys.argv[1])
    from tests import finnhub_golden as g

    print("director", sha(director_prompt(g.snapshot(with_finnhub=False))))
    print("rules", sha("\n".join(strict_rules())))
