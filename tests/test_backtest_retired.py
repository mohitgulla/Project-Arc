"""E26.5 (D127): retired backtest studies.

``config/backtest_studies/retired.yaml`` lists studies that are history, not evidence. Pinned
here: the strict loader (malformed / duplicate ids refused), the committed file (BT-1..BT-8,
the stdlib Analyst reader agrees with it), the ``BT-*.yaml`` globs ignore it and no study hash
changes, ``show`` marks it, ``run`` / ``adopt`` / the nightly runner refuse it, ids are never
reused, the Tower lists it under ``retired`` and the Analyst gate keeps it out of evidence,
the context and the verdict wake.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from arc.backtest.queue import load_queue
from arc.backtest.study import (
    RETIRED_FILE,
    STUDIES_DIR,
    RetiredStudy,
    load_retired,
    load_study,
    retired_from_text,
    retired_text,
    study_hash,
)
from arc.backtest.study_adopt import AdoptStudyError, check_not_retired
from arc.config import ArcSettings
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from tests import test_analyst_gate as gate
from tests import test_backtest_queue as bq
from tests import test_backtest_runner as br

aa = gate.aa
REPO = Path(__file__).resolve().parent.parent
REASON = (
    "strategy/backtest harness evolved since these were specced; results no longer relevant "
    "(owner 2026-10-17)"
)
DAY = dt.date(2026, 10, 17)


def _row(sid: str, reason: str = "old harness") -> RetiredStudy:
    return RetiredStudy(id=sid, at=DAY, by="owner", reason=reason)


def _write(d: Path, *ids: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / RETIRED_FILE).write_text(retired_text([_row(i) for i in ids]))
    return d


# ---------------------------------------------------------------------------
# the loader and the committed file
# ---------------------------------------------------------------------------


def test_committed_file_retires_bt1_to_bt8() -> None:
    rows = load_retired()
    assert sorted(rows, key=lambda s: int(s[3:])) == [f"BT-{n}" for n in range(1, 9)]
    assert {r.reason for r in rows.values()} == {REASON}
    assert {(r.at, r.by) for r in rows.values()} == {(DAY, "owner")}
    # the stdlib Analyst reader sees exactly the same rows
    seen, errs = aa.load_retired_studies(STUDIES_DIR / RETIRED_FILE)
    assert errs == [] and set(seen) == set(rows)
    assert all(seen[k]["reason"] == rows[k].reason for k in rows)


def test_committed_file_round_trips_through_the_writer() -> None:
    text = (STUDIES_DIR / RETIRED_FILE).read_text()
    assert retired_text(list(load_retired().values())) == text


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('{"retired": [{"id": "BT-0", "at": "2026-10-17", "by": "o", "reason": "r"}]}', "BT-<n>"),
        ('{"retired": [{"id": "XP-3", "at": "2026-10-17", "by": "o", "reason": "r"}]}', "BT-<n>"),
        ('{"retired": [{"id": "bt-3", "at": "2026-10-17", "by": "o", "reason": "r"}]}', "BT-<n>"),
        ('{"retired": [{"id": "BT-3", "at": "2026-10-17", "by": "o", "reason": "r"},'
         ' {"id": "BT-3", "at": "2026-10-17", "by": "o", "reason": "s"}]}', "duplicate"),
        ('{"retired": [{"id": "BT-3", "at": "2026-10-17", "by": "o", "reason": ""}]}', "reason"),
        ('{"retired": [{"id": "BT-3", "at": "2026-10-17", "by": "o", "reason": "r", "x": 1}]}',
         "x"),
        ('{"retired": [{"id": "BT-3", "at": "someday", "by": "o", "reason": "r"}]}', "at"),
        ("retired: [", "retired.yaml"),
    ],
)  # fmt: skip
def test_loader_rejects_malformed_and_duplicate_ids(body: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        retired_from_text(body)


def test_loader_missing_or_empty_is_nothing_retired(tmp_path: Path) -> None:
    assert load_retired(tmp_path) == {}
    assert retired_from_text("# only a comment\n") == {}
    (tmp_path / RETIRED_FILE).write_text("{bad")
    with pytest.raises(ValueError, match=str(tmp_path)):
        load_retired(tmp_path)


def test_stdlib_reader_names_bad_rows(tmp_path: Path) -> None:
    p = tmp_path / RETIRED_FILE
    p.write_text('# c\n{"retired": [{"id": "BT-2"}, {"id": "BT-2"}, {"id": "nope"}]}')
    rows, errs = aa.load_retired_studies(p)
    assert set(rows) == {"BT-2"} and len(errs) == 2
    p.write_text("{bad")
    assert aa.load_retired_studies(p)[1][0].startswith("retired.yaml is not readable JSON")
    assert aa.load_retired_studies(tmp_path / "missing.yaml") == ({}, [])


def test_spec_globs_ignore_retired_yaml_and_hashes_are_unchanged() -> None:
    specs = sorted(STUDIES_DIR.glob("BT-*.yaml"))
    assert (STUDIES_DIR / RETIRED_FILE).is_file()
    assert all(p.name != RETIRED_FILE for p in specs)
    # every spec still loads and hashes on its own file only (the lock ignores the dir)
    for p in specs:
        s = load_study(p)
        assert study_hash(s) == study_hash(load_study(p))
    # the tower's tolerant reader sees no study named "retired"
    from arc.tower.data_backtests import BacktestPaths, _specs

    assert all(k.startswith("BT-") for k in _specs(BacktestPaths(root=REPO)))


# ---------------------------------------------------------------------------
# CLI: show / run / retire
# ---------------------------------------------------------------------------


def _spec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "studies"
    d.mkdir()
    (d / "BT-7.yaml").write_text(yaml.safe_dump(bq._draft_doc("BT-7")))
    return d


def test_cli_show_marks_retired_and_run_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    d = _spec_dir(tmp_path)
    base = ["--studies-dir", str(d), "--ledger", str(tmp_path / "ledger.jsonl")]
    assert main(["backtest", "study", "show", "BT-7", *base, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["retired"] is None
    _write(d, "BT-7")
    assert main(["backtest", "study", "show", "BT-7", *base, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["retired"] == {"at": "2026-10-17", "by": "owner", "reason": "old harness"}
    assert doc["spec_hash"]  # the hash is still readable (history)
    assert main(["backtest", "study", "show", "BT-7", *base]) == 0
    assert "RETIRED: BT-7 is retired (D127, 2026-10-17 by owner): old harness" in (
        capsys.readouterr().out
    )
    rc = main(["backtest", "study", "run", "BT-7", *base, "--data-dir", str(tmp_path)])
    assert rc == 2
    assert "refused: BT-7 is retired (D127" in capsys.readouterr().err
    assert not (tmp_path / "ledger.jsonl").exists()


def test_cli_show_and_run_refuse_a_malformed_retired_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    d = _spec_dir(tmp_path)
    (d / RETIRED_FILE).write_text('{"retired": [{"id": "BT-0"}]}')
    assert main(["backtest", "study", "run", "BT-7", "--studies-dir", str(d)]) == 2
    assert "retired.yaml" in capsys.readouterr().err


def test_cli_retire_appends_and_refuses_repeats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    d = _write(tmp_path / "s", "BT-1")
    args = ["backtest", "study", "retire", "BT-9", "--reason", "superseded", "--studies-dir",
            str(d), "--at", "2026-10-18"]  # fmt: skip
    assert main([*args, "--dry-run"]) == 0
    assert '"BT-9"' in capsys.readouterr().out and set(load_retired(d)) == {"BT-1"}
    assert main(args) == 0
    assert "BT-9 is retired (D127, 2026-10-18 by owner): superseded" in capsys.readouterr().out
    assert set(load_retired(d)) == {"BT-1", "BT-9"}
    assert main(args) == 3
    assert "already retired" in capsys.readouterr().err
    assert main([*args[:3], "BT-x", *args[4:]]) == 3
    assert "use BT-<n>" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


def test_adopt_refuses_a_retired_id_and_ids_at_or_below_the_top(tmp_path: Path) -> None:
    _write(tmp_path / "studies", "BT-1", "BT-8")
    for sid in ("BT-8", "BT-1"):
        spec = bq._write_draft(tmp_path, sid)
        with pytest.raises(AdoptStudyError, match=rf"{sid} is retired \(D127"):
            bq._adopt(tmp_path, spec)
    spec = bq._write_draft(tmp_path, "BT-5")
    with pytest.raises(AdoptStudyError, match="at or below the highest retired id BT-8"):
        bq._adopt(tmp_path, spec)
    assert not (tmp_path / "queue.json").exists()
    assert bq._adopt(tmp_path, bq._write_draft(tmp_path, "BT-9")).status == "queued"


def test_adopt_reads_retired_from_origin_main(tmp_path: Path) -> None:
    text = retired_text([_row("BT-12")])
    main = bq.FakeMain({"config/backtest_studies/retired.yaml": text})
    with pytest.raises(AdoptStudyError, match=r"BT-12 is retired \(D127"):
        bq._adopt(tmp_path, bq._write_draft(tmp_path), main=main)
    bad = bq.FakeMain({"config/backtest_studies/retired.yaml": "{bad"})
    with pytest.raises(AdoptStudyError, match="cannot read the retired-study list"):
        bq._adopt(tmp_path, bq._write_draft(tmp_path), main=bad)


def test_a_queued_low_id_retries_but_a_retired_one_never() -> None:
    retired = {"BT-8": _row("BT-8")}
    check_not_retired("BT-3", retired, queued=True)  # adopted before the retirement
    with pytest.raises(AdoptStudyError, match="never reused"):
        check_not_retired("BT-3", retired, queued=False)
    with pytest.raises(AdoptStudyError, match="is retired"):
        check_not_retired("BT-8", retired, queued=True)
    check_not_retired("BT-9", retired, queued=False)
    check_not_retired("BT-1", {}, queued=False)


# ---------------------------------------------------------------------------
# nightly runner
# ---------------------------------------------------------------------------


def test_runner_skips_and_logs_a_retired_queue_entry(tmp_path: Path) -> None:
    world = br.World(tmp_path, br.Clock(br.TUE_NIGHT))
    world.add("BT-1")
    world.add("BT-9")

    class _Main(br.Main):
        def files(self, directory: str) -> dict[str, str]:
            return {**super().files(directory), f"{directory}/{RETIRED_FILE}": retired_text(
                [_row("BT-1")])}  # fmt: skip

    world.main = _Main(texts=world.main.texts)
    res = world.night()
    assert "BT-1 skipped: retired" in res.summary
    assert res.metrics["retired_skipped"] == ["BT-1"]
    assert world.entry("BT-1").status == "queued" and world.entry("BT-1").runs == []
    assert world.entry("BT-9").status == "concluded"
    assert all("BT-1" not in " ".join(a) for a in world.launches)
    assert not any("BT-1" in p for p in world.posts)


def test_runner_refuses_the_night_on_a_malformed_retired_list(tmp_path: Path) -> None:
    world = br.World(tmp_path, br.Clock(br.TUE_NIGHT))
    world.add("BT-9")

    class _Main(br.Main):
        def files(self, directory: str) -> dict[str, str]:
            return {**super().files(directory), f"{directory}/{RETIRED_FILE}": "{bad"}

    world.main = _Main(texts=world.main.texts)
    with pytest.raises(br.SkipNightError, match="cannot read the retired-study list"):
        world.night()
    assert world.launches == [] and load_queue(world.paths.queue).get("BT-9") is not None


# ---------------------------------------------------------------------------
# Tower
# ---------------------------------------------------------------------------


def _tower(tmp_path: Path, retired: str | None) -> TestClient:
    root = tmp_path / "root"
    specs = root / "config" / "backtest_studies"
    specs.mkdir(parents=True)
    (specs / "BT-1.yaml").write_text("id: BT-1\nhypothesis: D4 old one\nprofiles: [margin]\n")
    (specs / "BT-9.yaml").write_text("id: BT-9\nhypothesis: fresh one\nprofiles: [margin]\n")
    if retired is not None:
        (specs / RETIRED_FILE).write_text(retired)
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    c.close()
    now = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.UTC)
    return TestClient(create_app(db, ArcSettings(), backtest_root=root, clock=lambda: now))


def test_tower_marks_retired_and_lists_them_last(tmp_path: Path) -> None:
    c = _tower(tmp_path, retired_text([_row("BT-1", "E7.2 era harness (D4)")]))
    items = c.get("/api/backtests").json()["items"]
    assert [i["study_id"] for i in items] == ["BT-9", "BT-1"]
    assert items[0]["retired"] is None
    assert items[1]["retired"] == {"at": "2026-10-17", "reason": "Era harness"}
    d = c.get("/api/backtests/BT-1").json()
    assert d["study"]["retired"]["reason"] == "Era harness"
    assert c.get("/api/backtests/BT-9").json()["study"]["retired"] is None


@pytest.mark.parametrize(
    "body",
    [None, "{bad", '{"retired": "x"}', '{"retired": [{"id": "BT-1"}, {"id": "nope", "at": '
                                         '"2026-10-17", "reason": "r"}]}'],
)  # fmt: skip
def test_tower_tolerates_a_missing_or_bad_retired_file(tmp_path: Path, body: str | None) -> None:
    items = _tower(tmp_path, body).get("/api/backtests").json()["items"]
    assert all(i["retired"] is None for i in items)


# ---------------------------------------------------------------------------
# Analyst gate
# ---------------------------------------------------------------------------


def _bt3_retired() -> list[dict[str, Any]]:
    studies = json.loads(json.dumps(gate.BT3))
    studies[0]["retired"] = {"at": "2026-10-17", "by": "owner", "reason": REASON}
    return studies


def test_bt_ref_to_a_retired_study_fails() -> None:
    studies = _bt3_retired()
    errs = aa.check_bt_evidence("BT-3", None, studies)
    assert errs == [f"BT-REF: BT-3 is retired (D127): {REASON}; cite a current study or 'none'"]
    assert aa.check_bt_evidence("BT-3", None, gate.BT3) == []  # the same study, not retired
    errs = gate._errs([gate._prop(n=2)], studies=studies)
    assert any("BT-REF: BT-3 is retired (D127)" in e for e in errs), errs
    live = gate._prop(n=25, evidence_kind="live", evidence_ref=None)
    live["proposal"]["spec"]["backtest_ref"] = "BT-3"
    errs = gate._errs([live], studies=studies)
    assert any("BT-REF: BT-3 is retired (D127)" in e for e in errs), errs


def test_next_text_may_name_a_retired_study_only_as_retired() -> None:
    studies = _bt3_retired()
    errs = gate._errs([], studies=studies, next_experiment="none: BT-3 holdout passed")
    assert any("next_experiment cites BT-3: it is retired" in e for e in errs), errs
    ok = "none: BT-3 is retired; no current study has a C500 holdout"
    assert gate._errs([], studies=studies, next_experiment=ok) == []
    assert gate._errs([], next_experiment="none: BT-3 holdout passed") == []  # not retired


def test_context_lists_retired_on_one_line_only() -> None:
    studies = [*_bt3_retired(), {"id": "BT-9", "retired": None}]
    assert aa.current_studies(studies) == [{"id": "BT-9", "retired": None}]
    (line,) = aa.retired_lines(studies)
    assert line.startswith("- RETIRED (D127, history only: never cite, re-run, extend or build")
    assert line.endswith(f"BT-3: {REASON} (2026-10-17)")
    assert aa.retired_lines(gate.BT3) == []
    assert aa.retired_refs("BT-3 and BT-30, XBT-3", studies) == ["BT-3"]


def _gate_root(tmp_path: Path) -> Path:
    h = "a" * 64
    root = gate._bt_root(tmp_path, gate._bt_line(spec_hash=h), gate._bt_line(
        sid="BT-9", spec_hash=h, at="2026-10-05T18:00:00-04:00"))  # fmt: skip
    _write(root / "config" / "backtest_studies", "BT-2", "BT-3", "BT-8")
    return root


def test_snapshot_keeps_ids_taken_but_retired_never_wakes(tmp_path: Path) -> None:
    root = _gate_root(tmp_path)
    p = aa.Paths(home=tmp_path / "h", repo=REPO, live_db=tmp_path / "db", arc=gate.ARC_ARGV,
                 backtest_root=root)  # fmt: skip
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bt = aa.backtest_snapshot(p, run_dir)
    by = {s["id"]: s for s in bt["studies"]}
    assert set(by) == {"BT-2", "BT-3", "BT-8", "BT-9"}
    assert {k for k, s in by.items() if s["retired"]} == {"BT-2", "BT-3", "BT-8"}
    assert bt["next_study_id"] == "BT-10"
    assert [h.split(":")[0] for h in bt["holdouts"]] == ["BT-9"]  # BT-3's holdout: retired
    assert not (run_dir / "study-show-BT-3.log").exists()  # no hash check on history
    assert (
        aa.live_holdouts(aa.load_bt_ledger(root / aa.BT_LEDGER)[0], aa.retired_ids(p))
        == (bt["holdouts"])
    )
    # a retired-only store still reserves above every retired id
    assert aa.study_floor(bt["studies"], bt["next_study_id"]) == 10


def test_verdict_context_carries_the_retired_line(tmp_path: Path) -> None:
    root = _gate_root(tmp_path)
    p = aa.Paths(home=tmp_path / "h", repo=REPO, live_db=tmp_path / "db", arc=gate.ARC_ARGV,
                 backtest_root=root)  # fmt: skip
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bt = aa.backtest_snapshot(p, run_dir)
    decision = {"new_holdouts": bt["holdouts"], "new_xp_verdicts": [], "next_weekly": "Sun"}
    text = aa.build_verdict_context(p, run_dir, decision, bt, aa.empty_backlog(), gate.NOW)
    assert "VERDICTS: BT-9" in text and "RETIRED (D127" in text
    assert "BT-2, BT-3, BT-8:" in text and "next free study id BT-10" in text


def test_prompts_and_skill_carry_the_rule() -> None:
    for name in ("prompt.md", "prompt_verdict.md"):
        text = (gate.ANALYST / name).read_text()
        assert "Retired studies (D127" in text and "never cite, re-run, extend or build on" in text
    skill = gate._skill()
    assert "**Retired studies (D127, `config/backtest_studies/retired.yaml`).**" in skill
