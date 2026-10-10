"""Tier admission rules (E12.4, D56 since E13.15).

Core skips the liquidity screen; momentum passes the ``standard`` profile and discovery
the ``loose`` one (``tiers.policy`` in ``config/universe.yaml``); a name in no tier is a
mention, never a candidate. Every tier has its own Scalp confidence floor. The screens
are profile-keyed and Slack-tunable (``universe_screen_<profile>_*``).
"""

from __future__ import annotations

import datetime as dt
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
    candidates_for_scanner,
    store_candidate,
    validate_scalp_candidate,
)
from arc.models import Candidate
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo
from arc.universe.config import LiquidityScreens, LiquidityThresholds, load_universe_config
from arc.universe.guard import REJECT_ILLIQUID, REJECT_NOT_IN_TIER, UniverseGuard
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

# passes loose (3.0 / 300k / 100 / 25 %), fails standard (OI 180 < 250)
LOOSE_ONLY = LiquidityMetrics(
    ticker="X",
    as_of=NOW.date(),
    price=40.0,
    adv_shares=2e6,
    adv_sessions=20,
    expiries_in_window=4,
    atm_strike=40.0,
    atm_open_interest=180,
    atm_spread_pct=0.05,
)


def _settings(**kw: Any) -> ArcSettings:
    base: dict[str, Any] = {"env": "paper", "universe": CORE}
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
    """Seed-mode guard; ``measure_liquidity`` is replaced by *metrics* (LOOSE_ONLY default)."""
    g = UniverseGuard.from_settings(
        _settings(**kw),
        now=NOW,
        conn=conn,
        master=_master("NVDA", "AAPL", "PLTR", "LLY", "HOOD", "XLE"),
        market_factory=mock.MagicMock,
    )
    table = metrics or {}

    def fake(_market: Any, sym: str, **_: Any) -> LiquidityMetrics:
        return table.get(sym, LOOSE_ONLY.model_copy(update={"ticker": sym}))

    mock.patch("arc.universe.guard.measure_liquidity", side_effect=fake).start()
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
        allowed_sources=frozenset({URL}),
        created_at=NOW,
    )


# -- config: profile-keyed screen ---------------------------------------------------


class TestScreenConfig:
    def test_repo_yaml_profiles(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        std, loose = cfg.liquidity_screen.standard, cfg.liquidity_screen.loose
        assert (std.min_price, std.min_adv_shares) == (7.5, 750_000)
        assert (std.min_atm_open_interest, std.max_atm_spread_pct) == (250, 0.15)
        assert (loose.min_price, loose.min_adv_shares) == (3.0, 300_000)
        assert (loose.min_atm_open_interest, loose.max_atm_spread_pct) == (100, 0.25)
        assert loose.atm_strikes == std.atm_strikes == 3
        assert cfg.screen_for("momentum") is cfg.liquidity_screen.standard
        assert cfg.screen_for("discovery") is cfg.liquidity_screen.loose

    def test_model_defaults_equal_the_yaml(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.liquidity_screen == LiquidityScreens()

    def test_unknown_profile_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump({"tiers": {"policy": {"discovery": {"screen": "bogus"}}}}))
        with pytest.raises(ValueError, match="screen"):
            load_universe_config(p)

    def test_retired_profile_is_not_a_policy_choice(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump({"tiers": {"policy": {"discovery": {"screen": "relaxed"}}}}))
        with pytest.raises(ValueError, match="screen"):
            load_universe_config(p)

    def test_overrides_patch_before_validation(self) -> None:
        cfg = load_universe_config(overrides={("liquidity_screen", "loose", "min_price"): 4.5})
        assert cfg.liquidity_screen.loose.min_price == 4.5
        assert cfg.liquidity_screen.standard.min_price == 7.5


# -- the screen: loose vs standard ----------------------------------------------------


class TestProfiles:
    def test_loose_only_fails_standard(self) -> None:
        screens = LiquidityScreens()
        std = screen_liquidity(LOOSE_ONLY, screens.standard)
        assert not std.passed and std.failures == ["near-ATM OI 180 < 250"]
        assert screen_liquidity(LOOSE_ONLY, screens.loose).passed

    @pytest.mark.parametrize(
        ("field", "value", "loose_ok"),
        [
            ("price", 3.0, True),
            ("price", 2.99, False),
            ("adv_shares", 300_000, True),
            ("adv_shares", 299_999, False),
            ("atm_open_interest", 100, True),
            ("atm_open_interest", 99, False),
            ("atm_spread_pct", 0.25, True),
            ("atm_spread_pct", 0.2501, False),
            ("expiries_in_window", 0, False),
        ],
    )
    def test_loose_boundaries(self, field: str, value: float, loose_ok: bool) -> None:
        m = LOOSE_ONLY.model_copy(update={field: value})
        assert screen_liquidity(m, LiquidityScreens().loose).passed is loose_ok
        assert not screen_liquidity(m, LiquidityScreens().standard).passed  # OI 180 < 250

    def test_loose_is_looser_on_every_threshold(self) -> None:
        s, r = LiquidityScreens().standard, LiquidityScreens().loose
        assert r.min_price < s.min_price and r.min_adv_shares < s.min_adv_shares
        assert r.min_atm_open_interest < s.min_atm_open_interest
        assert r.max_atm_spread_pct > s.max_atm_spread_pct
        assert isinstance(r, LiquidityThresholds)


# -- guard: tier membership drives screen + floor -------------------------------------


class TestGuardTiers:
    def test_membership_four_tiers(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY", "NVDA"])  # NVDA keeps core
        _write_tier(conn, Tier.DISCOVERY, ["HOOD", "LLY"])  # LLY keeps momentum
        _write_tier(conn, Tier.TRENDING, ["XLE", "HOOD"])  # D58: 4th tier; HOOD keeps discovery
        got = tier_membership(conn, _settings(), NOW)
        assert got == {
            "NVDA": Tier.CORE,
            "AAPL": Tier.CORE,
            "PLTR": Tier.CORE,
            "LLY": Tier.MOMENTUM,
            "HOOD": Tier.DISCOVERY,
            "XLE": Tier.TRENDING,
        }
        assert tier_membership(None, _settings(), NOW) == {t: Tier.CORE for t in CORE}

    def test_core_never_screened(self, conn: sqlite3.Connection) -> None:
        bad = LOOSE_ONLY.model_copy(update={"price": 1.0})
        g = _guard(conn, metrics={"NVDA": bad})
        assert g.is_seed("NVDA") and g.admit("NVDA") is None
        assert not g.screens and g.admitted_tier == []

    def test_momentum_standard_discovery_loose(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        _write_tier(conn, Tier.DISCOVERY, ["HOOD"])
        g = _guard(conn)
        assert g.screen_profile("LLY") == "standard" and g.screen_profile("HOOD") == "loose"
        assert g.admit("LLY") == REJECT_ILLIQUID
        assert g.details["LLY"] == "momentum standard screen: near-ATM OI 180 < 250"
        assert g.admit("HOOD") is None and g.admitted_tier == ["HOOD"]

    def test_discovery_screen_fail_rejects(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.DISCOVERY, ["HOOD"])
        g = _guard(conn, metrics={"HOOD": LOOSE_ONLY.model_copy(update={"atm_open_interest": 90})})
        assert g.admit("HOOD") == REJECT_ILLIQUID
        assert g.details["HOOD"] == "discovery loose screen: near-ATM OI 90 < 100"

    def test_no_tier_is_a_mention_without_market_data(self) -> None:
        g = _guard()
        assert g.membership("XLE") is None and g.admit("XLE") == REJECT_NOT_IN_TIER
        assert g.mentions == ["XLE"] and not g.screens

    def test_screen_cached_per_profile(self) -> None:
        g = _guard()
        a = g.screen("XLE", "loose")
        assert g.screen("XLE", "loose") is a
        b = g.screen("XLE", "standard")
        assert a.passed and not b.passed and g.screens["XLE"] is b

    def test_tier_floors(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        _write_tier(conn, Tier.DISCOVERY, ["HOOD"])
        g = _guard(conn)
        assert g.tier_floors() == {
            "NVDA": 0.4,
            "AAPL": 0.4,
            "PLTR": 0.4,
            "LLY": 0.5,
            "HOOD": 0.6,
        }


# -- Scalp confidence floor ----------------------------------------------------------


class TestConfidenceFloor:
    def test_floor_per_tier(self, conn: sqlite3.Connection) -> None:
        _write_tier(conn, Tier.MOMENTUM, ["LLY"])
        _write_tier(conn, Tier.DISCOVERY, ["HOOD"])
        liquid = LOOSE_ONLY.model_copy(update={"atm_open_interest": 900})
        g = _guard(conn, metrics={"LLY": liquid})
        assert _validate(g, "NVDA", 0.39) == REJECT_THRESHOLD
        assert isinstance(_validate(g, "NVDA", 0.4), Candidate)
        assert _validate(g, "LLY", 0.49) == REJECT_THRESHOLD
        assert _validate(g, "HOOD", 0.59) == REJECT_THRESHOLD
        assert not g.screens  # the floor runs before any market data
        assert isinstance(_validate(g, "LLY", 0.5), Candidate)
        assert isinstance(_validate(g, "HOOD", 0.6), Candidate)

    def test_allow_list_keeps_the_floor_for_everyone(self) -> None:
        out = validate_scalp_candidate(
            _item("NVDA", 0.5),
            universe=frozenset(CORE),
            min_confidence=0.6,
            allowed_sources=frozenset({URL}),
            created_at=NOW,
        )
        assert out == REJECT_THRESHOLD

    def test_candidates_for_scanner_uses_tier_floors(self, conn: sqlite3.Connection) -> None:
        repo = CandidateRepo(conn)
        for t, conf in (("NVDA", 0.4), ("LLY", 0.45), ("XLE", 0.9), ("HOOD", 0.7)):
            fields = {k: v for k, v in _item(t, conf).items() if k != "rationale"}
            cand = Candidate.model_validate({**fields, "created_at": NOW})
            store_candidate(repo, cand, day=DAY, run_id="r", source_key_of=lambda u: u)
        got = candidates_for_scanner(conn, DAY, tier_floors={"NVDA": 0.4, "LLY": 0.5, "HOOD": 0.6})
        assert [c.ticker for c in got] == ["HOOD", "NVDA"]  # LLY below, XLE in no tier
        plain = candidates_for_scanner(conn, DAY, min_confidence=0.6)
        assert [c.ticker for c in plain] == ["XLE", "HOOD"]


# -- registry ------------------------------------------------------------------------

SCREEN_KEYS = {
    "min_price": Risk.DOWN,
    "min_adv_shares": Risk.DOWN,
    "min_atm_open_interest": Risk.DOWN,
    "max_atm_spread_pct": Risk.UP,
}


class TestRegistry:
    def test_screen_keys_classified(self) -> None:
        got = {k for k, t in REGISTRY.items() if t.target is Target.UNIVERSE}
        want = {
            f"universe_screen_{p}_{leaf}" for p in ("standard", "loose") for leaf in SCREEN_KEYS
        } | {"universe.active_fill"}  # D67 (E14.9)
        assert got == want
        screens = LiquidityScreens()
        for prof in ("standard", "loose"):
            base = getattr(screens, prof)
            for leaf, risk in SCREEN_KEYS.items():
                t = lookup(f"universe_screen_{prof}_{leaf}")
                assert t.path == ("liquidity_screen", prof, leaf)
                assert t.risk is risk and t.group.value == "universe"
                default = getattr(base, leaf)
                assert t.min is not None and t.max is not None and t.min <= default <= t.max
                assert t.hard_ceiling in (t.min, t.max)

    def test_looser_is_riskier(self) -> None:
        t = lookup("universe_screen_loose_min_atm_open_interest")
        assert direction(t, 150, 100) is Direction.RISKIER
        assert direction(t, 150, 300) is Direction.SAFER
        s = lookup("universe_screen_loose_max_atm_spread_pct")
        assert direction(s, 0.20, 0.30) is Direction.RISKIER

    def test_budget_keys(self) -> None:
        assert ArcSettings(_env_file=None).finnhub_max_tickers == 50  # type: ignore[call-arg]

    def test_override_reaches_the_guard(self, conn: sqlite3.Connection) -> None:
        from arc.control.effective import effective_settings
        from arc.control.service import ControlService

        _write_tier(conn, Tier.DISCOVERY, ["HOOD"])
        owner = _settings(approver_slack_user_ids=["U0OWNER001"])
        svc = ControlService(conn, base=owner, now=lambda: NOW)
        key = "universe_screen_loose_min_atm_open_interest"
        r = svc.set(key, "500", actor="U0OWNER001", source="cli")
        assert r.outcome == "applied"  # tighter = safer, no confirm
        assert svc.view(key).value == 500
        eff = effective_settings(conn, base=owner)
        g = UniverseGuard.from_settings(
            eff, now=NOW, conn=conn, master=_master("HOOD"), market_factory=mock.MagicMock
        )
        assert g.config.liquidity_screen.loose.min_atm_open_interest == 500
        with mock.patch("arc.universe.guard.measure_liquidity", return_value=LOOSE_ONLY):
            assert g.admit("HOOD") == REJECT_ILLIQUID
