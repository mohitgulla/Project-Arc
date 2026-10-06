"""D32 daily options order budget in the pipeline: tiers, short-circuit, manifest, tower, notice."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

import pytest

from arc.budget import Tier, current_budget
from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import to_db
from arc.ingest.scalp import load_fixture_docs
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.env import FIXTURE_SETS
from arc.pipeline.runner import open_db, run_propose
from arc.routines.config import load_routines
from arc.routines.heartbeat import LogNotifier
from arc.store.repos import CandidateRepo, OrderRepo, ProposalRepo

if TYPE_CHECKING:
    import sqlite3


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> ArcSettings:
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


def seed_orders(conn: sqlite3.Connection, n: int, at: dt.datetime = FIXTURE_NOW) -> None:
    """``n`` broker submissions today, as the ladder would have recorded them."""
    if not conn.execute("SELECT 1 FROM proposals WHERE proposal_hash = 'filler'").fetchone():
        CandidateRepo(conn).insert(
            ticker="SPY", stance="bullish", catalyst_type="t", confidence=0.7, id="c-filler"
        )
        ProposalRepo(conn).insert(
            candidate_id="c-filler",
            proposal_hash="filler",
            structure_json="{}",
            thesis="t",
            quant_json="{}",
            sizing_json="{}",
            expires_at=at.isoformat(),
            ticker="SPY",
        )
    repo = OrderRepo(conn)
    start = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    for k in range(start, start + n):
        repo.create(proposal_hash="filler", client_order_id=f"filler.s{k}", created_at=to_db(at))


def run(conn: sqlite3.Connection, settings: ArcSettings, fixture_set: str = "neutral"):  # noqa: ANN201
    env = PipelineEnv.fixtures(FIXTURE_SETS[fixture_set])
    return run_propose(
        conn,
        settings,
        load_routines(),
        env,
        now=FIXTURE_NOW,
        notifier=LogNotifier(),
        mode="fixtures",
    )


def run_recording(conn: sqlite3.Connection, settings: ArcSettings):  # noqa: ANN201
    """Like ``run`` but keeps each persona's prompt text (persona_calls stores a hash)."""
    env = PipelineEnv.fixtures(FIXTURE_SETS["neutral"])
    prompts: dict[str, str] = {}

    class _Rec:
        def __init__(self, persona: str, inner) -> None:  # noqa: ANN001
            self.persona, self.inner = persona, inner
            self.model = getattr(inner, "model", "fixture")

        def complete(self, prompt: str):  # noqa: ANN202
            prompts[self.persona] = prompt
            return self.inner.complete(prompt)

    env.llms = {k: _Rec(k, v) for k, v in env.llms.items()}  # type: ignore[misc]
    report = run_propose(
        conn,
        settings,
        load_routines(),
        env,
        now=FIXTURE_NOW,
        notifier=LogNotifier(),
        mode="fixtures",
    )
    return report, prompts


def decisions(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT choice, reason_code FROM decisions ORDER BY rowid").fetchall()
    return [(r[0], r[1]) for r in rows]


def manifests(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT job, payload FROM run_manifests ORDER BY created_at").fetchall()
    return {job: json.loads(js) for job, js in rows}


def test_normal_tier_proposes_and_stamps_manifest(settings: ArcSettings) -> None:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    report = run(conn, settings)
    assert not report.failed and len(report.proposals) == 1
    m = manifests(conn)
    for routine in ("research", "quant.propose"):
        assert m[routine]["order_budget"] == {"used": 0, "limit": 200, "tier": "normal"}
    # the run recorded the budget as an external input, with its count
    inputs = [i for i in m["quant.propose"]["external_inputs"] if i["name"] == "order_budget"]
    assert inputs and inputs[0]["source"] == "db" and inputs[0]["count"] == 0
    assert ("no_trade", "order_budget_exhausted") not in decisions(conn)


def test_restrictive_tier_caps_the_run(settings: ArcSettings) -> None:
    """At 100 used: shortlist capped to 1, ≤1 new open, 2 improvement steps, stricter floors."""
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    seed_orders(conn, 100)
    assert current_budget(conn, None, settings, now=FIXTURE_NOW).tier is Tier.RESTRICTIVE
    report, prompts = run_recording(conn, settings)
    assert not report.failed
    m = manifests(conn)
    assert m["quant.propose"]["order_budget"] == {"used": 100, "limit": 200, "tier": "restrictive"}
    # Research saw the tier in its rules (advisory); E5.7: no count cap in the prompt
    assert "order budget tier: restrictive (100/200)" in prompts["research"]
    assert "At most" not in prompts["research"]
    # the deterministic cap: the Quant/Risk budget drops to 1 in the restrictive tier
    sl = ContextStore(conn).snapshot(FIXTURE_NOW, kinds=("shortlist",)).of_kind("shortlist")
    assert sl and sl[-1].payload["budget"] == 1
    # at most one new open; the neutral fixture only has one candidate anyway
    assert len(report.proposals) <= 1
    codes = {c for _, c in decisions(conn)}
    assert "order_budget_exhausted" not in codes
    # the neutral iron condor's managed net EV is below 1.5x round-trip costs: dropped
    if not report.proposals:
        assert "budget_restrictive" in codes


def test_opens_exhausted_short_circuits_before_llm_spend(settings: ArcSettings) -> None:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    seed_orders(conn, 175)
    report = run(conn, settings)
    assert not report.failed and report.proposals == []
    # Research never called its LLM: no persona call recorded for it
    n = conn.execute("SELECT COUNT(*) FROM persona_calls WHERE persona = 'research'").fetchone()[0]
    assert n == 0
    assert ("no_trade", "order_budget_exhausted") in decisions(conn)
    m = manifests(conn)
    assert m["research"]["order_budget"] == {"used": 175, "limit": 200, "tier": "opens_exhausted"}
    # a second run the same day posts no second notice (once per day per tier)
    from arc.routines.runs import RoutineStateRepo

    marker = RoutineStateRepo(conn).get("order_budget:notice")
    assert marker == f"{FIXTURE_NOW.date().isoformat()}|opens_exhausted"


def test_exhausted_notice_only_once_per_tier(settings: ArcSettings) -> None:
    from arc.pipeline.budget import budget_notice
    from arc.routines.handlers import JobContext

    conn = open_db(":memory:", copy=False)

    class Ctx:  # the two attributes budget_notice reads
        def __init__(self) -> None:
            self.conn, self.now = conn, FIXTURE_NOW

    ctx = Ctx()
    from arc.budget import OrderBudgetConfig, budget_state

    cfg = OrderBudgetConfig()
    day = FIXTURE_NOW.date()
    assert budget_notice(ctx, budget_state(10, cfg, day=day)) == ""  # type: ignore[arg-type]
    first = budget_notice(ctx, budget_state(100, cfg, day=day))  # type: ignore[arg-type]
    assert first.startswith("order budget: restrictive tier on (100/200")
    assert budget_notice(ctx, budget_state(120, cfg, day=day)) == ""  # type: ignore[arg-type]
    second = budget_notice(ctx, budget_state(175, cfg, day=day))  # type: ignore[arg-type]
    assert "opens stopped at 175" in second
    assert budget_notice(ctx, budget_state(176, cfg, day=day)) == ""  # type: ignore[arg-type]
    third = budget_notice(ctx, budget_state(200, cfg, day=day))  # type: ignore[arg-type]
    assert "EXHAUSTED" in third and "Alpaca dashboard" in third
    # a new day starts over
    nxt = day + dt.timedelta(days=1)
    assert budget_notice(ctx, budget_state(100, cfg, day=nxt)).startswith(  # type: ignore[arg-type]
        "order budget: restrictive"
    )
    assert JobContext is not None


def test_config_only_change_moves_restrict_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """``order_budget_restrict_at=20`` (a yaml/env/Slack change) makes 20 used restrictive."""
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile="margin", order_budget_restrict_at=20)  # type: ignore[call-arg]
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    seed_orders(conn, 20)
    run(conn, s)
    assert manifests(conn)["quant.propose"]["order_budget"]["tier"] == "restrictive"


def test_control_panel_override_reaches_the_budget(settings: ArcSettings) -> None:
    """A Slack `set order_budget.daily_max 130` (D26) lowers the cap the pipeline enforces.

    (30 would be refused: ``restrict_at`` 100 must stay within the open limit.)
    """
    from arc.control.effective import effective_settings
    from arc.control.service import LOCAL_ACTOR, ControlService

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    svc = ControlService(conn, base=settings, now=lambda: FIXTURE_NOW, is_halted=lambda: False)
    refused = svc.set("order_budget.daily_max", "30", actor=LOCAL_ACTOR, source="cli")
    assert refused.outcome == "refused" and "validators rejected" in refused.message
    r = svc.set("order_budget.daily_max", "130", actor=LOCAL_ACTOR, source="cli")
    assert r.outcome == "applied", r  # lowering the cap is the safer direction: no confirm
    eff = effective_settings(conn, base=settings)
    assert eff.order_budget_daily_max == 130
    seed_orders(conn, 105)  # 130 - 25 reserve = 105 -> opens exhausted
    report = run(conn, eff)
    assert report.proposals == []
    assert manifests(conn)["research"]["order_budget"] == {
        "used": 105,
        "limit": 130,
        "tier": "opens_exhausted",
    }


def test_tower_and_monitor_expose_the_budget(settings: ArcSettings) -> None:
    from arc.monitoring.store import HeartbeatRepo
    from arc.tower.data import load_snapshot

    conn = open_db(":memory:", copy=False)
    HeartbeatRepo(conn).record(
        "monitor",
        "ok",
        at=FIXTURE_NOW,
        detail={"order_budget": {"used": 120, "limit": 200, "tier": "restrictive"}},
    )
    snap = load_snapshot(conn, now=FIXTURE_NOW + dt.timedelta(minutes=3))
    ob = snap.ops.order_budget
    assert ob is not None and (ob.used, ob.limit, ob.tier) == (120, 200, "restrictive")
    assert ob.as_of == FIXTURE_NOW
