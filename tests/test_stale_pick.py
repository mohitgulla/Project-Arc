"""E5.16 (PLAN D116): the stale-pick cooldown.

A ``ticker|stance`` ranked by Research in 4 consecutive full loops without a tradeable
structure (sized >= 1 contract and ``gate:pass``) is suppressed for the next 10
research runs, then eligible again with its streak reset. Pure replay
(:func:`arc.pipeline.stale_pick.stale_state`), the journal reads, the Research step
wiring (pool, prompt, shortlist ``suppressed``, journal row) and the Slack section.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

from arc.context.ttl import to_db
from arc.control.registry import REGISTRY, Risk, Target, lookup
from arc.journal.reasons import REASON_LABELS, Choice, JournalPersona, ReasonCode, Stage
from arc.journal.store import Recorder
from arc.pipeline.env import FIXTURE_NOW, PipelineEnv
from arc.pipeline.stale_pick import LoopRecord, load_loops, pick_key, stale_state
from arc.routines.config import ResearchStrategySettings, StalePickSettings, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_e59_research_portfolio import _run, _settings, _shortlist

if TYPE_CHECKING:
    import sqlite3

T0 = dt.datetime(2026, 10, 14, 9, 40, tzinfo=ET)
STEP = dt.timedelta(minutes=10)
MU = pick_key("MU", "bullish")


def _loop(
    i: int,
    ranked: tuple[str, ...] = (),
    *,
    full: bool = True,
    tradeable: tuple[str, ...] = (),
) -> LoopRecord:
    return LoopRecord(
        run_id=f"run-{i}",
        at=T0 + STEP * i,
        full=full,
        ranked=frozenset(ranked),
        tradeable=frozenset(tradeable),
    )


def _state(loops: list[LoopRecord], after: int = 4, suppress: int = 10):  # noqa: ANN202
    return stale_state(loops, after_loops=after, suppress_loops=suppress, every=STEP)


# ---------------------------------------------------------------------------
# Pure replay
# ---------------------------------------------------------------------------


def test_fourth_consecutive_full_loop_starts_a_suppression() -> None:
    three = [_loop(i, (MU,)) for i in range(3)]
    assert _state(three).active == {}
    assert _state(three).streaks == {MU: 3}
    four = [*three, _loop(3, (MU,))]
    st = _state(four)
    sup = st.active[MU]
    assert (sup.streak, sup.trigger_run_id) == (4, "run-3")
    # back = the first run after the 10 suppressed ones: 10:10 + 11 x 10 min
    assert sup.back_at == T0 + STEP * 14
    assert sup.line() == (
        "MU bullish — stale: ranked 4 loops, no tradeable structure (back 12:00 ET)"
    )
    assert sup.card_line().startswith("stale: MU bullish — ranked 4 loops")


def test_no_change_and_failed_loops_are_ignored_for_the_streak() -> None:
    loops = [
        _loop(0, (MU,)),
        _loop(1, full=False),  # no_change skip
        _loop(2, (MU,)),
        _loop(3, full=False),  # failed run
        _loop(4, (MU,)),
        _loop(5, (MU,)),
    ]
    assert _state(loops).active[MU].trigger_run_id == "run-5"


def test_a_full_loop_without_the_pick_breaks_the_streak() -> None:
    loops = [_loop(0, (MU,)), _loop(1, (MU,)), _loop(2, (MU,)), _loop(3, ()), _loop(4, (MU,))]
    st = _state(loops)
    assert st.active == {} and st.streaks == {MU: 1}


def test_ranked_suppressed_eligible_cycle() -> None:
    loops = [_loop(i, (MU,)) for i in range(4)]  # trigger at index 3
    # runs 4..13 are suppressed (any status counts, no_change included)
    for i in range(4, 14):
        assert MU in _state(loops).active, i
        loops.append(_loop(i, full=i % 3 == 0))  # Research never sees MU
    assert MU not in _state(loops).active  # run 14: eligible again
    # streak reset: it needs 4 fresh loops to be suppressed again
    for i in range(14, 17):
        loops.append(_loop(i, (MU,)))
        assert MU not in _state(loops).active
    loops.append(_loop(17, (MU,)))
    st = _state(loops)
    assert st.active[MU].trigger_run_id == "run-17"
    assert [s.trigger_run_id for s in st.history] == ["run-3", "run-17"]


def test_a_tradeable_structure_resets_the_streak() -> None:
    loops = [
        _loop(0, (MU,)),
        _loop(1, (MU,)),
        _loop(2, (MU,)),
        _loop(3, (MU,), tradeable=("MU",)),
        _loop(4, (MU,)),
        _loop(5, (MU,)),
        _loop(6, (MU,)),
    ]
    st = _state(loops)
    assert st.active == {} and st.streaks == {MU: 3}
    loops.append(_loop(7, (MU,)))
    assert MU in _state(loops).active


def test_two_stances_of_one_ticker_are_tracked_separately() -> None:
    bear = pick_key("MU", "bearish")
    loops = [_loop(0, (MU,)), _loop(1, (MU,)), _loop(2, (MU, bear)), _loop(3, (MU, bear))]
    st = _state(loops)
    assert set(st.active) == {MU}
    assert st.streaks == {bear: 2}


def test_after_loops_zero_is_off() -> None:
    loops = [_loop(i, (MU,)) for i in range(20)]
    assert _state(loops, after=0).active == {}


# ---------------------------------------------------------------------------
# Journal reads
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _seed_loop(
    c: sqlite3.Connection,
    i: int,
    ranked: dict[str, str],
    *,
    full: bool = True,
    proposals: dict[str, tuple[int, bool]] | None = None,
    job: str = "research",
    base: dt.datetime = T0,
) -> str:
    """One research run (+ its chain's sizing / gate rows) in the journal."""
    at = base + STEP * i
    tag = "" if base == T0 else f"{base:%m%d%H%M}-"
    run_id, chain = f"run-{job}-{tag}{i}", f"chain-{job}-{tag}{i}"
    c.execute(
        "INSERT INTO routine_runs (run_id, job, chain_run_id, reason, scheduled_for, "
        "started_at, status) VALUES (?, ?, ?, 'schedule', ?, ?, 'ok')",
        (run_id, job, chain, to_db(at), to_db(at)),
    )
    j = Recorder(c, at=at, chain_run_id=chain, run_id=run_id)
    if full:
        j.add(JournalPersona.RESEARCH, Stage.SHORTLIST, "session", Choice.NOTED,
              ReasonCode.MARKET_READ)  # fmt: skip
    else:
        j.add(JournalPersona.RESEARCH, Stage.SHORTLIST, "session", Choice.NO_TRADE,
              ReasonCode.LOOP_NO_CHANGE)  # fmt: skip
    for t, stance in ranked.items():
        j.add(JournalPersona.RESEARCH, Stage.SHORTLIST, t, Choice.SELECTED,
              ReasonCode.SHORTLISTED, payload={"ticker": t, "stance": stance})  # fmt: skip
    pj = Recorder(c, at=at, chain_run_id=chain, run_id=f"{run_id}-propose")
    for t, (contracts, passed) in (proposals or {}).items():
        ph = f"hash-{tag}{i}-{t}"
        pj.add(JournalPersona.SIZING, Stage.SIZING, t, Choice.SIZED,
               ReasonCode.SIZING_OK if contracts else ReasonCode.SIZING_CAP_ZERO,
               proposal_hash=ph, payload={"contracts": contracts})  # fmt: skip
        if passed:
            pj.add(JournalPersona.GATE, Stage.GATE, t, Choice.PASSED, ReasonCode.GATE_PASS,
                   proposal_hash=ph)  # fmt: skip
        else:
            pj.add(JournalPersona.GATE, Stage.GATE, t, Choice.FAILED,
                   ReasonCode.GATE_DELTA_CAP, proposal_hash=ph)  # fmt: skip
    c.commit()
    return run_id


def test_load_loops_reads_ranked_full_and_tradeable(conn: sqlite3.Connection) -> None:
    _seed_loop(conn, 0, {"MU": "bullish", "UNH": "bearish"}, proposals={"UNH": (1, True)})
    _seed_loop(conn, 1, {}, full=False)
    _seed_loop(conn, 2, {"MU": "bullish"}, job="research.other")
    loops = load_loops(conn, job="research", now=T0 + STEP * 5)
    assert [(lp.run_id, lp.full) for lp in loops] == [("run-research-0", True),
                                                      ("run-research-1", False)]  # fmt: skip
    assert loops[0].ranked == {MU, pick_key("UNH", "bearish")}
    assert loops[0].tradeable == {"UNH"}
    # the run at `now` itself (the current run) can be excluded
    assert load_loops(conn, job="research", now=T0, exclude_run_id="run-research-0") == []


def test_gate_fail_and_zero_contracts_are_not_tradeable(conn: sqlite3.Connection) -> None:
    for i in range(4):
        _seed_loop(conn, i, {"MU": "bullish", "UNH": "bearish"},
                   proposals={"MU": (0, True), "UNH": (1, False)})  # fmt: skip
    loops = load_loops(conn, job="research", now=T0 + STEP * 4)
    assert all(not lp.tradeable for lp in loops)
    st = stale_state(loops, after_loops=4, suppress_loops=10)
    assert set(st.active) == {MU, pick_key("UNH", "bearish")}


def test_streaks_never_cross_sessions(conn: sqlite3.Connection) -> None:
    yday = T0 - dt.timedelta(days=1)
    for i in range(3):
        _seed_loop(conn, i, {"MU": "bullish"}, base=yday)
    _seed_loop(conn, 0, {"MU": "bullish"})
    loops = load_loops(conn, job="research", now=T0 + STEP)
    assert len(loops) == 1
    assert stale_state(loops, after_loops=4, suppress_loops=10).streaks == {MU: 1}


# ---------------------------------------------------------------------------
# Research step wiring (fixture pipeline day)
# ---------------------------------------------------------------------------


def _routines(after: int = 4) -> Any:
    cfg = load_routines()
    return cfg.model_copy(
        update={
            "director_diversification": cfg.director_diversification.model_copy(
                update={"mode": "strict"}
            ),
            "research": ResearchStrategySettings(
                stale_pick=StalePickSettings(after_loops=after, suppress_loops=10)
            ),
        }
    )


def _seed_fixture_day(c: sqlite3.Connection, ticker: str, stance: str) -> None:
    base = FIXTURE_NOW - STEP * 5  # 15:10 .. 15:40; trigger 15:40 -> back 15:40 + 11 x 10m
    for i in range(4):
        _seed_loop(c, i, {ticker: stance}, base=base)


def _research_prompt(c: sqlite3.Connection) -> str:
    row = c.execute(
        "SELECT prompt_text FROM persona_calls WHERE persona='research' ORDER BY rowid DESC"
    ).fetchone()
    return str(row[0])


def test_research_step_removes_a_stale_pick_and_lists_it() -> None:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import open_db

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    _seed_fixture_day(conn, "NVDA", "bullish")
    conn, rep = _run(_settings(), _routines(), PipelineEnv.fixtures(), conn=conn)
    sl = _shortlist(conn)
    assert "NVDA" not in [i["ticker"] for i in sl["shortlist"]]
    stale = [s for s in sl["suppressed"] if s.startswith("stale: ")]
    assert stale == ["stale: NVDA bullish — ranked 4 loops, no tradeable structure (back 17:30 ET)"]
    prompt = _research_prompt(conn)
    assert "NVDA bullish — stale: ranked 4 loops" in prompt
    rows = conn.execute(
        "SELECT subject, choice, payload FROM decisions WHERE reason_code = ?",
        (ReasonCode.STALE_PICK_SUPPRESSED.value,),
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("NVDA", "rejected")]
    assert json.loads(rows[0][2])["streak"] == 4
    # NVDA was not offered to Research (no candidate / not_ranked row for this run)
    research = next(o for o in rep.outcomes if o.job == "research")
    offered = {
        r[0]
        for r in conn.execute(
            "SELECT subject FROM decisions WHERE run_id = ? AND stage = 'candidate'",
            (research.run_id,),
        )
    }
    assert "NVDA" not in offered and offered
    assert research.metrics["stale_suppressed"] == 1


def test_stale_pick_off_is_byte_identical() -> None:
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import open_db

    prompts = []
    for after in (0, 4):
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        if after:
            _seed_fixture_day(conn, "AMD", "bearish")  # never a pool name: nothing suppressed
        else:
            _seed_fixture_day(conn, "NVDA", "bullish")  # off: ignored
        conn, _ = _run(_settings(), _routines(after), PipelineEnv.fixtures(), conn=conn)
        prompts.append(_research_prompt(conn))
        assert not any(s.startswith("stale: ") for s in _shortlist(conn)["suppressed"])
        n = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE reason_code = ?",
            (ReasonCode.STALE_PICK_SUPPRESSED.value,),
        ).fetchone()[0]
        assert n == (1 if after else 0)  # AMD's suppression start is journaled (not in pool)
    assert prompts[0] == prompts[1]


# ---------------------------------------------------------------------------
# Config, registry, labels, Slack
# ---------------------------------------------------------------------------


def test_strategy_yaml_ships_on_with_owner_values() -> None:
    sp = load_routines().research.stale_pick
    assert (sp.after_loops, sp.suppress_loops, sp.enabled) == (4, 10, True)
    assert StalePickSettings().enabled is False  # model default (no strategy.yaml) = off


def test_registry_keys() -> None:
    for key, path in (
        ("stale_pick_after_loops", ("research", "stale_pick", "after_loops")),
        ("stale_pick_suppress_loops", ("research", "stale_pick", "suppress_loops")),
    ):
        t = REGISTRY[key]
        assert (t.target, t.path, t.risk) == (Target.STRATEGY, path, Risk.NONE)
        assert lookup(".".join(path)) is t


def test_reason_label() -> None:
    assert REASON_LABELS[ReasonCode.STALE_PICK_SUPPRESSED]
    assert set(REASON_LABELS) == set(ReasonCode)


def test_slack_section_title_is_suppressed() -> None:
    from arc.context.kinds import ShortlistPayload
    from arc.slack.digests import research_card

    out = ShortlistPayload(
        shortlist=[],
        market_regime="risk_on",
        session_notes="",
        suppressed=["dedupe: AAPL bearish vertical_spread: proposed Oct 14",
                    "stale: MU bullish — ranked 4 loops, no tradeable structure (back 12:00 ET)"],
    )  # fmt: skip
    text = json.dumps(research_card(out, candidates=3).blocks)
    assert "Suppressed (2)" in text and "by dedupe" not in text
    assert "dedupe: AAPL" in text and "stale: MU bullish" in text
