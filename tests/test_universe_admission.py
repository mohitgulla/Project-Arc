"""E12.4 (D51): tier admission rules.

Core + momentum skip the liquidity screen and the Scalp confidence floor (kept, and
journaled ``scalp_candidate`` with ``confidence_floor_skipped``); trending names and
discoveries pass the relaxed screen profile; discoveries alone count against
``scalp_max_new_tickers`` (25). The screen is profile-keyed in ``config/universe.yaml``
with a back-compatible loader, and the relaxed thresholds are Slack-tunable.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
import yaml

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.control.registry import REGISTRY, Direction, Risk, Target, direction, lookup
from arc.ingest.scalp import (
    REJECT_THRESHOLD,
    ScalpRunResult,
    candidates_for_scanner,
    store_candidate,
    validate_scalp_candidate,
)
from arc.models import Candidate
from arc.routines.config import RoutinesConfig
from arc.routines.handlers import JobContext, _journal_floor_skips
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo
from arc.universe.config import (
    LiquidityScreens,
    LiquidityThresholds,
    load_universe_config,
)
from arc.universe.guard import REJECT_ILLIQUID, REJECT_NEW_TICKER_CAP, UniverseGuard
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics, screen_liquidity
from arc.universe.tiers import Tier, TierMember, UniverseTierPayload, tier_membership
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ET)
DAY = NOW.date().isoformat()
REPO = Path(__file__).resolve().parent.parent
URL = "https://example.com/a"
CORE = ["NVDA", "AAPL", "PLTR"]

# 2026-10-04 live read (card E12.4): PLTR near-ATM OI 462, ATM spread 1.9%.
PLTR_LIKE = LiquidityMetrics(
    ticker="PLTR",
    as_of=NOW.date(),
    price=180.0,
    adv_shares=60e6,
    adv_sessions=20,
    expiries_in_window=4,
    atm_strike=180.0,
    atm_open_interest=462,
    atm_spread_pct=0.019,
)


def _settings(**kw: Any) -> ArcSettings:
    base: dict[str, Any] = {"env": "paper", "universe": CORE, "scalp_min_confidence": 0.6}
    return ArcSettings(_env_file=None, **{**base, **kw})  # type: ignore[call-arg]


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


def _write_tier(conn: sqlite3.Connection, tier: Tier, names: list[str]) -> None:
    payload = UniverseTierPayload(
        tier=tier,
        members=[
            TierMember(ticker=t, tier=tier, rank=i, source="test", as_of=NOW.date())
            for i, t in enumerate(names, 1)
        ],
        fetched_at=NOW,
        source="test",
    )
    ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=payload,
        produced_by="test",
        ttl="35d",
        valid_from=NOW - dt.timedelta(hours=1),
        now=NOW - dt.timedelta(hours=1),
    )
    conn.commit()


def _master(*syms: str) -> SymbolMaster:
    return SymbolMaster(
        fetched_at=NOW,
        symbols={
            s: SymbolInfo(symbol=s, sources=["sec", "alpaca"], options=True, tradable=True)
            for s in syms
        },
    )


def _guard(
    conn: sqlite3.Connection | None = None,
    *,
    metrics: dict[str, LiquidityMetrics] | None = None,
    **kw: Any,
) -> UniverseGuard:
    """Seed-mode guard; ``measure_liquidity`` is replaced by *metrics* (PLTR-like default)."""
    g = UniverseGuard.from_settings(
        _settings(**kw),
        now=NOW,
        conn=conn,
        master=_master("NVDA", "AAPL", "PLTR", "LLY", "HOOD", "XLE", "ACN", "HON", "ZZLO"),
        market_factory=mock.MagicMock,
    )
    table = metrics or {}

    def fake(_market: Any, sym: str, **_: Any) -> LiquidityMetrics:
        return table.get(sym, PLTR_LIKE.model_copy(update={"ticker": sym}))

    patcher = mock.patch("arc.universe.guard.measure_liquidity", side_effect=fake)
    patcher.start()
    g._test_patcher = patcher  # type: ignore[attr-defined]
    return g


@pytest.fixture(autouse=True)
def _stop_patches() -> Any:
    yield
    mock.patch.stopall()


def _item(ticker: str, confidence: float) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "stance": "bullish",
        "catalyst_type": "news",
        "catalyst_date": None,
        "confidence": confidence,
        "sources": [URL],
        "rationale": "r",
    }


def _validate(guard: UniverseGuard, ticker: str, confidence: float) -> Candidate | str:
    return validate_scalp_candidate(
        _item(ticker, confidence),
        universe=guard,
        min_confidence=0.6,
        allowed_sources=frozenset({URL}),
        created_at=NOW,
    )


# -- config: profile-keyed screen, back-compatible loader ---------------------------


class TestScreenConfig:
    def test_repo_yaml_profiles(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        strict, relaxed = cfg.liquidity_screen.strict, cfg.liquidity_screen.relaxed
        assert (strict.min_price, strict.min_adv_shares) == (10.0, 1_000_000)
        assert (strict.min_atm_open_interest, strict.max_atm_spread_pct) == (500, 0.10)
        assert (relaxed.min_price, relaxed.min_adv_shares) == (5.0, 500_000)
        assert (relaxed.min_atm_open_interest, relaxed.max_atm_spread_pct) == (150, 0.20)
        assert relaxed.atm_strikes == strict.atm_strikes == 3
        assert cfg.tiers.trending.screen == cfg.tiers.discovery.screen == "relaxed"
        assert cfg.screen_for("trending") is cfg.liquidity_screen.relaxed
        assert cfg.screen_for("discovery") is cfg.liquidity_screen.relaxed

    def test_model_defaults_equal_the_yaml(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.liquidity_screen == LiquidityScreens()

    def test_flat_pre_d51_block_loads_as_strict(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(
            yaml.safe_dump({"liquidity_screen": {"min_price": 12.0, "min_atm_open_interest": 700}})
        )
        screens = load_universe_config(p).liquidity_screen
        assert screens.strict.min_price == 12.0 and screens.strict.min_atm_open_interest == 700
        assert screens.relaxed == LiquidityScreens().relaxed  # untouched default

    def test_flat_keys_next_to_profiles_go_to_strict(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(
            yaml.safe_dump({"liquidity_screen": {"min_price": 11.0, "relaxed": {"min_price": 3.0}}})
        )
        screens = load_universe_config(p).liquidity_screen
        assert screens.strict.min_price == 11.0 and screens.relaxed.min_price == 3.0

    def test_unknown_profile_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump({"tiers": {"trending": {"screen": "bogus"}}}))
        with pytest.raises(ValueError, match="screen"):
            load_universe_config(p)

    def test_strict_tier_profile_is_config(self, tmp_path: Path) -> None:
        raw = yaml.safe_load((REPO / "config" / "universe.yaml").read_text())
        raw["tiers"]["discovery"] = {"screen": "strict"}
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump(raw))
        cfg = load_universe_config(p)
        assert cfg.screen_for("discovery") is cfg.liquidity_screen.strict
        assert cfg.screen_for("trending") is cfg.liquidity_screen.relaxed

    def test_overrides_patch_before_validation(self) -> None:
        cfg = load_universe_config(overrides={("liquidity_screen", "relaxed", "min_price"): 7.5})
        assert cfg.liquidity_screen.relaxed.min_price == 7.5
        assert cfg.liquidity_screen.strict.min_price == 10.0


# -- the screen: PLTR-like passes relaxed, fails strict -------------------------------


class TestProfiles:
    def test_pltr_like_passes_relaxed_fails_strict(self) -> None:
        screens = LiquidityScreens()
        strict = screen_liquidity(PLTR_LIKE, screens.strict)
        assert not strict.passed and strict.failures == ["near-ATM OI 462 < 500"]
        assert screen_liquidity(PLTR_LIKE, screens.relaxed).passed

    @pytest.mark.parametrize(
        ("field", "value", "relaxed_ok"),
        [
            ("price", 5.0, True),
            ("price", 4.99, False),
            ("adv_shares", 500_000, True),
            ("adv_shares", 499_999, False),
            ("atm_open_interest", 150, True),
            ("atm_open_interest", 149, False),
            ("atm_spread_pct", 0.20, True),
            ("atm_spread_pct", 0.2001, False),
            ("expiries_in_window", 0, False),
        ],
    )
    def test_relaxed_boundaries(self, field: str, value: float, relaxed_ok: bool) -> None:
        m = PLTR_LIKE.model_copy(update={field: value})
        assert screen_liquidity(m, LiquidityScreens().relaxed).passed is relaxed_ok
        assert not screen_liquidity(m, LiquidityScreens().strict).passed  # OI 462 < 500

    def test_relaxed_is_looser_on_every_threshold(self) -> None:
        s, r = LiquidityScreens().strict, LiquidityScreens().relaxed
        assert r.min_price < s.min_price and r.min_adv_shares < s.min_adv_shares
        assert r.min_atm_open_interest < s.min_atm_open_interest
        assert r.max_atm_spread_pct > s.max_atm_spread_pct
        assert isinstance(r, LiquidityThresholds)


# -- guard: tier membership drives screen + floor + cap ------------------------------


class TestGuardTiers:
    def test_membership_core_momentum_trending(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY", "NVDA"])  # NVDA keeps core
        _write_tier(conn, Tier.TRENDING, ["HOOD", "LLY"])  # LLY keeps momentum
        got = tier_membership(conn, _settings(), NOW)
        assert got == {
            "NVDA": Tier.CORE,
            "AAPL": Tier.CORE,
            "PLTR": Tier.CORE,
            "LLY": Tier.MOMENTUM,
            "HOOD": Tier.TRENDING,
        }
        assert tier_membership(None, _settings(), NOW) == {t: Tier.CORE for t in CORE}

    def test_core_and_momentum_never_screened(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        bad = PLTR_LIKE.model_copy(update={"price": 1.0})
        g = _guard(conn, metrics={"LLY": bad, "NVDA": bad})
        assert g.is_seed("LLY") and g.tier_of("LLY") is Tier.MOMENTUM
        assert g.admit("LLY") is None and g.admit("NVDA") is None
        assert not g.screens and g.admitted_new == []

    def test_trending_relaxed_and_not_capped(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.TRENDING, ["HOOD"])
        g = _guard(conn, scalp_max_new_tickers=0)
        assert not g.is_seed("HOOD") and g.tier_of("HOOD") is Tier.TRENDING
        assert g.screen_profile("HOOD") == "relaxed"
        assert g.admit("HOOD") is None  # OI 462 passes relaxed; cap 0 does not apply
        assert g.admitted_trending == ["HOOD"] and g.admitted_new == []
        assert g.admit("XLE") == REJECT_NEW_TICKER_CAP  # a discovery does count

    def test_trending_screen_fail_rejects(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.TRENDING, ["HOOD"])
        g = _guard(conn, metrics={"HOOD": PLTR_LIKE.model_copy(update={"atm_open_interest": 90})})
        assert g.admit("HOOD") == REJECT_ILLIQUID
        assert g.details["HOOD"] == "relaxed screen: near-ATM OI 90 < 150"

    def test_discovery_relaxed_by_default(self) -> None:
        g = _guard()
        assert g.tier_of("XLE") is Tier.DISCOVERY and g.screen_profile("XLE") == "relaxed"
        assert g.admit("XLE") is None and g.admitted_new == ["XLE"]
        assert g.screens["XLE"].passed

    def test_discovery_strict_when_configured(self, tmp_path: Path) -> None:
        raw = yaml.safe_load((REPO / "config" / "universe.yaml").read_text())
        raw["tiers"]["discovery"] = {"screen": "strict"}
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump(raw))
        g = _guard(universe_config_file=p)
        assert g.admit("XLE") == REJECT_ILLIQUID
        assert g.details["XLE"] == "strict screen: near-ATM OI 462 < 500"

    def test_screen_cached_per_profile(self) -> None:
        g = _guard()
        a = g.screen("XLE", "relaxed")
        assert g.screen("XLE", "relaxed") is a
        b = g.screen("XLE", "strict")
        assert a.passed and not b.passed and g.screens["XLE"] is b

    def test_cap_default_25(self) -> None:
        assert ArcSettings(_env_file=None).scalp_max_new_tickers == 25  # type: ignore[call-arg]
        g = _guard(metrics={})
        assert g.max_new == 25

    def test_floor_skip_tiers(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        _write_tier(conn, Tier.TRENDING, ["HOOD"])
        g = _guard(conn)
        assert g.skips_confidence_floor("NVDA") is Tier.CORE
        assert g.skips_confidence_floor("LLY") is Tier.MOMENTUM
        assert g.skips_confidence_floor("HOOD") is None
        assert g.skips_confidence_floor("XLE") is None
        assert g.floor_exempt() == frozenset({"NVDA", "AAPL", "PLTR", "LLY"})


# -- Scalp confidence floor ----------------------------------------------------------


class TestConfidenceFloor:
    def test_core_and_momentum_below_floor_kept(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        g = _guard(conn)
        for t in ("NVDA", "LLY"):
            c = _validate(g, t, 0.35)
            assert isinstance(c, Candidate) and c.confidence == 0.35

    def test_trending_and_discovery_below_floor_dropped(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.TRENDING, ["HOOD"])
        g = _guard(conn)
        assert _validate(g, "HOOD", 0.59) == REJECT_THRESHOLD
        assert _validate(g, "XLE", 0.59) == REJECT_THRESHOLD
        assert not g.screens  # the floor runs before any market data
        assert isinstance(_validate(g, "XLE", 0.6), Candidate)

    def test_allow_list_keeps_the_floor_for_everyone(self) -> None:
        out = validate_scalp_candidate(
            _item("NVDA", 0.5),
            universe=frozenset(CORE),
            min_confidence=0.6,
            allowed_sources=frozenset({URL}),
            created_at=NOW,
        )
        assert out == REJECT_THRESHOLD

    def test_candidates_for_scanner_exempts_core_momentum(self, conn: sqlite3.Connection) -> None:
        repo = CandidateRepo(conn)
        for t, conf in (("NVDA", 0.4), ("LLY", 0.45), ("XLE", 0.5), ("HOOD", 0.7)):
            fields = {k: v for k, v in _item(t, conf).items() if k != "rationale"}
            cand = Candidate.model_validate({**fields, "created_at": NOW})
            store_candidate(repo, cand, day=DAY, run_id="r", source_key_of=lambda u: u)
        floor = candidates_for_scanner(conn, DAY, min_confidence=0.6)
        assert [c.ticker for c in floor] == ["HOOD"]
        exempt = candidates_for_scanner(
            conn, DAY, min_confidence=0.6, floor_exempt=frozenset({"NVDA", "lly"})
        )
        assert [c.ticker for c in exempt] == ["HOOD", "LLY", "NVDA"]  # best first

    def test_run_scalp_keeps_and_records_floor_skips(self, conn: sqlite3.Connection) -> None:
        from arc.ingest.llm import LLMResult
        from arc.ingest.scalp import run_scalp
        from arc.ingest.store import RawDocRepo

        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        RawDocRepo(conn).insert(
            source="rss",
            url=URL,
            published_at=(NOW - dt.timedelta(minutes=30)).isoformat(),
            text="LLY NVDA XLE news",
            tickers_hint=["LLY", "NVDA", "XLE"],
            id="doc-1",
        )
        items = [_item("NVDA", 0.4), _item("LLY", 0.5), _item("XLE", 0.5), _item("AAPL", 0.8)]
        reply = json.dumps({"candidates": items, "scan_summary": "s"})

        class _LLM:
            model = "test"

            def complete(self, _prompt: str) -> LLMResult:
                return LLMResult(text=reply, model="test")

        g = _guard(conn)
        res = run_scalp(conn, _settings(), llm=_LLM(), now=NOW, guard=g)
        assert res.rejected == {REJECT_THRESHOLD: 1}  # XLE (a discovery)
        assert res.floor_skipped == {"NVDA": ("core", 0.4), "LLY": ("momentum", 0.5)}
        assert [c.ticker for c in res.candidates] == ["AAPL", "LLY", "NVDA"]


# -- journal -------------------------------------------------------------------------


def _ctx(conn: sqlite3.Connection, now: dt.datetime = NOW) -> JobContext:
    routines = RoutinesConfig.model_validate(
        {"personas": {"scalp": {"schedule": ["12:00"], "writes": ["candidate", "note"]}}}
    )
    kind, spec = routines.step("scalp")
    return JobContext(
        job="scalp",
        kind=kind,
        spec=spec,
        run_id="run-1",
        chain_run_id="chain-1",
        scheduled_for=now,
        now=now,
        conn=conn,
        snapshot=ContextStore(conn).snapshot(now),
        routines=routines,
        settings_factory=_settings,
    )


class TestJournal:
    def test_floor_skip_journaled_once_per_day(self, conn: sqlite3.Connection) -> None:
        res = ScalpRunResult(run_id="r", day=DAY, dry_run=False)
        res.floor_skipped = {"NVDA": ("core", 0.4), "LLY": ("momentum", 0.5)}
        assert _journal_floor_skips(_ctx(conn), res) == 2
        rows = conn.execute(
            "SELECT subject, choice, reason_code, payload, confidence FROM decisions"
            " ORDER BY subject"
        ).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            ("LLY", "selected", "scalp_candidate"),
            ("NVDA", "selected", "scalp_candidate"),
        ]
        payload = json.loads(rows[1][3])
        assert payload["confidence_floor_skipped"] == "tier=core"
        assert payload["min_confidence"] == 0.6 and rows[1][4] == 0.4
        later = NOW + dt.timedelta(minutes=30)
        assert _journal_floor_skips(_ctx(conn, later), res) == 0  # same day: once
        tomorrow = NOW + dt.timedelta(days=1)
        assert _journal_floor_skips(_ctx(conn, tomorrow), res) == 2

    def test_nothing_to_journal(self, conn: sqlite3.Connection) -> None:
        res = ScalpRunResult(run_id="r", day=DAY, dry_run=False)
        assert _journal_floor_skips(_ctx(conn), res) == 0


# -- registry ------------------------------------------------------------------------

RELAXED_KEYS = {
    "universe_screen_relaxed_min_price": ("min_price", Risk.DOWN, 5.0),
    "universe_screen_relaxed_min_adv_shares": ("min_adv_shares", Risk.DOWN, 500_000),
    "universe_screen_relaxed_min_atm_open_interest": ("min_atm_open_interest", Risk.DOWN, 150),
    "universe_screen_relaxed_max_atm_spread_pct": ("max_atm_spread_pct", Risk.UP, 0.20),
}


class TestRegistry:
    def test_relaxed_keys_classified(self) -> None:
        got = {k for k, t in REGISTRY.items() if t.target is Target.UNIVERSE}
        # E13.4: the standard/loose screens mirror relaxed, plus the tier-model flag
        d56 = {k.replace("relaxed", prof) for k in RELAXED_KEYS for prof in ("standard", "loose")}
        assert got == set(RELAXED_KEYS) | d56 | {"universe.tiers.model"}
        for key, (leaf, risk, default) in RELAXED_KEYS.items():
            t = lookup(key)
            assert t.path == ("liquidity_screen", "relaxed", leaf)
            assert t.risk is risk and t.group.value == "universe"
            assert t.min is not None and t.max is not None and t.min <= default <= t.max
            assert t.hard_ceiling in (t.min, t.max)

    def test_looser_is_riskier(self) -> None:
        t = lookup("universe_screen_relaxed_min_atm_open_interest")
        assert direction(t, 150, 100) is Direction.RISKIER
        assert direction(t, 150, 300) is Direction.SAFER
        s = lookup("universe_screen_relaxed_max_atm_spread_pct")
        assert direction(s, 0.20, 0.30) is Direction.RISKIER

    def test_budget_keys(self) -> None:
        assert lookup("scalp_max_new_tickers").hard_ceiling == 25
        assert ArcSettings(_env_file=None).finnhub_max_tickers == 50  # type: ignore[call-arg]

    def test_override_reaches_the_guard(self, conn: sqlite3.Connection) -> None:
        from arc.control.effective import effective_settings
        from arc.control.service import ControlService

        owner = _settings(approver_slack_user_ids=["U0OWNER001"])
        svc = ControlService(conn, base=owner, now=lambda: NOW)
        r = svc.set(
            "universe_screen_relaxed_min_atm_open_interest", "500", actor="U0OWNER001", source="cli"
        )
        assert r.outcome == "applied"  # tighter = safer, no confirm
        assert svc.view("universe_screen_relaxed_min_atm_open_interest").value == 500
        eff = effective_settings(conn, base=owner)
        g = UniverseGuard.from_settings(
            eff, now=NOW, master=_master("XLE"), market_factory=mock.MagicMock
        )
        assert g.config.liquidity_screen.relaxed.min_atm_open_interest == 500
        with mock.patch("arc.universe.guard.measure_liquidity", return_value=PLTR_LIKE):
            assert g.admit("XLE") == REJECT_ILLIQUID
