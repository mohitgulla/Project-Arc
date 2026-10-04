"""E10.7 (D44 two lanes): the strategy-lane CI check, one test per rule.

Pure tests drive :func:`evaluate` with fixture diffs and PR bodies; the end-to-end
tests build a throwaway git repo and run :func:`main` on it. No network.
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

X2_OVERLAY = {"exits": {"kinds": {"long_call": {"take_profit_pct_of_debit": 0.75}}}}
EXPERIMENTS = {
    "XP-1": lane.Experiment(id="XP-1", overlay={}, verdict="futility"),
    "XP-2": lane.Experiment(id="XP-2", overlay=X2_OVERLAY, verdict="win"),
    "XP-3": lane.Experiment(id="XP-3", overlay=X2_OVERLAY, verdict=None),
}


def _eval(changed: list[str], body: str, deltas: dict[str, Any] | None = None) -> Any:
    return lane.evaluate(changed, body, deltas or {}, EXPERIMENTS, CFG)


def _exits_delta(old: Any, new: Any) -> dict[str, Any]:
    return {"exits": lane.yaml_delta("exits", old, new)}


# -- strategy paths -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "arc/exits/policy.py",
        "arc/scanner/rank.py",
        "arc/sizing.py",
        "arc/pipeline/steps.py",
        "arc/personas/builders.py",
        "hermes/skills/arc-director/SKILL.md",
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


def test_non_strategy_pr_passes_without_a_lane_line() -> None:
    r = _eval(["arc/gate/rules.py", "docs/OPS.md"], "")
    assert r.ok and r.lane == "none" and "no strategy paths" in r.render()


def test_strategy_pr_without_lane_line_fails() -> None:
    r = _eval(["arc/scanner/rank.py"], "Refactor the ranker.")
    assert not r.ok and "no lane line" in r.errors[0]
    assert "Experiment: XP-<n>" in r.render() and "FAIL" in r.render()


# -- Experiment: XP-<n> --------------------------------------------------------


def test_registered_experiment_passes() -> None:
    r = _eval(["arc/scanner/rank.py"], "Adds a ranker.\n\nExperiment: XP-1\n")
    assert r.ok and r.lane == "experiment"


def test_unknown_experiment_fails() -> None:
    r = _eval(["arc/scanner/rank.py"], "Experiment: XP-99")
    assert not r.ok and "XP-99 has no spec" in r.errors[0]


def test_experiment_line_is_case_and_bullet_tolerant() -> None:
    assert _eval(["arc/sizing.py"], "- **Experiment:** XP-1").ok
    assert _eval(["arc/sizing.py"], "- experiment: xp-1").ok


@pytest.mark.parametrize("xid", ["X-1", "XP-0", "XP-01", "XP1", "XP-"])
def test_experiment_line_rejects_old_or_malformed_ids(xid: str) -> None:
    assert not _eval(["arc/sizing.py"], f"Experiment: {xid}").ok


# -- Lane: fast — <reason> ----------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Lane: fast — fix NaN in the scanner IV filter",
        "Lane: fast - fix NaN in the scanner IV filter",
        "lane: fast: fix NaN in the scanner IV filter",
    ],
)
def test_fast_lane_with_reason_passes(line: str) -> None:
    r = _eval(["arc/scanner/iv.py"], f"Body.\n{line}\n")
    assert r.ok and r.lane == "fast" and "sentinel audits" in " ".join(r.notes)


@pytest.mark.parametrize("line", ["Lane: fast", "Lane: fast — bug"])
def test_fast_lane_without_a_real_reason_fails(line: str) -> None:
    r = _eval(["arc/scanner/iv.py"], line)
    assert not r.ok and "reason of at least" in r.errors[0]


def test_a_broken_extra_line_fails_even_when_another_lane_passes() -> None:
    r = _eval(["arc/scanner/iv.py"], "Lane: fast — fix NaN in IV filter\nExperiment: XP-42")
    assert not r.ok and r.lane == "fast" and "XP-42" in r.errors[0]


# -- Flag: <stem>.<key> -------------------------------------------------------

BASE_EXITS = {"pipeline": {"rank_menu_by": "scanner"}, "kinds": {"long_call": {"x": 1}}}


def _with(extra: dict[str, Any]) -> dict[str, Any]:
    return {**BASE_EXITS, "pipeline": {**BASE_EXITS["pipeline"], **extra}}


@pytest.mark.parametrize("off", [False, None, "off", "none", "control"])
def test_new_flag_defaulting_off_passes(off: Any) -> None:
    deltas = _exits_delta(BASE_EXITS, _with({"skip_iv_crush": off}))
    r = _eval(
        ["arc/pipeline/steps.py", "config/exits.yaml"],
        "Flag: exits.pipeline.skip_iv_crush",
        deltas,
    )
    assert r.ok and r.lane == "flag"


def test_new_flag_defaulting_on_fails() -> None:
    deltas = _exits_delta(BASE_EXITS, _with({"skip_iv_crush": True}))
    r = _eval(["config/exits.yaml"], "Flag: `exits.pipeline.skip_iv_crush`", deltas)
    assert not r.ok and "must default to off" in r.errors[0]


def test_flag_that_is_not_added_by_the_pr_fails() -> None:
    r = _eval(["arc/pipeline/steps.py"], "Flag: exits.pipeline.skip_iv_crush")
    assert not r.ok and "not a key this PR adds" in r.errors[0]


def test_flag_in_a_non_strategy_yaml_counts() -> None:
    deltas = {"routines": lane.yaml_delta("routines", {"loop": {}}, {"loop": {"new_x": False}})}
    r = _eval(
        ["arc/pipeline/steps.py", "config/routines.yaml"], "Flag: routines.loop.new_x", deltas
    )
    assert r.ok and r.lane == "flag"


def test_existing_key_cannot_be_cited_as_a_flag() -> None:
    deltas = _exits_delta(BASE_EXITS, _with({"rank_menu_by": "managed_net_ev"}))
    r = _eval(
        ["config/exits.yaml"],
        "Flag: exits.pipeline.rank_menu_by\nLane: fast — flip it",
        deltas,
    )
    # Flipping an existing value is a promotion; neither Flag nor fast covers it.
    assert not r.ok and r.lane == "promotion"
    assert any("already exists" in e for e in r.errors)


# -- promotion (flip a default) -----------------------------------------------

OLD = {"kinds": {"long_call": {"take_profit_pct_of_debit": 1.0, "close_at_dte": 7}}}
NEW = {"kinds": {"long_call": {"take_profit_pct_of_debit": 0.75, "close_at_dte": 7}}}


def test_promotion_with_winning_experiment_passes() -> None:
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits_delta(OLD, NEW))
    assert r.ok and r.lane == "promotion" and "promotes XP-2" in r.notes


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("Lane: fast — the old take profit was a typo", "promotion"),
        ("Experiment: XP-1", "promotion"),  # verdict futility
        ("Experiment: XP-3", "promotion"),  # no verdict committed
        ("", "promotion"),
    ],
)
def test_promotion_without_a_win_fails(body: str, why: str) -> None:
    r = _eval(["config/exits.yaml"], body, _exits_delta(OLD, NEW))
    assert not r.ok and r.lane == "promotion" and why in r.errors[-1]


def test_promotion_must_match_the_tested_overlay() -> None:
    other = {"kinds": {"long_call": {"take_profit_pct_of_debit": 0.5, "close_at_dte": 7}}}
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits_delta(OLD, other))
    assert not r.ok and "not the treatment overlay value of XP-2" in r.errors[0]


def test_promotion_cannot_piggyback_an_untested_change() -> None:
    both = {"kinds": {"long_call": {"take_profit_pct_of_debit": 0.75, "close_at_dte": 3}}}
    r = _eval(["config/exits.yaml"], "Experiment: XP-2", _exits_delta(OLD, both))
    assert not r.ok and "close_at_dte" in r.errors[0]


def test_removing_an_existing_strategy_value_is_a_promotion() -> None:
    gone = {"kinds": {"long_call": {"take_profit_pct_of_debit": 1.0}}}
    r = _eval(["config/exits.yaml"], "Lane: fast — clean up unused key", _exits_delta(OLD, gone))
    assert not r.ok and r.lane == "promotion"


def test_value_change_in_a_non_overlayable_yaml_is_not_a_promotion() -> None:
    deltas = {"universe": lane.yaml_delta("universe", {"a": {"m": 10}}, {"a": {"m": 12}})}
    r = _eval(["config/universe.yaml"], "Lane: fast — raise the min price after a halt", deltas)
    assert r.ok and r.lane == "fast"


def test_comment_only_yaml_edit_is_not_a_promotion() -> None:
    r = _eval(
        ["config/exits.yaml"], "Lane: fast — reword exits.yaml comments", _exits_delta(OLD, OLD)
    )
    assert r.ok and r.lane == "fast"


# -- yaml_delta ---------------------------------------------------------------


def test_yaml_delta_leaves() -> None:
    d = lane.yaml_delta(
        "s", {"a": {"b": 1, "c": [1, 2]}, "d": 1}, {"a": {"b": 2, "c": [1, 2]}, "e": 0}
    )
    assert d.changed == {("s", "a", "b"): (1, 2)}
    assert d.added == {("s", "e"): 0}
    assert d.removed == {("s", "d"): 1}
    assert lane.yaml_delta("s", None, {"x": 1}).added == {("s", "x"): 1}


# -- config -------------------------------------------------------------------


def test_lane_config_rejects_unknown_keys_and_empty_paths() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        lane.LaneConfig.from_mapping({"strategy_paths": ["a"], "bogus": 1})
    with pytest.raises(ValueError, match="strategy_paths is empty"):
        lane.LaneConfig.from_mapping({})


def test_promotion_stems_are_overlay_targets() -> None:
    from arc.experiments.models import OVERLAY_TARGETS

    assert set(CFG.promotion_stems) <= set(OVERLAY_TARGETS)
    for stem in CFG.promotion_stems:
        assert f"config/{stem}.yaml" in CFG.strategy_paths


def test_every_strategy_path_glob_matches_a_real_file() -> None:
    files = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file()]
    files = [f for f in files if not f.startswith((".venv/", ".git/", "web/node_modules/"))]
    for glob in CFG.strategy_paths:
        one = lane.LaneConfig.from_mapping({"strategy_paths": [glob]})
        assert lane.strategy_files(files, one), f"{glob} matches nothing in the repo"


def test_repo_experiments_load() -> None:
    xs = lane.load_experiments(REPO, CFG)
    assert "XP-1" in xs and xs["XP-1"].overlay == {}


# -- end to end on a scratch git repo -----------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, data: Any) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "config/strategy_lane.yaml", (REPO / "config/strategy_lane.yaml").read_text())
    _write(root, "config/exits.yaml", OLD)
    _write(root, "arc/scanner/rank.py", "x = 1\n")
    _write(
        root,
        "config/experiments/live/x2.yaml",
        {"id": "XP-2", "kind": "ab", "arms": {"treatment": {"overlay": X2_OVERLAY}}},
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "pr")
    return root


def _run(
    root: Path, body: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "pr")
    bf = tmp_path / "body.md"
    bf.write_text(body)
    rc = lane.main(["--base", "main", "--head", "pr", "--body-file", str(bf), "--repo", str(root)])
    return rc, capsys.readouterr().out


def test_e2e_code_change_needs_a_lane(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "arc/scanner/rank.py", "x = 2\n")
    rc, out = _run(repo, "no lane", tmp_path, capsys)
    assert rc == 1 and "arc/scanner/rank.py" in out


def test_e2e_promotion_needs_committed_win(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/exits.yaml", NEW)
    rc, out = _run(repo, "Experiment: XP-2", tmp_path, capsys)
    assert rc == 1 and "committed `win` verdict" in out
    _write(
        repo,
        "config/experiments/live/verdicts/XP-2.yaml",
        {"experiment_id": "XP-2", "verdict": "win", "report_hash": "ab" * 32},
    )
    rc, out = _run(repo, "Experiment: XP-2", tmp_path, capsys)
    assert rc == 0 and "lane: promotion" in out


def test_e2e_new_flag(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/exits.yaml", {**OLD, "pipeline": {"new_menu": False}})
    _write(repo, "arc/scanner/rank.py", "x = 3\n")
    rc, out = _run(repo, "Flag: exits.pipeline.new_menu", tmp_path, capsys)
    assert rc == 0 and "lane: flag" in out


def test_e2e_non_strategy(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "docs/x.md", "hi\n")
    rc, out = _run(repo, "", tmp_path, capsys)
    assert rc == 0 and "no strategy paths" in out


def test_e2e_bad_verdict_file_is_a_config_error(repo: Path, tmp_path: Path, capsys: Any) -> None:
    _write(repo, "config/experiments/live/verdicts/XP-2.yaml", {"verdict": "win"})
    bf = tmp_path / "b.md"
    bf.write_text("")
    rc = lane.main(
        ["--base", "main", "--head", "main", "--body-file", str(bf), "--repo", str(repo)]
    )
    assert rc == 2 and "report_hash" in capsys.readouterr().err


def test_ci_runs_the_check_on_pull_requests() -> None:
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    job = ci["jobs"]["strategy-lane"]
    assert job["if"] == "github.event_name == 'pull_request'"
    run = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "scripts/strategy_lane_check.py" in run and "gh pr view" in run
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0  # the merge base must be reachable
