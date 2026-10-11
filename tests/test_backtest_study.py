"""E7.12 (D84): the backtest study framework (arc.backtest.study)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.backtest.ranking import DecisionRule, challenge
from arc.backtest.study import (
    LedgerEntry,
    StudyPeriod,
    StudyRefusedError,
    StudySpec,
    _as_rank_run,
    arm_set_hash,
    check_holdout,
    compare_runs,
    ladder_flags,
    load_ledger,
    load_study,
    run_study,
    study_hash,
    variants,
)
from arc.config import ArcSettings
from arc.utils.calendar import ET
from tests.backtest_data_fixtures import (
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

REPO = Path(__file__).resolve().parent.parent
N = 60
DAYS = sessions(dt.date(2025, 3, 3), N)
DEV_END = DAYS[5]
HOLD_START = DAYS[6]
HOLD_END = DAYS[11]
LATEST = DAYS[-1]
NOW = dt.datetime(2026, 10, 10, 18, 0, tzinfo=ET)

SECTIONS = (
    "## Header",
    "## Verdict",
    "## Per arm × profile",
    "## Exit-reason mix",
    "## Skip reasons",
    "## Sub-periods",
    "## By regime",
    "## By universe tier",
    "## By sector",
    "## ETF vs single stock",
    "## By liquidity bucket",
    "## Uncertainty",
    "## Checkpoint ladder",
)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("study")
    for i, t in enumerate(("AAA", "ZZZ")):
        spots = spot_path(N, seed=3 + i)
        write_history(root, t, synth_rows(t, DAYS, spots, oi=400.0))
        write_underlying(root, t, DAYS, spots)
    q = dt.date(2025, 1, 1)
    write_universe(root, ["AAA", "ZZZ"], [(q, "AAA", True), (q, "ZZZ", True)])
    return root


@pytest.fixture(scope="module")
def ranking_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raw = yaml.safe_load((REPO / "config" / "ranking.yaml").read_text())
    raw["backtest"]["n_paths"] = 200
    raw["backtest"]["stance"] = "none"
    raw["backtest"]["data"]["pit"] = False
    raw["backtest"]["sim"]["apply_screen"] = False  # two synthetic names, neither seed
    raw["ranking"]["filters"]["enabled"] = False  # flat-vol synthetic chains carry no edge
    p = tmp_path_factory.mktemp("cfg") / "ranking.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


def _spec_data(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "BT-9",
        "hypothesis": "managed_net_ev beats debit_width on cash_debit",
        "owner": "owner",
        "created": "2026-10-10",
        "arms": {
            "control": {"ranker": "debit_width"},
            "treatments": {"t1": {"ranker": "managed_net_ev"}},
        },
        "profiles": ["cash_debit"],
        "universe_ladder": ["c125", "c500"],
        "period": {
            "dev_start": DAYS[0].isoformat(),
            "dev_end": DEV_END.isoformat(),
            "holdout_start": HOLD_START.isoformat(),
            "holdout_end": HOLD_END.isoformat(),
            "settle_days": 120,
        },
    }
    d |= kw
    return d


def _spec(**kw: Any) -> StudySpec:
    return StudySpec.model_validate(_spec_data(**kw))


def _run(
    spec: StudySpec,
    data_dir: Path,
    ranking_path: Path,
    tmp: Path,
    *,
    phase: str = "dev",
    universe: str | None = None,
    now: dt.datetime = NOW,
    **kw: Any,
) -> LedgerEntry:
    return run_study(
        spec,
        phase=phase,  # type: ignore[arg-type]
        universe=universe,
        now=now,
        latest=LATEST,
        data_dir=data_dir,
        ledger=tmp / "ledger.jsonl",
        runs_dir=tmp / "runs",
        report_dir=tmp / "reports",
        settings=ArcSettings(),
        paths={"ranking": ranking_path},
        cache=False,
        git_sha="abc123",
        **kw,
    )


def _entry(spec: StudySpec, **kw: Any) -> LedgerEntry:
    base: dict[str, Any] = {
        "at": NOW,
        "study": spec.id,
        "spec_hash": study_hash(spec),
        "arm_set_hash": arm_set_hash(spec),
        "arms": spec.arms.model_dump(mode="json"),
        "profiles": list(spec.profiles),
        "phase": "dev",
        "universe": "c125",
        "universe_version": "u-test",
        "partitions": {"thetadata": 2},
        "start": DAYS[0],
        "end": DEV_END,
        "data_end": LATEST,
        "git_sha": "abc123",
        "primary_metric": "net_pnl",
        "metrics": {},
        "verdicts": {},
        "variants": 1,
        "run_dir": "x",
    }
    return LedgerEntry.model_validate(base | kw)


def _variant(i: int) -> StudySpec:
    """The same study with a different arm set (a distinct treatment overlay)."""
    return _spec(
        arms={
            "control": {},
            "treatments": {"t1": {"overlay": {"ranking": {"backtest": {"n_paths": 200 + i}}}}},
        }
    )


# ---------------------------------------------------------------------------
# Spec + hash locking
# ---------------------------------------------------------------------------


def test_spec_validation_rejects_bad_shapes() -> None:
    _spec()  # ok
    bad: list[dict[str, Any]] = [
        {"id": "XP-1"},
        {"id": "BT-0"},
        {"arms": {"treatments": {}}},
        {"arms": {"treatments": {f"t{i}": {} for i in (1, 2, 3, 4)}}},
        {"arms": {"treatments": {"treatment": {}}}},
        {"arms": {"treatments": {"t1": {"overlay": {"routines": {"x": 1}}}}}},
        {"arms": {"treatments": {"t1": {"overlay": {"ranking": {}}}}}},
        {"universe_ladder": ["c500", "c125"]},
        {"universe_ladder": []},
        {"universe_ladder": ["c125", "c125"]},
        {"period": {"dev_end": "2024-06-01"}},
        {"period": {"holdout_start": "2024-01-01", "holdout_end": "2023-01-01"}},
        {"max_variants": 0},
        {"surprise": 1},
    ]
    for b in bad:
        with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError
            _spec(**b)
    s = _spec(arms={"treatments": {"t2": {}, "t1": {}}})
    assert list(s.arms.treatments) == ["t1", "t2"]
    assert s.arms.names == ["control", "t1", "t2"]


def test_load_study_checks_file_name_and_overlays(tmp_path: Path, ranking_path: Path) -> None:
    p = tmp_path / "BT-9.yaml"
    p.write_text(yaml.safe_dump(_spec_data()))
    assert load_study(p, paths={"ranking": ranking_path}).id == "BT-9"
    wrong = tmp_path / "BT-8.yaml"
    wrong.write_text(p.read_text())
    with pytest.raises(ValueError, match="file name"):
        load_study(wrong)
    bad = _spec_data(
        arms={"treatments": {"t1": {"overlay": {"ranking": {"backtest": {"nope": 1}}}}}}
    )
    p.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="t1 overlay does not validate"):
        load_study(p)
    p.write_text("- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_study(p)


def test_committed_bt1_smoke_spec_loads() -> None:
    s = load_study(REPO / "config" / "backtest_studies" / "BT-1.yaml")
    assert s.profiles == ["cash_debit"] and s.universe_ladder == ["c125"]
    assert s.arms.control.ranker == "debit_width"
    assert s.arms.treatments["t1"].ranker == "managed_net_ev"
    # smoke period on the alpaca partition (2024-02 ->); the D84 default split is
    # dev 2020-2023 / holdout 2024 -> latest (StudyPeriod())
    assert s.period.dev_start == dt.date(2024, 3, 1) and s.period.holdout_end is None
    assert s.period.dev_end < s.period.holdout_start
    d = StudyPeriod()
    assert (d.dev_start, d.dev_end) == (dt.date(2020, 1, 1), dt.date(2023, 12, 31))
    assert d.holdout_start == dt.date(2024, 1, 1) and d.holdout_end is None
    assert s.max_variants == 3


def test_edited_spec_gets_a_new_hash_arm_set_only_on_arm_change() -> None:
    a = _spec()
    assert study_hash(a) == study_hash(_spec())  # deterministic
    text = _spec(hypothesis="a reworded hypothesis")
    assert study_hash(text) != study_hash(a)
    assert arm_set_hash(text) == arm_set_hash(a)  # same variant
    arms = _variant(1)
    assert study_hash(arms) != study_hash(a) and arm_set_hash(arms) != arm_set_hash(a)
    profiles = _spec(profiles=["margin"])
    assert arm_set_hash(profiles) != arm_set_hash(a)


# ---------------------------------------------------------------------------
# Protocol on synthetic ledger lines (pure)
# ---------------------------------------------------------------------------


def test_variant_counter_and_overfit_refusal() -> None:
    s = _spec(max_variants=2)
    v = [_variant(i) for i in range(3)]
    entries = [_entry(v[0]), _entry(v[0], universe="c500"), _entry(v[1])]
    assert variants(entries, "BT-9") == 2  # rung/repeat of a set is not a new variant
    assert variants(entries, "BT-9", arm_set_hash(v[0])) == 2
    assert variants(entries, "BT-9", arm_set_hash(s)) == 3
    assert variants(entries, "BT-7") == 0
    # within max -> no warning
    assert check_holdout(v[1], entries, allow_overfit_warning=False) is False
    # the third distinct set exceeds max_variants 2
    with pytest.raises(StudyRefusedError, match="max_variants"):
        check_holdout(s, entries, allow_overfit_warning=False)
    assert check_holdout(s, entries, allow_overfit_warning=True) is True


def test_holdout_is_one_per_spec_hash_and_per_arm_set() -> None:
    s = _spec()
    held = [_entry(s, phase="holdout")]
    with pytest.raises(StudyRefusedError, match="one holdout per spec hash"):
        check_holdout(s, held, allow_overfit_warning=True)
    reworded = _spec(hypothesis="same arms, new words")
    with pytest.raises(StudyRefusedError, match="does not buy a second holdout"):
        check_holdout(reworded, held, allow_overfit_warning=True)
    # another study with an identical arm set is not blocked by BT-9's holdout
    other = _spec(id="BT-10")
    assert check_holdout(other, held, allow_overfit_warning=False) is False


def test_ladder_flags_a_sign_flip_vs_the_first_rung() -> None:
    s = _spec()

    def v(d: float) -> dict[str, Any]:
        return {"cash_debit": {"t1": {"primary_diff": d, "switch": False}}}

    rows = ladder_flags(
        [
            _entry(s, universe="c125", verdicts=v(120.0)),
            _entry(s, universe="c500", verdicts=v(-40.0), at=NOW + dt.timedelta(hours=1)),
            _entry(s, universe="c500", phase="holdout", verdicts=v(10.0)),
        ],
        s,
    )
    assert [(r["universe"], r["phase"], r["sign_flip_vs_first_rung"]) for r in rows] == [
        ("c125", "dev", False),
        ("c500", "dev", True),
        ("c500", "holdout", False),
    ]
    # other arm sets of the same study are not mixed in
    assert ladder_flags([_entry(_variant(1), verdicts=v(-1.0))], s) == []


# ---------------------------------------------------------------------------
# Runs on fixture history
# ---------------------------------------------------------------------------


def test_dev_runs_append_to_the_ledger_and_write_the_report(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    s = _spec()
    e1 = _run(s, data_dir, ranking_path, tmp_path)
    e2 = _run(s, data_dir, ranking_path, tmp_path, universe="c500", now=NOW + dt.timedelta(1))
    led = load_ledger(tmp_path / "ledger.jsonl")
    assert [x.universe for x in led] == ["c125", "c500"]  # one line per run
    assert led[0] == e1 and led[1] == e2
    assert e1.phase == "dev" and (e1.start, e1.end) == (DAYS[0], DEV_END)
    assert e1.spec_hash == study_hash(s) and e1.git_sha == "abc123"
    assert e1.variants == 1 and e2.variants == 1  # same arm set on two rungs
    assert e1.partitions == {"thetadata": 2} and e1.universe_version == "u-test"
    assert set(e1.metrics["cash_debit"]) == {"control", "t1"}
    m = e1.metrics["cash_debit"]["control"]
    for k in ("net_pnl", "return_pct", "max_dd_pct", "sharpe", "profit_factor", "skips"):
        assert k in m
    assert m["trades"] > 0
    assert {"switch", "ci_lo", "ci_hi", "subperiods_won", "primary_diff"} <= set(
        e1.verdicts["cash_debit"]["t1"]
    )
    # aggregates only: the ledger line carries no per-trade rows or option symbols
    raw = (tmp_path / "ledger.jsonl").read_text()
    assert "AAA2" not in raw and "legs" not in raw
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    for sec in SECTIONS:
        assert sec in md, sec
    assert "dev result (not a verdict)" in md
    assert "Discovery and Trending" in md and "LLM stance" in md and "intraday" in md
    assert "variants tried 1 of max 3" in md and "git `abc123`" in md
    payload = json.loads((tmp_path / "reports" / "BT-9.json").read_text())
    assert payload["header"]["spec_hash"] == study_hash(s)
    assert [r["universe"] for r in payload["ladder"]] == ["c125", "c500"]
    assert {r["period"] for r in payload["subperiods"]["cash_debit"]["control"]} == {
        "2020 crash",
        "2020-21 bull",
        "2022 bear",
        "2023",
        "2024 -> latest",
    }
    assert set(payload["breakdowns"]["cash_debit"]["control"]) == {
        "regime",
        "tier",
        "sector",
        "etf",
        "liquidity",
    }
    run_dir = Path(e1.run_dir)
    assert (run_dir / "cash_debit" / "t1" / "trades.csv").is_file()
    assert (run_dir / "report.md").is_file()


def test_holdout_runs_once_per_spec_hash(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    s = _spec(universe_ladder=["c125"])
    h = _run(s, data_dir, ranking_path, tmp_path, phase="holdout")
    assert h.phase == "holdout" and (h.start, h.end) == (HOLD_START, HOLD_END)
    assert h.universe == "c125" and not h.overfit_warning
    with pytest.raises(StudyRefusedError, match="one holdout per spec hash"):
        _run(s, data_dir, ranking_path, tmp_path, phase="holdout")
    # an edited spec has a new hash; the old holdout stays attached to the old hash
    edited = _spec(universe_ladder=["c125"], hypothesis="reworded after seeing the holdout")
    assert study_hash(edited) != h.spec_hash
    with pytest.raises(StudyRefusedError, match="does not buy a second holdout"):
        _run(edited, data_dir, ranking_path, tmp_path, phase="holdout")
    led = load_ledger(tmp_path / "ledger.jsonl")
    assert len(led) == 1 and led[0].spec_hash == study_hash(s)  # refusals write nothing
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    assert "VERDICT · cash_debit · t1 vs control" in md


def test_overfit_holdout_needs_the_flag_and_is_stamped(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    s = _spec(universe_ladder=["c125"], max_variants=1)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_entry(_variant(7)).model_dump_json() + "\n")  # an earlier variant
    with pytest.raises(StudyRefusedError, match="allow-overfit-warning"):
        _run(s, data_dir, ranking_path, tmp_path, phase="holdout")
    e = _run(s, data_dir, ranking_path, tmp_path, phase="holdout", allow_overfit_warning=True)
    assert e.overfit_warning and e.variants == 2
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    assert "OVERFIT WARNING" in md
    assert load_ledger(ledger)[-1].overfit_warning


def test_universe_off_the_ladder_and_bad_ranker_are_refused(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(StudyRefusedError, match="not on the ladder"):
        _run(_spec(), data_dir, ranking_path, tmp_path, universe="c250")
    credit = _spec(arms={"treatments": {"t1": {"ranker": "credit_width"}}})
    with pytest.raises(StudyRefusedError, match="does not apply"):
        _run(credit, data_dir, ranking_path, tmp_path)
    assert not (tmp_path / "ledger.jsonl").exists()


def test_the_d25_rule_is_applied_verbatim(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    """The study verdict is arc.backtest.ranking.challenge on the arms' runs, unchanged,
    and the report quotes DecisionRule.text exactly."""
    import pandas as pd

    from arc.backtest.sim import SimRun

    s = _spec(decision_rule={"min_subperiod_wins": 1, "subperiods": ["bull", "bear"]})
    e = _run(s, data_dir, ranking_path, tmp_path)
    run_dir = Path(e.run_dir) / "cash_debit"

    def load(arm: str) -> SimRun:
        t = pd.read_csv(run_dir / arm / "trades.csv", parse_dates=["entry_date", "exit_date"])
        for c in ("entry_date", "exit_date"):
            t[c] = t[c].dt.date
        eq = pd.read_csv(run_dir / arm / "equity.csv", parse_dates=["day"])
        series = pd.Series(eq["equity"].to_numpy(float), index=eq["day"].dt.date.to_list())
        return SimRun(
            ranker=arm,
            profile="cash_debit",
            trades=t,
            equity=series,
            decisions=pd.DataFrame(),
            skips=pd.DataFrame(),
            skip_counts={},
        )

    ctrl, t1 = load("control"), load("t1")
    rule = DecisionRule(min_subperiod_wins=1, subperiods=["bull", "bear"])
    ref = challenge(_as_rank_run(ctrl), _as_rank_run(t1), rule=rule, boot=s.decision_rule.bootstrap)
    got = e.verdicts["cash_debit"]["t1"]
    assert got["switch"] == ref["switch"] and got["subperiods_won"] == ref["subperiods_won"]
    assert got["won"] == ref["won"]
    for k in ("pnl_diff", "ci_lo", "ci_hi"):
        assert got[k] == pytest.approx(ref[k], abs=0.01)
    again = compare_runs(ctrl, t1, s.decision_rule)
    assert {k: again[k] for k in ref} == pytest.approx({k: got[k] for k in ref}, abs=0.01)
    md = (tmp_path / "reports" / "BT-9.md").read_text()
    assert f"> {rule.text(0.90)}" in md
    assert s.decision_rule.text() == rule.text(s.decision_rule.bootstrap.ci)


def test_overlay_arm_builds_its_own_config(
    data_dir: Path, ranking_path: Path, tmp_path: Path
) -> None:
    """An overlay arm (sim knob) is merged like --experiment and changes that arm only."""
    s = _spec(
        arms={
            "control": {},
            "treatments": {
                "t1": {"overlay": {"ranking": {"backtest": {"sim": {"equity": 250000}}}}}
            },
        }
    )
    e = _run(s, data_dir, ranking_path, tmp_path)
    m = e.metrics["cash_debit"]
    assert m["control"]["trades"] > 0 and m["t1"]["trades"] > 0
    c = Path(e.run_dir) / "cash_debit"
    import pandas as pd

    eq_c = pd.read_csv(c / "control" / "equity.csv")["equity"].iloc[0]
    eq_t = pd.read_csv(c / "t1" / "equity.csv")["equity"].iloc[0]
    assert eq_c == pytest.approx(25_000, abs=500) and eq_t == pytest.approx(250_000, abs=5000)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_show_ledger_and_refusal(
    data_dir: Path, ranking_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    studies = tmp_path / "studies"
    studies.mkdir()
    (studies / "BT-9.yaml").write_text(yaml.safe_dump(_spec_data(universe_ladder=["c125"])))
    ledger = tmp_path / "ledger.jsonl"
    s = load_study(studies / "BT-9.yaml")
    ledger.write_text(_entry(s, phase="holdout").model_dump_json() + "\n")
    base = ["--studies-dir", str(studies), "--ledger", str(ledger)]

    assert main(["backtest", "study", "show", "BT-9", *base]) == 0
    out = capsys.readouterr().out
    assert study_hash(s) in out and "holdout refused" in out and "runs 1 (holdout 1)" in out
    # E7.15: one JSON object the stdlib-only Analyst gate reads the current hash from
    assert main(["backtest", "study", "show", "BT-9", *base, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["spec_hash"] == study_hash(s) and doc["id"] == "BT-9"
    assert doc["holdout_runs"] == 1 and doc["runs"] == 1 and doc["variants_tried"] >= 1
    assert doc["universe_ladder"] == ["c125"] and "t1" in doc["arms"]["treatments"]

    assert main(["backtest", "study", "ledger", "BT-9", *base]) == 0
    assert "BT-9 holdout c125" in capsys.readouterr().out
    assert main(["backtest", "study", "ledger", "all", *base, "--json"]) == 0
    line = capsys.readouterr().out.strip()
    assert LedgerEntry.model_validate_json(line).study == "BT-9"
    assert main(["backtest", "study", "ledger", "BT-3", *base]) == 0
    assert "no ledger lines" in capsys.readouterr().out

    rc = main(
        [
            "backtest",
            "study",
            "run",
            "BT-9",
            *base,
            "--holdout",
            "--data-dir",
            str(data_dir),
            "--latest",
            LATEST.isoformat(),
            "--now",
            "2026-10-10T18:00:00",
            "--no-report",
        ]
    )
    assert rc == 3
    assert "one holdout per spec hash" in capsys.readouterr().err
    assert main(["backtest", "study", "show", "BT-404", *base]) == 2


def test_cli_run_dev_writes_ledger_and_report(
    data_dir: Path, ranking_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through `arc backtest study run` on the repo's base config; the
    fixture's test knobs ride in as the same overlay on both arms."""
    from arc.cli import main

    studies = tmp_path / "studies"
    studies.mkdir()
    overlay = {"ranking": {"backtest": {"n_paths": 200, "stance": "none", "data": {"pit": False}}}}
    data = _spec_data(
        universe_ladder=["c125"],
        arms={
            "control": {"ranker": "debit_width", "overlay": overlay},
            "treatments": {"t1": {"ranker": "managed_net_ev", "overlay": overlay}},
        },
    )
    (studies / "BT-9.yaml").write_text(yaml.safe_dump(data))
    rc = main(
        [
            "backtest",
            "study",
            "run",
            "BT-9",
            "--studies-dir",
            str(studies),
            "--ledger",
            str(tmp_path / "ledger.jsonl"),
            "--data-dir",
            str(data_dir),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--report-dir",
            str(tmp_path / "reports"),
            "--latest",
            LATEST.isoformat(),
            "--now",
            "2026-10-10T18:00:00",
            "--no-cache",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Header" in out and "dev result (not a verdict) · cash_debit · t1" in out
    led = load_ledger(tmp_path / "ledger.jsonl")
    assert len(led) == 1 and led[0].at == NOW and led[0].phase == "dev"
    assert (tmp_path / "reports" / "BT-9.md").is_file()
