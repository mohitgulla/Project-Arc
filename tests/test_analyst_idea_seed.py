"""E21.2 (PLAN D86): the Analyst's experiment idea seed (``hermes/analyst/idea_seed.yaml``).

The retired dev-drafted XP specs are leads for the weekly Analyst, not proposals: the seed
is validated by the stdlib gate (``arc_analyst.py``), installed into the profile by
``install.sh`` and listed in the gate's context. Every treatment overlay must still load as
a real ``config/experiments/live`` arm, so a lead can become a ``P-<n>`` proposal as is.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from arc.experiments.overlay import validate_arms
from tests import experiment_fixtures as fx

REPO = Path(__file__).resolve().parents[1]
ANALYST = REPO / "hermes" / "analyst"
SEED = ANALYST / "idea_seed.yaml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("arc_analyst_e212", ANALYST / "arc_analyst.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


aa = _load()


def _doc() -> dict[str, Any]:
    return aa.parse_seed_text(SEED.read_text())


def test_committed_seed_is_valid_and_holds_every_retired_draft() -> None:
    ideas, errs = aa.load_seed(SEED)
    assert errs == []
    assert [x["id"] for x in ideas] == ["S-1", "S-2", "S-3", "S-4", "S-5"]
    # one lead per retired draft, each pointing at the deleted spec in git
    stems = [x["retired_spec"].split(":", 1)[1] for x in ideas]
    assert stems == [
        f"config/experiments/live/{n}.yaml"
        for n in (
            "xp10_trending_velocity",
            "xp11_scalp_movers",
            "xp12_retail_sentiment",
            "xp13_technicals",
            "xp14_menu_measure",
        )
    ]
    for x in ideas:
        assert not (REPO / x["retired_spec"].split(":", 1)[1]).exists()  # retired
        for ref in x.get("evidence_refs", []):
            path = ref.split(" ", 1)[0]
            assert (REPO / path).exists(), ref


def test_seed_is_yaml_too() -> None:
    # `#` comments + JSON: the repo's YAML tooling reads the same document.
    assert yaml.safe_load(SEED.read_text()) == _doc()


@pytest.mark.parametrize("idea", _load().parse_seed_text(SEED.read_text())["ideas"])
def test_every_seed_overlay_loads_as_an_arm(idea: dict[str, Any]) -> None:
    arms = {f"t{i}": {"overlay": ov} for i, ov in enumerate(idea["treatments"].values(), start=1)}
    spec = fx.spec(
        "XP-20", spec_version=2, area=idea["area"], arms={"control": {}, "treatments": arms}
    )
    validate_arms(spec)  # each overlay validates under its config file's own model


def _with(mutate: Any) -> list[str]:
    doc = copy.deepcopy(_doc())
    mutate(doc)
    return aa.validate_seed(doc)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda d: d.update(extra=1), "unknown keys ['extra']"),
        (lambda d: d.update(seed_version=2), "seed_version must be 1"),
        (lambda d: d.update(ideas={}), "ideas must be a list"),
        (lambda d: d["ideas"][0].update(id="XP-10"), "must look like S-<n>"),
        (lambda d: d["ideas"][1].update(id="S-1"), "is a duplicate"),
        (lambda d: d["ideas"][0].update(area="vibes"), "area must be one of"),
        (lambda d: d["ideas"][0].pop("source"), "missing ['source']"),
        (lambda d: d["ideas"][0].update(proposed_by="dev"), "unknown keys ['proposed_by']"),
        (lambda d: d["ideas"][0].update(evidence_refs="docs"), "evidence_refs must be a list"),
        (lambda d: d["ideas"][0].update(title=3), "title must be a string"),
        (lambda d: d["ideas"][0].update(variables=["nope"]), "must be <stem>.<path>"),
        (lambda d: d["ideas"][0].update(variables="x"), "variables must be a list"),
        (lambda d: d["ideas"][0].update(treatments=["x"]), "treatments must map"),
        (lambda d: d["ideas"][0]["treatments"].update(x={}), "must be a non-empty overlay"),
        (
            lambda d: d["ideas"][0]["treatments"].update(x={"gate": {"a": 1}}),
            "targets ['gate'] not in",
        ),
        (
            lambda d: d["ideas"][1]["treatments"].update(
                x={"routines": {"personas": {"anti_chase": "off"}}}
            ),
            "sets routines.personas.anti_chase, not in variables",
        ),
        (
            lambda d: d["ideas"][3]["treatments"].update(
                t5={"routines": {"anti_chase": {"vwap": "on"}}}
            ),
            "5 treatments, at most 4",
        ),
        (lambda d: d["ideas"].append("x"), "must be an object"),
    ],
)
def test_seed_validation_refuses(mutate: Any, needle: str) -> None:
    errs = _with(mutate)
    assert any(needle in e for e in errs), errs


def test_validate_seed_refuses_a_non_object() -> None:
    assert aa.validate_seed([]) == ["idea seed must be an object with seed_version and ideas"]


def test_load_seed_missing_and_bad_json(tmp_path: Path) -> None:
    assert aa.load_seed(tmp_path / "none.yaml") == ([], [])
    bad = tmp_path / "idea_seed.yaml"
    bad.write_text("# comment\nseed_version: 1\n")
    ideas, errs = aa.load_seed(bad)
    assert ideas == [] and "not valid JSON-after-comments" in errs[0]


def test_seed_lines() -> None:
    ideas, _ = aa.load_seed(SEED)
    lines = aa.seed_lines(ideas, [])
    assert len(lines) == 5
    assert lines[0].startswith("- S-1 [entries] Velocity-weighted trending tier")
    assert "vars routines.sources.universe.trending.scoring; treatments velocity" in lines[0]
    assert "evidence docs/RESEARCH/anti-chase-backtest.md" in lines[3]
    assert aa.seed_lines([], []) == ["- none"]
    assert aa.seed_lines([], ["boom"]) == [
        "- idea seed INVALID (fix hermes/analyst/idea_seed.yaml): boom"
    ]


def test_check_seed_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = aa.Paths(home=tmp_path / "analyst", repo=REPO, live_db=tmp_path / "x.db", arc=("arc",))
    assert aa.main(["check-seed"], p) == 2  # not installed yet
    assert "idea seed missing" in capsys.readouterr().out
    assert aa.main(["check-seed", str(SEED)], p) == 0
    assert "idea seed ok (5 leads: S-1, S-2, S-3, S-4, S-5)" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(json.dumps({"seed_version": 1, "ideas": [{"id": "XP-21"}]}))
    assert aa.main(["check-seed", str(bad)], p) == 2
    assert "idea seed rejected" in capsys.readouterr().out
    assert p.idea_seed == tmp_path / "analyst" / "idea_seed.yaml"


def test_check_seed_runs_under_the_host_python() -> None:
    # The cron runs the gate with the host's python3 (stdlib only, no yaml/pydantic).
    r = subprocess.run(
        [sys.executable, "-S", str(ANALYST / "arc_analyst.py"), "check-seed", str(SEED)],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "idea seed ok (5 leads" in r.stdout


def test_install_copies_the_seed(tmp_path: Path) -> None:
    r = subprocess.run(
        ["bash", str(ANALYST / "install.sh"), "--dry-run"],
        capture_output=True, text=True, check=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "ARC_ANALYST_PROFILE_HOME": str(tmp_path / "prof"), "HERMES_BIN": "hermes"},
    )  # fmt: skip
    line = next(x for x in r.stdout.splitlines() if "idea_seed.yaml" in x)
    assert line.startswith("+ install -m 0644")
    assert line.endswith(f"{tmp_path / 'prof'}/analyst/idea_seed.yaml")
