"""E27.4 nightly ``ml.refresh``: live panel, parity on the overlapping session, score
drift by kind, the ``ml_health`` record and the routine handler."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from arc.ml import monitor as mon
from arc.ml import refresh as rf
from arc.ml.features.registry import schema_version
from tests.test_ml_feature_parity import ALL, END, _history_dir, _live_store
from tests.test_ml_feature_parity import facts as facts  # noqa: F401 - module fixture

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module")
def refreshed(facts: dict[str, Any], tmp_path_factory: Any) -> tuple[Path, Any, rf.RefreshResult]:
    hdir = _history_dir(tmp_path_factory.mktemp("refresh"), facts)
    conn = _live_store(facts)
    ticks = iter(range(1000))
    res = rf.run_refresh(
        conn,
        data_dir=hdir,
        day=END,
        tickers=ALL,
        family="credit",
        etfs=frozenset({"SPY"}),
        extend_store=False,
        clock=lambda: float(next(ticks)),
    )
    return hdir, conn, res


def test_refresh_writes_the_live_panel_and_health_record(refreshed) -> None:
    hdir, _, res = refreshed
    assert res.panel_rows == len(ALL)
    stored = pd.read_parquet(res.panel_path)
    assert set(stored["ticker"]) == set(ALL) and set(stored["date"]) == {END}
    assert res.panel_path == (
        hdir / "ml" / "live" / "features" / f"v{schema_version()}" / f"date={END}.parquet"
    )
    assert res.health_path == mon.health_path(hdir, END)
    rec = json.loads(res.health_path.read_text())
    assert rec["schema"] == mon.HEALTH_SCHEMA and rec["day"] == END.isoformat()
    assert rec["serving"]["status"] == "unavailable"  # no registered model: fail-open
    assert rec["feature_drift"]["status"] == "not_run"
    assert rec["refresh"]["panel_rows"] == len(ALL)
    assert res.m1_rows == 0
    assert "live panel" in res.summary() and res.metrics()["panel_rows"] == len(ALL)


def test_refresh_parity_on_the_overlapping_session_is_ok(refreshed) -> None:
    _, _, res = refreshed
    par = res.health["parity"]
    assert par["status"] == "ok", par
    assert par["day"] == END.isoformat() and par["names"] == len(ALL)
    assert par["over_tolerance"] == 0 and par["features"] > 0


def test_a_failing_step_fails_open(facts: dict[str, Any], tmp_path: Path) -> None:
    hdir = _history_dir(tmp_path, facts)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("tape unreadable")

    res = rf.run_refresh(
        _live_store(facts),
        data_dir=hdir,
        day=END,
        tickers=ALL,
        family="credit",
        extend_store=False,
        live_inputs_fn=broken,
    )
    assert "inputs" in res.errors and res.panel_rows == 0
    assert res.health_path is not None  # the record is still written
    assert res.health["parity"]["status"] == "not_run"
    assert "step errors" in res.summary()


def test_history_cache_end_reads_the_ohlc_cache(refreshed, tmp_path: Path) -> None:
    hdir, _, _ = refreshed
    assert rf.history_cache_end(hdir, END) == END
    assert rf.history_cache_end(hdir, END + dt.timedelta(days=3)) == END
    assert rf.history_cache_end(tmp_path, END) is None


def test_latest_health_and_monitor_render(refreshed, tmp_path: Path) -> None:
    hdir, _, _ = refreshed
    rec = rf.latest_health(hdir, END + dt.timedelta(days=2))
    assert rec is not None and rec["day"] == END.isoformat()
    assert rf.latest_health(hdir, END - dt.timedelta(days=1)) is None
    assert rf.latest_health(tmp_path, END) is None
    rec = {
        **rec,
        "score_drift": rf.score_drift_by_kind(
            {"all": {"e_r": [0.1] * 40, "p_win": [0.5] * 40}, "bull_put": {"e_r": [0.1]}},
            {"all": {"e_r": [0.0] * 40, "p_win": [0.5] * 40}},
        ),
        "last_retrain": {
            "version": "m2-x",
            "passed": False,
            "holdout_start": "2026-02-01",
            "holdout_end": "2026-07-31",
            "n": 900,
            "gap_days": 60,
        },
    }
    text = mon.render(rec)
    assert "score drift e_r [all]" in text and "[bull_put]" in text
    assert "last retrain: m2-x fail" in text and "candidate only" in text


def test_score_drift_reference_and_live_values_by_kind(tmp_path: Path) -> None:
    vdir = tmp_path / "credit" / "m2-a"
    vdir.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": [dt.date(2022, 10, 3), dt.date(2022, 10, 4), dt.date(2022, 11, 1)],
            "kind": ["bull_put", "iron_condor", "bull_put"],
            "e_r": [0.1, 0.2, 9.0],
            "p_win": [0.6, 0.7, 0.1],
            "fold": ["test2022"] * 3,
        }
    ).to_parquet(vdir / "oos_scores.parquet")
    ref = rf.score_drift_ref(tmp_path, "credit", "m2-a", 10)
    assert ref["all"]["e_r"] == [0.1, 0.2]
    assert ref["bull_put"] == {"e_r": [0.1], "p_win": [0.6]}
    assert rf.score_drift_ref(tmp_path, "credit", "missing", 10) == {}
    entries = [
        {
            "as_of": "2026-10-20T20:00:00+00:00",
            "scores": [
                {"status": "ok", "kind": "bull_put", "e_r": 0.3, "p_win": 0.55},
                {"status": "unavailable", "kind": "bull_put", "e_r": None},
            ],
        },
        {"as_of": "2026-09-30T20:00:00+00:00", "scores": [{"status": "ok", "kind": "x"}]},
        {"as_of": "bad", "scores": []},
    ]
    live = rf.live_score_values(entries, 10)
    assert live["all"] == {"e_r": [0.3], "p_win": [0.55]} and "x" not in live
    out = rf.score_drift_by_kind(live, ref)
    assert set(out["by_kind"]) == {"bull_put", "iron_condor"}
    assert out["all"]["e_r"]["status"] == "insufficient"


def test_refresh_tickers_cover_active_reference_and_open(monkeypatch) -> None:
    import arc.universe.tiers as tiers

    monkeypatch.setattr(tiers, "active_tickers", lambda *_a: ["MSFT", "AAPL"])
    monkeypatch.setattr(tiers, "market_reference", lambda _s: ["SPY", "QQQ", "IWM"])
    monkeypatch.setattr(tiers, "open_underlyings", lambda _c: ["TSLA", "AAPL"])
    names = rf.refresh_tickers(None, None, dt.datetime(2026, 10, 27, 20, 45))  # type: ignore[arg-type]
    assert names == ["AAPL", "IWM", "MSFT", "QQQ", "SPY", "TSLA"]


def test_refresh_handler_runs_the_refresh(refreshed, monkeypatch) -> None:
    from types import SimpleNamespace

    import arc.ml.refresh as refresh
    from arc.routines.handlers import ml_refresh_source
    from arc.utils.calendar import ET

    hdir, conn, _ = refreshed
    seen: dict[str, Any] = {}

    def fake_run(c: Any, **kw: Any) -> rf.RefreshResult:
        seen.update(kw, conn=c)
        out = rf.RefreshResult(day=kw["day"], panel_rows=4)
        out.health_path = hdir / "x.json"
        return out

    monkeypatch.setattr(refresh, "run_refresh", fake_run)
    monkeypatch.setattr(refresh, "refresh_tickers", lambda *_a: ["AAA", "SPY"])
    ctx = SimpleNamespace(
        spec=SimpleNamespace(options={"universe": "c250"}),
        now=dt.datetime(2026, 9, 30, 20, 45, tzinfo=ET),
        conn=conn,
        settings=SimpleNamespace(account_profile="margin", ml_data_dir=hdir),
    )
    res = ml_refresh_source(ctx, data_dir=hdir)  # type: ignore[arg-type]
    assert seen["day"] == END and seen["tickers"] == ["AAA", "SPY"]
    assert seen["family"] == "credit" and seen["universe"] == "c250"
    assert res.metrics["panel_rows"] == 4  # noqa: PLR2004

    def no_health(c: Any, **kw: Any) -> rf.RefreshResult:
        return rf.RefreshResult(day=kw["day"], errors={"health": "OSError: disk"})

    monkeypatch.setattr(refresh, "run_refresh", no_health)
    with pytest.raises(RuntimeError, match="no ml_health record"):
        ml_refresh_source(ctx, data_dir=hdir)  # type: ignore[arg-type]


def test_feature_drift_against_the_serving_models_training_window(
    facts: dict[str, Any], tmp_path: Path
) -> None:
    """A served bundle: drift compares the live panels with its train-window panels."""
    from arc.ml.features.compute import compute_panel
    from arc.ml.features.store import date_path, version_dir
    from tests.test_ml_serve import _bundle

    hdir = _history_dir(tmp_path, facts)
    schema = schema_version()
    feat = "ret_1d"
    vdir = _bundle(hdir / "ml" / "models", schema=schema)
    (vdir / "features.json").write_text(json.dumps([feat]))
    man = json.loads((vdir / "manifest.json").read_text())
    from arc.ml.registry import hash_files

    man["files"] = hash_files(vdir, list(man["files"]))
    man["train_window"] = {"start": "2026-08-01", "end": "2026-09-25"}
    (vdir / "manifest.json").write_text(json.dumps(man))
    from arc.ml.features.adapters.history import history_inputs

    inp = history_inputs(hdir, ALL, dt.date(2026, 8, 1), dt.date(2026, 9, 25))
    from arc.utils.calendar import sessions_between

    hist = compute_panel(inp, sessions_between(dt.date(2026, 9, 1), dt.date(2026, 9, 25)), ALL)
    fv = version_dir(hdir / "ml" / "features", schema)
    fv.mkdir(parents=True, exist_ok=True)
    for d, g in hist.groupby("date"):
        g.to_parquet(date_path(fv, d), index=False)
    res = rf.run_refresh(
        _live_store(facts),
        data_dir=hdir,
        day=END,
        tickers=ALL,
        family="credit",
        extend_store=False,
    )
    drift = res.health["feature_drift"]
    assert drift["train_window"] == {"start": "2026-08-01", "end": "2026-09-25"}
    assert drift["live_sessions"] == 1
    assert res.health["serving"]["version"] == "m2-aaa"
    assert "drift" not in res.errors
