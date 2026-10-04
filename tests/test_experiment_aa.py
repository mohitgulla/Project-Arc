"""E10.4 (D44): the XP-1 A/A setup and its calibration report, dry-run end to end.

The dry run (``scripts/xp1_aa_dry_run.py``) registers the committed A/A spec, starts
it on fixtures (scratch arm store, no broker), pairs one fixture loop chain, writes
10 simulated EOD sessions per arm, evaluates and renders the Markdown report. No
order reaches any broker; every clock is injected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from arc.experiments.calibration import PENDING_HEADER, aa_document, calibration_markdown
from arc.experiments.config import load_experiments_config
from arc.experiments.overlay import load_spec
from arc.store.db import connect

REPO = Path(__file__).resolve().parent.parent
EXPERIMENTS_YAML = REPO / "config" / "experiments.yaml"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


dry = _load("xp1_aa_dry_run", REPO / "scripts" / "xp1_aa_dry_run.py")


@pytest.fixture(scope="module")
def identical(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return dry.run(tmp_path_factory.mktemp("aa") / "run", sessions=10, seed=7)


def test_committed_spec_is_the_aa_baseline() -> None:
    sp = load_spec(REPO / "config" / "experiments" / "live" / "xp1_aa_baseline.yaml")
    assert sp.id == "XP-1" and sp.kind.value == "aa"
    assert sp.arms.control.overlay == {} and sp.arms.treatment.overlay == {}
    assert load_experiments_config(EXPERIMENTS_YAML).defaults.aa_sessions == 10


def test_identical_arms_end_to_end(identical: dict[str, Any]) -> None:
    r = identical["report"]
    # registered with aa_sessions as the A/A length, ran 10 paired sessions
    assert (r.min_sessions, r.max_sessions, r.sessions) == (10, 10, 10)
    assert r.missing_sessions == []
    assert r.verdict == "futility" and identical["status"] == ("stopped", "futility")
    assert r.primary.ci is not None and not r.primary.ci.excludes_zero
    # every calibration field the card names is filled
    c = r.calibration
    assert c.sigma is not None and c.sigma > 0
    assert set(c.mde_fixed) >= {20, 40, 60} and set(c.mde_always_valid) >= {20, 40, 60}
    assert all(c.mde_always_valid[n] > c.mde_fixed[n] for n in (20, 40, 60))
    assert c.slippage_gap_bps is not None and c.fill_rate_gap is not None
    assert c.paired_chains >= 1 and c.llm_divergence_rate is not None
    for a in r.arms:
        assert a.sessions == 10 and a.worst_day is not None and a.orders > 0
        assert a.mean_slippage_bps is not None
    # the arm's rows came from its own store, read through the paired view
    assert r.arms[1].arm_id == "XP-1:treatment" and r.treatment_sha


def test_markdown_shows_every_field_and_reuses_the_report(identical: dict[str, Any]) -> None:
    r = identical["report"]
    md = identical["markdown"]
    for needle in (
        "sigma(d_t) =",
        "| 20 |",
        "| 40 |",
        "| 60 |",
        "Slippage gap",
        "Fill-rate gap",
        "LLM divergence on identical inputs:",
        "| control | 10 |",
        "| treatment | 10 |",
        "Max DD",
        "Worst day",
        r.report_hash(),
    ):
        assert needle in md, needle
    # nothing is n/a in the dry run: every field is non-null
    assert "n/a" not in md
    # numbers are the report's, formatted by the shared view (no second computation)
    from arc.experiments import view

    assert view.pct(r.calibration.sigma, 3, sign=False) in md
    assert view.pct(r.calibration.mde_fixed[40], 3, sign=False) in md
    # no recommendation for the owner-fixed policy
    assert "recommend" not in md.lower()


def test_injected_arm_difference_ends_invalid(tmp_path: Path) -> None:
    """A treatment that out-earns an identical control by $150/day: the harness is broken."""
    res = dry.run(tmp_path / "run", sessions=10, seed=7, inject_usd=150.0)
    r = res["report"]
    assert r.verdict == "invalid"
    assert r.primary.ci is not None and r.primary.ci.lo > 0
    assert res["status"] == ("stopped", "invalid")
    conn = connect(res["control"])
    codes = [x[0] for x in conn.execute("SELECT reason_code FROM decisions")]
    conn.close()
    assert "experiment:invalid" in codes


def test_aa_path_never_changes_the_policy_defaults(tmp_path: Path) -> None:
    """alpha / min / max are owner-fixed (D44): the A/A writes no config, anywhere."""
    before = hashlib.sha256(EXPERIMENTS_YAML.read_bytes()).hexdigest()
    res = dry.run(tmp_path / "run", sessions=10, seed=3)
    assert res["defaults_before"] == res["defaults_after"]
    d = res["defaults_after"]
    assert (d.alpha, d.min_sessions, d.max_sessions) == (0.05, 20, 60)
    assert hashlib.sha256(EXPERIMENTS_YAML.read_bytes()).hexdigest() == before
    conn = connect(res["control"])
    changes = conn.execute(
        "SELECT count(*) FROM config_changes WHERE key LIKE 'experiments.%'"
    ).fetchone()[0]
    conn.close()
    assert changes == 0
    # the report states the fixed policy and never sets it
    md = res["markdown"]
    assert "alpha 0.05" in md and "min 20 / max 60" in md


def test_dry_run_sends_no_orders(identical: dict[str, Any]) -> None:
    """Fixture start + pair only: no broker order rows in either store."""
    for path in (identical["control"], identical["arm"]):
        conn = connect(path)
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
        conn.close()


def test_dry_run_refuses_a_used_dir(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("")
    with pytest.raises(SystemExit, match="not empty"):
        dry.run(tmp_path)


def test_cli_report_format_md(identical: dict[str, Any], tmp_path: Path) -> None:
    from arc.cli import main

    db = str(identical["control"])
    out = tmp_path / "docs" / "XP-1-aa.md"
    assert main(["experiment", "report", "XP-1", "--db", db, "--stored", "--format", "md",
                 "--out", str(out)]) == 0  # fmt: skip
    text = out.read_text()
    assert text.startswith("# XP-1 A/A calibration") and "Status: live run" in text
    assert "## Noise: sigma of the paired daily difference" in text
    assert main(["experiment", "report", "XP-1", "--db", db, "--stored", "--out", "x.md"]) == 2


def test_example_document_header_and_committed_copy(identical: dict[str, Any]) -> None:
    doc = aa_document(identical["report"], example=True)
    assert PENDING_HEADER in doc and "fixture dry run" in doc
    assert "### Noise: sigma of the paired daily difference" in doc
    committed = (REPO / "docs" / "RESEARCH" / "experiments" / "XP-1-aa.md").read_text()
    assert committed.startswith("# XP-1 A/A calibration")
    assert PENDING_HEADER in committed
    assert "## Example: fixture dry run (synthetic data)" in committed


def test_markdown_before_any_session_is_all_na(tmp_path: Path) -> None:
    """A report with no paired session yet renders without crashing (n/a everywhere)."""
    from tests import experiment_fixtures as fx

    conn = connect(tmp_path / "x.db")
    from arc.store.migrate import migrate

    migrate(conn)
    store = fx.start(conn, fx.spec("XP-1", kind="aa"))
    from arc.experiments.config import ExperimentsConfig
    from arc.experiments.evaluate import build_report

    r = build_report(
        conn, store.require("XP-1"), ExperimentsConfig(), now=fx.T0 + dt.timedelta(hours=1)
    )
    md = "\n".join(calibration_markdown(r))
    assert "| n/a | n/a | n/a |" in md and "sigma(d_t) = n/a" in md
    assert "## Series" not in md
