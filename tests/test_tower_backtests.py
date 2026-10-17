"""D124: backtest studies on the tower's Experiments page (``/api/backtests*``).

The tower reads the research ledger, the study specs and the runner's queue from files,
read-only, with its own tolerant parser (never :mod:`arc.backtest.study`). Pinned here:
the headline follows the live account's profile, the ladder and status, a ledger line the
tower cannot parse is skipped (not a 503), owner-facing text carries no plan/card ids,
unknown ids 404, and an experiment citing the study is linked.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path  # noqa: TC003 - pytest fixture annotations

import pytest
from fastapi.testclient import TestClient

from arc.config import ArcSettings
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.data_backtests import scrub_ids

NOW = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.UTC)

SPEC_A = """\
id: BT-1
hypothesis: >
  E7.2 / D4 re-run: the D25 rule keeps v2 (E7.5a) over v1 on both profiles.
created: 2026-10-01
profiles: [margin, cash_debit]
primary_metric: net_pnl
universe_ladder: [c125, c500]
arms:
  control: {}
  treatments:
    t1:
      overlay:
        regime: {version: 2}
decision_rule:
  kind: keep_v2_unless_worse
  verbatim: |
    D4 (PLAN §0): keep v2 if it is not worse on net P&L.
  bootstrap: {ci: 0.9}
"""

SPEC_B = """\
id: BT-2
hypothesis: A dev-only study.
profiles: [cash_debit]
arms:
  treatments:
    t1: {overlay: {ranking: {method: x}}}
    t2: {overlay: {ranking: {method: y}}}
decision_rule: {kind: recommend}
"""


def _arm(net: float, trades: int = 100) -> dict:
    return {
        "trades": trades, "net_pnl": net, "return_pct": net / 250, "max_dd_pct": 12.0,
        "sortino": 0.5, "profit_factor": 1.1, "win_rate": 0.5, "avg_days_held": 9.0,
        "cost_share_of_gross": 0.4, "capacity_clipped_share": 0.1,
        "exit_reasons": {"take_profit": 0.6, "stop": 0.4}, "skips": {"capacity": 3},
    }  # fmt: skip


def _row(study: str, phase: str, univ: str, at: str, verdicts: dict, **kw) -> str:
    profiles = list(verdicts)
    return json.dumps({
        "at": at, "study": study, "spec_hash": "h" * 64, "phase": phase, "universe": univ,
        "start": "2024-01-01", "end": "2026-10-15" if phase == "holdout" else "2025-12-31",
        "primary_metric": "net_pnl", "profiles": profiles,
        "metrics": {p: {"control": _arm(1000),
                        **{a: _arm(1000 + v["pnl_diff"]) for a, v in vs.items()}}
                    for p, vs in verdicts.items()},
        "verdicts": verdicts, "overfit_warning": False, "runtime_s": 600.0, **kw,
    })  # fmt: skip


def _v(
    diff: float, lo: float, hi: float, *, switch: bool = False, call: str = "keep control"
) -> dict:
    return {"pnl_diff": diff, "ci_lo": lo, "ci_hi": hi, "switch": switch, "call": call,
            "subperiods_won": 2, "won": "bear,bull"}  # fmt: skip


@pytest.fixture
def root(tmp_path: Path) -> Path:
    specs = tmp_path / "config" / "backtest_studies"
    specs.mkdir(parents=True)
    (specs / "BT-1.yaml").write_text(SPEC_A)
    (specs / "BT-2.yaml").write_text(SPEC_B)
    (tmp_path / "data" / "backtest").mkdir(parents=True)
    rows = [
        _row("BT-1", "dev", "c125", "2026-10-10T01:00:00+00:00",
             {"margin": {"t1": _v(-50, -500, 400)}, "cash_debit": {"t1": _v(20, -100, 140)}}),
        _row("BT-1", "dev", "c500", "2026-10-11T01:00:00+00:00",
             {"margin": {"t1": _v(900, -200, 2000)}, "cash_debit": {"t1": _v(-30, -300, 250)}}),
        _row("BT-1", "holdout", "c500", "2026-10-12T01:00:00+00:00",
             {"margin": {"t1": _v(35000, 3500, 69400, switch=True, call="switch")},
              "cash_debit": {"t1": _v(-923, -47500, 50200)}}),
        "{not json",
        json.dumps({"study": "BT-1", "phase": "sideways"}),
        _row("BT-2", "dev", "c125", "2026-10-13T01:00:00+00:00",
             {"cash_debit": {"t1": _v(-2004, -7200, 2900, call="do not recommend"),
                             "t2": _v(150, -900, 1200, call="do not recommend (future XP arm)")}}),
    ]  # fmt: skip
    (tmp_path / "data" / "backtest" / "ledger.jsonl").write_text("\n".join(rows) + "\n")
    (tmp_path / "data" / "backtest" / "queue.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"id": "BT-3", "status": "queued", "approved_at": "2026-10-16T12:00:00+00:00"}
                ]
            }
        )
    )
    return tmp_path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "arc.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


def _client(db: Path, root: Path, profile: str = "cash_debit") -> TestClient:
    settings = ArcSettings(account_profile=profile)
    return TestClient(create_app(db, settings, backtest_root=root, clock=lambda: NOW))


def test_list_rows_follow_the_live_profile(db: Path, root: Path) -> None:
    body = _client(db, root).get("/api/backtests").json()
    assert body["account_profile"] == "cash_debit"
    assert body["skipped_rows"] == 2
    assert body["queued"] == 1 and body["running"] is None
    ids = [i["study_id"] for i in body["items"]]
    assert ids == ["BT-2", "BT-1", "BT-3"]  # latest run first, never-run last
    bt1 = next(i for i in body["items"] if i["study_id"] == "BT-1")
    assert bt1["status"] == "done" and bt1["stage"] == "C500 Holdout" and bt1["runs"] == 3
    assert bt1["profile"] == "cash_debit"
    assert bt1["headline"]["pnl_diff"] == -923 and bt1["headline"]["call"] == "keep control"
    assert [r["done"] for r in bt1["ladder"]] == [True, True, True]
    assert bt1["ci_level"] == 0.9 and bt1["variants"] == 1


def test_margin_account_leads_with_margin(db: Path, root: Path) -> None:
    body = _client(db, root, "margin").get("/api/backtests").json()
    bt1 = next(i for i in body["items"] if i["study_id"] == "BT-1")
    assert bt1["profile"] == "margin"
    assert bt1["headline"]["pnl_diff"] == 35000 and bt1["headline"]["switch"] is True
    # BT-2 never ran on margin: falls back to its own profile.
    bt2 = next(i for i in body["items"] if i["study_id"] == "BT-2")
    assert bt2["profile"] == "cash_debit"


def test_dev_only_study_best_arm_and_ladder(db: Path, root: Path) -> None:
    body = _client(db, root).get("/api/backtests").json()
    bt2 = next(i for i in body["items"] if i["study_id"] == "BT-2")
    assert bt2["status"] == "stopped_at_dev" and bt2["stage"] == "C125 Dev"
    assert [r["done"] for r in bt2["ladder"]] == [True, False, False]
    assert bt2["headline"]["arm"] == "t2" and bt2["variants"] == 2
    assert bt2["headline"]["call"] == "do not recommend"  # "(future XP arm)" dropped
    bt3 = next(i for i in body["items"] if i["study_id"] == "BT-3")
    assert bt3["status"] == "queued" and bt3["headline"] is None and bt3["runs"] == 0


def test_owner_text_has_no_plan_ids(db: Path, root: Path) -> None:
    c = _client(db, root)
    bt1 = next(i for i in c.get("/api/backtests").json()["items"] if i["study_id"] == "BT-1")
    assert bt1["hypothesis"] == "Re-run: the rule keeps v2 over v1 on both profiles."
    d = c.get("/api/backtests/BT-1").json()
    assert d["rule"] == "Keep v2 if it is not worse on net P&L."


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("E13.3 / D25 re-run: x holds.", "Re-run: x holds."),
        ("Keep v2 (E7.5a) over v1.", "Keep v2 over v1."),
        ("no ids here: 3.5 days", "No ids here: 3.5 days"),
    ],
)
def test_scrub_ids(raw: str, want: str) -> None:
    assert scrub_ids(raw) == want


def test_detail_runs_metrics_and_arms(db: Path, root: Path) -> None:
    d = _client(db, root).get("/api/backtests/BT-1").json()
    assert d["study"]["study_id"] == "BT-1" and d["account_profile"] == "cash_debit"
    assert [r["stage"] for r in d["runs"]] == ["C125 Dev", "C500 Dev", "C500 Holdout"]
    hold = d["runs"][-1]
    cash = next(r for r in hold["results"] if r["profile"] == "cash_debit")
    assert [a["arm"] for a in cash["arms"]] == ["control", "t1"]
    assert cash["arms"][0]["exit_reasons"] == {"take_profit": 0.6, "stop": 0.4}
    assert cash["treatments"][0]["periods_won_names"] == ["bear", "bull"]
    assert d["arms"] == {"t1": {"regime": {"version": 2}}}
    assert d["rule_kind"] == "keep_v2_unless_worse" and d["linked"] == []


def test_unknown_and_malformed_ids_404(db: Path, root: Path) -> None:
    c = _client(db, root)
    for sid in ("BT-99", "XP-1", "BT-0", "..%2Fetc"):
        r = c.get(f"/api/backtests/{sid}")
        assert r.status_code == 404, sid
        assert r.json()["error"] == "not_found"


def test_no_files_is_an_empty_list(db: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    body = _client(db, empty).get("/api/backtests").json()
    assert body["items"] == [] and body["queued"] == 0 and body["skipped_rows"] == 0


def test_linked_live_experiment(db: Path, root: Path) -> None:
    from arc.experiments.store import ExperimentStore
    from tests import experiment_fixtures as fx

    conn = connect(db)
    store = ExperimentStore(conn)
    for eid, ref in (("XP-4", "BT-1 holdout (margin)"), ("XP-5", "BT-12")):
        store.create(fx.spec(eid, backtest_ref=ref), actor="owner", owner_approval="owner:test")
    conn.close()
    d = _client(db, root).get("/api/backtests/BT-1").json()
    assert [x["experiment_id"] for x in d["linked"]] == ["XP-4"]


def test_tower_does_not_import_the_simulator() -> None:
    """The tower reads the files itself: ``arc.backtest`` pulls pandas and the scanner."""
    import ast

    tree = ast.parse(
        (Path(__file__).parents[1] / "arc" / "tower" / "data_backtests.py").read_text()
    )
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("arc.backtest") for m in mods), mods
