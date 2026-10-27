"""E27.4 (D126): the shadow-score monitor maths on fixtures (arc.ml.monitor)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

from arc.ml import monitor as mon

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def test_wilson_and_mean_ci() -> None:
    assert mon.wilson(0, 0) is None
    lo, hi = mon.wilson(15, 30)  # type: ignore[misc]
    assert lo < 0.5 < hi and hi - lo == pytest.approx(0.34, abs=0.01)
    assert mon.mean_ci([1.0]) is None
    lo, hi = mon.mean_ci([0.0, 1.0, 2.0])  # type: ignore[misc]
    assert lo < 1.0 < hi


def test_calibration_perfect_vs_base_and_insufficient() -> None:
    p = [0.2] * 50 + [0.8] * 50
    y = [0.0] * 40 + [1.0] * 10 + [0.0] * 10 + [1.0] * 40
    c = mon.calibration(p, y)
    assert c["n"] == 100 and c["status"] == "ok"
    assert c["base_rate"] == 0.5
    assert c["brier"] == pytest.approx(0.16) and c["brier_base"] == pytest.approx(0.25)
    assert [(b["n"], b["rate"]) for b in c["bins"]] == [(50, 0.2), (50, 0.8)]
    small = mon.calibration([0.5] * 29, [1.0] * 29)
    assert small["status"] == "insufficient" and small["n"] == 29
    assert mon.calibration([], [])["status"] == "insufficient"


def test_psi_zero_on_same_and_large_on_shift() -> None:
    rng = np.random.default_rng(1)
    a = rng.normal(size=5000)
    assert mon.psi(a, a) == pytest.approx(0.0, abs=1e-9)
    assert mon.psi(a, a + 1.0) > mon.PSI_WARN  # type: ignore[operator]
    assert mon.psi([], a) is None


def test_feature_drift_names_top_and_warns() -> None:
    rng = np.random.default_rng(2)
    train = pd.DataFrame({"a": rng.normal(size=2000), "b": rng.normal(size=2000)})
    live = pd.DataFrame({"a": rng.normal(size=40), "b": rng.normal(size=40) + 2.0})
    d = mon.feature_drift(train, live, ["a", "b", "missing"])
    assert d["features"] == 2 and d["top"][0]["feature"] == "b" and d["warn"] == ["b"]
    assert d["status"] == "ok"


def _entry(conn: sqlite3.Connection, at: str, scores: list[dict[str, Any]], **kw: object) -> None:
    payload = {
        "as_of": at[:10],
        "status": "ok",
        "model_version": "m2-x",
        "n": len(scores),
        "n_ok": sum(1 for s in scores if s["status"] == "ok"),
        "latency_ms": 80,
        "scores": scores,
        **kw,
    }
    conn.execute(
        "INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by, "
        "created_at, valid_from) VALUES (?, 'ml_scores', 'session', ?, 1, 'quant.open', ?, ?)",
        (f"e-{at}", json.dumps(payload), at, at),
    )


def test_health_coverage_calibration_and_render(tmp_path: Path) -> None:
    from arc.store.db import connect
    from arc.store.migrate import migrate

    conn = connect(tmp_path / "s.db")
    migrate(conn)
    ok: dict[str, Any] = {"status": "ok", "kind": "bull_put", "e_r": 0.05, "p_win": 0.6}
    bad: dict[str, Any] = {"status": "unavailable", "kind": "bull_put", "reason": "no bundle"}
    _entry(conn, "2026-10-26T10:00:00-04:00", [ok, ok, bad])
    _entry(conn, "2026-10-27T10:00:00-04:00", [ok])
    _entry(conn, "2026-09-01T10:00:00-04:00", [bad])  # outside the window
    rec = mon.health(conn, day=dt.date(2026, 10, 27), since=dt.date(2026, 10, 7))
    c = rec["coverage"]
    assert (c["ticks"], c["candidates"], c["scored"], c["pct_scored"]) == (2, 4, 3, 75.0)
    assert c["unavailable_reasons"] == {"no bundle": 1}
    assert c["scored_by_kind"] == {"bull_put": 3}
    assert rec["calibration"]["p_win"]["status"] == "insufficient"
    assert rec["model_versions"] == ["m2-x"]
    text = mon.render(rec)
    assert "3/4 candidates scored (75.0%)" in text
    assert "calibration p_win: insufficient (n=0 < 30 closed trades)" in text
    p = mon.write_health(tmp_path, rec)
    assert p.name == "2026-10-27.json" and json.loads(p.read_text())["schema"] == 1
