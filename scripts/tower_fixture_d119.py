"""E25.8 (D120): regenerate ``web/src/lib/experiments.d119.fixture.ts``.

The D119 K=3 pre-registered report with a SPY series, served by the real tower API
(``/api/experiments/XP-3`` + its list row). Same seeded setup as
``tests/test_experiment_hac.py::test_multi_arm_pre_registered_pairs_and_holm`` (seed 31,
24 sessions): t1 = +400/day edge + 0.6 beta, t2 = pure 0.6 SPY beta, t3 = null.

    .venv/bin/python scripts/tower_fixture_d119.py

Writes only to a temp dir and the fixture file; never touches ``data/``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arc.experiments.evaluate import build_report, store_report  # noqa: E402
from arc.store.db import connect  # noqa: E402
from arc.store.migrate import migrate  # noqa: E402
from arc.tower.api import create_app  # noqa: E402
from arc.utils.calendar import previous_session  # noqa: E402
from tests import experiment_fixtures as fx  # noqa: E402
from tests.test_experiment_hac import ANALYSIS, CFG, FakeSpy, _after, _closes  # noqa: E402

OUT = ROOT / "web" / "src" / "lib" / "experiments.d119.fixture.ts"
HEAD = """/**
 * D119 K=3 pre-registered fixture (E25.8): `/api/experiments/XP-3` and its list row,
 * served by the real tower API from the seeded setup of
 * tests/test_experiment_hac.py::test_multi_arm_pre_registered_pairs_and_holm (seed 31,
 * 24 sessions, SPY series): t1 = +400/day edge + 0.6 beta, t2 = pure 0.6 SPY beta, t3 = null.
 * Regenerate with `.venv/bin/python scripts/tower_fixture_d119.py`; do not edit by hand.
 */
import type { ExperimentDetail, ExperimentRow } from "./experiments";

"""


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "arc.db"
        conn = connect(str(db))
        migrate(conn)
        rng = np.random.default_rng(31)
        n = 24
        x = list(rng.normal(0, 0.01, n))
        over = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
        sp = fx.spec(
            "XP-3",
            arms={"treatments": {f"t{i}": over for i in (1, 2, 3)}},
            max_sessions=40,
            analysis=ANALYSIS,
        )
        store = fx.start(conn, sp)
        days = fx.sessions(n)
        fx.pnl_row(conn, previous_session(days[0]), fx.T0_EQUITY, None)
        ctrl = list(rng.normal(20, 300, n))
        eq = {"c": fx.T0_EQUITY, "t1": fx.T0_EQUITY, "t2": fx.T0_EQUITY, "t3": fx.T0_EQUITY}
        for i, day in enumerate(days):
            eq["c"] += ctrl[i]
            fx.pnl_row(conn, day, eq["c"], None)
            for name, edge, b in (("t1", 400, 0.6), ("t2", 0, 0.6), ("t3", 0, 0.0)):
                eq[name] += ctrl[i] + edge + b * x[i] * fx.T0_EQUITY + rng.normal(0, 100)
                fx.pnl_row(conn, day, eq[name], f"XP-3:{name}")
        conn.commit()
        rep = build_report(
            conn, store.require("XP-3"), CFG, now=_after(days), market=FakeSpy(_closes(days, x))
        )
        with conn:
            store_report(conn, rep, run_id="e258")
        conn.close()
        client = TestClient(create_app(db, clock=lambda: _after(days)))
        detail = client.get("/api/experiments/XP-3").json()
        row = client.get("/api/experiments").json()["items"][0]
    OUT.write_text(
        HEAD
        + f"export const K3_D119_DETAIL = {json.dumps(detail, sort_keys=True)}"
        + " as unknown as ExperimentDetail;\n\n"
        + f"export const K3_D119_ROW = {json.dumps(row, sort_keys=True)}"
        + " as unknown as ExperimentRow;\n"
    )


if __name__ == "__main__":
    main()
