"""E13.17 (D56): Research exit watchlist + quant.exit exit cases.

Runs the fixture chain (bundled SPY recording + fixture personas). Since E13.15 the
exit path is always on (``personas.exit_path`` removed): the exit block + watchlist;
one ``exit_case`` per triggered position; journal rows; a reply that omits a case
holds. The close path (``risk.exit`` + ``quant.propose``) is in
``tests/test_risk_exit.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import ArcSettings
from arc.control.registry import is_orphaned
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
    AUTO_CHAINS,
    DEFAULT_ROUTINES_PATH,
    PERSONA_CHOICES,
    RoutinesConfig,
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

OPEN_CHAIN = ["quant.open", "risk.open", "quant.revise", "quant.propose", "broker.execute"]


def _settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "account_profile": "margin"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


def _routines(mode: str = "research") -> RoutinesConfig:
    """The shipped routines (E13.15: the exit path is always ``research``)."""
    assert mode == "research"
    return load_routines(DEFAULT_ROUTINES_PATH)


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
    def test_fixed_chains(self) -> None:
        assert AUTO_CHAINS["research"] == (
            "exits.mandatory", "quant.exit", "risk.exit", *OPEN_CHAIN,
        )  # fmt: skip
        assert AUTO_CHAINS["positions.evaluate"] == ("exits.mandatory", "broker.execute")

    def test_shipped_chain_and_steps(self) -> None:
        r = load_routines(DEFAULT_ROUTINES_PATH)
        assert r.personas["research"].chain == list(AUTO_CHAINS["research"])
        q = r.steps["quant.exit"]
        assert q.on_no_change == "skip" and q.writes == ["exit_case", "note"]
        assert {"exit_watchlist", "position_review"} <= set(r.personas["research"].writes or [])
        assert "exit_path" not in PERSONA_CHOICES

    def test_old_switch_ignored(self, tmp_path: Any) -> None:
        text = DEFAULT_ROUTINES_PATH.read_text().replace(
            "personas:\n", "personas:\n  exit_path: deterministic\n", 1
        )
        path = tmp_path / "routines.yaml"
        path.write_text(text)
        r = load_routines(path)
        assert r.personas["research"].chain == list(AUTO_CHAINS["research"])

    def test_experiment_fork_knows_the_step(self) -> None:
        from arc.experiments.runner import STEP_TARGETS

        assert "exits" in STEP_TARGETS["quant.exit"]

    def test_registry(self) -> None:
        assert is_orphaned("personas.exit_path")
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
    def test_writes_watchlist_and_cases(self) -> None:
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
        conn, report = _run(_settings(), _routines(), env, conn=conn)
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
        # quant.exit itself proposes nothing: the close needs Risk's verdict (E13.18)
        rows = conn.execute("SELECT structure_json FROM proposals").fetchall()
        assert len(rows) == before + len(report.proposals)

    def test_missing_judgement_holds_and_hold_item_has_no_case(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid)])
        _quant(env, json.dumps({"cases": []}))
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        (case,) = _kind(conn, "exit_case")
        assert case["recommendation"] == "hold"
        assert case["rationale"] == "no judgement (fail closed to hold)"

        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid, "hold")])
        quant = _quant(env, None)
        conn, report = _run(_settings(), _routines(), env, conn=conn)
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
        conn, report = _run(_settings(), _routines(), env, conn=conn)
        assert not report.failed
        (wl,) = _kind(conn, "exit_watchlist")
        assert wl["items"] == [] and wl["missing"] == [sid]
        assert _codes(conn, "exit")["exit:watch_missing"] == [sid]

    def test_quant_failure_fails_closed(self) -> None:
        conn, env, sid = _book()
        env.llms["research"] = FixtureScalpLLM([_watch_reply(sid)])
        _quant(env, "not json")
        conn, report = _run(_settings(), _routines(), env, conn=conn)
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
        conn, report = _run(_settings(quant_exit_max_cases=1), _routines(), env, conn=conn)
        assert not report.failed and sids
        assert len(_kind(conn, "exit_case")) == 1
        assert _outcome(report, "quant.exit").metrics["exit_over_limit"] == 1


def test_now_is_injected() -> None:
    assert FIXTURE_NOW.tzinfo is not None and isinstance(FIXTURE_NOW, dt.datetime)
