"""E26.2 (D121): the backtest study queue and ``arc backtest study adopt``."""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.backtest.queue import (
    NIGHT_WINDOW_S,
    QueueEntry,
    QueueError,
    QueueRun,
    StudyQueue,
    enqueue,
    eta_rows,
    load_queue,
    night_of,
    rung_estimates,
    save_queue,
    stop_entry,
    update_queue,
)
from arc.backtest.study import LedgerEntry, StudySpec, load_study, study_hash
from arc.backtest.study_adopt import AdoptStudyError, adopt_study
from arc.experiments.publish import PublishResult
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 16, 15, 0, tzinfo=ET)
APPROVAL = "slack:1791669727.601139"
TP = {"exits": {"default": {"take_profit_pct_of_max_gain": 0.4}}}


def _draft_doc(sid: str = "BT-12", **over: Any) -> dict[str, Any]:
    d = {
        "id": sid,
        "hypothesis": "take profit 0.4 beats 0.5 on margin condors",
        "owner": "Arc Analyst A-3",
        "created": "2026-10-16",
        "arms": {"control": {}, "treatments": {"t1": {"overlay": TP}}},
        "profiles": ["margin"],
        "universe_ladder": ["c125", "c500"],
        **over,
    }
    return {k: v for k, v in d.items() if v is not None}


def _write_draft(tmp: Path, sid: str = "BT-12", **over: Any) -> Path:
    path = tmp / "study-specs" / f"{sid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# DRAFT study spec by the Arc Analyst\n" + json.dumps(_draft_doc(sid, **over)))
    return path


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def publish(self, *, branch: str, files: Any, title: str, body: str) -> PublishResult:
        self.calls.append({"branch": branch, "files": dict(files), "title": title, "body": body})
        if self.fail:
            from arc.experiments.publish import PublishError

            raise PublishError("gh pr create failed (1): network")
        url = "https://github.com/x/y/pull/9"
        return PublishResult(branch=branch, url=url, status="opened", auto_merge=True)


class FakeMain:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self._files = files or {}

    def files(self, directory: str) -> dict[str, str]:
        return {k: v for k, v in self._files.items() if k.startswith(directory)}


def _adopt(tmp: Path, spec: Path, **kw: Any) -> Any:
    kw.setdefault("approval", APPROVAL)
    kw.setdefault("now", NOW)
    kw.setdefault("queue_path", tmp / "queue.json")
    kw.setdefault("ledger", tmp / "ledger.jsonl")
    kw.setdefault("studies_dir", tmp / "studies")
    kw.setdefault("main", FakeMain())
    kw.setdefault("publisher", FakePublisher())
    return adopt_study(spec.stem, spec, **kw)


def _entry(**over: Any) -> QueueEntry:
    d: dict[str, Any] = {
        "id": "BT-12",
        "spec_hash": "a" * 64,
        "approved_at": NOW,
        "approval": APPROVAL,
        **over,
    }
    return QueueEntry.model_validate(d)


def _ledger_row(phase: str, universe: str, runtime: float, at: dt.datetime) -> LedgerEntry:
    return LedgerEntry.model_validate(
        {
            "at": at, "study": "BT-3", "spec_hash": "b" * 64, "arm_set_hash": "c" * 64,
            "arms": {}, "profiles": ["margin"], "phase": phase, "universe": universe,
            "universe_version": "u", "partitions": {}, "start": "2020-01-01",
            "end": "2023-12-31", "data_end": "2026-10-01", "git_sha": "abc",
            "primary_metric": "net_pnl", "metrics": {}, "verdicts": {}, "variants": 1,
            "run_dir": "/nonexistent", "runtime_s": runtime,
        }
    )  # fmt: skip


# ---------------------------------------------------------------------------
# adopt: validation
# ---------------------------------------------------------------------------


def test_adopt_commits_the_spec_and_queues_it(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    pub = FakePublisher()
    res = _adopt(tmp_path, spec, publisher=pub)
    h = study_hash(load_study(spec))
    assert (res.id, res.status, res.created, res.spec_hash) == ("BT-12", "queued", True, h)
    assert res.pr_url == "https://github.com/x/y/pull/9" and res.auto_merge
    assert res.queue_position == 1
    (call,) = pub.calls
    assert call["branch"] == "bt/12"
    ((path, text),) = call["files"].items()
    assert path == "config/backtest_studies/BT-12.yaml"
    assert "owner's approval slack:1791669727.601139" in text
    # the committed text re-parses to the locked hash
    assert study_hash(StudySpec.model_validate(yaml.safe_load(text))) == h
    assert call["body"].startswith("XP-advisory: none")
    q = load_queue(tmp_path / "queue.json")
    (e,) = q.entries
    assert (e.id, e.status, e.ladder_state, e.spec_hash) == ("BT-12", "queued", "c125_dev", h)
    assert e.approval == APPROVAL and e.pr_url == res.pr_url
    assert set(res.as_json()) >= {"id", "pr_url", "status", "queue_position", "eta_nights"}


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"profiles": ["cash"]}, "does not validate as a StudySpec"),
        ({"arms": {"control": {}, "treatments": {"t1": {"overlay": {"exits": {"nope": 1}}}}}},
         "does not validate as a StudySpec"),
        ({"color": "red"}, "does not validate as a StudySpec"),
    ],
)  # fmt: skip
def test_adopt_refuses_a_bad_spec(tmp_path: Path, over: dict[str, Any], needle: str) -> None:
    spec = _write_draft(tmp_path, **over)
    with pytest.raises(AdoptStudyError, match=needle):
        _adopt(tmp_path, spec)
    assert not (tmp_path / "queue.json").exists()


@pytest.mark.parametrize("approval", ["", "owner", "XP-3", "slack:abc", "cli:1791669727.6"])
def test_adopt_needs_the_owner_slack_approval(tmp_path: Path, approval: str) -> None:
    pub = FakePublisher()
    with pytest.raises(AdoptStudyError, match="slack:<ts>"):
        _adopt(tmp_path, _write_draft(tmp_path), approval=approval, publisher=pub)
    assert pub.calls == [] and not (tmp_path / "queue.json").exists()


def test_adopt_refuses_a_taken_id(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    other = yaml.safe_dump(_draft_doc(hypothesis="something else entirely"))
    # committed locally under another design
    (tmp_path / "studies").mkdir()
    (tmp_path / "studies" / "BT-12.yaml").write_text(other)
    with pytest.raises(AdoptStudyError, match=r"BT-12 is taken: .*studies/BT-12.yaml holds"):
        _adopt(tmp_path, spec)
    (tmp_path / "studies" / "BT-12.yaml").unlink()
    # ... on origin/main
    main = FakeMain({"config/backtest_studies/BT-12.yaml": other})
    with pytest.raises(AdoptStudyError, match="origin/main:config/backtest_studies/BT-12.yaml"):
        _adopt(tmp_path, spec, main=main)
    # ... in the research ledger
    row = _ledger_row("dev", "c125", 10.0, NOW).model_copy(update={"study": "BT-12"})
    (tmp_path / "ledger.jsonl").write_text(row.model_dump_json() + "\n")
    with pytest.raises(AdoptStudyError, match="research ledger already has 1 run"):
        _adopt(tmp_path, spec)
    assert not (tmp_path / "queue.json").exists()
    # the same design already on main (a retry after the PR merged) is not "taken"
    (tmp_path / "ledger.jsonl").unlink()
    same = FakeMain({"config/backtest_studies/BT-12.yaml": spec.read_text()})
    assert _adopt(tmp_path, spec, main=same).status == "queued"


def test_adopt_refuses_a_hash_mismatch(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    with pytest.raises(AdoptStudyError, match="hash mismatch: the draft is"):
        _adopt(tmp_path, spec, expect_hash="f" * 64)
    _adopt(tmp_path, spec)
    # the draft edited after it was queued: refused, the queue keeps the approved hash
    spec2 = _write_draft(tmp_path, hypothesis="a changed design")
    with pytest.raises(AdoptStudyError, match="hash mismatch: queued with spec"):
        _adopt(tmp_path, spec2)
    (e,) = load_queue(tmp_path / "queue.json").entries
    assert e.spec_hash != study_hash(load_study(spec2))


def test_adopt_refuses_a_misnamed_or_missing_draft(tmp_path: Path) -> None:
    with pytest.raises(AdoptStudyError, match="no draft spec"):
        adopt_study("BT-12", tmp_path / "BT-12.yaml", approval=APPROVAL, now=NOW,
                    queue_path=tmp_path / "q.json", ledger=tmp_path / "l.jsonl")  # fmt: skip
    spec = _write_draft(tmp_path)
    with pytest.raises(AdoptStudyError, match="must be BT-13.yaml"):
        adopt_study("BT-13", spec, approval=APPROVAL, now=NOW,
                    queue_path=tmp_path / "q.json", ledger=tmp_path / "l.jsonl")  # fmt: skip


def test_adopt_is_idempotent_and_retries_a_failed_pr(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    with pytest.raises(Exception, match="network"):
        _adopt(tmp_path, spec, publisher=FakePublisher(fail=True))
    assert not (tmp_path / "queue.json").exists()  # nothing queued without the PR step
    first = _adopt(tmp_path, spec)
    again = _adopt(tmp_path, spec, now=NOW + dt.timedelta(hours=2))
    assert first.created and not again.created
    (e,) = load_queue(tmp_path / "queue.json").entries
    assert e.approved_at == NOW  # the first approval's time is kept
    # a queue written without the PR (dry-run-pr) picks the PR URL up on the retry
    q2 = tmp_path / "q2.json"
    res = _adopt(tmp_path, spec, queue_path=q2, publisher=None, dry_run_pr=True)
    assert res.pr_status == "dry-run" and load_queue(q2).entries[0].pr_url is None
    _adopt(tmp_path, spec, queue_path=q2)
    assert load_queue(q2).entries[0].pr_url == "https://github.com/x/y/pull/9"


def test_adopt_dry_run_writes_nothing(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    pub = FakePublisher()
    res = _adopt(tmp_path, spec, publisher=pub, dry_run=True)
    assert res.status == "dry-run" and res.pr_status == "dry-run" and not res.created
    assert pub.calls == [] and not (tmp_path / "queue.json").exists()
    assert "config/backtest_studies/BT-12.yaml" in res.path


# ---------------------------------------------------------------------------
# the queue: atomic, locked, stop, ETA
# ---------------------------------------------------------------------------


def test_queue_model_is_strict() -> None:
    with pytest.raises(ValueError, match="use BT-<n>"):
        _entry(id="XP-3")
    with pytest.raises(ValueError, match="slack:<ts>"):
        _entry(approval="cli")
    with pytest.raises(ValueError, match="Extra inputs"):
        QueueEntry.model_validate({**_entry().model_dump(), "colour": 1})
    with pytest.raises(ValueError):
        _entry(status="paused")


def test_save_is_atomic_a_failed_write_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.json"
    save_queue(path, StudyQueue(entries=[_entry()]))
    before = path.read_text()

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("arc.backtest.queue.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_queue(path, StudyQueue())
    assert path.read_text() == before
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")] == []


def test_update_leaves_the_file_unchanged_when_the_change_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    update_queue(path, lambda q: enqueue(q, _entry()))
    before = path.read_text()
    with pytest.raises(QueueError, match="a changed design needs a new BT id"):
        update_queue(path, lambda q: enqueue(q, _entry(spec_hash="b" * 64)))
    assert path.read_text() == before


def _append_many(path: str, start: int) -> None:
    for i in range(start, start + 10):
        update_queue(
            Path(path), lambda q, i=i: enqueue(q, _entry(id=f"BT-{i}", spec_hash=f"{i:064d}"))
        )


@pytest.mark.serial
def test_concurrent_writers_never_lose_an_entry(tmp_path: Path) -> None:
    path = tmp_path / "queue.json"
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(path), s)) for s in (1, 11, 21, 31)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(60)
        assert pr.exitcode == 0
    ids = sorted(int(e.id.split("-")[1]) for e in load_queue(path).entries)
    assert ids == list(range(1, 41))


def test_stop_marks_stopped_and_is_refused_twice(tmp_path: Path) -> None:
    q = StudyQueue(entries=[_entry()])
    with pytest.raises(QueueError, match="needs a reason"):
        stop_entry(q, "BT-12", " ")
    with pytest.raises(QueueError, match="not in the study queue"):
        stop_entry(q, "BT-99", "x")
    e = stop_entry(q, "BT-12", "superseded by BT-14")
    assert (e.status, e.stop_reason) == ("stopped", "owner: superseded by BT-14")
    with pytest.raises(QueueError, match="already stopped"):
        stop_entry(q, "BT-12", "again")


def test_eta_counts_remaining_rungs_behind_the_entries_ahead() -> None:
    t0 = NOW - dt.timedelta(days=3)
    rows = [
        *(_ledger_row("dev", "c125", 3600.0 * h, t0 + dt.timedelta(hours=i))
          for i, h in enumerate((9, 1, 2, 3, 4, 5))),  # only the last 5 count: median 3 h
        _ledger_row("dev", "c500", 5 * 3600.0, t0),
        _ledger_row("holdout", "c500", 2.5 * 3600.0, t0),
    ]  # fmt: skip
    est = rung_estimates(rows)
    assert est["c125_dev"] == {"seconds": round(3 * 3600 * 1.2), "rows": 5}
    assert est["c500_dev"]["seconds"] == 6 * 3600
    assert est["holdout"]["seconds"] == 3 * 3600
    q = StudyQueue(
        entries=[
            _entry(id="BT-5", approved_at=NOW, ladder_state="holdout", status="running",
                   runs=[QueueRun(rung="c500_dev", at=NOW.replace(hour=22))]),
            _entry(id="BT-6", approved_at=NOW + dt.timedelta(hours=1)),
            _entry(id="BT-4", approved_at=NOW - dt.timedelta(days=9), status="stopped",
                   ladder_state=None, stop_reason="owner: x"),
        ]
    )  # fmt: skip
    by = {r["id"]: r for r in eta_rows(q, rows)}
    assert by["BT-5"]["queue_position"] == 1 and by["BT-5"]["eta_nights"] == 1  # 3 h
    assert by["BT-5"]["nights"] == 1 and by["BT-5"]["remaining_rungs"] == ["holdout"]
    # BT-6: 3 h (BT-5) + 3.6 + 6 + 3 h = 15.6 h -> 2 nights of 11 h
    assert by["BT-6"]["compute_s"] == round(3.6 * 3600 + 6 * 3600 + 3 * 3600)
    assert by["BT-6"]["eta_nights"] == 2 and NIGHT_WINDOW_S == 11 * 3600
    assert by["BT-4"]["queue_position"] is None and by["BT-4"]["eta_nights"] is None
    # no measured runtime on a rung: ETA unknown, not a guess
    assert {r["id"]: r["eta_nights"] for r in eta_rows(q, rows[:6])}["BT-6"] is None


def test_runtime_falls_back_to_the_report_mtime(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text("{}")
    at = dt.datetime.fromtimestamp((run / "report.json").stat().st_mtime, tz=ET)
    row = _ledger_row("dev", "c125", 0.0, at - dt.timedelta(seconds=600))
    row = row.model_copy(update={"runtime_s": None, "run_dir": str(run)})
    assert round(rung_estimates([row])["c125_dev"]["seconds"]) == 720


def test_a_night_spans_midnight() -> None:
    assert night_of(dt.datetime(2026, 10, 16, 21, 30, tzinfo=ET)) == night_of(
        dt.datetime(2026, 10, 17, 2, 0, tzinfo=ET)
    )
    assert night_of(dt.datetime(2026, 10, 17, 21, 30, tzinfo=ET)) == dt.date(2026, 10, 17)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ARC_ENV": "paper"}
    return subprocess.run(
        [sys.executable, "-m", "arc.cli", "backtest", "study", *args],
        cwd=REPO, capture_output=True, text=True, env=env, check=False, timeout=120,
    )  # fmt: skip


def test_cli_adopt_queue_stop(tmp_path: Path) -> None:
    spec = _write_draft(tmp_path)
    q = str(tmp_path / "queue.json")
    common = ["--queue", q, "--ledger", str(tmp_path / "ledger.jsonl"),
              "--studies-dir", str(tmp_path / "studies")]  # fmt: skip
    r = _arc("adopt", "BT-12", "--spec", str(spec), "--approval", "slack:1", *common, "--json")
    assert r.returncode == 3 and "slack:<ts>" in r.stderr
    r = _arc("adopt", "BT-12", "--spec", str(spec), "--approval", APPROVAL, *common,
             "--dry-run-pr", "--no-fetch", "--now", NOW.isoformat(), "--json")  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["status"] == "queued" and out["queue_position"] == 1 and out["pr_url"] is None
    r = _arc("queue", "--queue", q, "--ledger", str(tmp_path / "ledger.jsonl"), "--json")
    view = json.loads(r.stdout)
    assert [e["id"] for e in view["entries"]] == ["BT-12"]
    assert set(view["rung_estimates"]) == {"c125_dev", "c500_dev", "holdout"}
    r = _arc("stop", "BT-12", "--reason", "owner changed his mind", "--queue", q, "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "stopped"
    r = _arc("stop", "BT-12", "--reason", "again", "--queue", q)
    assert r.returncode == 3 and "already stopped" in r.stderr
    r = _arc("queue", "--queue", q, "--ledger", str(tmp_path / "ledger.jsonl"))
    assert "BT-12 [stopped]" in r.stdout and "owner: owner changed his mind" in r.stdout
