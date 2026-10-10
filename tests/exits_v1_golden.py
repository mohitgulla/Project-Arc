"""E18.1 (D78) rollback golden: exit-model and evaluate_position outputs under the v1 policy.

``python -m tests.exits_v1_golden --write`` regenerates
``tests/fixtures/exits_v1_golden.json``. It was generated on main before E18.1
(commit 9175fb1), so the test proves that the rollback values (``profit_lock:
null``, debit take profit 1.00) reproduce main's numbers exactly.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from arc.backtest.costs import CostModel
from arc.exits import (
    ExitModelConfig,
    ExitPolicy,
    OpenPosition,
    PositionMarks,
    StopBasis,
    StopRule,
    evaluate_position,
    model_exits,
)
from arc.structures import credit_vertical, debit_vertical, long_call, long_put

GOLDEN = Path(__file__).parent / "fixtures" / "exits_v1_golden.json"

AS_OF = dt.date(2026, 9, 25)
EXP = dt.date(2026, 10, 28)  # 33 DTE
CFG = ExitModelConfig(n_paths=3000, seed=11)
COST = CostModel()

#: main's (pre-E18.1) per-kind policies from config/exits.yaml.
V1_DEBIT = ExitPolicy(
    take_profit_pct_of_max_gain=None,
    take_profit_pct_of_debit=1.00,
    stop=StopRule(basis=StopBasis.PCT_DEBIT, value=0.75),
    stop_eod_only=True,
    close_at_dte=7,
)
V1_CREDIT = ExitPolicy(
    take_profit_pct_of_max_gain=0.50,
    take_profit_pct_of_debit=None,
    stop=StopRule(basis=StopBasis.PCT_MAX_LOSS, value=0.75),
    stop_eod_only=True,
    close_at_dte=7,
)


def structures() -> dict[str, Any]:
    return {
        "bull_call": debit_vertical(
            "call", "TST", EXP, long_strike=100, long_premium=2.60, short_strike=105,
            short_premium=0.90, as_of=AS_OF,
        ),
        "long_call": long_call("TST", EXP, 100, 2.60, as_of=AS_OF),
        "long_put": long_put("TST", EXP, 100, 2.40, as_of=AS_OF),
        "bull_put": credit_vertical(
            "put", "TST", EXP, short_strike=95, short_premium=1.60, long_strike=90,
            long_premium=0.60, as_of=AS_OF,
        ),
    }  # fmt: skip


def _policy(name: str) -> ExitPolicy:
    return V1_CREDIT if name == "bull_put" else V1_DEBIT


def _mids(st: Any, scale: float) -> dict[str, float]:
    return {leg.occ_symbol: round(float(leg.premium) * scale, 4) for leg in st.legs}


def compute() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, st in structures().items():
        pol = _policy(name)
        for rv in (None, 0.16):
            res = model_exits(st, pol, spot=100.0, iv=0.20, r=0.04, cost=COST, cfg=CFG,
                              realized_vol=rv)  # fmt: skip
            out[f"model:{name}:{rv}"] = res.model_dump(mode="json")
        for scale in (0.5, 1.0, 1.4, 2.2):
            state = evaluate_position(
                OpenPosition(structure=st),
                PositionMarks(
                    as_of=AS_OF, leg_mids=_mids(st, scale), spot=100.0, iv=0.20,
                    realized_vol=0.18,
                ),
                pol, cost=COST, cfg=CFG,
            )  # fmt: skip
            out[f"eval:{name}:{scale}"] = state.model_dump(mode="json")
    return out


if __name__ == "__main__":  # pragma: no cover - fixture generator
    if "--write" in sys.argv:
        GOLDEN.write_text(json.dumps(compute(), indent=1, sort_keys=True) + "\n")
    else:
        sys.stdout.write(json.dumps(compute(), sort_keys=True)[:400] + "\n")
