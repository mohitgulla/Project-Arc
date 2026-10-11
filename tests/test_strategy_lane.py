"""E21.1 (D86, revising E10.7 / D44): the strategy-lane CI check.

Dev changes ship to every arm; the one hard rule is that a PR may not change a config
leaf an *open* experiment tests, except as that experiment's promotion. Everything else
is advisory. Pure tests drive :func:`evaluate` with fixture diffs and PR bodies; the
end-to-end tests build a throwaway git repo and run :func:`main` on it. No network.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


lane = _load("strategy_lane_check", REPO / "scripts" / "strategy_lane_check.py")
CFG = lane.load_lane_config(REPO / "config" / "strategy_lane.yaml")

# XP-2: v1 spec (one treatment = t1) on exits; XP-14: v2 spec with three arms on exits.
TP = ("exits", "kinds", "long_call", "take_profit_pct_of_debit")
X2_ARMS = {"t1": {"exits": {"kinds": {"long_call": {"take_profit_pct_of_debit": 0.75}}}}}
X14_ARMS = {
    "t1": {"exits": {"pipeline": {"menu_measure": "rorc_day_full"}}},
    "t2": {"exits": {"pipeline": {"menu_measure": "rorc_day_tilted", "direction_tilt": 0.25}}},
    "t3": {"exits": {"pipeline": {"menu_measure": "managed_net_ev_full"}}},
}


def _exp(xid: str, arms: dict[str, Any], **kw: Any) -> Any:
    return lane.Experiment(id=xid, arms=arms, **kw)


OPEN = {"XP-2": _exp("XP-2", X2_ARMS), "XP-14": _exp("XP-14", X14_ARMS)}


def _eval(
    changed: list[str],
    body: str,
    deltas: dict[str, Any] | None = None,
    experiments: dict[str, Any] | None = None,
    **kw: Any,
) -> Any:
    return lane.evaluate(
        changed, body, deltas or {}, OPEN if experiments is None else experiments, CFG, **kw
    )


def _exits(old: Any, new: Any) -> dict[str, Any]:
    return {"exits": lane.yaml_delta("exits", old, new)}


BASE = {
    "pipeline": {"menu_measure": "control", "direction_tilt": 0.0},
    "kinds": {"long_call": {"take_profit_pct_of_debit": 1.0, "close_at_dte": 7}},
}


def _with(**pipeline: Any) -> dict[str, Any]:
    return {**BASE, "pipeline": {**BASE["pipeline"], **pipeline}}


def _tp(v: Any) -> dict[str, Any]:
    return {**BASE, "kinds": {"long_call": {"take_profit_pct_of_debit": v, "close_at_dte": 7}}}


# -- strategy paths -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "arc/exits/policy.py",
        "arc/scanner/rank.py",
        "arc/sizing.py",
        "arc/pipeline/steps.py",
        "arc/personas/builders.py",
        "hermes/skills/arc-research/SKILL.md",
        "config/exits.yaml",
        "config/ranking.yaml",
        "config/account_profiles.yaml",
        "config/universe.yaml",
        "config/costs.yaml",
        "config/strategy_lane.yaml",
    ],
)
def test_strategy_paths_are_strategy_lane(path: str) -> None:
    assert lane.strategy_files([path], CFG) == [path]


@pytest.mark.parametrize(
    "path",
    [
        "arc/gate/rules.py",
        "arc/budget/orders.py",
        "arc/pipeline/market_guard.py",
        "arc/execution/ladder.py",
        "arc/reconcile/baseline.py",
        "config/routines.yaml",
        "docs/PLAN.md",
        "tests/test_exits.py",
        "arc/exits/__pycache__/policy.cpython-312.pyc",
    ],
)
def test_gate_safety_and_other_paths_are_not(path: str) -> None:
    assert lane.strategy_files([path], CFG) == []


def test_non_strategy_pr_passes_silently() -> None:
    r = _eval(["arc/gate/rules.py", "docs/OPS.md"], "")
    assert r.ok and r.status == "none" and "no strategy paths" in r.render()
    assert r.annotations() == [] and not r.warnings


# -- rule 3: advisory, never fails ---------------------------------------------


def test_non_locked_strategy_change_passes_with_an_advisory() -> None:
    deltas = _exits(BASE, {**_tp(1.0), "default": {"close_at_dte": 5}})
    r = _eval(["arc/scanner/rank.py", "config/exits.yaml"], "Refactor.", deltas)
    assert r.ok and r.status == "advisory"
    assert r.strategy_files == ["arc/scanner/rank.py", "config/exits.yaml"]
    assert r.leaves == ["exits.default.close_at_dte"]
    assert any("XP-advisory" in w for w in r.warnings)
    notes = r.annotations()
    assert notes[0].startswith("::warning title=strategy-lane advisory::")
    assert "arc/scanner/rank.py" in notes[0] and "XP-advisory: none" in notes[0]
    md = r.summary_markdown()
    assert "`config/exits.yaml`" in md and "No `XP-advisory:` line" in md
    assert "PASS (advisory)" in r.render()


@pytest.mark.parametrize(
    ("line", "text"),
    [
        ("XP-advisory: none", "none"),
        ("- **XP-advisory:** menu ranking change, worth an XP", "menu ranking change, worth an XP"),
        ("xp-advisory: — none", "none"),
    ],
)
def test_advisory_line_is_read_and_shown(line: str, text: str) -> None:
    r = _eval(["arc/scanner/rank.py"], f"Body.\n{line}\n")
    assert r.ok and r.advisory == text and not r.warnings
    assert r.annotations()[0].startswith("::notice title=strategy-lane advisory::")
    assert f"XP-advisory: {text}" in r.render() and f"XP-advisory: {text}" in r.summary_markdown()


def test_empty_advisory_line_is_a_warning_only() -> None:
    r = _eval(["arc/scanner/rank.py"], "XP-advisory:")
    assert r.ok and r.advisory == "" and r.warnings


@pytest.mark.parametrize(
    "body",
    [
        "Flag: exits.pipeline.skip_iv_crush",
        "Lane: fast — fix NaN in the scanner IV filter",
        "Lane: fast",  # a reason used to be required; now ignored
        "Experiment: XP-1",
        "Experiment: XP-99",  # unknown ids are a note, not an error
        "- **Flag:** `x.y`\nLane: fast — owner-directed (PLAN D78)",
    ],
)
def test_old_style_bodies_pass(body: str) -> None:
    r = _eval(["arc/scanner/rank.py"], body)
    assert r.ok and r.status == "advisory", r.errors


def test_old_flag_default_on_now_passes() -> None:
    # E10.7 failed a `Flag:` key defaulting on; D86 ships dev changes on.
    deltas = _exits(BASE, _with(skip_iv_crush=True))
    r = _eval(["config/exits.yaml"], "Flag: exits.pipeline.skip_iv_crush", deltas)
    assert r.ok and r.leaves == ["exits.pipeline.skip_iv_crush"]


def test_value_flip_of_a_non_locked_leaf_passes() -> None:
    # E10.7 called this a promotion needing a win verdict; D86 lets it ship.
    deltas = {"ranking": lane.yaml_delta("ranking", {"w": {"ev": 1.0}}, {"w": {"ev": 0.5}})}
    r = _eval(["config/ranking.yaml"], "XP-advisory: none", deltas)
    assert r.ok and r.leaves == ["ranking.w.ev"]


def test_unknown_experiment_is_a_note() -> None:
    r = _eval(["arc/scanner/rank.py"], "Experiment: XP-99\nXP-advisory: none")
    assert r.ok and any("XP-99 has no spec" in n for n in r.notes)


# -- rules 1 + 2: locked leaves ---------------------------------------------------


def test_locked_leaf_change_fails() -> None:
    r = _eval(["config/exits.yaml"], "XP-advisory: none", _exits(BASE, _with(direction_tilt=0.1)))
    assert not r.ok and r.status == "locked"
    assert "XP-14 is open" in r.errors[0] and "exits.pipeline.direction_tilt" in r.errors[0]
    assert any(a.startswith("::error title=strategy-lane::") for a in r.annotations())
    assert "FAIL (locked)" in r.render()


def test_locked_v1_leaf_change_fails_in_a_non_strategy_yaml_too() -> None:
    exp = {"XP-3": _exp("XP-3", {"t1": {"routines": {"personas": {"x": "relaxed"}}}})}
    deltas = {
        "routines": lane.yaml_delta("routines", {"personas": {"x": "strict"}}, {"personas": {}})
    }
    r = _eval(["config/routines.yaml"], "", deltas, exp)
    assert not r.ok and "routines.personas.x" in r.errors[0]
    assert r.strategy_files == []  # routines.yaml is not strategy lane; the lock still holds


def test_adding_a_locked_leaf_fails() -> None:
    base = {**BASE, "pipeline": {"menu_measure": "control"}}
    r = _eval(["config/exits.yaml"], "", _exits(base, _with()))
    assert not r.ok and len(r.errors) == 1 and "exits.pipeline.direction_tilt" in r.errors[0]


def test_replacing_a_locked_parent_fails() -> None:
    # The whole `pipeline` mapping replaced by a scalar: the locked leaves go with it.
    r = _eval(["config/exits.yaml"], "", _exits(BASE, {**BASE, "pipeline": "off"}))
    assert not r.ok and "XP-14" in r.errors[0]


def test_locked_leaf_untouched_when_a_sibling_changes() -> None:
    r = _eval(["config/exits.yaml"], "", _exits(BASE, _with(menu_pool_max=40)))
    assert r.ok


def test_lock_lifts_with_a_verdict_on_the_base() -> None:
    closed = {"XP-14": _exp("XP-14", X14_ARMS, verdict="futility", open_at_base=False)}
    r = _eval(["config/exits.yaml"], "", _exits(BASE, _with(direction_tilt=0.1)), closed)
    assert r.ok


def test_empty_overlay_locks_nothing() -> None:
    aa = {"XP-1": _exp("XP-1", {"t1": {}})}
    assert _eval(["config/exits.yaml"], "", _exits(BASE, _tp(0.5)), aa).ok


# -- promotion -------------------------------------------------------------------


def _won(xid: str, arms: dict[str, Any], winner: str | None) -> dict[str, Any]:
    return {xid: _exp(xid, arms, verdict="win", winner=winner)}


def test_promotion_with_the_winner_arm_passes() -> None:
    exp = _won("XP-14", X14_ARMS, "t2")
    deltas = _exits(BASE, _with(menu_measure="rorc_day_tilted", direction_tilt=0.25))
    r = _eval(["config/exits.yaml"], "Experiment: XP-14\nXP-advisory: none", deltas, exp)
    assert r.ok and r.status == "promotion" and "promotes XP-14 (t2)" in r.notes


def test_promotion_of_part_of_the_winner_arm_passes() -> None:
    exp = _won("XP-14", X14_ARMS, "t2")
    deltas = _exits(BASE, _with(direction_tilt=0.25))
    assert _eval(["config/exits.yaml"], "Experiment: XP-14", deltas, exp).ok


def test_promotion_with_a_wrong_arm_value_fails() -> None:
    exp = _won("XP-14", X14_ARMS, "t2")
    deltas = _exits(BASE, _with(menu_measure="rorc_day_full"))  # t1's value, t2 won
    r = _eval(["config/exits.yaml"], "Experiment: XP-14", deltas, exp)
    assert not r.ok and "not arm t2's overlay value ('rorc_day_tilted')" in r.errors[0]


def test_promotion_of_a_leaf_the_winner_did_not_test_fails() -> None:
    exp = _won("XP-14", X14_ARMS, "t1")
    deltas = _exits(BASE, _with(menu_measure="rorc_day_full", direction_tilt=0.25))
    r = _eval(["config/exits.yaml"], "Experiment: XP-14", deltas, exp)
    assert not r.ok and "direction_tilt" in r.errors[0] and "untested" in r.errors[0]


def test_v1_verdict_without_winner_maps_to_t1() -> None:
    exp = _won("XP-2", X2_ARMS, None)
    assert exp["XP-2"].winning_arm() == "t1"
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits(BASE, _tp(0.75)), exp)
    assert r.ok and "promotes XP-2 (t1)" in r.notes
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits(BASE, _tp(0.5)), exp)
    assert not r.ok


@pytest.mark.parametrize(
    ("verdict", "body", "why"),
    [
        ("win", "XP-advisory: none", "XP-2 is open"),  # no Experiment: line
        ("futility", "Experiment: XP-2", "verdict is futility"),
        (None, "Experiment: XP-2", "not committed"),
    ],
)
def test_promotion_needs_the_citation_and_a_win(verdict: Any, body: str, why: str) -> None:
    exp = {"XP-2": _exp("XP-2", X2_ARMS, verdict=verdict)}
    r = _eval(["config/exits.yaml"], body, _exits(BASE, _tp(0.75)), exp)
    assert not r.ok and why in r.errors[0]


def test_promotion_never_removes_a_locked_value() -> None:
    exp = _won("XP-2", X2_ARMS, None)
    gone = {**BASE, "kinds": {"long_call": {"close_at_dte": 7}}}
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits(BASE, gone), exp)
    assert not r.ok and "never a promotion" in r.errors[0]


def test_winner_that_is_not_an_arm_fails() -> None:
    exp = _won("XP-2", X2_ARMS, "t3")
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits(BASE, _tp(0.75)), exp)
    assert not r.ok and "not an arm" in r.errors[0]


def test_deleted_open_spec_is_a_warning() -> None:
    r = _eval(["config/experiments/live/xp2.yaml"], "", removed_specs=["config/x/xp2.yaml (XP-2)"])
    assert r.ok and r.status == "advisory" and "XP-2" in r.warnings[0]
    assert "warning: deletes open experiment spec" in r.render()  # shown without strategy files
    assert r.annotations() == [f"::warning title=strategy-lane::{r.warnings[0]}"]


# -- build_experiments (base vs head) --------------------------------------------


V1 = {"id": "XP-2", "arms": {"control": {"overlay": {}}, "treatment": {"overlay": X2_ARMS["t1"]}}}
V2 = {
    "id": "XP-14",
    "arms": {
        "control": {"overlay": {}},
        "treatments": {k: {"overlay": v} for k, v in X14_ARMS.items()},
    },
}
WIN = lane.Verdict(verdict="win", winner=None)


def test_build_experiments_reads_v1_and_v2_arms() -> None:
    specs = {"a.yaml": V1, "b.yaml": V2, "c.yaml": {"not": "a spec"}}
    xs, removed = lane.build_experiments(specs, specs, {}, {})
    assert set(xs) == {"XP-2", "XP-14"} and removed == []
    assert xs["XP-2"].arms == X2_ARMS and xs["XP-14"].arms == X14_ARMS
    assert all(x.open_at_base for x in xs.values())


def test_spec_added_by_the_pr_locks_nothing() -> None:
    xs, _ = lane.build_experiments({}, {"a.yaml": V1}, {}, {})
    assert not xs["XP-2"].open_at_base


def test_spec_deleted_by_the_pr_is_reported() -> None:
    xs, removed = lane.build_experiments({"a.yaml": V1}, {}, {}, {})
    assert xs == {} and removed == ["a.yaml (XP-2)"]
    xs, removed = lane.build_experiments({"a.yaml": V1}, {}, {"XP-2": WIN}, {"XP-2": WIN})
    assert removed == [] and not xs["XP-2"].open_at_base


def test_editing_an_open_spec_keeps_the_base_lock() -> None:
    edited = {**V1, "arms": {"control": {"overlay": {}}, "treatment": {"overlay": {}}}}
    xs, _ = lane.build_experiments({"a.yaml": V1}, {"a.yaml": edited}, {}, {})
    assert lane.locked_leaves(xs["XP-2"]) == {TP}


def test_verdict_comes_from_the_head() -> None:
    v = lane.Verdict(verdict="win", winner="t2")
    xs, _ = lane.build_experiments({"b.yaml": V2}, {"b.yaml": V2}, {}, {"XP-14": v})
    assert xs["XP-14"].open_at_base and xs["XP-14"].winning_arm() == "t2"


def test_parse_verdict_is_strict() -> None:
    with pytest.raises(ValueError, match="needs experiment_id"):
        lane.parse_verdict({"verdict": "win"}, "f")
    with pytest.raises(ValueError, match="winner must be"):
        lane.parse_verdict(
            {"experiment_id": "XP-2", "verdict": "win", "report_hash": "a", "winner": "x"}, "f"
        )
    xid, v = lane.parse_verdict(
        {"experiment_id": "xp-2", "verdict": "WIN", "report_hash": "a", "winner": "t4"}, "f"
    )
    assert (xid, v) == ("XP-2", lane.Verdict(verdict="win", winner="t4"))


# -- yaml_delta / config ------------------------------------------------------


def test_yaml_delta_leaves() -> None:
    d = lane.yaml_delta(
        "s", {"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"b": 2, "c": [1, 2]}, "e": 0}
    )
    assert d.changed == {("s", "a", "b"): (1, 2)}
    assert d.added == {("s", "e"): 0}
    assert d.removed == {("s", "d"): 1}
    assert lane.yaml_delta("s", None, {"x": 1}).added == {("s", "x"): 1}


def test_lane_config_rejects_unknown_and_retired_keys() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        lane.LaneConfig.from_mapping({"strategy_paths": ["a"], "bogus": 1})
    for gone in ("promotion_stems", "owner_waivers", "flag_off_values", "fast_reason_min_chars"):
        with pytest.raises(ValueError, match="unknown keys"):
            lane.LaneConfig.from_mapping({"strategy_paths": ["a"], gone: []})
    with pytest.raises(ValueError, match="strategy_paths is empty"):
        lane.LaneConfig.from_mapping({})


def test_repo_lane_config_has_only_the_d86_keys() -> None:
    raw = yaml.safe_load((REPO / "config" / "strategy_lane.yaml").read_text())
    assert set(raw) == {"strategy_paths", "exclude_paths", "experiments_dir", "verdicts_dir"}


def test_every_strategy_path_glob_matches_a_real_file() -> None:
    files = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file()]
    files = [f for f in files if not f.startswith((".venv/", ".git/", "web/node_modules/"))]
    for glob in CFG.strategy_paths:
        one = lane.LaneConfig.from_mapping({"strategy_paths": [glob]})
        assert lane.strategy_files(files, one), f"{glob} matches nothing in the repo"


def test_repo_experiments_load() -> None:
    xs = lane.load_experiments(REPO, CFG)
    assert "XP-1" in xs and lane.locked_leaves(xs["XP-1"]) == set()


def test_repo_open_experiment_leaves_are_overlay_targets() -> None:
    from arc.experiments.models import OVERLAY_TARGETS

    for x in lane.load_experiments(REPO, CFG).values():
        assert {leaf[0] for leaf in lane.locked_leaves(x)} <= set(OVERLAY_TARGETS), x.id


# -- end to end on a scratch git repo -----------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, data: Any) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data))


@pytest.fixture(autouse=True)
def _not_under_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI sets GITHUB_ACTIONS=true, which makes the check print ::warning:: annotations;
    # tests opt in explicitly (test_github_annotations_and_summary).
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "config/strategy_lane.yaml", (REPO / "config/strategy_lane.yaml").read_text())
    _write(root, "config/exits.yaml", BASE)
    _write(root, "arc/scanner/rank.py", "x = 1\n")
    _write(root, "config/experiments/live/xp2.yaml", V1)
    _write(root, "config/experiments/live/xp14.yaml", V2)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "pr")
    return root


def _run(
    root: Path, body: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "pr", "--allow-empty")
    bf = tmp_path / "body.md"
    bf.write_text(body)
    rc = lane.main(["--base", "main", "--head", "pr", "--body-file", str(bf), "--repo", str(root)])
    return rc, capsys.readouterr().out


def _verdict(root: Path, xid: str, **extra: Any) -> None:
    _write(
        root,
        f"config/experiments/live/verdicts/{xid}.yaml",
        {"experiment_id": xid, "verdict": "win", "report_hash": "ab" * 32, **extra},
    )


def test_e2e_code_change_is_advisory(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "arc/scanner/rank.py", "x = 2\n")
    rc, out = _run(repo, "no lane", tmp_path, capsys)
    assert rc == 0 and "PASS (advisory)" in out and "arc/scanner/rank.py" in out
    assert "::" not in out  # annotations only under GitHub Actions


def test_e2e_locked_leaf_then_promotion(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/exits.yaml", _with(menu_measure="managed_net_ev_full"))
    rc, out = _run(repo, "XP-advisory: none", tmp_path, capsys)
    assert rc == 1 and "XP-14 is open" in out
    rc, out = _run(repo, "Experiment: XP-14", tmp_path, capsys)
    assert rc == 1 and "not committed" in out
    _verdict(repo, "XP-14", winner="t3")
    rc, out = _run(repo, "Experiment: XP-14", tmp_path, capsys)
    assert rc == 0 and "PASS (promotion)" in out and "promotes XP-14 (t3)" in out


def test_e2e_v1_verdict_is_t1(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/exits.yaml", _tp(0.75))
    _verdict(repo, "XP-2")
    rc, out = _run(repo, "Experiment: XP-2", tmp_path, capsys)
    assert rc == 0 and "promotes XP-2 (t1)" in out


def test_e2e_closed_experiment_frees_its_leaves(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _verdict(repo, "XP-2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "verdict")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "pr")
    _git(repo, "checkout", "-qb", "pr2")
    _write(repo, "config/exits.yaml", _tp(0.6))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pr2")
    bf = tmp_path / "b.md"
    bf.write_text("XP-advisory: none")
    rc = lane.main(["--base", "main", "--head", "pr2", "--body-file", str(bf), "--repo", str(repo)])
    assert rc == 0, capsys.readouterr().out


def test_e2e_deleting_an_open_spec_warns(repo: Path, tmp_path: Path, capsys: Any) -> None:
    (repo / "config/experiments/live/xp2.yaml").unlink()
    _write(repo, "config/exits.yaml", _tp(0.6))
    rc, out = _run(repo, "", tmp_path, capsys)
    assert rc == 0 and "deletes open experiment spec" in out and "XP-2" in out


def test_e2e_spec_added_by_the_pr_locks_nothing(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/experiments/live/xp9.yaml", {**V1, "id": "XP-9"})
    _write(repo, "config/exits.yaml", {**_tp(1.0), "default": {"x": 1}})
    rc, out = _run(repo, "", tmp_path, capsys)
    assert rc == 0, out


def test_e2e_non_strategy(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "docs/x.md", "hi\n")
    rc, out = _run(repo, "", tmp_path, capsys)
    assert rc == 0 and "no strategy paths" in out


def test_e2e_github_summary_and_annotations(
    repo: Path, tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _write(repo, "arc/scanner/rank.py", "x = 3\n")
    rc, out = _run(repo, "", tmp_path, capsys)
    assert rc == 0 and "::warning title=strategy-lane advisory::" in out
    assert "`arc/scanner/rank.py`" in summary.read_text()


def test_e2e_bad_verdict_file_is_a_config_error(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/experiments/live/verdicts/XP-2.yaml", {"verdict": "win"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bad")
    bf = tmp_path / "b.md"
    bf.write_text("")
    rc = lane.main(["--base", "main", "--head", "pr", "--body-file", str(bf), "--repo", str(repo)])
    assert rc == 2 and "report_hash" in capsys.readouterr().err


def test_ci_runs_the_check_on_pull_requests() -> None:
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    job = ci["jobs"]["strategy-lane"]
    assert job["if"] == "github.event_name == 'pull_request'"
    run = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "scripts/strategy_lane_check.py" in run and "gh pr view" in run
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # the merge base must be reachable
