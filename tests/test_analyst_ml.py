"""E27.8 (D133): the Arc Analyst proposes ML-<n> model studies in its backlog.

The Analyst drafts ``ml_study_spec`` findings (``kind: ml`` backlog entries, ids above every
spec/queue/ledger/retired/PR number), validates each draft with ``arc ml study validate``
(fail closed), ranks them by compute nights like BT, names ``next_ml_study``, routes the
owner's ``approve|drop|stop ML-<n>`` through ``arc ml study``, shares the 2-per-ISO-week
approval cap with BT and surfaces the registry, ML studies and live shadow health.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from arc.backtest.queue import QueueEntry, StudyQueue, update_queue
from arc.ml import study as mls
from arc.ml.m1.train import LGB_PARAMS, model_features
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
ANALYST = REPO / "hermes" / "analyst"
NOW = _dt.datetime(2026, 10, 25, 15, tzinfo=ET)  # a Sunday, ISO week 2026-W43
ARC_ARGV = (sys.executable, "-c", "import sys; from arc.cli import main; sys.exit(main())")
INC = "m1-aaaaaaaaaaaa"
APPROVAL = ["--approval", "slack:1791669727.601139"]

_spec = importlib.util.spec_from_file_location("arc_analyst_ml", ANALYST / "arc_analyst.py")
assert _spec and _spec.loader
aa = importlib.util.module_from_spec(_spec)
sys.modules["arc_analyst_ml"] = aa
_spec.loader.exec_module(aa)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _paths(tmp_path: Path) -> Any:
    return aa.Paths(
        home=tmp_path / "analyst", repo=REPO, live_db=tmp_path / "unused.db", arc=ARC_ARGV,
        backtest_root=tmp_path / "bt", verdicts_dir=tmp_path / "xp_verdicts",
    )  # fmt: skip


def _models(root: Path, champion: str | None = INC) -> None:
    """A registered m1 family (manifest + models.json) under ``<root>/data/ml/models``."""
    vdir = root / "data" / "ml" / "models" / "m1" / INC
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "manifest.json").write_text(json.dumps({
        "version": INC, "lgb_params": LGB_PARAMS, "num_rounds": 300,
        "features": model_features(), "config": {}, "holdout_reads": 0,
    }))  # fmt: skip
    (vdir.parent / "models.json").write_text(json.dumps({
        "family": "m1", "champion": champion,
        "versions": {INC: {"status": "champion" if champion else "candidate",
                           "rule": "m1_keep", "rule_passed": True,
                           "created": "2026-10-20T00:00:00-04:00"}},
    }))  # fmt: skip


def _ml_spec(**over: Any) -> dict[str, Any]:
    return {
        "hypothesis": "monthly rolling CQR fixes rv coverage drift", "family": "m1",
        "incumbent": "champion", "change_kind": "calibration",
        "overlay": {"targets": ["rv"], "calibration": {"method": "rolling_monthly"}},
        "grid": [], "decision_rule": {"kind": "m1_keep"}, **over,
    }  # fmt: skip


def _sv(**over: Any) -> dict[str, Any]:
    return {"pct_per_day": 0.02, "source": "m1 coverage gap in ## ML live", "affected_share": 0.4,
            "p_success": 0.3, "nights_needed": 1, **over}  # fmt: skip


def _ml_finding(key: str = "theme:m1-rolling-cqr", **over: Any) -> dict[str, Any]:
    return {
        "key": key, "title": "m1 rv coverage drifts", "severity": "info", "category": "theme",
        "bucket": "all", "n": 0, "theme": "models", "evidence": "h5 rv coverage 0.71 vs 0.80",
        "recommendation": "test a rolling CQR in an ML study", "action": "no-action",
        "ml_study_spec": _ml_spec(), "study_value": _sv(), **over,
    }  # fmt: skip


def _themes() -> list[dict[str, str]]:
    return [{"theme": t, "status": "no-evidence", "note": "n=0"} for t in aa.THEMES]


def _doc(*findings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"verdict": "findings" if findings else "quiet", "themes": _themes(),
            "findings": list(findings), "resolved": [], "experiments": [],
            "quick_changes_none": "no bucket has n >= 20",
            "next_experiment": "none: n < 20", "next_study": "none: no study_spec draft",
            "next_ml_study": "none: no ml_study_spec draft", **extra}  # fmt: skip


def _meta(**over: Any) -> dict[str, Any]:
    snap = {"models": [{"family": "m1", "champion": INC,
                        "versions": [{"version": INC, "status": "champion"}]}],
            "verdicts": {}, "retired": [], "next_id": "ML-1", **over}  # fmt: skip
    return snap


def _record(p: Any, doc: dict[str, Any], ml: dict[str, Any] | None = None) -> tuple[int, Path]:
    run_dir = p.runs / "2026-10-25-1500"
    run_dir.mkdir(parents=True, exist_ok=True)
    aa.save_json(run_dir / "metrics.json", {
        "now": aa.iso(NOW), "closed_max_rowid": 0,
        "backtest": {"studies": [], "next_study_id": "BT-1"}, "ml": ml or _meta(),
        "ml_verdicts": [],
    })  # fmt: skip
    (run_dir / "findings.json").write_text(json.dumps(doc))
    return aa.main(["record", str(run_dir)], p, NOW), run_dir


def _errs(*findings: dict[str, Any], ml: dict[str, Any] | None = None, **extra: Any) -> list:
    return aa.validate_findings(
        _doc(*findings, **extra),
        {"items": {}},
        None,
        [],
        backlog=None,
        studies=[],
        ml=ml if ml is not None else _meta(),
    )


# ---------------------------------------------------------------------------
# mirrors of arc.ml.study (the analyst script is stdlib-only)
# ---------------------------------------------------------------------------


def test_mirrors_match_arc_ml_study() -> None:
    assert aa.ML_FAMILY_RULE == mls.FAMILY_RULE
    assert set(aa.ML_CHANGE_KINDS) == set(mls.ChangeKind.__args__)  # type: ignore[attr-defined]
    fields = set(mls.MlStudySpec.model_fields)
    assert set(aa.ML_SPEC_KEYS) | {"id", "owner", "created", "study_value"} == fields
    assert mls.MlStudySpec.model_fields["grid"].metadata[0].max_length == aa.ML_MAX_GRID
    assert aa.ML_STUDY_ID_RE.match("ML-12") and not aa.ML_STUDY_ID_RE.match("ML-0")
    assert "models" in aa.THEMES and "Next ML" in aa.REPORT_SECTIONS
    assert aa.verdict_refs("see ML-4 and BT-2") == {"ML-4", "BT-2"}


# ---------------------------------------------------------------------------
# finding validation
# ---------------------------------------------------------------------------


def test_valid_ml_finding_passes() -> None:
    assert _errs(_ml_finding()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"family": "iv"}, "new model families are cards"),
        ({"decision_rule": {"kind": "m2_promote"}}, "is not the registered rule of m1"),
        ({"change_kind": "rewrite"}, "change_kind must be one of"),
        ({"overlay": {"split": "holdout"}}, "never reads the holdout"),
        ({"grid": [{}] * 49}, "at most 48"),
        ({"incumbent": "m1-bbbbbbbbbbbb"}, "is not a m1 version"),
        ({"id": "ML-3"}, "are filled by `record`"),
        ({"mystery": 1}, "unknown keys"),
        ({"hypothesis": ""}, "missing ['hypothesis']"),
    ],
)
def test_ml_spec_rules(over: dict[str, Any], needle: str) -> None:
    errs = _errs(_ml_finding(ml_study_spec=_ml_spec(**over)))
    assert any(needle in e for e in errs), errs


def test_ml_finding_shape_rules() -> None:
    both = _ml_finding(study_spec={"hypothesis": "x"})
    assert any("both study_spec and ml_study_spec" in e for e in _errs(both))
    no_value = _ml_finding()
    no_value.pop("study_value")
    assert any("study_value" in e for e in _errs(no_value))
    flaw = _ml_finding(category="flaw", severity="high")
    assert any("drafts a model study: only on" in e for e in _errs(flaw))
    no_champion = _meta(
        models=[
            {
                "family": "m1",
                "champion": None,
                "versions": [{"version": INC, "status": "candidate"}],
            }
        ]
    )
    assert any("has no champion" in e for e in _errs(_ml_finding(), ml=no_champion))


def test_ml_citations_need_a_ledger_verdict() -> None:
    cites = _ml_finding(evidence="ML-2 dev 2023 pinball -4%")
    assert any("cites ML-2: no verdict in the ML ledger" in e for e in _errs(cites))
    ok = _meta(verdicts={"ML-2": {"verdict": "pass", "status": "shadow", "version": "m1-x"}})
    assert _errs(cites, ml=ok) == []
    retired = _meta(verdicts={}, retired=["ML-2"])
    assert any("ML-2: it is retired" in e for e in _errs(cites, ml=retired))


def test_next_ml_study_is_required_and_ranked() -> None:
    assert any("next_ml_study is required" in e for e in _errs(next_ml_study=""))
    assert any("needs a reason" in e for e in _errs(next_ml_study="none"))
    assert any("must be a proposed ML-<n>" in e for e in _errs(next_ml_study="ML-9"))
    two = (_ml_finding(), _ml_finding("theme:m1-tuning", study_value=_sv(p_success=0.6)))
    # ML-2 (p 0.6) outranks ML-1 (p 0.3): naming ML-1 needs a reason
    assert any("not the top-ranked ML study (ML-2)" in e for e in _errs(*two, next_ml_study="ML-1"))
    assert _errs(*two, next_ml_study="ML-1", next_ml_study_reason="cheaper") == []
    assert _errs(*two, next_ml_study="ML-2") == []


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


def test_reserve_ml_id_above_every_source(tmp_path: Path) -> None:
    root = tmp_path / "bt"
    _models(root)
    specs = root / "config" / "ml_studies"
    specs.mkdir(parents=True)
    (specs / "ML-2.yaml").write_text("{}")
    (specs / "retired.yaml").write_text(
        '# retired\n{"retired": [{"id": "ML-5", "at": "2026-10-20", "reason": "old"}]}\n'
    )
    ledger = root / "data" / "ml" / "studies" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"study": "ML-3", "rung": "dropped", "verdict": "dropped"}) + "\n")
    q = root / "data" / "backtest" / "queue.json"
    q.parent.mkdir(parents=True)
    entry = QueueEntry(
        id="ML-4", kind="ml", spec_hash="a" * 64, approval=APPROVAL[1], approved_at=NOW
    )
    update_queue(q, lambda qu: qu.entries.append(entry))
    snap = aa.ml_snapshot(_paths(tmp_path))
    assert snap["next_id"] == "ML-6"
    meta = aa.ml_snapshot_meta(snap)
    assert meta["retired"] == ["ML-5"]
    assert aa.ml_floor(meta) == 6 and aa.ml_floor(meta, {9}) == 10  # an open ml/9 PR
    b = aa.empty_backlog()
    assert aa.reserve_ml_id(b, 6) == "ML-6"
    b["items"]["ml:x"] = {"id": "ML-8", "kind": "ml", "status": "dropped"}
    assert aa.reserve_ml_id(b, 6) == "ML-9"  # a dropped number is never reused
    assert isinstance(StudyQueue(), StudyQueue)


# ---------------------------------------------------------------------------
# record: draft, real CLI validation, backlog, ranking
# ---------------------------------------------------------------------------


def test_record_drafts_validates_and_ranks_ml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _paths(tmp_path)
    _models(p.bt_root)
    code, run_dir = _record(p, _doc(_ml_finding(), next_ml_study="ML-1"))
    out = capsys.readouterr().out
    assert code == 0, out
    draft = run_dir / "study-specs" / "ML-1.yaml"
    assert draft.is_file() and not (run_dir / "study-specs" / ".validate").exists()
    spec = mls.load_ml_spec(draft)  # the draft is a valid MlStudySpec (adopt reads it)
    assert (spec.id, spec.family, spec.incumbent, spec.change_kind) == (
        "ML-1", "m1", "champion", "calibration",
    )  # fmt: skip
    assert spec.study_value and spec.study_value.nights_needed == 1
    b = aa.load_backlog(p.backlog)
    e = aa.ml_by_id(b, "ML-1")
    assert (e["kind"], e["key"], e["status"]) == ("ml", "ml:theme:m1-rolling-cqr", "proposed")
    assert e["draft_path"] == str(draft)
    assert b["next_ml_study"]["value"] == "ML-1"
    # ranked by value like BT (compute nights); `backlog` prints its kind
    assert [x["id"] for x in aa.ranked_backlog(b, kind="ml")] == ["ML-1"]
    assert aa.ranked_backlog(b, kind="bt") == [] and aa.ranked_backlog(b, kind="xp") == []
    assert aa.main(["backlog"], p, NOW) == 0
    assert "ML-1 [proposed] ML study" in capsys.readouterr().out
    # re-listing the key keeps ML-1; an edit updates the draft while proposed
    edit = _ml_finding(study_value=_sv(p_success=0.5))
    code, _ = _record(p, _doc(edit, next_ml_study="ML-1"))
    assert code == 0, capsys.readouterr().out
    e = aa.ml_by_id(aa.load_backlog(p.backlog), "ML-1")
    assert e["p_success"] == 0.5 and e["history"][-1]["event"] == "edit"


def test_record_rejects_what_arc_ml_study_validate_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An overlay key the family's training config lacks passes the stdlib mirror but not
    the pydantic validator: record fails closed and writes nothing."""
    p = _paths(tmp_path)
    _models(p.bt_root)
    bad = _ml_finding(ml_study_spec=_ml_spec(overlay={"no_such_knob": 1}))
    code, run_dir = _record(p, _doc(bad, next_ml_study="ML-1"))
    out = capsys.readouterr().out
    assert code == 2
    assert "ML-1 (key theme:m1-rolling-cqr): `arc ml study validate` refused it" in out
    assert not p.backlog.exists() and not (run_dir / "study-specs" / "ML-1.yaml").exists()


def test_record_rejects_a_family_with_no_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _paths(tmp_path)
    finding = _ml_finding(ml_study_spec=_ml_spec(family="credit", decision_rule={
        "kind": "m2_promote"}, overlay={}))  # fmt: skip
    assert _record(p, _doc(finding, next_ml_study="ML-1"))[0] == 2
    assert "family credit has no model under data/ml/models" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# owner commands
# ---------------------------------------------------------------------------


def _proposed_ml(p: Any, sid: str = "ML-1", key: str = "theme:m1-rolling-cqr") -> dict:
    b = aa.load_backlog(p.backlog)
    f = _ml_finding(key=key)
    ((_, entry),) = aa.apply_ml_backlog(b, {"findings": [f]}, "r", aa.iso(NOW), aa._study_num(sid))
    path = p.runs / "r" / "study-specs" / f"{entry['id']}.yaml"
    entry["draft_path"] = str(aa.write_ml_study_spec(path, entry["id"], f["ml_study_spec"], f,
                                                     "A-1", NOW))  # fmt: skip
    aa.save_json(p.backlog, b)
    return entry


def _fake_ml_cli(calls: list[list[str]], code: int = 0) -> Any:
    def fake(p: Any, args: list[str], timeout: int = 600) -> Any:
        calls.append(list(args))
        if code:
            return code, "gh pr create failed: network"
        if args[0] == "adopt":
            return 0, {"id": args[1], "status": "queued", "spec_hash": "f" * 64,
                       "pr_url": "https://github.com/x/y/pull/9", "pr_status": "opened",
                       "queue_position": 2, "eta_nights": 3,
                       "entry": {"ladder_state": "dev_2022"}}  # fmt: skip
        if args[0] == "stop":
            return 0, {"id": args[1], "status": "stopped", "stop_reason": args[2]}
        return 0, "ML-1 dropped"

    return fake


def test_approve_ml_adopts_through_arc_ml_study(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path)
    _proposed_ml(p)
    calls: list[list[str]] = []
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli(calls))
    monkeypatch.setattr(aa, "adopt_study", lambda *a, **k: pytest.fail("BT adopt for an ML id"))
    assert aa.main(["approve", "ML-1", "go", *APPROVAL], p, NOW) == 0
    out = capsys.readouterr().out
    assert out.startswith("ML-1 [queued]")
    assert "Spec PR: https://github.com/x/y/pull/9" in out and "ETA 3 night(s)" in out
    assert "never to champion" in out
    (argv,) = calls
    assert argv[:2] == ["adopt", "ML-1"] and argv[argv.index("--approval") + 1] == APPROVAL[1]
    assert argv[argv.index("--queue") + 1] == str(p.bt_root / aa.BT_QUEUE)
    assert argv[argv.index("--studies-dir") + 1] == str(p.bt_root / aa.ML_SPECS)
    e = aa.ml_by_id(aa.load_backlog(p.backlog), "ML-1")
    assert (e["status"], e["spec_hash"], e["ladder_state"]) == ("queued", "f" * 64, "dev_2022")
    assert [h["event"] for h in e["history"]] == ["proposed", "approved", "adopted"]
    assert aa.main(["approve", "ML-1", *APPROVAL], p, NOW) == 2
    assert "already committed and queued" in capsys.readouterr().out


def test_approve_ml_retry_keeps_its_week_and_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path)
    _proposed_ml(p)
    calls: list[list[str]] = []
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli(calls, code=3))
    assert aa.main(["approve", "ML-1", *APPROVAL], p, NOW) == 1
    assert "Re-send `approve ML-1` to retry" in capsys.readouterr().out
    e = aa.ml_by_id(aa.load_backlog(p.backlog), "ML-1")
    assert (e["status"], e["approved_at"]) == ("approved", aa.iso(NOW))
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli(calls))
    later = NOW + _dt.timedelta(days=8)  # next ISO week: the retry is not a new approval
    assert aa.main(["approve", "ML-1", *APPROVAL], p, later) == 0
    assert aa.ml_by_id(aa.load_backlog(p.backlog), "ML-1")["approved_at"] == aa.iso(NOW)


def test_weekly_cap_is_shared_by_bt_and_ml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path)
    _proposed_ml(p, "ML-1", "theme:a")
    _proposed_ml(p, "ML-2", "theme:b")
    b = aa.load_backlog(p.backlog)
    b["items"]["bt:theme:x"] = {"id": "BT-7", "kind": "bt", "key": "bt:theme:x",
                                "status": "queued", "approved_at": aa.iso(NOW - _dt.timedelta(
                                    days=4)), "history": []}  # fmt: skip
    aa.save_json(p.backlog, b)
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli([]))
    assert aa.main(["approve", "ML-1", *APPROVAL], p, NOW) == 0
    capsys.readouterr()
    assert aa.main(["approve", "ML-2", *APPROVAL], p, NOW) == 2
    out = capsys.readouterr().out
    assert "ML-2 refused: the weekly cap is 2 study approvals per ISO week, BT and ML" in out
    assert "BT-7 (2026-10-21, queued), ML-1 (2026-10-25, queued)" in out
    assert "resets Mon 2026-10-26 00:00 ET" in out
    week = aa.study_approvals_in_week(aa.load_backlog(p.backlog), NOW)
    assert [e["id"] for e in week] == ["BT-7", "ML-1"]
    # the cap resets Monday 00:00 ET
    assert aa.main(["approve", "ML-2", *APPROVAL], p, _dt.datetime(2026, 10, 26, tzinfo=ET)) == 0


def test_drop_and_stop_ml_route_to_arc_ml_study(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path)
    _proposed_ml(p, "ML-1", "theme:a")
    _proposed_ml(p, "ML-2", "theme:b")
    calls: list[list[str]] = []
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli(calls))
    assert aa.main(["stop", "ML-1", "x"], p, NOW) == 2
    assert "ML-1 is proposed (not committed): use `drop ML-1 <why>`" in capsys.readouterr().out
    assert aa.main(["drop", "ML-1", "superseded"], p, NOW) == 0
    assert calls[-1][:3] == ["drop", "ML-1", "superseded"]
    b = aa.load_backlog(p.backlog)
    assert aa.ml_by_id(b, "ML-1")["status"] == "dropped"
    assert aa.reserve_ml_id(b, 1) == "ML-3"  # ML-1 stays a gap
    assert aa.main(["approve", "ML-2", *APPROVAL], p, NOW) == 0
    assert aa.main(["drop", "ML-2", "x"], p, NOW) == 2
    assert "ML-2 is queued (committed and queued): use `stop ML-2 <why>`" in (
        capsys.readouterr().out
    )
    assert aa.main(["stop", "ML-2", "owner needs the nights"], p, NOW) == 0
    assert calls[-1][:3] == ["stop", "ML-2", "owner needs the nights"]
    assert aa.ml_by_id(aa.load_backlog(p.backlog), "ML-2")["status"] == "stopped"


def test_drop_ml_refusal_leaves_the_backlog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _paths(tmp_path)
    _proposed_ml(p)
    monkeypatch.setattr(aa, "ml_study_cli", _fake_ml_cli([], code=3))
    assert aa.main(["drop", "ML-1", "x"], p, NOW) == 2
    assert "`arc ml study drop` refused" in capsys.readouterr().out
    assert aa.ml_by_id(aa.load_backlog(p.backlog), "ML-1")["status"] == "proposed"


def test_real_cli_drop_writes_the_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """End to end through the real `arc ml study drop` (no fakes)."""
    p = _paths(tmp_path)
    _proposed_ml(p)
    assert aa.main(["drop", "ML-1", "superseded"], p, NOW) == 0, capsys.readouterr().out
    rows, errs = aa.load_ml_ledger_rows(p.bt_root / aa.ML_LEDGER)
    assert errs == [] and [(r["study"], r["rung"]) for r in rows] == [("ML-1", "dropped")]
    assert aa.ml_snapshot(p)["next_id"] == "ML-2"


# ---------------------------------------------------------------------------
# context and verdict wake-ups
# ---------------------------------------------------------------------------


def _ledger(root: Path, *rows: dict[str, Any]) -> None:
    path = root / aa.ML_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


VERDICT = {"study": "ML-1", "spec_hash": "c" * 64, "family": "m1", "incumbent": INC,
           "rung": "verdict", "at": "2026-10-24T06:00:00-04:00", "rule": "m1_keep",
           "verdict": "pass", "status": "shadow", "version": "m1-bbbbbbbbbbbb",
           "best_variant": 0}  # fmt: skip
DEV = {
    **VERDICT,
    "rung": "dev_2022",
    "verdict": None,
    "rung_pass": True,
    "vs_incumbent": {"pinball_rel": -0.04, "cov_err_max": 0.012},
    "variants": [{}, {}],
}


def test_context_lines_models_studies_live(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    _models(p.bt_root)
    _ledger(p.bt_root, DEV, VERDICT)
    snap = aa.ml_snapshot(p)
    models = "\n".join(aa.ml_models_lines(snap["models"]))
    assert f"- m1: champion {INC}" in models and "holdout reads 0" in models
    studies = "\n".join(aa.ml_studies_lines(snap))
    assert "- ML-1 [ledger only] m1 vs m1-aaaaaaaaaaaa" in studies
    assert "dev 2022 pass: pinball -4% vs incumbent, cov err 1.2 pts, best of 2" in studies
    assert "verdict pass -> m1-bbbbbbbbbbbb (shadow, variant 0); dev only" in studies
    assert snap["verdict_keys"] == [f"ML-1:{'c' * 12}:2026-10-24T06:00:00-04:00"]
    live = aa.ml_live_lines({
        "day": "2026-10-24", "schema": 1, "model_versions": [INC],
        "coverage": {"scored": 40, "candidates": 50, "pct_scored": 80.0, "ticks": 12,
                     "latency_ms_p95": 9},
        "calibration": {"all": {"pop": {"n": 12, "brier": 0.2}, "e_r": {"n": 40,
                                "mean_e_r": 0.1, "mean_r": 0.05}}},
        "feature_drift": {"status": "not_run"}, "parity": {"status": "ok"},
    })  # fmt: skip
    text = "\n".join(live)
    assert "coverage: 40/50 candidates scored (80.0%)" in text
    assert "pop n=12 insufficient" in text and "e_r n=40 mean e_r 0.1 vs mean R 0.05" in text
    assert "feature drift (PSI): not run" in text and "parity: ok" in text
    assert aa.ml_live_lines(None) == [
        "- no ml_health record yet (the nightly ml refresh has not written one)"
    ]


def test_latest_ml_health_on_or_before_day(tmp_path: Path) -> None:
    hdir = tmp_path / aa.ML_HEALTH
    hdir.mkdir(parents=True)
    for d in ("2026-10-22", "2026-10-24", "2026-10-26"):
        (hdir / f"{d}.json").write_text(json.dumps({"day": d}))
    assert aa.latest_ml_health(tmp_path, _dt.date(2026, 10, 25)) == {"day": "2026-10-24"}
    assert aa.latest_ml_health(tmp_path, _dt.date(2026, 10, 1)) is None


def test_new_ml_verdict_wakes_the_verdict_run_once(tmp_path: Path) -> None:
    keys = [f"ML-1:{'c' * 12}:2026-10-24T06:00:00-04:00"]
    tue = NOW + _dt.timedelta(days=2)
    d = aa.verdict_gate_decision({}, tue, [], [], keys)
    assert d["wake"] and d["new_ml_verdicts"] == keys and "ML-1" in d["reason"]
    seen = {"ml_verdicts_seen": keys}
    assert not aa.verdict_gate_decision(seen, tue, [], [], keys)["wake"]
    assert aa.unseen_verdicts(seen, [], [], keys) == ([], [], [])


def test_mark_stores_ml_verdicts_seen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _paths(tmp_path)
    run_dir = p.runs / "r"
    run_dir.mkdir(parents=True)
    keys = [f"ML-1:{'c' * 12}:2026-10-24T06:00:00-04:00"]
    aa.save_json(run_dir / "metrics.json", {"now": aa.iso(NOW), "closed_max_rowid": 3,
                                            "ml_verdicts": keys})  # fmt: skip
    (run_dir / "reconciled.json").write_text("{}")
    assert aa.main(["mark", str(run_dir)], p, NOW) == 0
    capsys.readouterr()
    assert aa.load_json(p.state, {})["ml_verdicts_seen"] == keys


def test_verdict_run_may_propose_one_ml_follow_up() -> None:
    vids = ["ML-1"]
    ml = _meta(verdicts={"ML-1": {"verdict": "pass", "status": "shadow", "version": "m1-b"}})
    f = _ml_finding(evidence="ML-1 verdict pass: try the CQR on h10 next")
    doc = {"mode": "verdict", "findings": [f],
           "verdicts": [{"id": "ML-1", "read": "dev pass -> shadow"}]}  # fmt: skip
    assert aa.validate_verdict_findings(doc, aa.empty_backlog(), vids, [], (), ml) == []
    two = {**doc, "findings": [f, _ml_finding("theme:other", evidence="ML-1 verdict")]}
    errs = aa.validate_verdict_findings(two, aa.empty_backlog(), vids, [], (), ml)
    assert any("2 new study proposals (BT/ML)" in e for e in errs), errs


def test_skill_and_prompts_document_ml_studies() -> None:
    skill = (ANALYST / "skills" / "arc-analyst" / "SKILL.md").read_text()
    for rule in (
        "**ML MODEL STUDIES (D133",
        "A **new family is a card**",
        "`next_ml_study: ML-<n>`",
        "The **2-per-ISO-week cap is shared** by BT and ML",
        "`approve ML-<n>` · `drop ML-<n> <why>` · `stop ML-<n> <why>` (E27.8",
        "*Next ML*",
        "`models` (D126/D133",
    ):
        assert rule in skill, rule
    assert "never train a model" in (ANALYST / "prompt.md").read_text()
    assert "ml_study_spec" in (ANALYST / "prompt_verdict.md").read_text()
