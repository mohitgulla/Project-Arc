"""E13.17 (D56): Research exit watchlist + quant.exit exit cases behind
``personas.exit_path`` (deterministic | shadow | research).

Runs the fixture chain (bundled SPY recording + fixture personas):

* ``deterministic`` (default): byte-identical Research prompt (no exit block, the
  E5.9 thesis-check wording), no ``exit_watchlist`` write, no ``quant.exit`` step;
* ``shadow``: the exit block + watchlist; one ``exit_case`` per triggered position;
  journal rows; **no proposal** from the exit path; a reply that omits a case holds;
* ``research`` adds the close path (E13.18, ``tests/test_risk_exit.py``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.control.registry import lookup
from arc.ingest.llm import FixtureScalpLLM
from arc.ingest.scalp import load_fixture_docs
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.portfolio_context import (
    build_portfolio_context,
    exit_block_entries,
    render_exit_block,
)
from arc.pipeline.runner import open_db
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    PERSONA_CHOICES,
    RoutinesConfig,
    chain_for,
    load_routines,
)
from tests.test_e59_research_portfolio import (
    FIXTURES_DIR,
    LONG_CALL,
    _open_structure,
    _outcome,
    _run,
)
from tests.test_routines_e53 import _env

if TYPE_CHECKING:
    import sqlite3

OFF_CHAIN = ["quant.open", "risk.open", "quant.propose", "broker.execute"]


def _settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "account_profile": "margin"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


def _routines(mode: str) -> RoutinesConfig:
    return load_routines(overrides={("personas", "exit_path"): mode})


def _research_reply(**over: Any) -> str:
    d = json.loads((FIXTURES_DIR / "research.json").read_text())
    d.update(over)
    return json.dumps(d)


def _codes(conn: sqlite3.Connection, stage: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT subject, reason_code FROM decisions WHERE stage = ? ORDER BY rowid", (stage,)
    ):
        out.setdefault(r["reason_code"], []).append(r["subject"])
    return out


def _kind(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = ? ORDER BY rowid", (kind,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _book() -> tuple[sqlite3.Connection, PipelineEnv, str]:
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures()
    sid = _open_structure(
        conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2,
        thesis="AI capex keeps SPY bid",
    )  # fmt: skip
    return conn, env, sid


def _quant(env: PipelineEnv, exit_reply: str | None) -> FixtureScalpLLM:
    """quant.exit uses the Quant model (LLM_PERSONA): its reply goes first, then the
    fixture quant.open reply."""
    quant_open = (FIXTURES_DIR / "quant.json").read_text()
    llm = FixtureScalpLLM([exit_reply, quant_open] if exit_reply is not None else [quant_open])
    env.llms["quant"] = llm
    return llm


def _watch_reply(sid: str, action: str = "review") -> str:
    return _research_reply(
        portfolio_view={"verdict": "concentrated", "notes": "all SPY"},
        exit_watchlist=[
            {
                "structure_id": sid,
                "ticker": "WRONG",  # code takes the ticker from the book
                "action": action,
                "thesis_status": "broken" if action == "review" else "intact",
                "evidence": ["[st_9] capex guide cut", "IV rank 0.81"],
                "reason": "capex guide cut breaks the thesis",
            },
            {
                "structure_id": "bogus",
                "ticker": "X",
                "action": "review",
                "thesis_status": "weakened",
                "evidence": [],
                "reason": "dropped",
            },
        ],
    )


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_chain_for_exit_path(self) -> None:
        assert chain_for("research", {}) == OFF_CHAIN
        assert chain_for("research", {}, {"exit_path": "deterministic"}) == OFF_CHAIN
        assert chain_for("research", {}, {"exit_path": "shadow"}) == ["quant.exit", *OFF_CHAIN]
        # E13.18: research adds the mandatory floor and Risk's exit review
        assert chain_for("research", {}, {"exit_path": "research"}) == [
            "exits.mandatory", "quant.exit", "risk.exit", *OFF_CHAIN,
        ]  # fmt: skip
        assert chain_for("research", {"quant_risk_loop": True}, {"exit_path": "shadow"}) == [
            "quant.exit", "quant.open", "risk.open", "quant.revise", "quant.propose",
            "broker.execute",
        ]  # fmt: skip
        # the positions chain keeps today's steps under shadow (E13.18: research differs)
        assert chain_for("positions.evaluate", {}, {"exit_path": "shadow"})[0] == "quant.exits"

    def test_shipped_default_is_deterministic(self) -> None:
        r = load_routines(DEFAULT_ROUTINES_PATH)
        assert r.exit_path.mode == "deterministic" and r.exit_path.watch is False
        assert r.personas["research"].chain == OFF_CHAIN
        assert _routines("shadow").personas["research"].chain[0] == "quant.exit"
        assert _routines("research").exit_path.watch is True
        q = r.steps["quant.exit"]
        assert q.on_no_change == "skip" and q.writes == ["exit_case", "note"]
        assert {"exit_watchlist", "position_review"} <= set(r.personas["research"].writes or [])
        assert PERSONA_CHOICES["exit_path"] == ("deterministic", "shadow", "research")

    def test_bad_value_rejected(self) -> None:
        with pytest.raises(ValueError, match="exit_path"):
            _routines("roll")

    def test_experiment_fork_knows_the_step(self) -> None:
        from arc.experiments.runner import STEP_TARGETS

        assert "exits" in STEP_TARGETS["quant.exit"]

    def test_registry(self) -> None:
        t = lookup("personas.exit_path")
        assert t.choices == ("deterministic", "shadow", "research")
        assert lookup("exit_path").key == t.key
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.exit_block_max_chars_per_position == 600
        assert (s.quant_exit_max_cases, s.quant_exit_case_max_chars) == (8, 900)


# ---------------------------------------------------------------------------
# facts + exit block
# ---------------------------------------------------------------------------


class TestExitBlock:
    def test_facts_and_render_cap(self) -> None:
        conn, env, sid = _book()
        settings = _settings()
        env2 = _env([])
        pc = build_portfolio_context(
            conn, env2, settings, info=env2.account(), now=FIXTURE_NOW, halted=False,
            budget_tier="normal", facts=True,
        )  # fmt: skip
        (pos,) = pc.positions
        assert pos.facts is not None and pos.structure_id == sid
        assert pos.facts.stories_fresh >= 0
        text = render_exit_block(pc, settings)
        assert sid in text and "facts:" in text and "AI capex keeps SPY bid" in text
        for _, entry in exit_block_entries(pc, settings):
            assert len(entry) <= settings.exit_block_max_chars_per_position
        tight = _settings(exit_block_max_chars_per_position=200)
        for _, entry in exit_block_entries(pc, tight):
            assert len(entry) <= 200
        # off path: no facts
        off = build_portfolio_context(
            conn, env2, settings, info=env2.account(), now=FIXTURE_NOW, halted=False,
            budget_tier="normal",
        )  # fmt: skip
        assert off.positions[0].facts is None
        assert "facts" not in json.loads(off.model_dump_json())["positions"][0] or (
            json.loads(off.model_dump_json())["positions"][0]["facts"] is None
        )


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


class TestChain:
    def test_deterministic_prompt_and_writes_unchanged(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_research_reply()])
        conn, report = _run(_settings(), _routines("deterministic"), env, conn=conn)
        assert not report.failed
        prompt = env.llms["research"].prompts[0]  # type: ignore[attr-defined]
        assert "exit watch" not in prompt and "exit_watchlist" not in prompt
        assert "`thesis_checks` entry per open structure" in prompt
        assert _kind(conn, "exit_watchlist") == [] and _kind(conn, "exit_case") == []
        assert "quant.exit" not in {o.job for o in report.outcomes}
        assert _kind(conn, "shortlist")[-1].get("exit_watchlist_counts") is None

    def test_deterministic_prompt_identical_to_default_routines(self) -> None:
        """The off value and today's shipped config build the same Research prompt."""
        prompts = []
        for routines in (load_routines(DEFAULT_ROUTINES_PATH), _routines("deterministic")):
            conn, env, _ = _book()
            env.llms["research"] = FixtureScalpLLM([_research_reply()])
            _run(_settings(), routines, env, conn=conn)
            prompts.append(env.llms["research"].prompts[0])  # type: ignore[attr-defined]
        ids = re.compile(r'"id": "[0-9a-f]{16}"|os-[0-9a-f]{16}|(ctx|st)-[0-9a-f]+')
        assert ids.sub("<id>", prompts[0]) == ids.sub("<id>", prompts[1])

    @pytest.mark.parametrize("mode", ["shadow", "research"])
    def test_shadow_writes_watchlist_and_cases_never_a_proposal(self, mode: str) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid)])
        quant_exit = _quant(
            env,
            json.dumps(
                {
                    "cases": [
                        {
                            "structure_id": sid,
                            "recommendation": "close",
                            "rationale": "thesis broken; remaining EV small",
                        }
                    ]
                }
            ),
        )
        before = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        conn, report = _run(_settings(), _routines(mode), env, conn=conn)
        assert not report.failed, report
        prompt = env.llms["research"].prompts[0]  # type: ignore[attr-defined]
        assert "### Open positions (exit watch; facts by code, E13.17)" in prompt
        assert "exit_watchlist" in prompt and sid in prompt
        (wl,) = _kind(conn, "exit_watchlist")
        assert [i["structure_id"] for i in wl["items"]] == [sid]  # bogus dropped
        assert wl["items"][0]["ticker"] == "SPY" and wl["positions_seen"] == 1
        assert wl["missing"] == []
        sl = _kind(conn, "shortlist")[-1]
        assert sl["exit_watchlist_counts"] == {"hold": 0, "review": 1}
        assert [c["status"] for c in sl["thesis_checks"]] == ["invalidated"]  # broken
        codes = _codes(conn, "exit")
        assert codes["exit:watch_review"] == [sid]
        # the reviews Research saw are stored for quant.exit
        assert any(p["structure_id"] == sid for p in _kind(conn, "position_review"))
        q = _outcome(report, "quant.exit")
        assert q.status == "ok", q.summary
        (case,) = _kind(conn, "exit_case")
        assert case["structure_id"] == sid and case["recommendation"] == "close"
        assert case["triggers"][0]["kind"] == "research_review"
        assert case["facts"]["thesis_status"] == "broken"
        assert case["options"] == ["hold", "close"]
        assert codes.get("exit:case_built") or _codes(conn, "exit")["exit:case_built"] == [sid]
        assert "Exit cases (deterministic facts" in quant_exit.prompts[0]
        # shadow: nothing proposed by the exit path
        rows = conn.execute("SELECT structure_json FROM proposals").fetchall()
        assert len(rows) == before + len(report.proposals)
        assert not [p for p in report.proposals if p.get("kind") == "close"]
        assert (
            conn.execute(
                "SELECT exit_proposal_hash FROM open_structures WHERE id = ?", (sid,)
            ).fetchone()[0]
            is None
        )

    def test_missing_judgement_holds_and_hold_item_has_no_case(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid)])
        _quant(env, json.dumps({"cases": []}))
        conn, report = _run(_settings(), _routines("shadow"), env, conn=conn)
        assert not report.failed
        (case,) = _kind(conn, "exit_case")
        assert case["recommendation"] == "hold"
        assert case["rationale"] == "no judgement (fail closed to hold)"

        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid, "hold")])
        quant = _quant(env, None)
        conn, report = _run(_settings(), _routines("shadow"), env, conn=conn)
        assert not report.failed
        codes = _codes(conn, "exit")
        assert codes["exit:watch_hold"] == [sid]
        # the fixture book fires no discretionary signal: no case, no exit LLM call
        assert _kind(conn, "exit_case") == []
        assert not any("Exit cases (deterministic facts" in p for p in quant.prompts)
        assert _outcome(report, "quant.exit").status == "skipped"
        assert codes["exit:case_skipped"] == [sid]

    def test_missing_item_journaled(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM(
            [_research_reply(portfolio_view={"verdict": "balanced", "notes": "ok"})]
        )
        _quant(env, None)
        conn, report = _run(_settings(), _routines("shadow"), env, conn=conn)
        assert not report.failed
        (wl,) = _kind(conn, "exit_watchlist")
        assert wl["items"] == [] and wl["missing"] == [sid]
        assert _codes(conn, "exit")["exit:watch_missing"] == [sid]

    def test_quant_failure_fails_closed(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid)])
        _quant(env, "not json")
        conn, report = _run(_settings(), _routines("shadow"), env, conn=conn)
        assert not report.failed
        (case,) = _kind(conn, "exit_case")
        assert case["recommendation"] == "hold"
        assert case["rationale"].startswith("Quant call failed (fail closed to hold)")

    def test_case_limit(self) -> None:
        conn, env, _ = _book()
        sids = [_open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10")]
        all_sids = [r[0] for r in conn.execute("SELECT id FROM open_structures ORDER BY id")]
        reply = _research_reply(
            portfolio_view={"verdict": "concentrated", "notes": "x"},
            exit_watchlist=[
                {
                    "structure_id": s, "ticker": "SPY", "action": "review",
                    "thesis_status": "weakened", "evidence": [], "reason": "r",
                }
                for s in all_sids
            ],
        )  # fmt: skip
        env.llms["research"] = FixtureScalpLLM([reply])
        _quant(env, json.dumps({"cases": []}))
        conn, report = _run(_settings(quant_exit_max_cases=1), _routines("shadow"), env, conn=conn)
        assert not report.failed and sids
        assert len(_kind(conn, "exit_case")) == 1
        assert _outcome(report, "quant.exit").metrics["exit_over_limit"] == 1


def test_now_is_injected() -> None:
    assert FIXTURE_NOW.tzinfo is not None and isinstance(FIXTURE_NOW, dt.datetime)
