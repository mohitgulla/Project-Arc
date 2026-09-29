"""E5.9 (D33): portfolio-aware Director, idea dedupe, explicit no-trade, market guard.

Deterministic tests on the bundled SPY recording + fixture personas:

* the portfolio context is built from the audit DB (open structures) and rendered
  into the Director prompt only when the book is not empty;
* an open structure on a candidate in the same stance is dropped before Quant;
* propose refuses a repeat fingerprint within the cooldowns, admits it after the
  cooldown, and re-admits it when spot moved or the regime changed;
* an empty Director shortlist journals ``director_no_trade`` with the stated reason;
* the market guard stops the entry chain (VIX, backwardation, transitional regime,
  missing VIX) before the LLM is called; exits never go through it;
* a 5-minute loop on one day proposes each distinct idea at most once.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import Ttl, to_db
from arc.ingest.llm import FixtureScoutLLM
from arc.ingest.scout import load_fixture_docs
from arc.journal.reasons import ReasonCode
from arc.models import LegIntent, Stance
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.dedupe import (
    DedupeConfig,
    IdeaFingerprint,
    RecentIdea,
    Suppression,
    check_idea,
    fingerprint,
    next_admissible,
    recent_ideas,
    sessions_back,
    strike_bucket,
)
from arc.pipeline.market import price_structure
from arc.pipeline.market_guard import market_guard
from arc.pipeline.portfolio_context import (
    build_portfolio_context,
    load_sectors,
    render_portfolio_context,
)
from arc.pipeline.runner import open_db, run_propose
from arc.routines.config import load_routines
from arc.routines.heartbeat import RecordingNotifier
from arc.store.execution import OpenStructureRepo
from arc.store.repos import CandidateRepo, ProposalRepo
from arc.utils.calendar import ET
from tests.test_routines_e53 import _env

FIXTURES_DIR = Path("arc/pipeline/fixtures/bullish")
IRON_CONDOR = [
    ["SPY261030P00740000", "long", 1],
    ["SPY261030P00745000", "short", 1],
    ["SPY261030C00798000", "short", 1],
    ["SPY261030C00803000", "long", 1],
]
LONG_CALL = [["SPY261030C00775000", "long", 1]]


def _settings(**kw: object) -> ArcSettings:
    base: dict[str, object] = {"_env_file": None, "account_profile": "margin"}
    base.update(kw)
    return ArcSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def routines():  # noqa: ANN201
    return load_routines()


@pytest.fixture
def settings() -> ArcSettings:
    return _settings()


def _director_reply(**over: Any) -> str:
    d = json.loads((FIXTURES_DIR / "director.json").read_text())
    d.update(over)
    return json.dumps(d)


def _run(
    settings: ArcSettings,
    routines: Any,
    env: PipelineEnv,
    *,
    conn: sqlite3.Connection | None = None,
    now: dt.datetime = FIXTURE_NOW,
    notifier: RecordingNotifier | None = None,
):  # noqa: ANN202
    if conn is None:
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
    report = run_propose(
        conn, settings, routines, env, now=now, notifier=notifier or RecordingNotifier()
    )
    return conn, report


def _posted(notes: RecordingNotifier) -> str:
    return json.dumps([t for _, t in notes.posts]) + json.dumps(notes.blocks)


def _outcome(report: Any, job: str) -> Any:
    return next(o for o in report.outcomes if o.job == job)


def _codes(conn: sqlite3.Connection, stage: str | None = None) -> dict[str, list[str]]:
    sql = "SELECT subject, reason_code FROM decisions"
    args: tuple[Any, ...] = ()
    if stage:
        sql += " WHERE stage = ?"
        args = (stage,)
    out: dict[str, list[str]] = {}
    for r in conn.execute(sql + " ORDER BY rowid", args).fetchall():
        out.setdefault(r["reason_code"], []).append(r["subject"])
    return out


def _shortlist(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind='shortlist' ORDER BY created_at DESC, "
        "rowid DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload"])


def _open_structure(
    conn: sqlite3.Connection,
    env: PipelineEnv,
    legs: list[list[Any]],
    *,
    stance: str = "neutral",
    entry: str = "-1.66",
    contracts: int = 14,
    at: dt.datetime = FIXTURE_NOW - dt.timedelta(days=3),
    thesis: str = "range-bound into October",
) -> str:
    st = price_structure(
        env.market, [(o, LegIntent(s), n) for o, s, n in legs], as_of=FIXTURE_NOW.date(), r=0.04
    ).structure
    cid = CandidateRepo(conn).insert(
        ticker="SPY", stance=stance, catalyst_type="macro", confidence=0.7
    )
    n = len(OpenStructureRepo(conn).list_open())
    phash = f"{n:064d}"
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash=phash, structure_json=st.model_dump_json(),
        thesis=thesis, quant_json="{}", sizing_json="{}", expires_at=at.isoformat(),
        created_at=at.isoformat(), day=at.date().isoformat(), ticker="SPY",
        fingerprint=fingerprint("SPY", stance, st, D("770")).key(), spot="770",
        regime="risk_on",
    )  # fmt: skip
    return OpenStructureRepo(conn).open(
        ticker="SPY", open_proposal_hash=phash, candidate_id=cid,
        structure_json=st.model_dump_json(), contracts=contracts, entry_net=D(entry), now=at,
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Fingerprint + cooldown math
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_strike_bucket_is_one_pct_of_spot(self) -> None:
        assert strike_bucket(D("745"), D("770")) == 97  # 745 / 7.70
        assert strike_bucket(D("748"), D("770")) == 97  # same bucket: within 1%
        assert strike_bucket(D("760"), D("770")) == 99
        with pytest.raises(ValueError, match="positive"):
            strike_bucket(D("1"), D("0"))

    def test_fingerprint_key_round_trips(self) -> None:
        env = PipelineEnv.fixtures()
        st = price_structure(
            env.market,
            [(o, LegIntent(s), n) for o, s, n in IRON_CONDOR],
            as_of=FIXTURE_NOW.date(),
            r=0.04,
        ).structure
        fp = fingerprint("spy", "neutral", st, D("770"))
        assert fp.ticker == "SPY" and fp.stance is Stance.NEUTRAL
        assert fp.structure_type == "iron_condor" and fp.expiry_week == "2026-W44"
        assert IdeaFingerprint.parse(fp.key()) == fp
        assert fp.prefix() == "SPY|neutral"

    def test_sessions_back_skips_weekends(self) -> None:
        mon = dt.datetime(2026, 9, 28, 10, 0, tzinfo=ET)
        assert sessions_back(mon, 0).date() == dt.date(2026, 9, 28)
        assert sessions_back(mon, 1).date() == dt.date(2026, 9, 25)  # Friday
        assert sessions_back(mon, 5).date() == dt.date(2026, 9, 21)

    def test_restrictive_tier_doubles_cooldowns(self) -> None:
        from arc.budget import Tier

        s = _settings()
        base = DedupeConfig.from_settings(s, Tier.NORMAL)
        strict = DedupeConfig.from_settings(s, Tier.RESTRICTIVE)
        assert (base.executed_sessions, base.proposed_sessions, base.rejected_sessions) == (5, 1, 1)
        assert (strict.executed_sessions, strict.proposed_sessions) == (10, 2)

    def test_override_on_spot_move_or_regime_change_but_never_for_open(self) -> None:
        cfg = DedupeConfig(reprice_move_pct=0.03)
        fp = IdeaFingerprint(
            ticker="SPY", stance=Stance.NEUTRAL, structure_type="iron_condor",
            expiry_week="2026-W44", strike_bucket=97,
        )  # fmt: skip
        prior = RecentIdea(
            fingerprint=fp.key(), kind=Suppression.PROPOSED, at=FIXTURE_NOW,
            spot=D("770"), regime="risk_on",
        )  # fmt: skip
        held = check_idea(fp, [prior], spot=D("770"), regime="risk_on", cfg=cfg)
        assert held.suppressed and held.reason_code is ReasonCode.DEDUPE_PROPOSED
        moved = check_idea(fp, [prior], spot=D("800"), regime="risk_on", cfg=cfg)
        assert not moved.suppressed and moved.override == "spot_moved"
        assert moved.reason_code is ReasonCode.DEDUPE_OVERRIDE
        regime = check_idea(fp, [prior], spot=D("771"), regime="risk_off", cfg=cfg)
        assert not regime.suppressed and regime.override == "regime_changed"
        open_prior = prior.model_copy(update={"kind": Suppression.EXECUTED, "open_structure": True})
        still = check_idea(fp, [open_prior], spot=D("900"), regime="risk_off", cfg=cfg)
        assert still.suppressed and still.reason_code is ReasonCode.DEDUPE_EXECUTED
        assert check_idea(fp, [], spot=D("770"), regime="risk_on", cfg=cfg).suppressed is False

    def test_executed_outranks_rejected_outranks_proposed(self) -> None:
        cfg = DedupeConfig()
        fp = IdeaFingerprint(
            ticker="SPY", stance=Stance.NEUTRAL, structure_type="iron_condor",
            expiry_week="2026-W44", strike_bucket=97,
        )  # fmt: skip
        mk = lambda k, h: RecentIdea(  # noqa: E731
            fingerprint=fp.key(), kind=k, at=FIXTURE_NOW - dt.timedelta(hours=h)
        )
        v = check_idea(
            fp,
            [mk(Suppression.PROPOSED, 1), mk(Suppression.REJECTED, 2), mk(Suppression.EXECUTED, 3)],
            spot=None,
            regime=None,
            cfg=cfg,
        )
        assert v.kind is Suppression.EXECUTED


# ---------------------------------------------------------------------------
# Portfolio context
# ---------------------------------------------------------------------------


class TestPortfolioContext:
    def test_sectors_yaml_maps_default_universe(self) -> None:
        sectors = load_sectors()
        assert sectors["SPY"] == "broad_market" and sectors["NVDA"] == "technology"
        assert "sectors" not in sectors

    def test_empty_book_renders_one_line(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        env = _env([])
        pc = build_portfolio_context(
            conn, env, settings, info=env.account(), now=FIXTURE_NOW, halted=False,
            budget_tier="normal",
        )  # fmt: skip
        assert pc.empty and pc.positions == [] and pc.aggregates is None
        text = render_portfolio_context(pc, settings)
        assert text.startswith("Portfolio: no open positions.") and "\n" not in text

    def test_open_book_aggregates_and_flags(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        env = _env([])
        sid = _open_structure(conn, env, IRON_CONDOR)
        sid2 = _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2)
        pc = build_portfolio_context(
            conn, env, settings, info=env.account(), now=FIXTURE_NOW, halted=False,
            budget_tier="normal",
        )  # fmt: skip
        assert not pc.empty and {p.structure_id for p in pc.positions} == {sid, sid2}
        ag = pc.aggregates
        assert ag is not None
        assert ag.positions == 2 and ag.max_positions == settings.max_open_positions
        assert ag.by_sector == {"broad_market": 1.0}
        assert "over_concentrated_sector" in ag.flags and ag.flagged_sectors == ["broad_market"]
        assert ag.hhi_underlying == 1.0  # one name
        assert ag.at_cap_underlyings == ["SPY"]  # 4,682 + 2×1,210 > 5% of equity ($5,000)
        by_id = {p.structure_id: p for p in pc.positions}
        condor = by_id[sid]
        assert condor.kind == "iron_condor" and condor.stance is Stance.NEUTRAL
        assert condor.expiry_bucket == "22-45" and condor.sector == "broad_market"
        assert condor.thesis.director == "range-bound into October"
        assert condor.review_source == "computed" and condor.mark_pnl_total is not None
        assert condor.max_loss_pct_equity == pytest.approx(
            condor.max_loss_total / 100_000, abs=1e-4
        )
        text = render_portfolio_context(pc, settings)
        assert sid in text and "Thesis: range-bound into October" in text
        assert "Flags: over_concentrated_sector" in text
        assert "by sector broad_market 100%" in text
        # the top-N cap bounds the block
        assert "1 of 2 shown" in render_portfolio_context(pc, settings, max_positions=1)

    def test_position_review_context_is_preferred_when_fresh(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        env = _env([])
        sid = _open_structure(conn, env, IRON_CONDOR)
        from arc.positions.steps import evaluate
        from tests.test_positions_steps import SPECS
        from tests.test_routines_e53 import _ctx

        evaluate(_ctx(conn, "positions.evaluate", SPECS["positions.evaluate"], FIXTURE_NOW), env)
        snap = ContextStore(conn).snapshot(FIXTURE_NOW + dt.timedelta(minutes=1))
        pc = build_portfolio_context(
            conn, env, settings, info=env.account(), now=FIXTURE_NOW + dt.timedelta(minutes=1),
            halted=False, budget_tier="normal", snapshot=snap,
        )  # fmt: skip
        (pos,) = pc.positions
        assert pos.structure_id == sid and pos.review_source == "position_review"


# ---------------------------------------------------------------------------
# Director stage
# ---------------------------------------------------------------------------


class TestDirectorPortfolioAware:
    def test_empty_book_prompt_has_no_portfolio_block(self, settings, routines) -> None:  # noqa: ANN001
        env = PipelineEnv.fixtures()
        conn, report = _run(settings, routines, env)
        assert not report.failed
        prompt = env.llms["director"].prompts[0]  # type: ignore[attr-defined]
        assert "### Current portfolio\nEquity $100,000.00. 0 open option position(s)." in prompt
        assert "(open book; deterministic, E5.9)" not in prompt
        assert "Recently suggested" not in prompt
        sl = _shortlist(conn)
        assert sl["portfolio_view"] is None and sl["thesis_checks"] == []
        assert sl["no_trade_reason"] is None and sl["market_guard"]["opens_allowed"]
        assert _outcome(report, "director").metrics["open_positions"] == 0
        # the context kind was written for audit even with an empty book
        pc = conn.execute(
            "SELECT payload FROM context_entries WHERE kind='portfolio_context'"
        ).fetchone()
        assert json.loads(pc["payload"])["empty"] is True

    def test_open_book_feeds_prompt_and_drops_held_stance(self, settings, routines) -> None:  # noqa: ANN001
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        env = PipelineEnv.fixtures()
        # SPY bullish, held; the bullish fixture Director ranks SPY bullish again
        sid = _open_structure(
            conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2,
            thesis="range-bound into October",
        )  # fmt: skip
        reply = _director_reply(
            portfolio_view={"verdict": "concentrated", "notes": "all SPY, add elsewhere"},
            thesis_checks=[
                {"structure_id": sid, "status": "intact", "reason": "still range-bound"},
                {"structure_id": "bogus", "status": "weakened", "reason": "ignored"},
            ],
        )
        env.llms["director"] = FixtureScoutLLM([reply])
        conn, report = _run(settings, routines, env, conn=conn)
        assert not report.failed
        prompt = env.llms["director"].prompts[0]  # type: ignore[attr-defined]
        assert "### Current portfolio (open book; deterministic, E5.9)" in prompt
        assert sid in prompt and "portfolio_fit" in prompt
        assert "Recently suggested or held ideas" in prompt
        assert "SPY bullish long_call: held (open position)" in prompt
        sl = _shortlist(conn)
        tickers = {i["ticker"] for i in sl["shortlist"]}
        assert tickers == set()  # the only pick (SPY bullish) is held
        assert sl["portfolio_view"]["verdict"] == "concentrated"
        assert [c["structure_id"] for c in sl["thesis_checks"]] == [sid]
        assert sl["suppressed"] and sl["suppressed"][0].startswith("SPY bullish long_call")
        d = _outcome(report, "director")
        assert d.metrics["dedupe_executed"] == 1 and d.metrics["thesis_checks"] == 1
        codes = _codes(conn, "shortlist")
        assert "SPY" in codes["dedupe_executed"]
        assert codes["portfolio_view"] == ["session"] and codes["thesis_check"] == [sid]
        notes = {
            (r["subject"], json.loads(r["payload"])["topic"])
            for r in conn.execute(
                "SELECT subject, payload FROM context_entries WHERE kind='note'"
            ).fetchall()
        }
        assert ("session", "portfolio_view") in notes and (sid, "thesis_check") in notes
        # Quant never saw SPY: no SPY structure, no SPY proposal
        assert not [p for p in report.proposals if p["ticker"] == "SPY"]

    def test_adds_concentration_is_dropped_only_on_a_flagged_dimension(
        self,
        settings,
        routines,  # noqa: ANN001
    ) -> None:
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        env = PipelineEnv.fixtures()
        # two SPY long calls: sector broad_market 100% and stance bullish 100% (both flagged)
        _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=1)
        _open_structure(
            conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=1,
            at=FIXTURE_NOW - dt.timedelta(days=2),
        )  # fmt: skip
        d = json.loads((FIXTURES_DIR / "director.json").read_text())
        base = d["shortlist"][0]
        d["shortlist"] = [
            {**base, "ticker": "XOM", "stance": "bearish", "rank": 1,
             "portfolio_fit": "adds_concentration"},  # energy, bearish: nothing flagged
            {**base, "ticker": "NVDA", "stance": "bullish", "rank": 2,
             "portfolio_fit": "adds_concentration"},  # bullish stance is flagged -> drop
            {**base, "ticker": "PLTR", "stance": "bullish", "rank": 3,
             "portfolio_fit": "neutral"},  # the Director did not call it concentration
        ]  # fmt: skip
        d["excluded"] = []
        env.llms["director"] = FixtureScoutLLM([json.dumps(d)])
        conn, report = _run(settings, routines, env, conn=conn)
        sl = _shortlist(conn)
        assert [(i["ticker"], i["rank"]) for i in sl["shortlist"]] == [("XOM", 1), ("PLTR", 2)]
        dm = _outcome(report, "director").metrics
        assert dm["drop_concentration"] == 1 and "dedupe_executed" not in dm
        assert _codes(conn, "shortlist")["drop_concentration"] == ["NVDA"]

    def test_at_cap_underlying_is_dropped(self, settings, routines) -> None:  # noqa: ANN001
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        env = PipelineEnv.fixtures()
        _open_structure(conn, env, IRON_CONDOR, contracts=14)  # 4,682 max loss
        _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2)
        # SPY is at its 5% cap; a bearish SPY pick (not held) is still refused
        d = json.loads((FIXTURES_DIR / "director.json").read_text())
        d["shortlist"] = [{**d["shortlist"][0], "stance": "bearish"}]
        d["excluded"] = []
        env.llms["director"] = FixtureScoutLLM([json.dumps(d)])
        conn, report = _run(settings, routines, env, conn=conn)
        assert _shortlist(conn)["shortlist"] == []
        assert _outcome(report, "director").metrics["drop_at_cap"] == 1
        assert _shortlist(conn)["no_trade_reason"] == "no_fit"

    def test_no_trade_reason_is_journalled(self, settings, routines) -> None:  # noqa: ANN001
        env = PipelineEnv.fixtures()
        env.llms["director"] = FixtureScoutLLM(
            [
                _director_reply(
                    shortlist=[],
                    excluded=[],
                    session_notes="nothing sets up",
                    no_trade_reason="no_fit",
                )
            ]
        )
        notes = RecordingNotifier()
        conn, report = _run(settings, routines, env, notifier=notes)
        d = _outcome(report, "director")
        assert d.status == "ok" and "no trade (no_fit)" in d.summary
        assert _shortlist(conn)["no_trade_reason"] == "no_fit"
        assert _codes(conn, "shortlist")["director_no_trade"] == ["session"]
        assert "No trade: No Fit" in _posted(notes)
        assert not report.proposals
        # the chain stopped after the Director: no Quant / Risk LLM calls
        assert [o.job for o in report.outcomes] == ["scout", "director"]
        assert d.metrics["stop_chain"] is True
        assert env.llms["quant"].prompts == [] and env.llms["risk"].prompts == []  # type: ignore[attr-defined]

    def test_empty_shortlist_without_reason_defaults_to_no_fit(
        self,
        settings,
        routines,  # noqa: ANN001
    ) -> None:
        env = PipelineEnv.fixtures()
        env.llms["director"] = FixtureScoutLLM([_director_reply(shortlist=[], excluded=[])])
        conn, _ = _run(settings, routines, env)
        assert _shortlist(conn)["no_trade_reason"] == "no_fit"


# ---------------------------------------------------------------------------
# Market guard
# ---------------------------------------------------------------------------


def _write_vol_term(conn: sqlite3.Connection, **payload: Any) -> None:
    base = {
        "as_of": "2026-09-24", "vix9d": 14.2, "vix": 15.6, "vix3m": 18.1, "vvix": 92.0,
        "ratio_3m_1m": 1.16, "ratio_9d_1m": 0.91, "structure": "contango", "source": "cboe",
    }  # fmt: skip
    base.update(payload)
    ContextStore(conn).write(
        kind="vol_term", subject="market", payload=base, produced_by="test",
        ttl=Ttl.model_validate("30h"), supersede="latest",
        valid_from=FIXTURE_NOW - dt.timedelta(hours=7), now=FIXTURE_NOW - dt.timedelta(hours=7),
    )  # fmt: skip


class TestMarketGuard:
    def test_fixture_vix_keeps_opens_allowed(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        snap = ContextStore(conn).snapshot(FIXTURE_NOW)
        env = PipelineEnv.fixtures()
        g = market_guard(snap, settings, now=FIXTURE_NOW, vix_quote=env.vix_quote)
        assert g.opens_allowed and g.vix is not None and g.vix.source == "market"
        assert g.vix.value == 15.6

    def test_missing_vix_fails_closed(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        snap = ContextStore(conn).snapshot(FIXTURE_NOW)
        g = market_guard(snap, settings, now=FIXTURE_NOW, vix_quote=lambda: None)
        assert not g.opens_allowed and g.reason_code == "market_data_missing"
        relaxed = settings.model_copy(update={"no_trade_require_vix": False})
        assert market_guard(snap, relaxed, now=FIXTURE_NOW, vix_quote=lambda: None).opens_allowed

    def test_vol_term_context_is_preferred_and_gates(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        _write_vol_term(conn, vix=41.0, structure="backwardation")
        snap = ContextStore(conn).snapshot(FIXTURE_NOW)
        g = market_guard(snap, settings, now=FIXTURE_NOW, vix_quote=lambda: (15.0, "x"))
        assert g.vix is not None and g.vix.source == "vol_term" and g.vix.value == 41.0
        assert not g.opens_allowed and g.reason_code == "market_unclear"
        assert any("VIX 41.0 >= 35" in r for r in g.reasons)
        assert any("backwardation" in r for r in g.reasons)
        # backwardation alone, switch off -> allowed
        conn2 = open_db(":memory:", copy=False)
        _write_vol_term(conn2, vix=20.0, structure="backwardation")
        snap2 = ContextStore(conn2).snapshot(FIXTURE_NOW)
        assert not market_guard(snap2, settings, now=FIXTURE_NOW).opens_allowed
        off = settings.model_copy(update={"no_trade_on_backwardation": False})
        assert market_guard(snap2, off, now=FIXTURE_NOW).opens_allowed

    def test_transitional_regime_blocks(self, settings: ArcSettings) -> None:
        conn = open_db(":memory:", copy=False)
        _write_vol_term(conn)
        # a raw regime row (the guard reads only regime.current / regime.stickiness)
        conn.execute(
            "INSERT INTO context_entries (id, kind, subject, payload, schema_version, "
            "produced_by, created_at, valid_from, status) VALUES (?, 'regime', 'SPY', ?, 1, "
            "'test', ?, ?, 'active')",
            (
                "ctx-regime-test",
                json.dumps({"regime": {"current": "sideways", "stickiness": 0.40}}),
                to_db(FIXTURE_NOW),
                to_db(FIXTURE_NOW),
            ),
        )
        conn.commit()
        snap = ContextStore(conn).snapshot(FIXTURE_NOW)
        g = market_guard(snap, settings, now=FIXTURE_NOW)
        assert not g.opens_allowed and g.regime == "sideways" and g.regime_stickiness == 0.40
        assert any("transitional" in r for r in g.reasons)
        loose = settings.model_copy(update={"no_trade_transitional_min_confidence": 0.3})
        assert market_guard(snap, loose, now=FIXTURE_NOW).opens_allowed

    def test_guard_stops_entry_chain_before_llm(self, settings, routines) -> None:  # noqa: ANN001
        env = PipelineEnv.fixtures()
        env.vix_quote = lambda: (45.0, "2026-09-25T15:59:00-04:00")
        notes = RecordingNotifier()
        conn, report = _run(settings, routines, env, notifier=notes)
        d = _outcome(report, "director")
        assert d.status == "ok" and d.metrics["market_guard_blocked"] == 1
        assert not env.llms["director"].prompts  # type: ignore[attr-defined]
        assert _codes(conn, "shortlist")["market_unclear"] == ["session"]
        sl = _shortlist(conn)
        assert sl["shortlist"] == [] and sl["no_trade_reason"] == "unclear"
        assert not sl["market_guard"]["opens_allowed"]
        assert not report.proposals
        assert "No trade: market unclear" in _posted(notes)
        assert [o.job for o in report.outcomes] == ["scout", "director"]
        assert env.llms["quant"].prompts == [] and env.llms["risk"].prompts == []  # type: ignore[attr-defined]

    def test_exits_ignore_the_guard(self, settings: ArcSettings) -> None:
        """The positions chain never consults the market guard (exits always allowed)."""
        import inspect

        from arc.positions import steps as pos_steps

        src = inspect.getsource(pos_steps)
        assert "market_guard" not in src


# ---------------------------------------------------------------------------
# Propose-stage dedupe + the 5-minute loop
# ---------------------------------------------------------------------------


class TestProposeDedupe:
    def test_loop_proposes_each_idea_once_per_day(self, settings, routines) -> None:  # noqa: ANN001
        env = PipelineEnv.fixtures()
        conn, first = _run(settings, routines, env)
        assert [p["ticker"] for p in first.proposals] == ["SPY"]
        for k in range(1, 6):  # five more 5-minute slots
            _, again = _run(
                settings, routines, PipelineEnv.fixtures(), conn=conn,
                now=FIXTURE_NOW + dt.timedelta(minutes=5 * k),
            )  # fmt: skip
            po = _outcome(again, "propose")
            assert po.metrics["proposals"] == 0 and po.metrics["dedupe"] == 1, po.summary
        rows = conn.execute("SELECT COUNT(*) FROM proposals WHERE kind='open'").fetchone()[0]
        assert rows == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE reason_code='dedupe_proposed'"
            ).fetchone()[0]
            == 5
        )

    def test_repeat_admitted_after_cooldown(self, settings, routines) -> None:  # noqa: ANN001
        """Session windows: ``N`` = this session plus the N previous ones."""
        conn, _ = _run(settings, routines, PipelineEnv.fixtures())
        cfg = DedupeConfig.from_settings(settings)
        same_day = FIXTURE_NOW + dt.timedelta(minutes=5)
        (prior,) = recent_ideas(conn, now=same_day, cfg=cfg)
        assert prior.kind is Suppression.PROPOSED
        monday = FIXTURE_NOW + dt.timedelta(days=3)  # Mon 9/28: within 1 session of Friday
        assert [r.kind for r in recent_ideas(conn, now=monday, cfg=cfg)] == [Suppression.PROPOSED]
        tuesday = monday + dt.timedelta(days=1)
        assert recent_ideas(conn, now=tuesday, cfg=cfg) == []
        assert next_admissible(same_day, prior, cfg) == tuesday.date()
        # the restrictive tier doubles the window (2 sessions): Tuesday is still held
        from arc.budget import Tier

        strict = DedupeConfig.from_settings(settings, Tier.RESTRICTIVE)
        assert [r.kind for r in recent_ideas(conn, now=tuesday, cfg=strict)] == [
            Suppression.PROPOSED
        ]
        assert recent_ideas(conn, now=tuesday + dt.timedelta(days=1), cfg=strict) == []
        # an executed idea: held while open (no window), then 5 sessions from the close
        (p,) = conn.execute("SELECT proposal_hash, candidate_id, structure_json FROM proposals")
        sid = OpenStructureRepo(conn).open(
            ticker="SPY", open_proposal_hash=p["proposal_hash"], candidate_id=p["candidate_id"],
            structure_json=p["structure_json"], contracts=1, entry_net=D("-1.66"), now=FIXTURE_NOW,
        )  # fmt: skip
        far = FIXTURE_NOW + dt.timedelta(days=30)
        (held,) = recent_ideas(conn, now=far, cfg=cfg)
        assert held.kind is Suppression.EXECUTED and held.open_structure and held.ref == sid
        closed_at = FIXTURE_NOW + dt.timedelta(days=10)  # Mon 10/5
        conn.execute(
            "UPDATE open_structures SET status='closed', closed_at=? WHERE id=?",
            (to_db(closed_at), sid),
        )
        conn.commit()
        (done,) = recent_ideas(conn, now=closed_at + dt.timedelta(days=7), cfg=cfg)  # Mon 10/12
        assert done.kind is Suppression.EXECUTED and not done.open_structure
        assert recent_ideas(conn, now=closed_at + dt.timedelta(days=8), cfg=cfg) == []  # 10/13

    def test_spot_move_overrides_cooldown(self, settings, routines) -> None:  # noqa: ANN001
        conn, _ = _run(settings, routines, PipelineEnv.fixtures())
        # pretend the earlier proposal was priced 5% lower: same fingerprint, spot moved
        conn.execute("UPDATE proposals SET spot = '730' WHERE kind='open'")
        conn.commit()
        _, r = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=FIXTURE_NOW + dt.timedelta(minutes=5),
        )  # fmt: skip
        assert _outcome(r, "propose").metrics["proposals"] == 1
        assert _codes(conn, "propose")["dedupe_override"] == ["SPY"]

    def test_regime_change_overrides_cooldown(self, settings, routines) -> None:  # noqa: ANN001
        conn, _ = _run(settings, routines, PipelineEnv.fixtures())
        conn.execute("UPDATE proposals SET regime = 'risk_off' WHERE kind='open'")
        conn.commit()
        _, r = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=FIXTURE_NOW + dt.timedelta(minutes=5),
        )  # fmt: skip
        assert _outcome(r, "propose").metrics["proposals"] == 1

    def test_owner_rejection_holds_rejected_cooldown(self, settings, routines) -> None:  # noqa: ANN001
        conn, first = _run(settings, routines, PipelineEnv.fixtures())
        (p,) = first.proposals
        conn.execute(
            "INSERT INTO approval_requests (proposal_hash, ticker, day, proposal_json, status, "
            "decided_by, channel, created_at, expires_at, decided_at) VALUES "
            "(?, 'SPY', '2026-09-25', '{}', 'rejected', 'U0C5KUMH28G', 'log', ?, ?, ?)",
            (p["proposal_hash"], to_db(FIXTURE_NOW), to_db(FIXTURE_NOW), to_db(FIXTURE_NOW)),
        )
        conn.commit()
        priors = recent_ideas(
            conn,
            now=FIXTURE_NOW + dt.timedelta(minutes=5),
            cfg=DedupeConfig.from_settings(settings),
        )
        assert [r.kind for r in priors] == [Suppression.REJECTED]
        _, r = _run(
            settings, routines, PipelineEnv.fixtures(), conn=conn,
            now=FIXTURE_NOW + dt.timedelta(minutes=5),
        )  # fmt: skip
        assert _outcome(r, "propose").metrics["proposals"] == 0
        assert _codes(conn, "propose")["dedupe_rejected"] == ["SPY"]
        # a system (TTL) expiry is only a `proposed` idea, not an owner rejection
        conn.execute("UPDATE approval_requests SET decided_by = 'arc:ttl'")
        conn.commit()
        (again,) = recent_ideas(
            conn,
            now=FIXTURE_NOW + dt.timedelta(minutes=5),
            cfg=DedupeConfig.from_settings(settings),
        )
        assert again.kind is Suppression.PROPOSED

    def test_manual_runs_have_a_chain_key(self, settings, routines) -> None:  # noqa: ANN001
        conn, _ = _run(settings, routines, PipelineEnv.fixtures())
        row = conn.execute("SELECT chain_run_id FROM proposals WHERE kind='open'").fetchone()
        assert row["chain_run_id"]  # chain runs stamp the id; manual runs a `manual-<day>-<run>`


# ---------------------------------------------------------------------------
# Property: inside the cooldown the same fingerprint is never proposed twice
# ---------------------------------------------------------------------------


@given(
    n_runs=st.integers(min_value=2, max_value=8),
    minutes=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=8),
    executed=st.integers(min_value=0, max_value=10),
    proposed=st.integers(min_value=1, max_value=10),
    rejected=st.integers(min_value=0, max_value=10),
    spot_bps=st.integers(min_value=-250, max_value=250),
)
@hsettings(max_examples=40, deadline=None)
def test_same_fingerprint_never_twice_inside_cooldown(
    n_runs: int, minutes: list[int], executed: int, proposed: int, rejected: int, spot_bps: int
) -> None:
    """Simulated loop: one proposal in the DB; every later run within the proposed
    cooldown (spot inside the reprice band, regime unchanged) is suppressed."""
    cfg = DedupeConfig(
        executed_sessions=executed,
        proposed_sessions=proposed,
        rejected_sessions=rejected,
        reprice_move_pct=0.03,
    )
    fp = IdeaFingerprint(
        ticker="SPY", stance=Stance.NEUTRAL, structure_type="iron_condor",
        expiry_week="2026-W44", strike_bucket=100,
    )  # fmt: skip
    prior = RecentIdea(
        fingerprint=fp.key(), kind=Suppression.PROPOSED, at=FIXTURE_NOW, spot=D("770"),
        regime="risk_on",
    )  # fmt: skip
    spot = D("770") * (1 + D(spot_bps) / 10_000)
    proposals = 0
    for _ in range(n_runs):
        v = check_idea(fp, [prior], spot=spot, regime="risk_on", cfg=cfg)
        if not v.suppressed:
            proposals += 1
    assert proposals == 0  # |move| <= 2.5% < 3% band -> always a repeat


@given(
    bucket=st.integers(min_value=50, max_value=150),
    week=st.integers(min_value=1, max_value=52),
    stance=st.sampled_from(list(Stance)),
    stype=st.sampled_from(["iron_condor", "vertical_spread", "long_call", "strangle"]),
)
@hsettings(max_examples=60, deadline=None)
def test_fingerprint_key_parse_round_trip(
    bucket: int, week: int, stance: Stance, stype: str
) -> None:
    fp = IdeaFingerprint(
        ticker="NVDA", stance=stance, structure_type=stype, expiry_week=f"2026-W{week:02d}",
        strike_bucket=bucket,
    )  # fmt: skip
    assert IdeaFingerprint.parse(fp.key()) == fp


# ---------------------------------------------------------------------------
# Migration 016: two different ideas on one ticker in one day are both allowed
# ---------------------------------------------------------------------------


def test_two_ideas_one_ticker_one_day_are_both_allowed() -> None:
    conn = open_db(":memory:", copy=False)
    env = PipelineEnv.fixtures()
    for n, legs in enumerate((IRON_CONDOR, LONG_CALL)):
        st_ = price_structure(
            env.market, [(o, LegIntent(s), q) for o, s, q in legs], as_of=FIXTURE_NOW.date(), r=0.04
        ).structure
        cid = CandidateRepo(conn).insert(
            ticker="SPY", stance="neutral", catalyst_type="macro", confidence=0.6
        )
        ProposalRepo(conn).insert(
            candidate_id=cid, proposal_hash=f"{n:064x}", structure_json=st_.model_dump_json(),
            thesis="t", quant_json="{}", sizing_json="{}", expires_at=to_db(FIXTURE_NOW),
            created_at=to_db(FIXTURE_NOW), day="2026-09-25", ticker="SPY",
            chain_run_id=f"chain-{n}",  # a later loop slot (one open per ticker per chain)
            fingerprint=fingerprint("SPY", "neutral", st_, D("770")).key(),
            spot="770", regime="risk_on",
        )  # fmt: skip
    rows = conn.execute(
        "SELECT fingerprint FROM proposals WHERE day='2026-09-25' AND ticker='SPY'"
    ).fetchall()
    assert len(rows) == 2 and len({r["fingerprint"] for r in rows}) == 2
    # the (day, ticker) unique index is gone; (chain_run_id, ticker) is the new idempotency key
    names = [r["name"] for r in conn.execute("PRAGMA index_list(proposals)")]
    assert "idx_proposals_day_ticker" not in names
    assert "idx_proposals_chain_ticker" in names
    with pytest.raises(sqlite3.IntegrityError):  # same chain run, same ticker -> refused
        ProposalRepo(conn).insert(
            candidate_id=cid, proposal_hash=f"{7:064x}", structure_json="{}", thesis="t",
            quant_json="{}", sizing_json="{}", expires_at=to_db(FIXTURE_NOW),
            created_at=to_db(FIXTURE_NOW), day="2026-09-25", ticker="SPY", chain_run_id="chain-0",
        )  # fmt: skip


# ---------------------------------------------------------------------------
# E6.4 fixture book (3 structures) as the portfolio context
# ---------------------------------------------------------------------------


def test_fixture_book_portfolio_context(settings: ArcSettings) -> None:
    from arc.positions.cli import BOOK_FIXTURE

    conn = open_db(":memory:", copy=False)
    env = _env([])
    data = json.loads(BOOK_FIXTURE.read_text())
    ids = []
    for p in data["positions"]:
        stance = "bullish" if p["id"] != "fx-bull-put" else "bullish"
        ids.append(
            _open_structure(
                conn,
                env,
                p["legs"],
                stance=stance,
                entry=str(p["entry_net"]),
                contracts=p["contracts"],
                thesis=f"thesis for {p['id']}",
            )  # fmt: skip
        )
    pc = build_portfolio_context(
        conn, env, settings, info=env.account(), now=FIXTURE_NOW, halted=False,
        budget_tier="normal",
    )  # fmt: skip
    assert len(pc.positions) == 3 and pc.aggregates is not None
    ag = pc.aggregates
    assert ag.positions == 3
    assert ag.by_underlying == {"SPY": 1.0} and ag.hhi_underlying == 1.0
    assert ag.by_stance == {"bullish": 1.0} and ag.flagged_stances == ["bullish"]
    assert "stance_skew" in ag.flags and "over_concentrated_sector" in ag.flags
    assert "expiry_cluster" in ag.flags and ag.by_expiry_bucket == {"22-45": 1.0}
    assert ag.total_max_loss == pytest.approx(sum(p.max_loss_total for p in pc.positions))
    assert ag.greeks_source == "as_opened"  # no priced portfolio passed: as-opened Greeks
    assert {p.thesis.director for p in pc.positions} == {
        f"thesis for {i}" for i in ("fx-bull-put", "fx-call-debit", "fx-long-call")
    }
    kinds = sorted(p.kind or "" for p in pc.positions)
    assert kinds == ["long_call", "vertical_credit", "vertical_debit"]
    text = render_portfolio_context(pc, settings)
    assert "3 of 8 positions open" in text and "Flags: " in text
    assert all(sid in text for sid in ids)
