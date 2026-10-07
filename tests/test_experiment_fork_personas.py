"""E13.12 (D56): experiments fork at any persona.

Pure rules (fork step at every loop persona, arm-owned Scout / Scalp from the
overlay, shared kinds minus the book kinds and the arm's own producers), the stored
plan (identity + ``running`` event, pre-E13.12 stores), the sync of control's inputs
(candidates rows, raw docs with per-store Scalp bookkeeping), the arm's tick running
its own Scout only, the dry-run preview CLI and migration 026.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from typing import TYPE_CHECKING

import pytest

from arc.context.store import ContextStore
from arc.experiments.arms import ArmIdentity, read_identity, write_identity
from arc.experiments.config import ArmRunner, RunnerConfig
from arc.experiments.models import ARM_PERSONAS, OVERLAY_TARGETS, Area, ArmPlan
from arc.experiments.runner import (
    BOOK_KINDS,
    PERSONA_OVERLAY_PREFIXES,
    arm_owned_personas,
    arm_plan,
    arm_routines,
    arms_preview,
    fork_step,
    persona_jobs,
    plan_of,
    start_arms,
    sync_persona_inputs,
    sync_shared_context,
)
from arc.ingest.store import FILTERED_STATUS, RawDocRepo
from arc.pipeline.env import FIXTURE_NOW
from arc.routines.config import chain_for, load_routines
from arc.routines.handlers import JobResult
from arc.store.migrate import migrate
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

T0 = FIXTURE_NOW - dt.timedelta(hours=1)
NOW = dt.datetime(2026, 10, 6, 9, 0, tzinfo=ET)  # Tuesday
XP5 = "tests/fixtures/experiments/xp5_universe_screen.yaml"
XP8 = "config/experiments/live/xp8_scalp_options_tape.yaml"

# research chain under exit_path research + quant_risk_loop on: every loop persona
FULL = ["research", *chain_for("research", {"quant_risk_loop": True}, {"exit_path": "research"})]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def _runner(**kw: object) -> RunnerConfig:
    return RunnerConfig(
        arms={
            "treatment": ArmRunner(
                spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
            ),
            "shadow_control": ArmRunner(
                spec_arm="control", keys_env="ALPACA_SHADOW", db="shadow-{experiment_id}.db"
            ),
        },
        **kw,  # type: ignore[arg-type]
    )


# --- fork at any persona ------------------------------------------------------------


def test_full_chain_has_every_loop_persona() -> None:
    assert FULL == [
        "research",
        "exits.mandatory",
        "quant.exit",
        "risk.exit",
        "quant.open",
        "risk.open",
        "quant.revise",
        "quant.propose",
        "broker.execute",
    ]


@pytest.mark.parametrize(
    ("overlay", "fork"),
    [
        ({}, "exits.mandatory"),  # the first account step: the arm's own book
        ({"universe": {"liquidity_screen": {"loose": {"min_price": 4.0}}}}, "research"),
        ({"routines": {"personas": {"exit_path": "research"}}}, "research"),
        ({"routines": {"personas": {"research_idea_pool": "all"}}}, "research"),
        ({"account_profiles": {"x": 1}}, "research"),
        ({"exits": {"x": 1}}, "exits.mandatory"),
        ({"costs": {"x": 1}}, "exits.mandatory"),
        ({"ranking": {"x": 1}}, "exits.mandatory"),
        ({"routines": {"personas": {"quant_risk_loop": "on"}}}, "exits.mandatory"),
    ],
)
def test_fork_on_the_exit_path_chain(overlay: dict, fork: str) -> None:
    assert fork_step(FULL, overlay) == fork


@pytest.mark.parametrize(
    ("overlay", "fork"),
    [
        ({"costs": {"x": 1}}, "quant.exit"),
        ({"exits": {"x": 1}}, "quant.exit"),
        ({"account_profiles": {"x": 1}}, "research"),
        ({"ranking": {"x": 1}}, "quant.propose"),
        ({"routines": {"personas": {"quant_risk_loop": "on"}}}, "risk.open"),
    ],
)
def test_fork_on_the_shadow_exit_chain(overlay: dict, fork: str) -> None:
    """exit_path shadow: quant.exit (no account step) precedes the open path."""
    chain = ["research", *chain_for("research", {"quant_risk_loop": True}, {"exit_path": "shadow"})]
    assert chain[1] == "quant.exit"
    assert fork_step(chain, overlay) == fork


def test_fork_at_risk_exit_and_quant_revise() -> None:
    """Every persona step is a reachable fork point (risk.exit, quant.revise)."""
    chain = ["research", "quant.open", "risk.exit", "quant.revise", "quant.propose"]
    assert fork_step(chain, {"account_profiles": {}, "exits": {"x": 1}}) == "quant.open"
    assert fork_step(["research", "risk.exit", "quant.propose"], {"exits": {"x": 1}}) == "risk.exit"
    loop = {"routines": {"personas": {"quant_risk_loop": "on"}}}
    assert fork_step(["research", "quant.open", "quant.revise", "quant.propose"], loop) == (
        "quant.revise"
    )


def test_owning_a_persona_forks_at_research_at_the_latest() -> None:
    chain = ["research", "quant.open", "risk.open", "quant.propose", "broker.execute"]
    assert fork_step(chain, {}) == "quant.propose"
    assert fork_step(chain, {}, ["scalp"]) == "research"
    assert fork_step(chain, {"ranking": {"x": 1}}, ["scout"]) == "research"
    # a chain with no research step keeps its own fork
    assert fork_step(["quant.open", "quant.propose"], {}, ["scout"]) == "quant.propose"


# --- arm-owned personas -------------------------------------------------------------


@pytest.mark.parametrize(
    ("overlay", "personas"),
    [
        ({}, set()),
        ({"exits": {"x": 1}}, set()),
        ({"routines": {"personas": {"research_idea_pool": "all"}}}, set()),
        ({"routines": {"personas": {"scout_feed": "on"}}}, {"scout"}),
        ({"routines": {"funnel": {"scout": {"max_discovery": 10}}}}, {"scout"}),
        ({"universe": {"liquidity_screen": {"loose": {"min_price": 4.0}}}}, {"scout"}),
        ({"routines": {"personas": {"scalp_options_tape": "on"}}}, {"scalp"}),
        ({"routines": {"funnel": {"scalp": {"x": 1}}}}, {"scalp"}),
        ({"routines": {"categories": {"market_news": {"weight": 2}}}}, {"scalp"}),
        (
            {
                "universe": {"core": ["AAPL"]},
                "routines": {"personas": {"scalp_options_tape": "on"}},
            },
            {"scout", "scalp"},
        ),
    ],
)
def test_arm_owned_personas_from_overlay_keys(overlay: dict, personas: set[str]) -> None:
    assert arm_owned_personas(overlay) == personas


def test_persona_prefix_table_covers_the_arm_personas() -> None:
    assert set(PERSONA_OVERLAY_PREFIXES) == set(ARM_PERSONAS)
    assert persona_jobs(["scalp", "scout", "scalp"]) == ["scalp", "scalp.overnight", "scout"]


def test_arm_personas_are_validated_and_never_tunable() -> None:
    from arc.control.registry import REGISTRY

    assert RunnerConfig(arm_personas=["scout"]).arm_personas == ["scout"]
    with pytest.raises(ValueError, match="scout"):
        RunnerConfig(arm_personas=["research"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="twice"):
        RunnerConfig(arm_personas=["scout", "scout"])
    assert not [k for k in REGISTRY if k.startswith("experiments.runner.arm_")]


# --- the plan -----------------------------------------------------------------------


def test_plan_for_xp5_runs_its_own_scout_and_syncs_its_inputs() -> None:
    routines = load_routines()
    plan = arm_plan(
        routines, {"universe": {"liquidity_screen": {"loose": {"min_price": 4.0}}}}, RunnerConfig()
    )
    assert plan.fork_step == "research"
    assert plan.arm_personas == ["scout"]
    assert plan.own_producers == ["scout"]
    # the Scout's inputs come from control; candidate stays shared by producer
    assert {"channel_brief", "options_daily", "vx_curve", "candidate"} <= set(plan.shared_kinds)
    assert "active_universe" not in plan.shared_kinds  # re-resolved by the arm's Scout
    assert not set(plan.shared_kinds) & BOOK_KINDS


def test_plan_for_a_scalp_arm_shares_raw_docs_and_the_tape() -> None:
    plan = arm_plan(
        load_routines(), {"routines": {"personas": {"scalp_options_tape": "on"}}}, RunnerConfig()
    )
    assert plan.arm_personas == ["scalp"]
    assert plan.own_producers == ["scalp", "scalp.overnight"]
    assert {"raw_doc_ref", "index_vols", "chain_snapshot"} <= set(plan.shared_kinds)


def test_plan_never_syncs_book_kinds_under_the_research_exit_path() -> None:
    routines = load_routines(overrides={("personas", "exit_path"): "research"})
    plan = arm_plan(routines, {"routines": {"personas": {"exit_path": "research"}}}, RunnerConfig())
    assert plan.fork_step == "research"
    assert not set(plan.shared_kinds) & BOOK_KINDS
    assert "portfolio_context" in plan.shared_kinds  # exit steps read it


def test_runner_arm_personas_apply_to_every_arm() -> None:
    plan = arm_plan(load_routines(), {}, RunnerConfig(arm_personas=["scalp"]))
    assert plan.arm_personas == ["scalp"] and plan.fork_step == "research"


def test_arm_routines_enables_only_arm_jobs_and_own_personas() -> None:
    routines = arm_routines(load_routines(), ["broker.reconcile", *persona_jobs(["scout"])])
    enabled = {n for n, (_, s) in routines.jobs().items() if s.enabled}
    assert enabled == {"broker.reconcile", "scout"}


def test_pre_e13_12_store_loads_with_no_personas(tmp_path: Path) -> None:
    """A store written before migration 026 (no plan) recomputes from its overlay alone."""
    arm = _db(tmp_path / "arm.db")
    ident = ArmIdentity(
        arm_id="XP-3:treatment",
        experiment_id="XP-3",
        arm="treatment",
        spec_arm="treatment",
        keys_env="ALPACA_EXP",
        control_db="/nonexistent/arc.db",
        overlay={"routines": {"personas": {"director_diversification": "relaxed"}}},
        created_at=NOW,
    )
    write_identity(arm, ident)
    got = read_identity(arm)
    assert got is not None and got.plan is None
    plan = plan_of(got, load_routines(), RunnerConfig(arm_personas=["scout"]))
    assert plan.arm_personas == []  # runner.arm_personas applies at t0 only
    assert plan.fork_step == "research"


# --- t0 stores the plan ---------------------------------------------------------------


@pytest.fixture
def control(tmp_path: Path) -> Path:
    db = tmp_path / "control.db"
    assert _arc("experiment", "create", "--spec", XP5, "--db", str(db)) == 0
    assert _arc("experiment", "register", "XP-5", "--db", str(db)) == 0
    return db


def test_start_stores_each_arm_plan(control: Path, tmp_path: Path) -> None:
    conn = _db(control)
    st = start_arms(
        conn,
        "XP-5",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner(),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
        aa_override=True,
    )
    assert st.running is not None and st.spec.area is Area.UNIVERSE
    plans = st.running.arm_plans
    assert plans["treatment"].arm_personas == ["scout"]
    assert plans["treatment"].fork_step == "research"
    assert plans["shadow_control"].arm_personas == []
    assert plans["shadow_control"].fork_step == "quant.propose"
    arm = _db(tmp_path / "arms" / "exp-XP-5.db")
    ident = read_identity(arm)
    assert ident is not None and ArmPlan.model_validate(ident.plan) == plans["treatment"]
    with pytest.raises(sqlite3.IntegrityError):  # identity stays immutable
        arm.execute("UPDATE arm_identity SET plan = '{}'")


# --- sync ------------------------------------------------------------------------------


def _candidate(conn: sqlite3.Connection, cid: str, ticker: str, produced_by: str) -> None:
    conn.execute(
        """INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, sources,
           created_at) VALUES (?, ?, 'bullish', 'news', 0.7, '[]', ?)""",
        (cid, ticker, NOW.isoformat()),
    )
    ContextStore(conn).write(
        kind="candidate",
        subject=ticker,
        payload={
            "id": cid,
            "ticker": ticker,
            "stance": "bullish",
            "catalyst_type": "news",
            "confidence": 0.7,
            "sources": [],
            "created_at": NOW.isoformat(),
            "feed": "scout" if produced_by == "scout" else "scalp",
        },
        produced_by=produced_by,
        ttl="1d",
        now=NOW - dt.timedelta(minutes=5),
    )
    conn.commit()


def test_sync_skips_the_arms_own_producers_and_copies_candidate_rows() -> None:
    control, arm = _db(), _db()
    _candidate(control, "c-scalp", "AAPL", "scalp")
    _candidate(control, "c-scout", "NVDA", "scout")
    routines = load_routines()
    chain = ["research", "quant.open", "risk.open", "quant.propose", "broker.execute"]
    n = sync_shared_context(control, arm, routines, chain, [], as_of=NOW, personas=["scout"])
    assert n >= 1
    subjects = {
        r[0] for r in arm.execute("SELECT subject FROM context_entries WHERE kind='candidate'")
    }
    assert subjects == {"AAPL"}  # the arm's own Scout writes NVDA, never control's copy
    assert {r[0] for r in arm.execute("SELECT id FROM candidates")} == {"c-scalp"}
    # without an own Scout both come over (pre-E13.12 behaviour), rows included
    arm2 = _db()
    sync_shared_context(control, arm2, routines, chain, [], as_of=NOW)
    assert {r[0] for r in arm2.execute("SELECT id FROM candidates")} == {"c-scalp", "c-scout"}


def _doc(conn: sqlite3.Connection, url: str, *, closed: str | None = None) -> str:
    did = RawDocRepo(conn).insert(
        source="rss",
        url=url,
        published_at=NOW.isoformat(),
        text="NVDA beats",
        closed_status=closed,
    )
    assert did is not None
    # ingested "now" by the repo; pin it to NOW (tests never read the wall clock)
    conn.execute(
        "UPDATE raw_docs SET ingested_at = ? WHERE id = ?",
        (NOW.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), did),
    )
    conn.commit()
    return did


def test_scalp_arm_gets_control_raw_docs_unread() -> None:
    control, arm = _db(), _db()
    unread = _doc(control, "https://x/1")
    filtered = _doc(control, "https://x/2", closed=FILTERED_STATUS)
    read = _doc(control, "https://x/3")
    control.execute(
        "UPDATE raw_docs SET scalped_at = ?, scalp_run_id = 'r1', scalp_status = 'scouted' "
        "WHERE id = ?",
        (NOW.isoformat(), read),
    )
    control.commit()
    later = NOW + dt.timedelta(minutes=1)
    n = sync_persona_inputs(control, arm, load_routines(), ["scalp"], as_of=later)
    assert n == 3
    rows = {r["id"]: r for r in arm.execute("SELECT * FROM raw_docs")}
    assert set(rows) == {unread, filtered, read}
    # control's Scalp run is control's: the arm's Scalp reads the doc itself
    assert rows[read]["scalped_at"] is None and rows[read]["scalp_status"] is None
    assert rows[filtered]["scalp_status"] == FILTERED_STATUS  # D55 ingest close kept
    assert rows[unread]["scalp_run_id"] is None
    # idempotent; an arm-side Scalp mark is never overwritten
    arm.execute("UPDATE raw_docs SET scalp_status = 'scouted' WHERE id = ?", (unread,))
    arm.commit()
    assert sync_persona_inputs(control, arm, load_routines(), ["scalp"], as_of=later) == 0
    assert (
        arm.execute("SELECT scalp_status FROM raw_docs WHERE id = ?", (unread,)).fetchone()[0]
        == "scouted"
    )
    # a Scout-only arm never pulls raw docs; nor does an arm before its t0
    assert sync_persona_inputs(control, _db(), load_routines(), ["scout"], as_of=later) == 0
    assert (
        sync_persona_inputs(control, _db(), load_routines(), ["scalp"], as_of=later, since=later)
        == 0
    )


# --- the arms' tick + the dry-run preview ----------------------------------------------


def test_arms_tick_runs_the_arms_own_scout_only(control: Path, tmp_path: Path) -> None:
    from arc.experiments.runner import arms_tick

    conn = _db(control)
    start_arms(
        conn,
        "XP-5",
        actor="local",
        now=T0,
        t0_equity=D(10000),
        runner=_runner(),
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
        aa_override=True,
    )
    ran: list[str] = []

    def handler(name: str):  # noqa: ANN202
        def run(ctx: object) -> JobResult:
            ran.append(name)
            return JobResult(summary=f"{name} stub")

        return run

    handlers = {j: handler(j) for j in ("scout", "scalp", "monitor", "broker.reconcile")}
    at = dt.datetime(2026, 10, 7, 6, 5, tzinfo=ET)  # Wednesday, just after the 06:00 slot
    report = arms_tick(
        conn,
        routines_path=None,
        now=at,
        lock_dir=None,
        handlers=handlers,
    )
    treat = report["arms"]["treatment"]
    shadow = report["arms"]["shadow_control"]
    assert treat["plan"]["arm_personas"] == ["scout"]
    assert treat["jobs"] == [("scout", "ok")]  # its own Scout at control's 06:00 slot
    assert shadow["jobs"] == []  # the shadow control reads control's Scout
    assert ran == ["scout"]  # never a source, never the Scalp


def test_preview_lists_scout_for_the_treatment_arm_only(control: Path) -> None:
    conn = _db(control)
    at = dt.datetime(2026, 10, 6, 6, 0, tzinfo=ET)
    rep = arms_preview(
        conn,
        "XP-5",
        routines_path=None,
        now=at,
        since=at - dt.timedelta(minutes=10),
        runner=_runner(),
    )
    assert rep["plans_from"] == "computed (not started)"
    treat, shadow = rep["arms"]["treatment"], rep["arms"]["shadow_control"]
    assert treat["plan"]["fork_step"] == "research"
    assert any(" scout " in line and "planned" in line for line in treat["tick"])
    assert not any(" scout " in line for line in shadow["tick"])
    # nothing written: the experiment is still registered, no arm store
    from arc.experiments.runner import arm_stores
    from arc.experiments.store import ExperimentStore

    assert ExperimentStore(conn).require("XP-5").status.value == "registered"
    assert arm_stores(conn) == {}


def test_cli_dry_runs(control: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        _arc("experiment", "start", "XP-5", "--dry-run", "--now", "2026-10-06T09:00-04:00",
             "--db", str(control))
        == 0
    )  # fmt: skip
    out = capsys.readouterr().out
    assert "fork step:     research" in out and "arm personas:  scout" in out
    assert (
        _arc("experiment", "arms-tick", "--dry-run", "--experiment", "XP-5",
             "--now", "2026-10-06T06:00-04:00", "--since", "2026-10-06T05:50-04:00",
             "--json", "--db", str(control))
        == 0
    )  # fmt: skip
    rep = json.loads(capsys.readouterr().out)
    assert rep["arms"]["treatment"]["plan"]["arm_personas"] == ["scout"]
    assert _arc("experiment", "arms-tick", "--experiment", "XP-5", "--db", str(control)) == 2


# --- spec, overlay, migration ---------------------------------------------------------


def test_universe_is_an_overlay_target_and_reaches_the_universe_config(tmp_path: Path) -> None:
    from arc.control.effective import effective_settings
    from arc.universe.config import universe_config

    assert "universe" in OVERLAY_TARGETS
    ctl_path = tmp_path / "arc.db"
    _db(ctl_path).close()
    arm = _db(tmp_path / "arm.db")
    write_identity(
        arm,
        ArmIdentity(
            arm_id="XP-5:treatment",
            experiment_id="XP-5",
            arm="treatment",
            spec_arm="treatment",
            keys_env="ALPACA_EXP",
            control_db=str(ctl_path),
            overlay={"universe": {"liquidity_screen": {"loose": {"min_price": 4.0}}}},
            created_at=NOW,
        ),
    )
    assert universe_config(effective_settings(arm)).liquidity_screen.loose.min_price == 4.0
    ctl = universe_config(effective_settings(_db(ctl_path)))
    assert ctl.liquidity_screen.loose.min_price == 3.0


def test_bad_universe_overlay_is_refused_at_create(tmp_path: Path) -> None:
    from arc.experiments.models import ExperimentSpec
    from arc.experiments.overlay import validate_arms

    with pytest.raises(ValueError, match="screen"):
        validate_arms(
            ExperimentSpec.model_validate(
                {
                    "spec_version": 1,
                    "id": "XP-50",
                    "title": "bad",
                    "hypothesis": "h",
                    "area": "universe",
                    "kind": "ab",
                    "arms": {
                        "control": {"overlay": {}},
                        "treatment": {
                            "overlay": {
                                "universe": {"tiers": {"policy": {"discovery": {"screen": "x"}}}}
                            }
                        },
                    },
                    "non_inferiority_margin": 0.5,
                    "proposed_by": "owner",
                    "backtest_ref": "none: test",
                }
            )
        )


def test_xp8_owns_the_scalp() -> None:
    import yaml

    spec = yaml.safe_load(open(XP8))  # noqa: PTH123, SIM115
    assert arm_owned_personas(spec["arms"]["treatment"]["overlay"]) == {"scalp"}


def test_migration_026_keeps_every_experiment_row_and_the_triggers(tmp_path: Path) -> None:
    conn = _db(tmp_path / "x.db")
    assert _arc("experiment", "create", "--spec", XP5, "--db", str(tmp_path / "x.db")) == 0
    row = conn.execute("SELECT area FROM experiments WHERE experiment_id = 'XP-5'").fetchone()
    assert row[0] == "universe"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM experiments")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE experiments SET area = 'other'")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(arm_identity)")}
    assert "plan" in cols
