"""E13.7 (D56): the daily Scout persona.

Covers the input budget split and presence lines, the output validation (origins,
discovery subset / higher tier / floor 0.6 / loose screen / cap), candidates with
``feed=scout`` and their tier floors, ``discovery_fill`` and the ``coverage:scout``
condition, the off switch (no LLM call), the reads/writes contract, the config and
registry wiring, and the Scout card.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.kinds import KINDS, CandidatePayload, ScoutReadPayload
from arc.context.store import ContextStore
from arc.control.registry import REGISTRY, TunableError, lookup
from arc.ingest.llm import LLMResult, ScalpLLMError
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.llm_routing import Persona
from arc.monitoring import checks
from arc.monitoring.alerts import SCOUT_COVERAGE, _slot_coverage
from arc.personas.schemas import ScoutOutput
from arc.personas.scout import (
    build_scout_prompt,
    discovery_members,
    origin_id,
    scout_input_from_context,
    validate_calls,
)
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    PERSONA_FLAGS,
    TIMELINE_PERSONAS,
    RoutinesConfig,
    load_routines,
)
from arc.routines.handlers import (
    BUILTIN_HANDLERS,
    ContractViolationError,
    JobContext,
    scout_persona,
)
from arc.slack.digests import scout_card
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics, ScreenResult
from arc.universe.tiers import Tier, TierMember, UniverseTierPayload, tier_membership
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 6, 6, 0, tzinfo=ET)
DAY = NOW.date()
D56 = {"universe": {("tiers", "model"): "d56"}}
CHANNELS = [
    {"slug": "fxevolution", "label": "FX", "category": "youtube_macro"},
    {"slug": "bravos", "label": "Bravos", "category": "youtube_macro"},
    {"slug": "stockedup", "label": "StockedUp", "category": "youtube_micro"},
    {"slug": "tradebrigade", "label": "TradeBrigade", "category": "youtube_micro"},
    {"slug": "arete", "label": "Arete", "category": "youtube_micro"},
]
BRIEFS_OPTIONS = {
    "channels": [
        {
            "slug": c["slug"],
            "channel": f"UC{c['slug']}",
            "label": c["label"],
            "category": c["category"],
        }
        for c in CHANNELS
    ]
}
MOMENTUM = ["LRCX", "AMAT", "SNDK"]


def _settings(**kw: Any) -> ArcSettings:
    s = ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]
    s._yaml_overrides = dict(D56)  # noqa: SLF001
    return s


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    yield
    mock.patch.stopall()


def _brief(slug: str, published: dt.datetime, calls: list[str] | None = None) -> dict[str, Any]:
    return {
        "brief_id": f"b-{slug}",
        "channel_slug": slug,
        "video_id": f"v-{slug}",
        "video_url": f"https://youtube.com/watch?v={slug}",
        "title": f"{slug} morning " + "x" * 9000,
        "published_at": published.isoformat(),
        "applies_to_session": DAY.isoformat(),
        "guidelines_version": "v1",
        "tickers_mentioned": calls or [],
    }


def _write_brief(
    conn: sqlite3.Connection, slug: str, *, age: dt.timedelta = dt.timedelta(hours=4)
) -> None:
    at = NOW - age
    ContextStore(conn).write(
        kind="channel_brief",
        subject=slug,
        payload=_brief(slug, at),
        produced_by="youtube.briefs",
        ttl="3d",
        valid_from=at,
        now=at,
    )


def _write_options(conn: sqlite3.Connection, *, age: dt.timedelta = dt.timedelta(hours=11)) -> None:
    at = NOW - age
    store = ContextStore(conn)
    store.write(
        kind="options_daily",
        subject="market",
        payload={
            "as_of": "2026-10-05",
            "fetched_at": at.isoformat(),
            "ratios": [{"segment": "total", "ratio": 0.91}, {"segment": "equity", "ratio": 0.62}],
            "open_interest": [{"product": "spx", "call_oi": 100, "put_oi": 200, "total_oi": 300}],
            "url": "https://cboe.example/stats",
        },
        produced_by="options_daily",
        ttl="3d",
        valid_from=at,
        now=at,
    )
    store.write(
        kind="vx_curve",
        subject="market",
        payload={
            "as_of": "2026-10-05",
            "fetched_at": at.isoformat(),
            "points": [
                {"symbol": "VX/V6", "expiry": "2026-10-21", "settle": 17.1},
                {"symbol": "VX/X6", "expiry": "2026-11-18", "settle": 18.0},
            ],
            "front": 17.1,
            "second": 18.0,
            "back": 18.0,
            "slope_1_2_pct": 5.3,
            "shape": "contango",
            "url": "https://cfe.example/vx",
        },
        produced_by="vix_futures",
        ttl="3d",
        valid_from=at,
        now=at,
    )
    # vol_term deliberately absent: "no fresh info", never blocks


def _write_tier(conn: sqlite3.Connection, tier: Tier, names: list[str]) -> None:
    at = NOW - dt.timedelta(hours=1)
    ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=UniverseTierPayload(
            tier=tier,
            members=[
                TierMember(ticker=t, tier=tier, rank=i, source="t", as_of=DAY)
                for i, t in enumerate(names, 1)
            ],
            fetched_at=at,
            source="test",
        ),
        produced_by="test",
        ttl="8d",
        valid_from=at,
        now=at,
    )


def _routines(*, on: bool = True, writes: list[str] | None = None) -> RoutinesConfig:
    return RoutinesConfig.model_validate(
        {
            "sources": {"youtube.briefs": {"schedule": ["02:00"], **BRIEFS_OPTIONS}},
            "personas": {
                "scout": {
                    "enabled": on,
                    "schedule": ["06:00"],
                    "persona": "scout",
                    "llm": True,
                    "writes": writes
                    or ["scout_read", "candidate", "note", "universe_tier", "active_universe"],
                },
            },
            "context_ttl": {"scout_read": {"ttl": "24h", "supersede": "latest"}},
        }
    )


def _ctx(conn: sqlite3.Connection, routines: RoutinesConfig, settings: ArcSettings) -> JobContext:
    kind, spec = routines.step("scout")
    return JobContext(
        job="scout",
        kind=kind,
        spec=spec,
        run_id="run-scout",
        chain_run_id="chain-scout",
        scheduled_for=NOW,
        now=NOW,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(NOW),
        routines=routines,
        settings_factory=lambda: settings,
    )


PASS = LiquidityMetrics(
    ticker="X",
    as_of=DAY,
    price=50.0,
    adv_shares=2e6,
    adv_sessions=20,
    expiries_in_window=4,
    atm_strike=50.0,
    atm_open_interest=300,
    atm_spread_pct=0.10,
)
ILLIQUID = PASS.model_copy(update={"price": 1.0, "adv_shares": 10e3})


def _guard(
    conn: sqlite3.Connection, settings: ArcSettings, bad: frozenset[str] = frozenset()
) -> Any:
    from arc.universe.guard import UniverseGuard

    syms = {*DEFAULT_UNIVERSE, *MOMENTUM, "RKLB", "IONQ", "ASTS", "SOFI", "PLTR", "OKLO", "ZZLO"}
    g = UniverseGuard.from_settings(
        settings,
        now=NOW,
        conn=conn,
        master=SymbolMaster(
            fetched_at=NOW,
            symbols={
                s: SymbolInfo(symbol=s, sources=["sec", "alpaca"], options=True, tradable=True)
                for s in syms
            },
        ),
        market_factory=mock.MagicMock,
    )

    def fake(_market: Any, sym: str, **_: Any) -> LiquidityMetrics:
        return (ILLIQUID if sym in bad else PASS).model_copy(update={"ticker": sym})

    mock.patch("arc.universe.guard.measure_liquidity", side_effect=fake).start()
    return g


def _call(
    ticker: str, conf: float, origins: list[str] | None = None, stance: str = "bullish"
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "stance": stance,
        "confidence": conf,
        "horizon": "weeks",
        "origins": origins or ["youtube:stockedup"],
        "thesis": f"{ticker} breakout",
        "catalyst_type": "technical",
        "catalyst_date": None,
    }


REPLY = {
    "regime": "Risk-on trend, VIX contango, breadth narrowing",
    "options_sentiment": "Equity put/call 0.62 shows call demand; VX curve in contango",
    "themes": ["AI capex", "Rate cuts priced"],
    "ticker_calls": [
        _call("RKLB", 0.8, ["youtube:stockedup", "youtube:arete"]),
        _call("IONQ", 0.7),
        _call("ASTS", 0.65, ["youtube:fxevolution"]),
        _call("SOFI", 0.55),  # below the discovery floor 0.6
        _call("ZZLO", 0.9),  # fails the loose screen
        _call("LRCX", 0.52),  # momentum: floor 0.5 applies -> candidate
        _call("NVDA", 0.35),  # core: below floor 0.4 -> journaled skip
        _call("PLTR", 0.9, ["youtube:cnbc"]),  # not a run origin -> dropped
        _call("SPY", 0.9, ["youtube:fxevolution"], "bearish"),  # market reference
    ],
    "discovery": ["RKLB", "LRCX", "SPY", "IONQ", "SOFI", "ZZLO", "PLTR", "OKLO", "ASTS"],
    "risks": ["CPI Thursday"],
}


class FakeLLM:
    def __init__(self, reply: dict[str, Any] | str = REPLY, *, fail: bool = False) -> None:
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.prompts: list[str] = []
        self.fail = fail
        self.model = "fake-cheap"

    def complete(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        if self.fail:
            raise ScalpLLMError("boom")
        return LLMResult(
            text=self.reply, model="fake-cheap", input_tokens=4000, output_tokens=600, cost_usd=0.0
        )


def _seed(
    conn: sqlite3.Connection, *, briefs: tuple[str, ...] = ("fxevolution", "stockedup", "arete")
) -> None:
    for slug in briefs:
        _write_brief(conn, slug)
    _write_options(conn)
    _write_tier(conn, Tier.MOMENTUM, MOMENTUM)
    conn.commit()


# -- inputs + prompt -------------------------------------------------------------------


class TestInputs:
    def _inp(self, conn: sqlite3.Connection, budget: int = 12_000) -> Any:
        r = _routines()
        from arc.personas.scout import category_max_ages

        return scout_input_from_context(
            ContextStore(conn).snapshot(NOW),
            channels=CHANNELS,
            budget_chars=budget,
            max_age=category_max_ages(r),
            max_discovery=20,
            discovery_floor=0.6,
            higher_tier=["AAPL", "LRCX"],
            now=NOW,
        )

    def test_equal_split_between_categories_then_channels(self, db: sqlite3.Connection) -> None:
        _seed(db)  # macro: FX only (1 of 2); micro: StockedUp + Arete (2 of 3)
        inp = self._inp(db)
        caps = {b.origin: len(b.text) for b in inp.briefs}
        # 12,000 / 2 categories = 6,000; macro has 1 channel present, micro 2
        assert caps["youtube:fxevolution"] == 6_000
        assert caps["youtube:stockedup"] == caps["youtube:arete"] == 3_000
        assert all(b.truncated for b in inp.briefs)
        # a missing macro channel never widens micro's share (no renormalisation)
        assert sum(caps.values()) <= 12_000

    def test_presence_lines_name_missing_channels(self, db: sqlite3.Connection) -> None:
        _seed(db)
        inp = self._inp(db)
        # D60: each present channel carries its brief's age
        assert (
            inp.presence["youtube_macro"]
            == "YouTube macro briefs: 1/2 channels: FX (4h) (missing: Bravos)"
        )
        assert inp.presence["youtube_micro"].endswith(
            "2/3 channels: StockedUp (4h), Arete (4h) (missing: TradeBrigade)"
        )
        assert inp.missing == {"youtube_macro": ["Bravos"], "youtube_micro": ["TradeBrigade"]}
        assert inp.origins == {"youtube:fxevolution", "youtube:stockedup", "youtube:arete"}

    def test_stale_brief_and_options_are_not_read(self, db: sqlite3.Connection) -> None:
        _write_brief(db, "stockedup", age=dt.timedelta(hours=49))  # > youtube_micro 48h (D60)
        _write_options(db, age=dt.timedelta(hours=30))  # > options_slow 24h
        db.commit()
        inp = self._inp(db)
        assert inp.briefs == []
        assert inp.options_slow == {"options_daily": None, "vx_curve": None, "vol_term": None}
        prompt = build_scout_prompt(inp)
        assert "No fresh brief today" in prompt
        assert "Cboe put/call: no fresh info" in prompt
        assert "VIX complex: no fresh info" in prompt

    def test_brief_47h_old_is_read_49h_is_not(self, db: sqlite3.Connection) -> None:
        """D60: the YouTube window is 48 h from the video's publish time."""
        _write_brief(db, "stockedup", age=dt.timedelta(hours=47))
        _write_brief(db, "arete", age=dt.timedelta(hours=49))
        db.commit()
        inp = self._inp(db)
        assert inp.origins == {"youtube:stockedup"}
        assert "StockedUp (47h)" in inp.presence["youtube_micro"]
        assert "Arete" in inp.presence["youtube_micro"].split("missing: ")[1]

    def test_prompt_sections_in_fixed_order(self, db: sqlite3.Connection) -> None:
        _seed(db)
        prompt = build_scout_prompt(self._inp(db))
        heads = [
            "1. Regime",
            "2. Options sentiment",
            "3. Themes",
            "4. Ticker calls",
            "5. Discovery",
            "6. Risks",
        ]
        pos = [prompt.index(h) for h in heads]
        assert pos == sorted(pos)
        assert "Cboe put/call (2026-10-05): total 0.91, equity 0.62" in prompt
        assert "slope 1→2 +5.3% · contango" in prompt
        assert "origin id `youtube:fxevolution`" in prompt
        assert "AAPL, LRCX" in prompt  # higher tier listed


# -- validation ------------------------------------------------------------------------


class TestValidation:
    def test_unknown_origin_drops_the_call(self) -> None:
        out = ScoutOutput.model_validate(REPLY)
        calls, dropped = validate_calls(
            out,
            frozenset(
                {"youtube:stockedup", "youtube:arete", "youtube:fxevolution", "youtube:bravos"}
            ),
        )
        assert "PLTR" not in {c.ticker for c in calls}
        assert dropped["PLTR"] == "origin_unknown: youtube:cnbc"

    def test_duplicate_ticker_kept_once(self) -> None:
        out = ScoutOutput.model_validate(
            {**REPLY, "ticker_calls": [_call("RKLB", 0.8), _call("RKLB", 0.9)]}
        )
        calls, dropped = validate_calls(out, frozenset({"youtube:stockedup"}))
        assert [c.confidence for c in calls] == [0.8]
        assert dropped == {"RKLB": "duplicate"}

    def test_discovery_rules(self) -> None:
        out = ScoutOutput.model_validate(REPLY)
        calls, _ = validate_calls(
            out,
            frozenset(
                {"youtube:stockedup", "youtube:arete", "youtube:fxevolution", "youtube:bravos"}
            ),
        )
        screened: list[str] = []

        def screen(sym: str) -> ScreenResult:
            screened.append(sym)
            ok = sym != "ZZLO"
            return ScreenResult(
                ticker=sym, passed=ok, failures=[] if ok else ["price 1.00 < 3.00"], metrics=PASS
            )

        res = discovery_members(
            out,
            calls,
            higher_tier={"LRCX": "momentum"},
            excluded=frozenset({"SPY", "QQQ", "IWM"}),
            floor=0.6,
            max_discovery=2,
            screen=screen,
        )
        assert res.tickers == ["RKLB", "IONQ"]
        assert res.screened_out["LRCX"] == "in_higher_tier: momentum"
        assert res.screened_out["SPY"] == "market_reference_or_etf"
        assert res.screened_out["SOFI"].startswith("confidence_floor_skipped: 0.55 < 0.60")
        assert res.screened_out["PLTR"] == "not_in_ticker_calls"  # dropped call
        assert res.screened_out["OKLO"] == "not_in_ticker_calls"
        assert res.screened_out["ZZLO"] == "over_max_discovery"  # cap hit before the screen
        assert res.screened_out["ASTS"] == "over_max_discovery"
        assert screened == ["RKLB", "IONQ"]  # no market data spent on dropped names

    def test_screen_drop_is_journaled_with_detail(self) -> None:
        out = ScoutOutput.model_validate({**REPLY, "discovery": ["ZZLO"]})
        calls, _ = validate_calls(out, frozenset({"youtube:stockedup"}))
        res = discovery_members(
            out,
            calls,
            higher_tier={},
            excluded=frozenset(),
            floor=0.6,
            max_discovery=20,
            screen=lambda s: ScreenResult(
                ticker=s, passed=False, failures=["adv 10k < 300k"], metrics=PASS
            ),
        )
        assert res.tickers == []
        assert res.journal == [("ZZLO", "screened_out", "adv 10k < 300k")]

    def test_schema_rejects_bad_reply(self) -> None:
        with pytest.raises(ValueError, match="discovery"):
            ScoutOutput.model_validate({**REPLY, "discovery": [f"T{i}" for i in range(21)]})
        with pytest.raises(ValueError, match="origins"):
            ScoutOutput.model_validate(
                {**REPLY, "ticker_calls": [{**_call("RKLB", 0.8), "origins": []}]}
            )


# -- handler -----------------------------------------------------------------------------


class TestHandler:
    def test_no_switch_left(self) -> None:
        """E13.15: ``personas.scout_feed`` is gone; the job's own ``enabled`` remains."""
        from arc.control.registry import is_orphaned

        assert is_orphaned("personas.scout_feed") and "scout_feed" not in PERSONA_FLAGS

    def test_full_run(self, db: sqlite3.Connection) -> None:
        _seed(db)
        settings = _settings()
        ctx = _ctx(db, _routines(), settings)
        res = scout_persona(ctx, llm=FakeLLM(), guard=_guard(db, settings, {"ZZLO"}))
        db.commit()

        read = ScoutReadPayload.model_validate(
            ContextStore(db)
            .snapshot(NOW + dt.timedelta(minutes=1))
            .latest("scout_read", "session")
            .payload
        )
        assert read.discovery == ["RKLB", "IONQ", "ASTS"]
        assert read.discovery_fill == 3
        assert read.screened_out["LRCX"] == "in_higher_tier: momentum"
        assert read.screened_out["SPY"] == "market_reference_or_etf"
        assert read.screened_out["ZZLO"].startswith("screened_out:")
        assert read.inputs.youtube_macro.present == 1 and read.inputs.youtube_macro.configured == 2
        assert read.inputs.youtube_micro.missing == ["TradeBrigade"]
        assert read.inputs.options_daily == "2026-10-05" and read.inputs.vol_term is None
        assert "PLTR" not in {c.ticker for c in read.ticker_calls}

        # discovery tier is the Scout's list; candidates use each tier's floor
        membership = tier_membership(db, settings, NOW + dt.timedelta(minutes=1))
        assert {t for t, tier in membership.items() if tier is Tier.DISCOVERY} == {
            "RKLB",
            "IONQ",
            "ASTS",
        }
        cands = {
            e.subject: CandidatePayload.model_validate(e.payload)
            for e in ContextStore(db).snapshot(NOW + dt.timedelta(minutes=1)).of_kind("candidate")
        }
        assert set(cands) == {"RKLB", "IONQ", "ASTS", "LRCX"}  # SOFI/ZZLO in no tier; NVDA < 0.4
        assert cands["RKLB"].feed == "scout"
        assert cands["RKLB"].origins == ["youtube:stockedup", "youtube:arete"]
        assert cands["RKLB"].corroboration == 2
        rows = {
            r[0]
            for r in db.execute("SELECT ticker FROM candidates WHERE day = ?", (DAY.isoformat(),))
        }
        assert rows == set(cands)  # proposals.candidate_id FK holds

        codes = {
            (r[0], r[1])
            for r in db.execute(
                "SELECT subject, reason_code FROM decisions WHERE persona = 'scout'"
            )
        }
        assert ("RKLB", "scout_discovery") in codes
        assert ("RKLB", "scout_feed_candidate") in codes
        assert ("ZZLO", "scout_discovery_screened_out") in codes
        assert ("LRCX", "scout_discovery_screened_out") in codes  # in_higher_tier
        assert ("NVDA", "universe:below_tier_floor") in codes

        call = db.execute(
            "SELECT persona, status, model, input_tokens FROM persona_calls"
        ).fetchone()
        assert tuple(call) == ("scout", "ok", "fake-cheap", 4000)
        assert res.metrics["discovery_fill"] == 3 and res.metrics["under_filled"] is True
        assert "discovery 3/25" in res.summary
        text = json.dumps(res.card.blocks)
        assert "Discovery: 3/25" in text and "coverage:scout" in text
        assert ctx.outputs  # writes recorded for the manifest

    def test_coverage_scout_condition(self, db: sqlite3.Connection) -> None:
        _seed(db)
        settings = _settings()
        routines = _routines()
        scout_persona(_ctx(db, routines, settings), llm=FakeLLM(), guard=_guard(db, settings))
        db.commit()
        later = NOW + dt.timedelta(hours=1)
        r = checks.scout_coverage(db, routines, later)
        assert r.severity == "failed"
        assert r.findings[0].key == SCOUT_COVERAGE == "coverage:scout"
        assert "4/20 names, below 5"  # ZZLO passes here in r.findings[0].message
        assert not _slot_coverage(SCOUT_COVERAGE)  # never folded into an incident
        assert checks.scout_coverage(db, _routines(on=False), later).severity == "ok"

        # the next run meeting the threshold clears it
        many = {
            **REPLY,
            "ticker_calls": [_call(t, 0.8) for t in ("RKLB", "IONQ", "ASTS", "SOFI", "OKLO")],
            "discovery": ["RKLB", "IONQ", "ASTS", "SOFI", "OKLO"],
        }
        ctx = _ctx(db, routines, settings)
        ctx.now = ctx.scheduled_for = NOW + dt.timedelta(minutes=30)  # a manual re-run
        scout_persona(ctx, llm=FakeLLM(many), guard=_guard(db, settings))
        db.commit()
        r2 = checks.scout_coverage(db, routines, NOW + dt.timedelta(hours=2))
        assert r2.severity == "ok" and r2.summary.startswith("discovery 5/25")

    def test_undeclared_write_fails_closed(self, db: sqlite3.Connection) -> None:
        _seed(db)
        settings = _settings()
        ctx = _ctx(db, _routines(writes=["candidate", "note"]), settings)
        with pytest.raises(ContractViolationError, match="scout_read"):
            scout_persona(ctx, llm=FakeLLM(), guard=_guard(db, settings))

    def test_llm_error_recorded(self, db: sqlite3.Connection) -> None:
        _seed(db)
        with pytest.raises(RuntimeError, match="scout LLM call failed"):
            scout_persona(_ctx(db, _routines(), _settings()), llm=FakeLLM(fail=True))
        row = db.execute("SELECT persona, status FROM persona_calls").fetchone()
        assert tuple(row) == ("scout", "llm_error")

    def test_parse_error_recorded(self, db: sqlite3.Connection) -> None:
        _seed(db)
        with pytest.raises(RuntimeError, match="does not match ScoutOutput"):
            scout_persona(_ctx(db, _routines(), _settings()), llm=FakeLLM("not json"))
        row = db.execute("SELECT persona, status FROM persona_calls").fetchone()
        assert tuple(row) == ("scout", "parse_error")


# -- config / wiring ---------------------------------------------------------------------


class TestWiring:
    def test_shipped_job(self) -> None:
        c = load_routines(DEFAULT_ROUTINES_PATH)
        kind, spec = c.step("scout")
        assert kind == "persona"
        assert [t.strftime("%H:%M") for t in spec.schedule] == ["06:00"]
        assert spec.persona == "scout" and spec.llm and spec.after_sources
        assert set(spec.reads or []) == {
            "channel_brief",
            "options_daily",
            "vx_curve",
            "retail_buzz",  # D58: context only in the prompt (E13.20)
            "retail_sentiment",  # E14.6: only with personas.retail_sentiment_context on
            "vol_term",
            "universe_tier",
            "active_universe",
        }
        assert set(spec.writes or []) == {
            "scout_read",
            "candidate",
            "note",
            "universe_tier",
            "active_universe",
        }
        assert c.context_policy("scout_read", "scout").ttl.duration == dt.timedelta(hours=24)
        assert BUILTIN_HANDLERS["scout"] == "arc.routines.handlers:scout_persona"
        assert "scout" in TIMELINE_PERSONAS

    def test_registry_flag(self) -> None:
        with pytest.raises(TunableError):
            lookup("personas.scout_feed")
        assert "personas.scout_feed" not in REGISTRY

    def test_strategy_lane_covers_scout(self) -> None:
        import yaml

        lane = yaml.safe_load((REPO / "config" / "strategy_lane.yaml").read_text())
        paths = json.dumps(lane)
        assert "arc-scout" in paths or "hermes/skills/" in paths

    def test_routing_and_kinds(self) -> None:
        assert Persona.SCOUT.value == "scout"
        assert KINDS["scout_read"].model is ScoutReadPayload
        assert KINDS["candidate"].schema_version == 3
        legacy = CandidatePayload.model_validate(
            {
                "ticker": "NVDA",
                "stance": "bullish",
                "catalyst_type": "technical",
                "confidence": 0.5,
                "sources": ["u"],
                "created_at": NOW,
            }
        )
        assert legacy.feed == "scalp" and legacy.origins == []  # v2 rows still validate
        with pytest.raises(ValueError, match="single id token"):
            CandidatePayload.model_validate({**legacy.model_dump(), "origins": ["youtube: x"]})

    def test_reason_labels(self) -> None:
        for code in (
            ReasonCode.SCOUT_CANDIDATE,
            ReasonCode.SCOUT_DISCOVERY,
            ReasonCode.SCOUT_DISCOVERY_SCREENED_OUT,
        ):
            assert REASON_LABELS[code]
        assert ReasonCode.SCOUT_CANDIDATE.value == "scout_feed_candidate"

    def test_origin_id(self) -> None:
        assert origin_id("stockedup") == "youtube:stockedup"

    def test_card_without_underfill(self) -> None:
        out = ScoutOutput.model_validate(REPLY)
        calls, _ = validate_calls(
            out, frozenset({"youtube:stockedup", "youtube:arete", "youtube:fxevolution"})
        )
        read = ScoutReadPayload(
            as_of=NOW.isoformat(),
            session=DAY.isoformat(),
            regime="r",
            options_sentiment="o",
            ticker_calls=calls,
            inputs={
                "youtube_macro": {"present": 2, "configured": 2},
                "youtube_micro": {"present": 3, "configured": 3},
                "options_daily": "2026-10-05",
                "vx_curve": "2026-10-05",
                "vol_term": "2026-10-05",
            },
            discovery=["A", "B", "C", "D", "E"],
            discovery_fill=5,
            prompt_sha="x",
            model="m",
        )
        card = scout_card(read=read, max_discovery=20, min_discovery_alert=5, candidates=5)
        text = json.dumps(card.blocks)
        assert card.text.startswith("🔭 [Scout] Daily read: Discovery 5/20")
        assert "Under-filled" not in text and "No fresh input" not in text


def test_scout_output_clips_overlong_prose_sections() -> None:
    """A regime/options_sentiment a little over 600 chars is clipped, not a failed run."""
    from arc.personas.schemas import SCOUT_PROSE_MAX, ScoutOutput

    long = ("Positioning is constructive " * 30).strip()
    assert len(long) > SCOUT_PROSE_MAX
    out = ScoutOutput.model_validate({"regime": long, "options_sentiment": long})
    for text in (out.regime, out.options_sentiment):
        assert len(text) <= SCOUT_PROSE_MAX
        assert text.endswith("…")
        assert not text[:-1].endswith(" ")
    short = ScoutOutput.model_validate({"regime": "calm", "options_sentiment": "no fresh info"})
    assert (short.regime, short.options_sentiment) == ("calm", "no fresh info")


@pytest.mark.parametrize(
    ("raw", "want"),
    [("geopolitical", "news"), ("fed", "macro"), ("other", "news"), ("earnings", "earnings")],
)
def test_brief_catalyst_kinds_map_onto_catalyst_type(raw: str, want: str) -> None:
    """A brief's catalyst kind copied into a Scout call no longer fails the whole run
    (live 2026-10-08: ``geopolitical``)."""
    out = ScoutOutput.model_validate(
        {
            "regime": "r",
            "options_sentiment": "o",
            "ticker_calls": [
                {
                    "ticker": "XOM",
                    "stance": "bullish",
                    "confidence": 0.5,
                    "horizon": "days",
                    "origins": ["youtube:stockedup"],
                    "thesis": "t",
                    "catalyst_type": raw,
                }
            ],
        }
    )
    assert out.ticker_calls[0].catalyst_type.value == want


def test_unknown_catalyst_type_still_rejected() -> None:
    call = {
        "ticker": "XOM",
        "stance": "bullish",
        "confidence": 0.5,
        "horizon": "days",
        "origins": ["youtube:stockedup"],
        "thesis": "t",
        "catalyst_type": "vibes",
    }
    with pytest.raises(ValueError, match="catalyst_type"):
        ScoutOutput.model_validate(
            {"regime": "r", "options_sentiment": "o", "ticker_calls": [call]}
        )
