"""E27.9 (D133): ``/api/ml/*`` over a fixture ``data/ml`` tree.

The M1 version is a trimmed copy of the real ``m1-53833784f276`` eval CSVs (aggregates
only, no rows). The M2 version, the health record, the ledger, the specs and the
Analyst draft are synthetic and built here.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from arc.tower.api import create_app
from arc.tower.data_ml import MlPaths, load_ml_model, load_ml_models, load_ml_studies

FIXTURE = Path(__file__).parent / "fixtures" / "ml"
M1 = "m1-53833784f276"
M2 = "m2-aaaaaaaaaaaa"
NOW = dt.datetime(2026, 10, 28, 9, 0, tzinfo=dt.UTC)
HYP5 = "A rolling 252-session CQR window fixes 2022 vol coverage"


def _csv(p: Path, header: str, rows: list[str]) -> None:
    p.write_text("\n".join([header, *rows]) + "\n")


def _m2(root: Path, *, status: str = "candidate", champion: str | None = None) -> Path:
    fam = root / "data/ml/models/credit"
    v = fam / M2
    v.mkdir(parents=True)
    (fam / "models.json").write_text(
        json.dumps(
            {
                "family": "credit",
                "champion": champion,
                "versions": {
                    M2: {
                        "created": "2026-10-26T16:51:52-04:00",
                        "feature_schema": "45ba7c64e725",
                        "rule": "m2_promote",
                        "rule_passed": False,
                        "status": status,
                        "summary": {"er_ic": {"test2023": 0.14}},
                    }
                },
            }
        )
    )
    (fam / "holdout_reads.jsonl").write_text(
        json.dumps({"kind": "rolling", "at": "2026-10-20"}) + "\n"
    )
    (v / "manifest.json").write_text(
        json.dumps(
            {
                "version": M2,
                "family": "credit",
                "created": "2026-10-26T16:51:52-04:00",
                "feature_schema": "45ba7c64e725",
                "data_digest": "d" * 64,
                "code_sha": "c" * 40,
                "study": "ML-2",
                "profile": "cash_credit",
                "n_rows": 1000,
                "n_raw_ledger_rows": 5000,
                "train_window": {"start": "2020-07-01", "end": "2023-12-31"},
                "rounds": {"win": 100, "stop": 90, "e_r": 80},
                "config": {
                    "learning_rate": 0.05,
                    "grid": [{}, {}],
                    "calibration": {"method": "isotonic"},
                },
                "timings": {
                    "total_s": 233.5,
                    "fold_fit_s": 141.3,
                    "final_fit_s": 31.3,
                    "peak_rss_mb": 8259.0,
                },
            }
        )
    )
    (v / "features.json").write_text(json.dumps(["ret_5d", "iv_rank", "rorc_day"]))
    (v / "feature_groups.json").write_text(
        json.dumps(
            {"ret_5d": "ticker_price", "iv_rank": "ticker_options", "rorc_day": "candidate_ledger"}
        )
    )
    _csv(
        v / "eval_rule.csv",
        "fold,ll_m2,ll_logistic,ll_managed_pop,beats_logistic,beats_managed_pop,er_ic,er_ic_t,"
        "ic_pass,replay_pass,net_incumbent,net_ml_er,dd_incumbent,dd_ml_er,year_pass",
        [
            "test2022,0.70,0.71,0.72,True,True,0.08,1.7,False,False,-100,-200,-50,-60,False",
            "test2023,0.66,0.67,0.69,True,True,0.14,14.4,True,True,-100,50,-50,-40,True",
        ],
    )
    hdr = (
        "fold,split,group,value,n,win_log_loss,win_ece,win_log_loss_logistic,"
        "win_log_loss_managed_pop,er_ic_mean"
    )
    rows = []
    for fold, ll in (("test2022", 0.70), ("test2023", 0.66)):
        for sp in ("All", "Stocks", "ETFs"):
            rows.append(f"{fold},{sp},all,all,100,{ll},0.05,0.71,0.72,0.14")
        rows.append(f"{fold},All,kind,bull_put,40,{ll},0.06,0.71,0.72,0.1")
    _csv(v / "eval_metrics.csv", hdr, rows)
    _csv(
        v / "eval_reliability.csv",
        "bin,n,weight,mean_p,observed,fold,target,split",
        [
            "0.0-0.1,30,0.1,0.06,0.10,test2023,win,All",
            "0.5-0.6,50,0.2,0.55,0.52,test2023,win,All",
            "0.5-0.6,50,0.2,0.55,0.40,test2023,win,Stocks",
        ],
    )
    _csv(
        v / "eval_gain.csv",
        "target,feature,gain_share",
        ["win,ret_5d,0.6", "win,iv_rank,0.4", "win,rorc_day,0.0", "stop,iv_rank,1.0"],
    )
    _csv(
        v / "eval_permutation.csv",
        "fold,feature,win_ll_rise,er_ic_drop",
        ["test2022,iv_rank,0.002,0.01", "test2023,iv_rank,0.004,0.02", "test2023,ret_5d,-0.001,0"],
    )
    _csv(
        v / "eval_regimes.csv",
        "fold,regime,n,win_ece,er_ic,er_bias",
        [
            "test2023,ctx_trend=bear,250,0.13,0.13,-0.08",
            "test2023,ctx_trend=bull,190,0.07,0.12,-0.06",
        ],
    )
    _csv(
        v / "eval_replay.csv",
        "family,year,use,profile,trades,net_pnl,max_dd_pct,return_pct,win_rate",
        [
            "credit,2023,incumbent,cash_credit,249,-100,-50,-1,0.45",
            "credit,2023,ml_er,cash_credit,212,50,-40,1,0.5",
        ],
    )
    return v


def _studies(root: Path, backlog: Path) -> None:
    specs = root / "config/ml_studies"
    specs.mkdir(parents=True)
    (specs / "ML-2.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "ML-2",
                "hypothesis": "Monotone constraints on rorc_day cut credit log-loss",
                "owner": "analyst",
                "created": "2026-10-20",
                "family": "credit",
                "incumbent": "m2-05fa82807a93",
                "change_kind": "tuning",
                "overlay": {"monotone": {"rorc_day": 1}},
                "grid": [{"num_leaves": 15}, {"num_leaves": 31}],
                "decision_rule": {"kind": "m2_promote"},
            }
        )
    )
    led = root / "data/ml/studies/ledger.jsonl"
    led.parent.mkdir(parents=True)
    base = {
        "study": "ML-2",
        "spec_hash": "h" * 64,
        "family": "credit",
        "incumbent": "m2-05fa82807a93",
        "rule": "m2_promote",
    }

    def dev(year: str, at: str, rel: float, ic: float, s: float) -> dict[str, Any]:
        vs = {"log_loss_rel": rel, "er_ic": ic}
        return {**base, "rung": f"dev_{year}", "at": at, "rung_pass": True,
                "variants": [{}, {}], "vs_incumbent": vs, "runtime_s": s}  # fmt: skip

    lines = [
        dev("2022", "2026-10-21T02:00:00+00:00", -0.02, 0.09, 1200.0),
        dev("2023", "2026-10-22T02:00:00+00:00", -0.03, 0.14, 1300.0),
        {**base, "rung": "verdict", "at": "2026-10-23T02:00:00+00:00", "verdict": "pass",
         "version": M2, "status": "shadow", "runtime_s": 900.0},
    ]  # fmt: skip
    led.write_text("".join(json.dumps(x) + "\n" for x in lines))
    q = root / "data/backtest/queue.json"
    q.parent.mkdir(parents=True)
    q.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "id": "ML-2",
                        "kind": "ml",
                        "status": "concluded",
                        "approved_at": "2026-10-20T10:00:00Z",
                    },
                    {
                        "id": "BT-7",
                        "kind": "bt",
                        "status": "running",
                        "approved_at": "2026-10-19T10:00:00Z",
                    },
                    {
                        "id": "ML-4",
                        "kind": "ml",
                        "status": "queued",
                        "approved_at": "2026-10-27T10:00:00Z",
                    },
                ],
            }
        )
    )
    (specs / "ML-4.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "ML-4",
                "hypothesis": "Drop the event group from M1 (E27.3 noise)",
                "owner": "analyst",
                "created": "2026-10-27",
                "family": "m1",
                "incumbent": M1,
                "change_kind": "features",
                "overlay": {"drop_features": ["sess_to_fomc"]},
                "decision_rule": {"kind": "m1_keep"},
            }
        )
    )
    backlog.parent.mkdir(parents=True, exist_ok=True)
    backlog.write_text(
        json.dumps(
            {
                "items": {
                    "ml:m1-calib": {
                        "id": "ML-5",
                        "kind": "ml",
                        "status": "proposed",
                        "title": "Rolling conformal window for M1 vol (D126)",
                        "hypothesis": HYP5,
                        "family": "m1",
                        "change_kind": "calibration",
                        "created": "2026-10-25T21:00:00Z",
                        "nights_needed": 2,
                        "ml_study_spec": {
                            "family": "m1",
                            "incumbent": "champion",
                            "change_kind": "calibration",
                            "hypothesis": HYP5,
                            "overlay": {"calibration": {"window": 252}},
                            "decision_rule": {"kind": "m1_keep"},
                        },
                    },
                    "bt:theme:x": {"id": "BT-9", "kind": "bt", "status": "proposed"},
                }
            }
        )
    )


def _health(root: Path) -> None:
    d = root / "data/ml/health"
    d.mkdir(parents=True)
    (d / "2026-10-27.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "day": "2026-10-27",
                "serving": {"family": "credit", "version": M2},
                "coverage": {
                    "candidates": 200,
                    "scored": 190,
                    "pct_scored": 95.0,
                    "latency_ms_p95": 41,
                },
                "calibration": {"all": {"p_win": {"status": "insufficient", "n": 4}}},
                "feature_drift": {
                    "status": "ok",
                    "warn": ["iv_rank"],
                    "top": [{"feature": "iv_rank", "psi": 0.31}],
                },
                "score_drift": {"all": {"e_r": {"psi": 0.05}}},
                "parity": {"status": "ok"},
                "last_retrain": {"at": "2026-10-01T03:00:00Z"},
            }
        )
    )
    (d / "2026-10-26.json").write_text(json.dumps({"day": "2026-10-26"}))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "data/ml")
    return tmp_path


@pytest.fixture
def full(root: Path) -> Path:
    _m2(root, status="shadow")
    _studies(root, root / "analyst/xp_backlog.json")
    _health(root)
    return root


def _client(root: Path, analyst: bool = True) -> TestClient:
    app = create_app(
        root / "missing.db",
        backtest_root=root,
        analyst_home=root / "analyst" if analyst else None,
        clock=lambda: NOW,
    )
    return TestClient(app)


def _paths(root: Path) -> MlPaths:
    return MlPaths(root=root, backlog=root / "analyst/xp_backlog.json")


def _stat(stats: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return next(s for s in stats if s["label"] == label)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_models_m1_only_real_numbers(root: Path) -> None:
    r = _client(root).get("/api/ml/models")
    assert r.status_code == 200
    body = r.json()
    rows = {(x["family"], x["status"]): x for x in body["items"]}
    m1 = rows[("m1", "champion")]
    assert m1["version"] == M1
    assert m1["source"] == "card"
    keep = _stat(m1["stats"], "Keep Rule")
    assert (keep["value"], keep["of"]) == (0, 8)  # the real M1: baseline kept on all 8
    # credit / debit have no version yet: a planned row each, no numbers
    assert rows[("credit", "planned")]["version"] is None
    assert rows[("debit", "planned")]["stats"] == []
    s = body["summary"]
    assert (s["champions"], s["families"], s["in_shadow"]) == (1, 3, 0)
    assert s["live_calibration"] is None
    assert s["health_day"] is None
    assert s["studies_proposed"] == 0


def test_m1_detail_matches_the_real_artefacts(root: Path) -> None:
    r = _client(root).get(f"/api/ml/models/m1/{M1}")
    assert r.status_code == 200
    d = r.json()["detail"]
    assert d["family_kind"] == "quantile"
    vol = _stat(d["headline"], "Vol vs Baseline")
    assert vol["value"] == pytest.approx(-0.3295, abs=1e-3)  # −33% vs HAR-IV, h21, 2023
    assert vol["tone"] == "pos"
    cov = _stat(d["headline"], "Coverage Error")
    assert cov["value"] == pytest.approx(0.5618, abs=1e-3)
    assert "vol h42" in cov["sub"]
    assert len(d["rule"]) == 8
    assert {c["year"] for row in d["rule"] for c in row["years"]} == {"2022", "2023"}
    assert all(row["ships"] == "baseline" for row in d["rule"])
    assert d["coverage"] and all(len(s["points"]) == 7 for s in d["coverage"])
    assert {b["group"] for b in d["loss"]} == {"Vol", "Return"}
    assert len(d["loss"]) == 16  # 8 targets × 2 test years
    assert {i["kind"] for i in d["importance"]} == {"gain", "permutation"}
    assert all(len(i["rows"]) <= 10 for i in d["importance"])
    assert 0 < len(d["regimes"]) <= 5
    spec = {kv["label"]: kv["value"] for kv in d["spec"]}
    assert spec["Universe"] == "481 names · 459,990 rows"
    assert spec["Horizons"] == "5 · 10 · 21 · 42 sessions"
    # no feature_groups.json on the old M1 version: every feature counts as "other"
    assert sum(d["feature_groups"].values()) == 85
    assert d["cost"]["train_s"] is not None
    assert d["live"] is None  # the health record serves M2, never M1
    assert [v["version"] for v in d["versions"]] == [M1]


def test_m1_groups_borrowed_from_a_same_schema_version(full: Path) -> None:
    d = _client(full).get(f"/api/ml/models/m1/{M1}").json()["detail"]
    # the M2 fixture's feature_groups.json is the same feature schema: ret_5d/iv_rank map
    assert set(d["feature_groups"]) >= {"other"}
    assert sum(d["feature_groups"].values()) == 85


def test_m2_detail_outcome_union(full: Path) -> None:
    d = _client(full).get(f"/api/ml/models/credit/{M2}").json()["detail"]
    assert d["family_kind"] == "outcome"
    assert d["model"]["status"] == "shadow"
    assert d["model"]["source"] == "ML-2"
    assert d["model"]["holdout_reads"] == 0  # a rolling read is not a holdout read
    assert [r["passed"] for r in d["rule"]] == [False, True]
    promote = _stat(d["headline"], "Promote Rule")
    assert (promote["value"], promote["of"]) == (1, 2)
    ll = _stat(d["headline"], "Log-Loss vs Logistic")
    assert ll["value"] == pytest.approx(0.66 / 0.71 - 1, abs=1e-6)
    assert _stat(d["headline"], "Replay vs Incumbent")["value"] == 150
    assert [b["bin"] for b in d["reliability"]] == ["0.0-0.1", "0.5-0.6"]  # All split only
    assert {(b["group"], b["label"]) for b in d["loss"]} >= {
        ("All", "Logistic"),
        ("ETFs", "Managed PoP"),
    }
    gain = next(i for i in d["importance"] if i["kind"] == "gain" and i["target"] == "win")
    assert [f for f, _ in gain["rows"]] == ["ret_5d", "iv_rank"]  # zero gain dropped
    assert d["regimes"][0]["regime"] == "Trend · Bear"
    assert d["feature_groups"] == {"candidate_ledger": 1, "ticker_options": 1, "ticker_price": 1}
    assert len(d["replay"]) == 2
    live = d["live"]
    assert live["day"] == "2026-10-27"  # the newest record with a day, not a later junk file
    assert live["pct_scored"] == 95.0
    assert live["calibration"] == "insufficient"
    assert live["worst_psi_feature"] == "iv_rank"
    assert live["last_retrain"] == "2026-10-01"


def test_models_summary_with_everything(full: Path) -> None:
    body = _client(full).get("/api/ml/models").json()
    s = body["summary"]
    assert s["in_shadow"] == 1
    assert s["live_calibration"] == "insufficient"
    assert s["worst_psi"] == 0.31
    assert s["running"] == "BT-7"  # the shared runner is on a backtest tonight
    assert s["queued"] == 1
    assert (s["studies_proposed"], s["studies_queued"]) == (1, 1)
    order = [x["status"] for x in body["items"]]
    assert order == sorted(order, key=["champion", "shadow", "candidate", "planned"].index)


@pytest.mark.parametrize(
    "url",
    ["/api/ml/models/m1/nope", "/api/ml/models/zz/m1-53833784f276", "/api/ml/models/m1/..%2Fx"],
)
def test_unknown_model_is_404(root: Path, url: str) -> None:
    assert _client(root).get(url).status_code == 404


def test_no_ml_dir_at_all(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/api/ml/models").json()
    assert [x["status"] for x in body["items"]] == ["planned"] * 3
    assert body["summary"]["champions"] == 0
    st = _client(tmp_path).get("/api/ml/studies").json()
    assert st["items"] == []


def test_corrupt_files_fail_soft(root: Path) -> None:
    fam = root / "data/ml/models/credit"
    fam.mkdir(parents=True)
    (fam / "models.json").write_text("{not json")
    (root / "data/ml/models/m1" / M1 / "eval_metrics.csv").write_text("")
    body = _client(root).get("/api/ml/models").json()
    assert {x["family"] for x in body["items"]} == {"m1", "credit", "debit"}
    d = _client(root).get(f"/api/ml/models/m1/{M1}").json()["detail"]
    assert d["loss"] == []


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------


def test_studies_list(full: Path) -> None:
    body = _client(full).get("/api/ml/studies").json()
    by = {x["study_id"]: x for x in body["items"]}
    assert set(by) == {"ML-2", "ML-4", "ML-5"}  # BT entries never leak in
    assert [x["study_id"] for x in body["items"]] == ["ML-4", "ML-5", "ML-2"]
    assert (body["proposed"], body["queued"]) == (1, 1)

    done = by["ML-2"]
    assert done["status"] == "concluded"
    assert done["verdict"] == "pass"
    assert done["version"] == M2
    states = {s["step"]: s["state"] for s in done["steps"]}
    assert states == {
        "proposed": "done",
        "approved": "done",
        "dev_2022": "done",
        "dev_2023": "done",
        "verdict": "done",
        "shadow": "done",
        "champion": "pending",
    }
    assert done["stage"] == "Shadow"
    assert done["variants"] == 2
    assert done["runtime_s"] == 3400.0
    assert done["evidence"][0].startswith("Dev 2022 · pass · log-loss −2% vs incumbent")
    assert done["evidence"][-1] == f"Verdict · pass · {M2} → Shadow"

    q = by["ML-4"]
    assert q["status"] == "queued"
    assert q["queue_position"] == 2  # BT-7 (running) is first in the shared queue
    assert q["eta_s"] == 3 * 2400.0 + 2 * 2400.0
    assert q["stage"] == "Approved"
    assert "E27.3" not in q["hypothesis"]  # plan ids scrubbed

    p = by["ML-5"]
    assert p["status"] == "proposed"
    assert p["title"].startswith("Rolling conformal window")
    assert "D126" not in p["title"]
    assert p["nights_needed"] == 2
    assert p["stage"] == "Proposed"
    assert p["family_label"] == "M1 · Move & Vol"


def test_study_detail(full: Path) -> None:
    r = _client(full).get("/api/ml/studies/ML-2")
    assert r.status_code == 200
    d = r.json()
    spec = {kv["label"]: kv["value"] for kv in d["spec"]}
    assert spec["Grid"] == "2 configs"
    assert spec["Challenger"] == "monotone.rorc_day: 1"
    assert spec["Spec File"] == "config/ml_studies/ML-2.yaml"
    assert d["rule"] == "m2_promote"
    p = _client(full).get("/api/ml/studies/ML-5").json()
    assert p["spec_file"] is None
    assert p["study"]["status"] == "proposed"


@pytest.mark.parametrize("sid", ["ML-99", "BT-1", "ML-0", "ml-2"])
def test_unknown_study_is_404(full: Path, sid: str) -> None:
    assert _client(full).get(f"/api/ml/studies/{sid}").status_code == 404


def test_no_analyst_home_hides_drafts(full: Path) -> None:
    body = _client(full, analyst=False).get("/api/ml/studies").json()
    assert {x["study_id"] for x in body["items"]} == {"ML-2", "ML-4"}


def test_dropped_and_retired(full: Path) -> None:
    led = full / "data/ml/studies/ledger.jsonl"
    with led.open("a") as f:
        f.write(
            json.dumps(
                {
                    "study": "ML-4",
                    "rung": "dropped",
                    "at": "2026-10-27T12:00:00Z",
                    "note": "owner (E27.8)",
                }
            )
            + "\n"
        )
    (full / "data/backtest/queue.json").write_text(json.dumps({"entries": []}))
    (full / "config/ml_studies/retired.yaml").write_text(
        yaml.safe_dump({"retired": [{"id": "ML-2"}]})
    )
    items = {x["study_id"]: x for x in load_ml_studies(_paths(full), now=NOW).model_dump()["items"]}
    assert items["ML-4"]["status"] == "dropped"
    assert items["ML-4"]["verdict"] == "dropped"
    assert items["ML-4"]["evidence"] == ["Dropped · Owner"]
    assert items["ML-2"]["retired"] is True


def test_scope_envelope(full: Path) -> None:
    body = _client(full).get("/api/ml/studies").json()
    assert body["scope"] == {"arm_id": None}
    assert body["shared_from_control"] is False


def test_response_time_on_fixture(full: Path) -> None:
    c = _client(full)
    for url in ("/api/ml/models", f"/api/ml/models/m1/{M1}", "/api/ml/studies"):
        c.get(url)  # warm
        t = time.perf_counter()
        assert c.get(url).status_code == 200
        assert time.perf_counter() - t < 0.3, url


def test_loaders_are_pure_reads(full: Path) -> None:
    before = sorted((p, p.stat().st_mtime_ns) for p in full.rglob("*") if p.is_file())
    load_ml_models(_paths(full), now=NOW)
    load_ml_model(_paths(full), "credit", M2, now=NOW)
    load_ml_studies(_paths(full), now=NOW)
    after = sorted((p, p.stat().st_mtime_ns) for p in full.rglob("*") if p.is_file())
    assert before == after
