"""E27.7 (D133): ML studies: spec, queue kind, adopt + shared weekly cap, ladder,
early stop, interrupt/resume, runner dispatch, holdout refusal, verdict → status,
determinism."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
import yaml

from arc.backtest import queue as bq
from arc.backtest.queue import QueueEntry, QueueError, StudyQueue, load_queue, update_queue
from arc.backtest.study_adopt import AdoptStudyError
from arc.experiments.publish import PublishResult
from arc.ml import registry as reg
from arc.ml import study as mls
from arc.ml.m1.dataset import HoldoutReadError
from arc.ml.m1.train import LGB_PARAMS, model_features
from arc.routines.config import BacktestStudiesSettings
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Sequence

APPROVAL = "slack:1791669727.601139"
NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=ET)  # Wed, ISO week 2026-W44
TUE_NIGHT = dt.datetime(2026, 10, 13, 21, 30, tzinfo=ET)
INC = "m1-aaaaaaaaaaaa"
FEAT = model_features()[0]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _doc(sid: str = "ML-1", **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": sid,
        "hypothesis": "monthly rolling CQR fixes rv coverage drift",
        "owner": "Arc Analyst A-7",
        "created": "2026-10-27",
        "family": "m1",
        "incumbent": INC,
        "change_kind": "calibration",
        "overlay": {"targets": ["rv"], "calibration": {"method": "rolling_monthly"}},
        "decision_rule": {"kind": "m1_keep"},
    }
    d.update(kw)
    return d


def _spec(sid: str = "ML-1", **kw: Any) -> mls.MlStudySpec:
    return mls.MlStudySpec.model_validate(_doc(sid, **kw))


def _models(root: Path, family: str = "m1", version: str = INC) -> Path:
    vdir = root / family / version
    vdir.mkdir(parents=True, exist_ok=True)
    man = {
        "version": version,
        "lgb_params": LGB_PARAMS,
        "num_rounds": 300,
        "features": model_features(),
        "config": {},
    }
    (vdir / "manifest.json").write_text(json.dumps(man))
    return root


def _write_spec(d: Path, spec_doc: dict[str, Any]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{spec_doc['id']}.yaml"
    p.write_text(yaml.safe_dump(spec_doc))
    return p


def _bt_entry(sid: str, at: dt.datetime) -> QueueEntry:
    return QueueEntry(id=sid, spec_hash="b" * 64, approved_at=at, approval=APPROVAL)


@dataclass
class Main:
    texts: dict[str, str] = field(default_factory=dict)

    def files(self, directory: str) -> dict[str, str]:
        return {k: v for k, v in self.texts.items() if k.startswith(directory + "/")}


@dataclass
class Pub:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def publish(
        self, *, branch: str, files: dict[str, str], title: str, body: str
    ) -> PublishResult:
        self.calls.append({"branch": branch, "files": files, "title": title, "body": body})
        return PublishResult(branch=branch, url="https://x/pr/9", status="opened", auto_merge=True)


# ---------------------------------------------------------------------------
# spec validation
# ---------------------------------------------------------------------------


def test_spec_valid_and_hash_locked() -> None:
    s = _spec()
    assert mls.variants(s) == [s.overlay]
    assert mls.ml_study_hash(s) == mls.ml_study_hash(_spec())
    assert mls.ml_study_hash(s) != mls.ml_study_hash(_spec(hypothesis="other"))


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"overlay": {"bogus": 1}}, "bogus"),
        ({"overlay": {"params": {"seed": 7}}}, "not tunable"),
        ({"overlay": {"features": {"add": ["no_such_feature"]}}}, "feature registry"),
        ({"grid": [{"features": {"drop": ["nope"]}}]}, r"grid\[0\].*feature registry"),
        ({"overlay": {"targets": ["skew_h5"]}}, "log_ret|rv"),
        ({"decision_rule": {"kind": "m2_promote"}}, "not registered for family m1"),
        ({"decision_rule": {"kind": "my_rule"}}, "not registered"),
        ({"id": "BT-3"}, "ML-<n>"),
        ({"extra_key": 1}, "extra"),
    ],
)
def test_spec_refusals(change: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _spec(**change)


def test_m2_overlay_validated_against_m2config() -> None:
    ok = _spec(family="debit", decision_rule={"kind": "m2_promote"}, overlay={})
    assert ok.family == "debit"
    with pytest.raises(ValueError, match="unknown_knob"):
        _spec(family="debit", decision_rule={"kind": "m2_promote"}, overlay={"unknown_knob": 1})


def test_known_feature_and_grid_variants() -> None:
    s = _spec(grid=[{"params": {"num_leaves": 15}}, {"features": {"drop": [FEAT]}}])
    vs = mls.variants(s)
    assert len(vs) == 2
    assert vs[0]["params"] == {"num_leaves": 15} and vs[0]["targets"] == ["rv"]
    assert vs[1]["features"] == {"drop": [FEAT]}


def test_resolve_grid_cap_incumbent_and_champion(tmp_path: Path) -> None:
    root = _models(tmp_path)
    grid = [{"params": {"num_leaves": n}} for n in range(7, 7 + 13)]
    with pytest.raises(ValueError, match="max_grid 12"):
        mls.resolve_spec(_spec(grid=grid), root, max_grid=12)
    assert mls.resolve_spec(_spec(grid=grid[:12]), root, max_grid=12).incumbent == INC
    with pytest.raises(ValueError, match="no manifest"):
        mls.resolve_spec(_spec(incumbent="m1-bbbbbbbbbbbb"), root, max_grid=12)
    with pytest.raises(ValueError, match="no champion"):
        mls.resolve_spec(_spec(incumbent="champion"), root, max_grid=12)
    reg.register(root, "m1", INC, now=NOW, rule="m1_keep", rule_passed=True)
    idx = reg.load_index(root, "m1").model_copy(update={"champion": INC})
    reg._save_index(root, idx)
    assert mls.resolve_spec(_spec(incumbent="champion"), root, max_grid=12).incumbent == INC
    with pytest.raises(ValueError, match="has no model"):
        mls.resolve_spec(_spec(family="credit", decision_rule={"kind": "m2_promote"}, overlay={}),
                         root, max_grid=12)  # fmt: skip


def test_spec_file_name_must_match(tmp_path: Path) -> None:
    p = _write_spec(tmp_path, _doc("ML-2"))
    p2 = p.rename(tmp_path / "ML-3.yaml")
    with pytest.raises(ValueError, match="file name"):
        mls.load_ml_spec(p2)
    s = _write_spec(tmp_path / "s", _doc("ML-0"))
    assert mls.load_ml_spec(s.rename(tmp_path / "scratch.yaml")).id == "ML-0"


# ---------------------------------------------------------------------------
# queue: kind, ordering, ETA
# ---------------------------------------------------------------------------


def test_old_bt_queue_loads_unchanged(tmp_path: Path) -> None:
    old = {
        "version": 1,
        "entries": [
            {
                "id": "BT-4",
                "spec_hash": "c" * 64,
                "status": "queued",
                "approved_at": "2026-10-12T10:00:00-04:00",
                "approval": APPROVAL,
                "ladder_state": "c125_dev",
                "runs": [],
                "stop_reason": None,
                "pr_url": None,
            },
            {
                "id": "BT-5",
                "spec_hash": "d" * 64,
                "approved_at": "2026-10-13T10:00:00-04:00",
                "approval": APPROVAL,
            },
        ],
    }
    p = tmp_path / "queue.json"
    p.write_text(json.dumps(old))
    q = load_queue(p)
    assert [(e.id, e.kind, e.ladder_state) for e in q.entries] == [
        ("BT-4", "bt", "c125_dev"),
        ("BT-5", "bt", "c125_dev"),
    ]


def test_ml_id_only_with_kind_ml() -> None:
    at = NOW
    with pytest.raises(ValueError, match="use BT-<n>"):
        QueueEntry(id="ML-1", spec_hash="a" * 64, approved_at=at, approval=APPROVAL)
    with pytest.raises(ValueError, match="use ML-<n>"):
        QueueEntry(id="BT-1", kind="ml", spec_hash="a" * 64, approved_at=at, approval=APPROVAL)
    with pytest.raises(ValueError, match="not on the ml ladder"):
        QueueEntry(id="ML-1", kind="ml", spec_hash="a" * 64, approved_at=at, approval=APPROVAL,
                   ladder_state="c125_dev")  # fmt: skip
    e = QueueEntry(id="ML-1", kind="ml", spec_hash="a" * 64, approved_at=at, approval=APPROVAL)
    assert e.ladder_state == "dev_2022"


def test_mixed_order_and_eta() -> None:
    q = StudyQueue(
        entries=[
            QueueEntry(
                id="ML-2",
                kind="ml",
                spec_hash="a" * 64,
                approved_at=NOW - dt.timedelta(days=3),
                approval=APPROVAL,
            ),  # fmt: skip
            _bt_entry("BT-9", NOW - dt.timedelta(days=4)),
            QueueEntry(
                id="ML-1",
                kind="ml",
                spec_hash="e" * 64,
                approved_at=NOW - dt.timedelta(days=1),
                approval=APPROVAL,
            ),  # fmt: skip
        ]
    )
    assert [e.id for e in q.active()] == ["BT-9", "ML-2", "ML-1"]
    spec = _spec("ML-2")
    est = mls.queue_ml_estimates(q, [], {"ML-2": spec}, default_s=2400)
    assert set(est) == {"ML-2"}  # ML-1's spec unknown -> no estimate
    rows = {r["id"]: r for r in bq.eta_rows(q, [], est)}
    assert rows["ML-2"]["kind"] == "ml" and rows["BT-9"]["kind"] == "bt"
    assert rows["ML-2"]["queue_position"] == 2
    assert rows["ML-1"]["eta_nights"] is None  # unknown estimate -> unknown ETA


def test_ml_estimates_from_own_ledger() -> None:
    spec = _spec(grid=[{"params": {"num_leaves": 15}}, {"params": {"num_leaves": 63}}])
    assert mls.ml_estimate_s([], spec, "dev_2022", default_s=1000) == round(1000 * 2 * 1.2)
    assert mls.ml_estimate_s([], spec, "verdict", default_s=1000) == round(3000 * 1.2)
    past = mls.MlLedgerEntry(study="ML-9", spec_hash="x", family="m1", incumbent=INC,
                             rung="dev_2022", at=NOW, rule="m1_keep", runtime_s=900.0,
                             variants=[{}, {}, {}])  # fmt: skip
    other = past.model_copy(update={"family": "credit", "runtime_s": 9e6})
    assert mls.ml_estimate_s([past, other], spec, "dev_2022", default_s=1000) == round(
        300 * 2 * 1.2
    )


# ---------------------------------------------------------------------------
# adopt + shared weekly cap
# ---------------------------------------------------------------------------


def _adopt(tmp_path: Path, sid: str = "ML-1", **kw: Any) -> Any:
    root = _models(tmp_path / "models")
    draft = _write_spec(tmp_path / "drafts", _doc(sid))
    args: dict[str, Any] = {
        "approval": APPROVAL,
        "now": NOW,
        "queue_path": tmp_path / "queue.json",
        "ledger": tmp_path / "ml_ledger.jsonl",
        "models_root": root,
        "studies_dir": tmp_path / "committed",
        "main": Main(),
        "publisher": Pub(),
    }
    args.update(kw)
    return mls.adopt_ml_study(sid, draft, **args), args


def test_adopt_writes_spec_and_queues_ml(tmp_path: Path) -> None:
    res, args = _adopt(tmp_path)
    assert res.created and res.status == "queued"
    assert res.path == "config/ml_studies/ML-1.yaml" and res.branch == "ml/1"
    call = args["publisher"].calls[0]
    text = call["files"]["config/ml_studies/ML-1.yaml"]
    assert mls.ml_study_hash(mls.MlStudySpec.model_validate(yaml.safe_load(text))) == res.spec_hash
    assert "XP-advisory:" in call["body"]
    e = load_queue(args["queue_path"]).get("ML-1")
    assert e is not None and e.kind == "ml" and e.ladder_state == "dev_2022"
    assert e.pr_url == "https://x/pr/9"
    assert res.queue_position == 1 and res.eta_nights is not None
    # retry of the same approval: idempotent, no second entry, cap not charged again
    res2, _ = _adopt(tmp_path, publisher=Pub())
    assert not res2.created and len(load_queue(args["queue_path"]).entries) == 1


def test_adopt_refusals(tmp_path: Path) -> None:
    with pytest.raises(AdoptStudyError, match="approval"):
        _adopt(tmp_path, approval="yes")
    with pytest.raises(AdoptStudyError, match="hash mismatch"):
        _adopt(tmp_path, expect_hash="f" * 64)
    main = Main({"config/ml_studies/ML-1.yaml": yaml.safe_dump(_doc(hypothesis="else"))})
    with pytest.raises(AdoptStudyError, match="is taken"):
        _adopt(tmp_path, main=main)
    retired = '{"retired": [{"id": "ML-1", "at": "2026-10-20", "by": "owner", "reason": "x"}]}'
    with pytest.raises(AdoptStudyError, match="retired"):
        _adopt(tmp_path, main=Main({"config/ml_studies/retired.yaml": retired}))
    res, args = _adopt(tmp_path, dry_run=True, publisher=None)
    assert res.status == "dry-run" and not args["queue_path"].exists()


def test_weekly_cap_counts_bt_and_ml_together(tmp_path: Path) -> None:
    qp = tmp_path / "queue.json"
    monday = dt.datetime(2026, 10, 26, 9, 0, tzinfo=ET)
    update_queue(qp, lambda q: q.entries.append(_bt_entry("BT-7", monday)))
    res, _ = _adopt(tmp_path, "ML-1")  # 2nd approval of the week: fine
    assert res.created
    with pytest.raises(AdoptStudyError, match=r"weekly cap is 2 .*BT-7.*ML-1"):
        _adopt(tmp_path, "ML-2")
    # the cap also binds a BT approval after an ML one (shared check)
    with pytest.raises(QueueError, match="BT and ML together"):
        bq.check_weekly_cap(load_queue(qp), "BT-8", NOW)
    # a retry of an already-queued id is not a new approval
    bq.check_weekly_cap(load_queue(qp), "ML-1", NOW)
    # next ISO week resets
    next_mon = dt.datetime(2026, 11, 2, 0, 0, tzinfo=ET)
    res3, _ = _adopt(tmp_path, "ML-2", now=next_mon)
    assert res3.created


def test_drop_reserves_the_number(tmp_path: Path) -> None:
    led = tmp_path / "l.jsonl"
    qp = tmp_path / "q.json"
    line = mls.drop_ml_study("ML-4", "superseded", now=NOW, queue_path=qp, ledger=led)
    assert line.rung == "dropped" and mls.ml_rung_line(line) == "ML-4 · dropped · owner: superseded"
    with pytest.raises(AdoptStudyError, match="already has ML ledger"):
        mls.drop_ml_study("ML-4", "again", now=NOW, queue_path=qp, ledger=led)
    _adopt(tmp_path, "ML-5", queue_path=qp)
    with pytest.raises(AdoptStudyError, match="use `stop ML-5"):
        mls.drop_ml_study("ML-5", "x", now=NOW, queue_path=qp, ledger=led)


# ---------------------------------------------------------------------------
# ladder (fake adapter): selection, early stop, interrupt/resume
# ---------------------------------------------------------------------------


@dataclass
class FakeAdapter:
    """Per (variant, year): (inner selection, passed, miss)."""

    table: dict[tuple[int, int], tuple[float, bool, float]]
    verdict_pass: bool = True
    calls: list[tuple[str, int]] = field(default_factory=list)
    picked: list[dict[str, Any]] = field(default_factory=list)

    def dev(self, i: int, overlay: dict[str, Any], year: int) -> mls.RungOutcome:
        self.calls.append(("dev", year))
        sel, ok, miss = self.table[(i, year)]
        tg = {"rv_h5": {"pass": ok, "pinball": 1.0 + miss, "cov_err": 0.024}}
        return mls.RungOutcome(i, overlay, sel, ok, miss, {"targets": tg}, 10.0)

    def verdict(self, overlay: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        self.picked.append(overlay)
        st = "shadow" if self.verdict_pass else "candidate"
        return {"version": "m1-cccccccccccc", "rule_passed": self.verdict_pass, "status": st,
                "years": {"2022": {"pass": True}, "2023": {"pass": self.verdict_pass}}}  # fmt: skip

    def incumbent_metric(self, year: int, target: str) -> float | None:
        return 1.25


def _env(tmp_path: Path, stop: Any = None) -> mls.StudyEnv:
    return mls.StudyEnv(
        data_dir=tmp_path / "data",
        models_root=tmp_path / "models",
        ledger=tmp_path / "ml" / "ledger.jsonl",
        runs_dir=tmp_path / "ml",
        stop=stop or (lambda: False),
    )


def _grid_spec() -> mls.MlStudySpec:
    return _spec(grid=[{"params": {"num_leaves": 15}}, {"params": {"num_leaves": 63}}])


def test_ladder_picks_best_inner_not_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # variant 0 wins the TEST years (smaller miss), variant 1 wins the INNER metric
    fake = FakeAdapter({(0, 2022): (0.30, True, -0.2), (1, 2022): (0.20, True, -0.01),
                        (0, 2023): (0.30, True, -0.2), (1, 2023): (0.20, True, -0.01)})  # fmt: skip
    monkeypatch.setattr(mls, "adapter", lambda _s, _e: fake)
    env = _env(tmp_path)
    lines = mls.run_ladder(_grid_spec(), env, clock=lambda: NOW, fail_margin=0.05)
    assert [ln.rung for ln in lines] == ["dev_2022", "dev_2023", "verdict"]
    assert lines[-1].best_variant == 1 and fake.picked[0]["params"] == {"num_leaves": 63}
    assert lines[-1].verdict == "pass" and lines[-1].status == "shadow"
    assert all(v["selection_metric"] == "inner_pinball" for v in lines[0].variants)
    assert len(lines[0].variants) == 2  # every configuration tried is logged
    assert mls.ml_rung_line(lines[1]).startswith("ML-1 · dev 2023 · pass · pinball −21%")
    assert "cov err 2.4 pts" in mls.ml_rung_line(lines[1])
    assert mls.ml_rung_line(lines[2]).endswith("→ Shadow")
    v = json.loads((env.runs_dir / "ML-1" / "verdict.json").read_text())
    assert v["verdict"] == "pass" and v["version"] == "m1-cccccccccccc"
    assert len(mls.load_ml_ledger(env.ledger)) == 3
    # rerun: nothing left to do
    assert mls.run_ladder(_grid_spec(), env, clock=lambda: NOW, fail_margin=0.05) == []


def test_clear_dev_fail_stops_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdapter({(0, 2022): (0.3, False, 0.08), (1, 2022): (0.2, False, 0.06)})
    monkeypatch.setattr(mls, "adapter", lambda _s, _e: fake)
    lines = mls.run_ladder(_grid_spec(), _env(tmp_path), clock=lambda: NOW, fail_margin=0.05)
    assert [ln.rung for ln in lines] == ["dev_2022"] and lines[0].clear_fail
    assert fake.calls == [("dev", 2022), ("dev", 2022)]


def test_near_miss_is_not_a_clear_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAdapter({(0, 2022): (0.3, False, 0.08), (1, 2022): (0.2, False, 0.03),
                        (0, 2023): (0.3, True, -0.1), (1, 2023): (0.2, True, -0.1)},
                       verdict_pass=False)  # fmt: skip
    monkeypatch.setattr(mls, "adapter", lambda _s, _e: fake)
    lines = mls.run_ladder(_grid_spec(), _env(tmp_path), clock=lambda: NOW, fail_margin=0.05)
    assert not lines[0].clear_fail and lines[-1].verdict == "fail"
    assert lines[-1].status == "candidate"


def test_interrupt_leaves_no_line_then_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeAdapter({(i, y): (0.2, True, -0.1) for i in (0, 1) for y in (2022, 2023)})
    monkeypatch.setattr(mls, "adapter", lambda _s, _e: fake)
    ticks = iter([False, False, False, True])  # yield before dev_2023's 2nd variant
    env = _env(tmp_path, stop=lambda: next(ticks))
    lines = mls.run_ladder(_grid_spec(), env, clock=lambda: NOW, fail_margin=0.05)
    assert [ln.rung for ln in lines] == ["dev_2022"]
    assert [e.rung for e in mls.load_ml_ledger(env.ledger)] == ["dev_2022"]
    env2 = _env(tmp_path)
    lines2 = mls.run_ladder(_grid_spec(), env2, clock=lambda: NOW, fail_margin=0.05)
    assert [ln.rung for ln in lines2] == ["dev_2023", "verdict"]


def test_verdict_needs_both_dev_rungs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mls, "adapter", lambda _s, _e: FakeAdapter({}))
    with pytest.raises(mls.MlStudyError, match="both dev rungs"):
        mls.run_rung(_spec(), "verdict", _env(tmp_path), now=NOW, fail_margin=0.05)


# ---------------------------------------------------------------------------
# holdout refusal
# ---------------------------------------------------------------------------


def test_study_refuses_holdout(tmp_path: Path) -> None:
    with pytest.raises(mls.MlStudyError, match="no holdout rung"):
        mls.run_rung(_spec(), "holdout", _env(tmp_path), now=NOW, fail_margin=0.05)
    env = _env(tmp_path)
    _models(env.models_root)
    _models(env.models_root, "debit", "m2-dddddddddddd")
    m1 = mls._M1(_spec(), env)
    # the guard fires before any data is read (data_dir does not even exist)
    with pytest.raises(HoldoutReadError, match="reaches the holdout"):
        m1.dev(0, {}, 2024)
    m2s = _spec(family="debit", incumbent="m2-dddddddddddd", decision_rule={"kind": "m2_promote"},
                overlay={})  # fmt: skip
    with pytest.raises(HoldoutReadError):
        mls._M2(m2s, env).dev(0, {}, 2025)
    for y in (2022, 2023):
        mls._check_dev(y)  # dev years pass


def test_m1_study_loader_reads_dev_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The study's dataset ends before the holdout, and E27.3's guard (``load_panel``)
    refuses a window reaching it."""
    from arc.ml.m1 import dataset, train

    seen: list[dt.date] = []

    def fake_panel(_root: Path, start: dt.date, end: dt.date, *_a: Any) -> pd.DataFrame:
        seen.append(end)
        raise FileNotFoundError("stop")

    monkeypatch.setattr(dataset, "load_panel", fake_panel)
    with pytest.raises(FileNotFoundError):
        train.load_dev_dataset(tmp_path, tmp_path)
    assert seen and seen[0] < mls.HOLDOUT_START
    monkeypatch.undo()
    with pytest.raises(HoldoutReadError):
        dataset.load_panel(tmp_path, dt.date(2023, 1, 1), mls.HOLDOUT_START)


# ---------------------------------------------------------------------------
# verdict → registry status (real registry; the trainer is faked)
# ---------------------------------------------------------------------------


@dataclass
class _Man:
    version: str
    feature_schema: str = "v1"


@dataclass
class _Run:
    manifest: _Man
    decisions: pd.DataFrame
    model_dir: Path


@pytest.mark.parametrize(("ok23", "status"), [(True, "shadow"), (False, "candidate")])
def test_m1_verdict_pass_shadow_fail_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok23: bool, status: str
) -> None:
    from arc.ml.m1 import report, train

    env = _env(tmp_path)
    _models(env.models_root)
    got: dict[str, Any] = {}

    def fake_train(*_a: Any, **kw: Any) -> _Run:
        got.update(kw)
        vdir = env.models_root / "m1" / "m1-eeeeeeeeeeee"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "manifest.json").write_text(json.dumps({"version": "m1-eeeeeeeeeeee"}))
        dec = pd.DataFrame(
            [
                {
                    "kind": k,
                    "h": h,
                    "source": "lgbm" if k == "rv" else "baseline",
                    "test2022_pass": True,
                    "test2022_detail": "ok",
                    "test2023_pass": ok23 or k != "rv",
                    "test2023_detail": "d",
                }  # fmt: skip
                for k in ("log_ret", "rv")
                for h in (5, 10, 21, 42)
            ]
        )
        return _Run(_Man("m1-eeeeeeeeeeee"), dec, vdir)

    monkeypatch.setattr(train, "train_m1", fake_train)
    monkeypatch.setattr(report, "render_card", lambda _d: "card")
    m1 = mls._M1(_spec(), env)
    monkeypatch.setattr(m1, "df", lambda: "DF")
    res = m1.verdict(mls.variants(_spec())[0], NOW)
    assert res["rule_passed"] is ok23 and res["status"] == status
    assert reg.load_index(env.models_root, "m1").versions["m1-eeeeeeeeeeee"].status == status
    assert got["study"] == "ML-1" and got["calibration"] == "rolling_monthly"
    assert got["lgbm_targets"] == [("rv", 5), ("rv", 10), ("rv", 21), ("rv", 42)]
    assert got["extra"] == {"calibration": {"method": "rolling_monthly"}, "targets": ["rv"]}
    assert set(res["years"]["2023"]["targets"]) == {"rv_h5", "rv_h10", "rv_h21", "rv_h42"}


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_m2_dev_rung_on_the_harness_is_deterministic(tmp_path: Path) -> None:
    """The real M2 dev path (``train_family(final_fit=False)``) on a synthetic store:
    one fold, nothing written, inner log-loss as the selection metric, same result twice."""
    pytest.importorskip("lightgbm")
    from tests.test_ml_m2_model import _cfg, _write_store

    data = tmp_path / "data"
    _write_store(data / "ml")
    env = _env(tmp_path)
    env.data_dir = data
    vdir = env.models_root / "debit" / "m2-dddddddddddd"
    vdir.mkdir(parents=True)
    (vdir / "manifest.json").write_text(json.dumps({"config": _cfg().model_dump(mode="json")}))
    spec = _spec(family="debit", incumbent="m2-dddddddddddd", decision_rule={"kind": "m2_promote"},
                 overlay={"max_rounds": 30})  # fmt: skip
    ad = mls._M2(spec, env)
    a = ad.dev(0, dict(spec.overlay), 2023)
    b = ad.dev(0, dict(spec.overlay), 2023)
    assert a.selection == b.selection and a.detail == b.detail and a.passed == b.passed
    assert 0 < a.selection < 1  # an inner-validation win log-loss
    assert set(a.detail) >= {"win_log_loss", "ll_logistic", "ll_managed_pop", "er_ic"}
    assert not any((env.models_root / "debit").glob("m2-[!d]*"))  # dev writes no version


def test_version_id_plain_unchanged_and_overlay_deterministic() -> None:
    from arc.ml.m1.train import version_of

    plain = version_of("v1", "d" * 16, LGB_PARAMS, 300)
    assert version_of("v1", "d" * 16, LGB_PARAMS, 300, extra=None) == plain
    ov = mls.M1Overlay.model_validate(_doc()["overlay"])
    a = version_of("v1", "d" * 16, LGB_PARAMS, 300, extra=ov.extra())
    b = version_of("v1", "d" * 16, LGB_PARAMS, 300, extra=mls.M1Overlay.model_validate(
        _doc()["overlay"]).extra())  # fmt: skip
    assert a == b != plain
    assert mls.M1Overlay().extra() is None  # an empty overlay is the plain trainer


def test_parallel_fit_is_byte_identical(tmp_path: Path) -> None:
    pytest.importorskip("lightgbm")
    import numpy as np

    from arc.ml.m1 import train

    rng = np.random.default_rng(5)
    days = list(pd.bdate_range("2020-07-01", periods=120).date)
    df = pd.DataFrame([{"date": d, "ticker": f"T{k}"} for k in range(8) for d in days])
    n = len(df)
    df["iv30"] = rng.uniform(0.15, 0.6, n)
    df["rv20"] = df["iv30"] * rng.uniform(0.7, 1.1, n)
    df["f_mom"] = rng.normal(size=n)
    df["har_rv_h5"] = df["rv20"]
    df["log_ret_h5"] = rng.normal(0, 0.02, n)
    df["rv_h5"] = df["rv20"] * np.exp(rng.normal(0, 0.2, n))
    feats = ["f_mom", "iv30", "rv20", "har_rv_h5"]
    params = {**train.LGB_PARAMS, "min_data_in_leaf": 50}

    def dumps(workers: int, targets: Sequence[tuple[str, int]] | None) -> dict[Any, str]:
        fs = train.fit_set(df, lambda _h: np.ones(n, bool), feats, horizons=(5,), params=params,
                           rounds=15, workers=workers, lgbm_targets=targets)  # fmt: skip
        return {k: b.model_to_string() for k, b in fs.boosters.items()}

    one = dumps(1, None)
    assert one == dumps(2, None)
    sub = dumps(2, [("rv", 5)])
    assert {k[0] for k in sub} == {"rv"} and all(sub[k] == one[k] for k in sub)


# ---------------------------------------------------------------------------
# runner: kind ml on the shared nightly runner
# ---------------------------------------------------------------------------


@dataclass
class Clock:
    t: dt.datetime

    def __call__(self) -> dt.datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += dt.timedelta(seconds=s)


@dataclass
class Handle:
    clock: Clock
    duration: float
    on_done: Any
    rc: int = 0
    pid: int = 4243
    started: dt.datetime = field(init=False)
    terminated: bool = False
    killed: bool = False

    def __post_init__(self) -> None:
        self.started = self.clock()

    def poll(self) -> int | None:
        if self.killed:
            return -9
        if self.terminated:
            return 143
        if (self.clock() - self.started).total_seconds() >= self.duration:
            if self.rc == 0:
                self.on_done()
            return self.rc
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@dataclass
class World:
    tmp: Path
    clock: Clock
    main: Main = field(default_factory=Main)
    #: (study, rung) -> (duration s, rc, rung_pass, clear_fail, verdict)
    plan: dict[tuple[str, str], tuple[float, int, bool, bool, str]] = field(default_factory=dict)
    posts: list[str] = field(default_factory=list)
    launches: list[list[str]] = field(default_factory=list)
    specs: dict[str, mls.MlStudySpec] = field(default_factory=dict)

    @property
    def cfg(self) -> BacktestStudiesSettings:
        ml = {
            "ledger": str(self.tmp / "ml" / "ledger.jsonl"),
            "runs_dir": str(self.tmp / "ml"),
            "models_dir": str(self.tmp / "models"),
            "default_rung_s": 600,
        }
        return BacktestStudiesSettings.model_validate({"ml_studies": ml})

    @property
    def paths(self) -> Any:
        from arc.backtest.runner import RunnerPaths

        return RunnerPaths(
            queue=self.tmp / "queue.json",
            ledger=self.tmp / "ledger.jsonl",
            runs_dir=self.tmp / "runs",
            report_dir=None,
            data_dir=self.tmp / "data",
            cwd=self.tmp,
        )

    def add(self, sid: str, *, at: dt.datetime, merged: bool = True) -> None:
        doc = _doc(sid)
        spec = mls.MlStudySpec.model_validate(doc)
        self.specs[sid] = spec
        if merged:
            self.main.texts[f"config/ml_studies/{sid}.yaml"] = yaml.safe_dump(doc)
        e = QueueEntry(id=sid, kind="ml", spec_hash=mls.ml_study_hash(spec), approved_at=at,
                       approval=APPROVAL)  # fmt: skip
        update_queue(self.paths.queue, lambda q: q.entries.append(e))

    def launcher(self, argv: Sequence[str], log_path: Path, cwd: Path) -> Handle:
        argv = list(argv)
        self.launches.append(argv)
        assert "--ignore-window" in argv
        sid = argv[argv.index("run") + 1]
        rung = argv[argv.index("--rung") + 1]
        at = dt.datetime.fromisoformat(argv[argv.index("--now") + 1])
        dur, rc, ok, clear, verdict = self.plan.get((sid, rung), (600.0, 0, True, False, "pass"))
        spec = self.specs[sid]
        led = Path(argv[argv.index("--ledger") + 1])

        def done() -> None:
            kw: dict[str, Any] = {"variants": [{"index": 0, "selection": 0.1, "passed": ok,
                                                "miss": 0.1}]}  # fmt: skip
            if rung == "verdict":
                kw |= {"verdict": verdict, "status": "shadow" if verdict == "pass" else
                       "candidate", "version": "m1-ffffffffffff", "best_variant": 0}  # fmt: skip
            else:
                kw |= {"rung_pass": ok, "clear_fail": clear, "vs_incumbent": {"pinball_rel": -0.18}}
            line = mls.MlLedgerEntry(study=sid, spec_hash=mls.ml_study_hash(spec), family="m1",
                                     incumbent=INC, rung=rung, at=at, rule="m1_keep",
                                     runtime_s=dur, **kw)  # fmt: skip
            mls.append_ml_ledger(led, line)

        if rc != 0:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("Traceback\nValueError: boom\n")
        return Handle(self.clock, dur, done, rc=rc)

    def night(self) -> Any:
        from arc.backtest.runner import run_night

        return run_night(
            cfg=self.cfg,
            paths=self.paths,
            clock=self.clock,
            sleep=self.clock.sleep,
            main=self.main,
            launcher=self.launcher,
            post=self.posts.append,
        )

    def entry(self, sid: str) -> QueueEntry:
        e = load_queue(self.paths.queue).get(sid)
        assert e is not None
        return e


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Clock(TUE_NIGHT))


def test_runner_runs_the_ml_ladder(world: World) -> None:
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    res = world.night()
    e = world.entry("ML-1")
    assert e.status == "concluded" and e.ladder_state is None
    assert [r.rung for r in e.runs] == ["dev_2022", "dev_2023", "verdict"]
    assert [r.outcome for r in e.runs] == ["done"] * 3
    argv = world.launches[0]
    assert argv[argv.index("--studies-dir") + 1].endswith("ML-1/spec")
    assert "ML-1 verdict concluded" in res.summary
    assert any("→ Shadow" in p and p.startswith("ML · ") for p in world.posts)
    assert any("ML-1 · dev 2022 · pass · pinball −18%" in p for p in world.posts)


def test_runner_clear_fail_stops(world: World) -> None:
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    world.plan[("ML-1", "dev_2022")] = (600.0, 0, False, True, "")
    world.night()
    e = world.entry("ML-1")
    assert e.status == "stopped" and "dev fail on dev_2022" in (e.stop_reason or "")
    assert len(world.launches) == 1


def test_runner_interrupt_and_resume(world: World) -> None:
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    world.plan[("ML-1", "dev_2022")] = (12 * 3600.0, 0, True, False, "")  # longer than the night
    world.night()
    e = world.entry("ML-1")
    assert e.ladder_state == "dev_2022"
    assert [(r.rung, r.outcome) for r in e.runs] == [("dev_2022", "interrupted")]
    assert not (world.tmp / "ml" / "ledger.jsonl").exists()  # no line for an interrupted rung
    world.plan[("ML-1", "dev_2022")] = (600.0, 0, True, False, "")
    world.clock.t = dt.datetime(2026, 10, 14, 21, 30, tzinfo=ET)
    world.night()
    assert world.entry("ML-1").status == "concluded"


def test_runner_failure_and_mixed_order(world: World) -> None:
    world.add("ML-2", at=TUE_NIGHT - dt.timedelta(days=2))
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    world.plan[("ML-2", "dev_2022")] = (60.0, 1, True, False, "")
    world.night()
    assert world.entry("ML-2").status == "failed"
    assert world.launches[0][world.launches[0].index("run") + 1] == "ML-2"  # oldest first
    assert world.entry("ML-1").status == "concluded"
    assert any(p.startswith("ML · ML-2 · failed on dev_2022") for p in world.posts)


def test_runner_defers_when_it_does_not_fit(world: World) -> None:
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    world.clock.t = dt.datetime(2026, 10, 14, 8, 20, tzinfo=ET)  # 10 min left
    res = world.night()
    assert "ML-1 dev_2022 deferred: window" in res.summary
    assert world.launches == []


def test_runner_refuses_hash_drift_and_skips_retired(world: World) -> None:
    world.add("ML-1", at=TUE_NIGHT - dt.timedelta(days=1))
    world.main.texts["config/ml_studies/ML-1.yaml"] = yaml.safe_dump(_doc(hypothesis="edited"))
    world.night()
    assert world.entry("ML-1").status == "failed"
    world.add("ML-3", at=TUE_NIGHT - dt.timedelta(hours=1))
    world.main.texts["config/ml_studies/retired.yaml"] = json.dumps(
        {"retired": [{"id": "ML-3", "at": "2026-10-13", "by": "owner", "reason": "x"}]}
    )
    res = world.night()
    assert "ML-3 skipped: retired" in res.summary


def test_rung_argv_is_a_valid_cli(tmp_path: Path) -> None:
    import argparse

    from arc.ml.cli import add_ml_parser

    argv = mls.rung_argv("ML-1", rung="dev_2023", spec_dir=tmp_path, ledger=tmp_path / "l",
                         runs_dir=tmp_path, models_root=tmp_path, data_dir=tmp_path, workers=3,
                         now=NOW, python="py")  # fmt: skip
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command")
    add_ml_parser(sub)
    ns = p.parse_args(argv[3:])
    assert (ns.study_command, ns.id, ns.rung, ns.workers, ns.ignore_window) == (
        "run", "ML-1", "dev_2023", 3, True,
    )  # fmt: skip
    assert ns.now == NOW


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def test_cli_validate_status_show_and_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _models(tmp_path / "data" / "ml" / "models")
    spec = _write_spec(tmp_path, _doc("ML-0"))
    assert _cli("ml", "study", "validate", str(spec), "--models", str(root)) == 0
    assert "ok: ML-0 m1 incumbent m1-aaaaaaaaaaaa · 1 variant(s) · rule m1_keep" in (
        capsys.readouterr().out
    )
    qp = tmp_path / "queue.json"
    update_queue(qp, lambda q: q.entries.append(_bt_entry("BT-3", NOW)))
    _adopt(tmp_path, "ML-1", queue_path=qp, now=NOW + dt.timedelta(hours=1))
    assert _cli("ml", "study", "status", "--queue", str(qp), "--data", str(tmp_path / "data"),
                "--studies-dir", str(tmp_path / "drafts")) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "BT-3   bt [queued] next c125_dev" in out and "ML-1   ml [queued] next dev_2022" in out
    # the runner's window is open (queue file in data/backtest): a manual run yields
    (tmp_path / "data" / "backtest").mkdir(parents=True)
    (tmp_path / "data" / "backtest" / "queue.json").write_text("{}")
    rc = _cli("ml", "study", "run", "--spec", str(spec), "--data", str(tmp_path / "data"))
    assert rc == 4 and "yielding" in capsys.readouterr().err
    # a numbered spec can't go through --spec
    num = _write_spec(tmp_path / "n", _doc("ML-7"))
    rc = _cli("ml", "study", "run", "--spec", str(num), "--data", str(tmp_path / "data"),
              "--ignore-window", "--models", str(root))  # fmt: skip
    assert rc == 2 and "scratch specs only" in capsys.readouterr().err
    assert _cli("ml", "study", "stop", "BT-3", "x", "--queue", str(qp)) == 3
    assert _cli("ml", "study", "stop", "ML-1", "owner call", "--queue", str(qp)) == 0
    assert load_queue(qp).get("ML-1").status == "stopped"  # type: ignore[union-attr]
