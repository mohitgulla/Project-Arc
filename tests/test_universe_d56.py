"""D56 tier layout (E13.4): core 20 / momentum 20 / discovery 20, no trending.

Behind ``config/universe.yaml`` ``tiers.model: d56`` (default ``d51``). Covers the
resolver (sizes, dedupe, discovery-tail cap cuts), the per-tier screens and floors,
``not_in_tier`` mentions, the market reference and the d51 regression.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
import yaml
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.store import ContextStore
from arc.control.registry import REGISTRY, Risk, lookup, parse_value
from arc.ingest.scalp import (
    REJECT_THRESHOLD,
    ScalpRunResult,
    candidates_for_scanner,
    store_candidate,
    validate_scalp_candidate,
)
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.models import Candidate
from arc.routines.config import RoutinesConfig
from arc.routines.handlers import JobContext, _journal_floor_rejects
from arc.slack.digests import scalp_card
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo
from arc.universe.config import (
    D56_MARKET_REFERENCE,
    TiersConfig,
    load_universe_config,
)
from arc.universe.guard import REJECT_ILLIQUID, REJECT_NOT_IN_TIER, UniverseGuard
from arc.universe.master import SymbolInfo, SymbolMaster
from arc.universe.screen import LiquidityMetrics
from arc.universe.tiers import (
    DROP_OVER_ACTIVE_CAP,
    DROP_OVER_TIER_SIZE,
    TIER_ORDER_D56,
    ActiveUniverse,
    Tier,
    TierMember,
    UniverseTierPayload,
    build_active,
    market_reference,
    record_active,
    resolve_active,
    seed_tickers,
    tier_membership,
    watch_tickers,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 6, 9, 0, tzinfo=ET)
DAY = NOW.date()
URL = "https://example.com/a"
D56 = {"universe": {("tiers", "model"): "d56"}}

# The brief's fixture: SPMO-like momentum feed, 25 rows; 7 of its top 20 are core
# (MU AAPL AMD INTC GOOGL XOM NVDA).
MOMENTUM_25 = [
    "MU", "AAPL", "AMD", "INTC", "GOOGL", "JNJ", "LRCX", "XOM", "AMAT", "SNDK",
    "CSCO", "MRK", "CAT", "STX", "PANW", "WDC", "NVDA", "KLAC", "DELL", "MRVL",
    "MS", "LITE", "GS", "GE", "LLY",
]  # fmt: skip
DISCOVERY_20 = [
    "RKLB", "ASTS", "OKLO", "IONQ", "SOUN", "HIMS", "QCOM", "CRWV", "NBIS", "TEM",
    "BBAI", "SOFI", "UBER", "BAC", "SMCI", "MARA", "RDDT", "APP", "VST", "CEG",
]  # fmt: skip


def _settings(model: str = "d56", **kw: Any) -> ArcSettings:
    s = ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]
    if model == "d56":
        s._yaml_overrides = dict(D56)  # noqa: SLF001
    return s


def _m(ticker: str, tier: Tier, rank: int) -> TierMember:
    return TierMember(ticker=ticker, tier=tier, rank=rank, source="t", as_of=DAY)


def _tier(tier: Tier, names: list[str]) -> list[TierMember]:
    return [_m(t, tier, i) for i, t in enumerate(names, 1)]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _write_tier(conn: sqlite3.Connection, tier: Tier, names: list[str]) -> None:
    at = NOW - dt.timedelta(hours=1)
    ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=UniverseTierPayload(
            tier=tier, members=_tier(tier, names), fetched_at=at, source="test"
        ),
        produced_by="test",
        ttl="8d",
        valid_from=at,
        now=at,
    )
    conn.commit()


# -- config ----------------------------------------------------------------------------


class TestConfig:
    def test_repo_yaml_is_d51_with_d56_policy(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.tiers.model == "d51"  # the control until E13.15
        assert cfg.tiers.order == ["core", "momentum", "trending", "discovery"]
        assert {t: p.screen for t, p in cfg.tiers.policy.items()} == {
            "core": "none",
            "momentum": "standard",
            "discovery": "loose",
        }
        std, loose = cfg.liquidity_screen.standard, cfg.liquidity_screen.loose
        assert (std.min_price, std.min_adv_shares, std.min_atm_open_interest) == (7.5, 750e3, 250)
        assert std.max_atm_spread_pct == 0.15 and std.atm_strikes == 3
        assert (loose.min_price, loose.min_adv_shares, loose.min_atm_open_interest) == (
            3.0,
            300e3,
            100,
        )
        assert loose.max_atm_spread_pct == 0.25

    def test_core_is_twenty(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.core == list(DEFAULT_UNIVERSE) and len(cfg.core) == 20

    def test_d56_order_and_reference(self) -> None:
        t = TiersConfig(model="d56")
        assert t.order == ["core", "momentum", "discovery"]
        assert t.reference() == ["SPY", "QQQ", "IWM"]
        assert TiersConfig().reference() == ["SPY", "QQQ"]  # d51 unchanged
        with pytest.raises(ValueError, match="fixed for model d56"):
            TiersConfig(model="d56", order=["core", "momentum", "trending", "discovery"])
        with pytest.raises(ValueError, match="fixed for model d51"):
            TiersConfig(order=["core", "momentum", "discovery"])  # type: ignore[list-item]

    def test_pre_d56_block_loads_as_d51(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump({"tiers": {"trending": {"screen": "strict"}}}))
        cfg = load_universe_config(p)
        assert cfg.tiers.model == "d51" and cfg.tier_screen("trending") == "strict"
        assert cfg.tier_screen("momentum") == "none"

    def test_partial_policy_keeps_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        policy = {"discovery": {"screen": "relaxed"}}
        p.write_text(yaml.safe_dump({"tiers": {"model": "d56", "policy": policy}}))
        cfg = load_universe_config(p)
        assert cfg.tier_screen("discovery") == "relaxed"
        assert cfg.tier_screen("momentum") == "standard"
        assert cfg.screen_for("momentum").min_price == 7.5
        with pytest.raises(ValueError, match="not screened"):
            cfg.screen_for("core")  # type: ignore[arg-type]

    def test_unknown_policy_screen_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "u.yaml"
        p.write_text(yaml.safe_dump({"tiers": {"policy": {"core": {"screen": "bogus"}}}}))
        with pytest.raises(ValueError, match="screen"):
            load_universe_config(p)


class TestRegistry:
    def test_new_knobs(self) -> None:
        s = ArcSettings(_env_file=None, env="paper")  # type: ignore[call-arg]
        for key, default in (
            ("universe_momentum_size_d56", 20),
            ("universe_discovery_size", 20),
            ("universe_floor_core", 0.4),
            ("universe_floor_momentum", 0.5),
            ("universe_floor_discovery", 0.6),
        ):
            assert key in REGISTRY, key
            assert getattr(s, key) == default
        for tier in ("core", "momentum", "discovery"):
            t = lookup(f"universe_floor_{tier}")
            assert t.risk is Risk.DOWN and t.min == 0.30
        for prof in ("standard", "loose"):
            assert lookup(f"universe_screen_{prof}_min_price").path == (
                "liquidity_screen",
                prof,
                "min_price",
            )

    def test_model_flag_is_a_choice(self) -> None:
        t = lookup("universe.tiers.model")
        assert t.choices == ("d51", "d56") and t.path == ("tiers", "model")
        assert parse_value(t, "D56") == "d56"
        with pytest.raises(ValueError):
            parse_value(t, "d52")

    def test_reason_labels(self) -> None:
        for code in (ReasonCode.UNIVERSE_NOT_IN_TIER, ReasonCode.UNIVERSE_BELOW_TIER_FLOOR):
            assert REASON_LABELS[code]

    def test_flag_off_values_include_d51(self) -> None:
        lane = yaml.safe_load((REPO / "config" / "strategy_lane.yaml").read_text())
        assert "d51" in lane["flag_off_values"]


# -- resolver ----------------------------------------------------------------------------


def _brief(**kw: Any) -> ActiveUniverse:
    return resolve_active(
        core=_tier(Tier.CORE, list(DEFAULT_UNIVERSE)),
        momentum=_tier(Tier.MOMENTUM, MOMENTUM_25),
        trending=[],
        discoveries=_tier(Tier.DISCOVERY, DISCOVERY_20),
        active_max=50,
        tier_sizes={Tier.MOMENTUM: 20, Tier.DISCOVERY: 20},
        as_of=DAY,
        model="d56",
        **kw,
    )


class TestResolve:
    def test_brief_fixture_counts_and_tail_cuts(self) -> None:
        a = _brief()
        assert a.model == "d56"
        assert a.counts == {"core": 20, "momentum": 13, "discovery": 17}
        assert len(a.tickers) == 50
        caps = [d for d in a.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
        # the 3 lowest-ranked discovery rows, in rank order, with their rank
        assert [(d.ticker, d.tier, d.rank) for d in caps] == [
            ("APP", Tier.DISCOVERY, 18),
            ("VST", Tier.DISCOVERY, 19),
            ("CEG", Tier.DISCOVERY, 20),
        ]
        # momentum rows 21-25 are cut by the tier size (none of them is core)
        sized = [d.ticker for d in a.dropped if d.reason == DROP_OVER_TIER_SIZE]
        assert sized == ["MS", "LITE", "GS", "GE", "LLY"]
        # 7 momentum overlaps are recorded on the core rows
        overlaps = {m.ticker for m in a.members if Tier.MOMENTUM in m.also_in}
        assert overlaps == set(MOMENTUM_25[:20]) & set(DEFAULT_UNIVERSE)
        assert len(overlaps) == 7

    def test_overflow_cuts_momentum_tail_after_discovery(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, list(DEFAULT_UNIVERSE)),
            momentum=_tier(Tier.MOMENTUM, MOMENTUM_25),
            trending=[],
            discoveries=_tier(Tier.DISCOVERY, DISCOVERY_20),
            active_max=30,
            tier_sizes={Tier.MOMENTUM: 20, Tier.DISCOVERY: 20},
            as_of=DAY,
            model="d56",
        )
        assert a.counts == {"core": 20, "momentum": 10, "discovery": 0}
        caps = [d for d in a.dropped if d.reason == DROP_OVER_ACTIVE_CAP]
        assert [d.tier for d in caps[:3]] == [Tier.MOMENTUM] * 3
        assert caps[-1].ticker == "CEG"

    def test_trending_rejected_under_d56(self) -> None:
        with pytest.raises(ValueError, match="no trending"):
            resolve_active(
                core=[],
                momentum=[],
                trending=_tier(Tier.TRENDING, ["RKLB"]),
                discoveries=[],
                active_max=50,
                as_of=DAY,
                model="d56",
            )

    def test_payload_v1_row_loads_as_d51(self) -> None:
        a = ActiveUniverse.model_validate(
            {"as_of": "2026-10-05", "members": [], "counts": {}, "raw_counts": {}}
        )
        assert a.model == "d51"


_names = st.lists(st.sampled_from([f"T{i}" for i in range(40)]), max_size=30)


@hsettings(max_examples=150, deadline=None)
@given(
    core=_names,
    mom=_names,
    disc=_names,
    cap=st.integers(min_value=0, max_value=60),
    msize=st.integers(min_value=0, max_value=25),
    dsize=st.integers(min_value=0, max_value=25),
)
def test_d56_resolver_properties(
    core: list[str], mom: list[str], disc: list[str], cap: int, msize: int, dsize: int
) -> None:
    kw: dict[str, Any] = {
        "core": _tier(Tier.CORE, core),
        "momentum": _tier(Tier.MOMENTUM, mom),
        "trending": [],
        "discoveries": _tier(Tier.DISCOVERY, disc),
        "active_max": cap,
        "tier_sizes": {Tier.MOMENTUM: msize, Tier.DISCOVERY: dsize},
        "as_of": DAY,
        "model": "d56",
    }
    a = resolve_active(**kw)
    assert resolve_active(**kw) == a
    tickers = a.tickers
    assert len(tickers) == len(set(tickers)) <= cap
    idx = [TIER_ORDER_D56.index(m.tier) for m in a.members]
    assert idx == sorted(idx)  # core, then momentum, then discovery: cuts hit the tail
    offered = set(core) | set(mom) | set(disc)
    assert offered == set(tickers) | {d.ticker for d in a.dropped}
    assert not set(tickers) & {d.ticker for d in a.dropped}
    assert a.counts[Tier.MOMENTUM.value] <= msize
    assert a.counts[Tier.DISCOVERY.value] <= dsize
    assert Tier.TRENDING.value not in a.counts
    assert tickers[: min(cap, len(set(core)))] == list(dict.fromkeys(core))[:cap]


# -- store, consumers --------------------------------------------------------------------


class TestStore:
    def test_build_active_reads_scout_discovery_and_journals_cuts(
        self, db: sqlite3.Connection
    ) -> None:
        _write_tier(db, Tier.MOMENTUM, MOMENTUM_25)
        _write_tier(db, Tier.DISCOVERY, DISCOVERY_20)
        _write_tier(db, Tier.TRENDING, ["PLUG"])  # ignored under d56
        # a Scalp candidate is not a discovery under d56 (Scout is the only entry point)
        db.execute(
            "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at, "
            "day, corroboration) VALUES ('c1', 'ZZZZ', 'bullish', 'news', 0.9, ?, ?, 1)",
            (NOW.isoformat(), DAY.isoformat()),
        )
        db.commit()
        s = _settings()
        a, inputs = build_active(db, s, NOW)
        assert a.model == "d56" and inputs.trending == []
        assert a.counts == {"core": 20, "momentum": 13, "discovery": 17}
        assert "ZZZZ" not in a.tickers and "PLUG" not in a.tickers
        store = ContextStore(db)
        n = record_active(
            db,
            a,
            at=NOW,
            write=lambda k, sub, p: store.write(
                kind=k, subject=sub, payload=p, produced_by="t", now=NOW
            ),
        )
        assert n == 3
        rows = db.execute(
            "SELECT subject, reason_code, payload FROM decisions ORDER BY subject"
        ).fetchall()
        assert {r[0] for r in rows} == {"APP", "VST", "CEG"}
        p = json.loads(next(r[2] for r in rows if r[0] == "CEG"))
        assert p == {"tier": "discovery", "as_of": "2026-10-06", "rank": 20, "model": "d56"}
        # consumers: the Scalp watches every active tier; seeds are core only
        later = NOW + dt.timedelta(minutes=5)
        assert watch_tickers(db, s, later)[-1] == "RDDT"
        assert seed_tickers(db, s, later) == list(DEFAULT_UNIVERSE)

    def test_membership_cuts_feeds_to_tier_size(self, db: sqlite3.Connection) -> None:
        _write_tier(db, Tier.MOMENTUM, MOMENTUM_25)
        tiers = tier_membership(db, _settings(), NOW)
        assert tiers["LRCX"] is Tier.MOMENTUM and tiers["NVDA"] is Tier.CORE
        assert "LLY" not in tiers  # momentum row 25: in no tier
        d51 = tier_membership(db, _settings("d51"), NOW)
        assert d51["LLY"] is Tier.MOMENTUM  # d51 reads the feed whole

    def test_empty_discovery_until_scout(self, db: sqlite3.Connection) -> None:
        _write_tier(db, Tier.MOMENTUM, MOMENTUM_25)
        a, inputs = build_active(db, _settings(), NOW)
        assert inputs.discoveries == [] and inputs.expired_tiers == []
        assert a.counts == {"core": 20, "momentum": 13, "discovery": 0}

    def test_d51_regression(self, db: sqlite3.Connection) -> None:
        _write_tier(db, Tier.MOMENTUM, ["NVDA", "LRCX", "KLAC"])
        a, _ = build_active(db, _settings("d51"), NOW)
        assert a.model == "d51"
        assert list(a.counts) == ["core", "momentum", "trending", "discovery"]
        assert a.tier_tickers(Tier.MOMENTUM) == ["LRCX", "KLAC"]

    def test_market_reference_includes_iwm(self) -> None:
        assert market_reference(_settings()) == list(D56_MARKET_REFERENCE)
        assert market_reference(_settings("d51")) == ["SPY", "QQQ"]
        assert market_reference(_settings("d51"), model="d56") == ["SPY", "QQQ", "IWM"]


def test_regime_step_iterates_market_reference() -> None:
    """IWM's regime entry is written every Research run under d56 (steps.py loop)."""
    import inspect

    from arc.pipeline import steps

    src = inspect.getsource(steps)
    assert "set(market_reference(settings))" in src
    assert "IWM" in market_reference(_settings())


# -- guard: per-tier screen + floor, mentions -----------------------------------------------

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
# passes loose (3.0 / 300k / 100 / 25 %) but not standard (7.5 / 750k / 250 / 15 %)
LOOSE_ONLY = PASS.model_copy(
    update={"price": 4.0, "adv_shares": 400e3, "atm_open_interest": 120, "atm_spread_pct": 0.2}
)


def _guard(conn: sqlite3.Connection, metrics: dict[str, LiquidityMetrics]) -> UniverseGuard:
    syms = {*DEFAULT_UNIVERSE, *MOMENTUM_25, *DISCOVERY_20, "ZZLO", "OUTX"}
    g = UniverseGuard.from_settings(
        _settings(),
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
        return metrics.get(sym, PASS).model_copy(update={"ticker": sym})

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


def _validate(g: UniverseGuard, ticker: str, conf: float) -> Candidate | str:
    return validate_scalp_candidate(
        _item(ticker, conf),
        universe=g,
        min_confidence=0.9,  # d51's floor: ignored under d56
        allowed_sources=frozenset({URL}),
        created_at=NOW,
    )


class TestGuard:
    @pytest.fixture
    def tiers(self, db: sqlite3.Connection) -> sqlite3.Connection:
        _write_tier(db, Tier.MOMENTUM, MOMENTUM_25)
        _write_tier(db, Tier.DISCOVERY, DISCOVERY_20)
        return db

    def test_floors_per_tier(self, tiers: sqlite3.Connection) -> None:
        g = _guard(tiers, {})
        assert g.model == "d56"
        assert _validate(g, "NVDA", 0.39) == REJECT_THRESHOLD
        assert isinstance(_validate(g, "NVDA", 0.4), Candidate)
        assert _validate(g, "LRCX", 0.49) == REJECT_THRESHOLD
        assert isinstance(_validate(g, "LRCX", 0.5), Candidate)
        assert _validate(g, "RKLB", 0.59) == REJECT_THRESHOLD
        assert isinstance(_validate(g, "RKLB", 0.6), Candidate)
        assert g.floor_for("rklb") == 0.6 and g.floor_for("OUTX") is None

    def test_screens_per_tier(self, tiers: sqlite3.Connection) -> None:
        bad = {"NVDA": LOOSE_ONLY, "LRCX": LOOSE_ONLY, "RKLB": LOOSE_ONLY}
        g = _guard(tiers, bad)
        assert isinstance(_validate(g, "NVDA", 0.5), Candidate)  # core: no screen
        assert _validate(g, "LRCX", 0.7) == REJECT_ILLIQUID  # momentum: standard
        assert "momentum standard screen" in g.details["LRCX"]
        assert isinstance(_validate(g, "RKLB", 0.7), Candidate)  # discovery: loose
        assert "NVDA" not in g.screens
        assert g.screen_profile("LRCX") == "standard" and g.screen_profile("RKLB") == "loose"

    def test_not_in_tier_is_a_mention(self, tiers: sqlite3.Connection) -> None:
        g = _guard(tiers, {})
        assert _validate(g, "OUTX", 0.95) == REJECT_NOT_IN_TIER
        assert _validate(g, "LLY", 0.95) == REJECT_NOT_IN_TIER  # momentum row 25
        assert g.mentions == ["OUTX", "LLY"]
        assert not g.screens  # never screened, no market data spent
        assert g.admitted_new == []  # no new-ticker / backfill path

    def test_run_scalp_journals_and_lists_mentions(self, tiers: sqlite3.Connection) -> None:
        from arc.ingest.llm import LLMResult
        from arc.ingest.scalp import run_scalp
        from arc.ingest.store import RawDocRepo

        RawDocRepo(tiers).insert(
            source="rss",
            url=URL,
            published_at=(NOW - dt.timedelta(minutes=30)).isoformat(),
            text="NVDA LRCX RKLB OUTX news",
            tickers_hint=["NVDA", "LRCX", "RKLB", "OUTX"],
            id="doc-1",
        )
        items = [
            _item("NVDA", 0.45),
            _item("LRCX", 0.55),
            _item("RKLB", 0.59),
            _item("OUTX", 0.95),
        ]
        reply = json.dumps({"candidates": items, "scan_summary": "s"})

        class _LLM:
            model = "test"

            def complete(self, _prompt: str) -> LLMResult:
                return LLMResult(text=reply, model="test")

        g = _guard(tiers, {})
        res = run_scalp(tiers, _settings(), llm=_LLM(), now=NOW, guard=g)
        assert [c.ticker for c in res.candidates] == ["LRCX", "NVDA"]
        assert res.rejected == {REJECT_THRESHOLD: 1, REJECT_NOT_IN_TIER: 1}
        assert res.floor_rejected == {"RKLB": ("discovery", 0.59, 0.6)}
        assert res.floor_skipped == {}
        assert [m.ticker for m in res.mentions] == ["OUTX"]
        stored = {r[0] for r in tiers.execute("SELECT ticker FROM candidates").fetchall()}
        assert stored == {"NVDA", "LRCX"}  # no candidate row for a mention

        card = scalp_card(
            docs=1,
            accepted=2,
            candidates=res.candidates,
            rejected=res.rejected,
            rejected_items=res.rejected_items,
            mentions=res.mentions,
        )
        text = json.dumps(card.blocks)
        assert "*Outside the universe (1):*" in text
        assert "1 outside the universe" in text
        assert "1 rejected" in text  # the mention is not counted twice

    def test_floor_rejects_journaled_once_per_day(self, db: sqlite3.Connection) -> None:
        routines = RoutinesConfig.model_validate(
            {"personas": {"scalp": {"schedule": ["12:00"], "writes": ["candidate", "note"]}}}
        )
        kind, spec = routines.step("scalp")

        def ctx(now: dt.datetime) -> JobContext:
            return JobContext(
                job="scalp",
                kind=kind,
                spec=spec,
                run_id="run-1",
                chain_run_id="chain-1",
                scheduled_for=now,
                now=now,
                conn=db,
                snapshot=ContextStore(db).snapshot(now),
                routines=routines,
                settings_factory=_settings,
            )

        res = ScalpRunResult(run_id="r", day=DAY.isoformat(), dry_run=False)
        res.floor_rejected = {"RKLB": ("discovery", 0.59, 0.6)}
        assert _journal_floor_rejects(ctx(NOW), res) == 1
        row = db.execute("SELECT subject, choice, reason_code, payload FROM decisions").fetchone()
        assert (row[0], row[1], row[2]) == ("RKLB", "rejected", "universe:below_tier_floor")
        assert json.loads(row[3])["confidence_floor_skipped"] == "tier=discovery floor=0.6"
        assert _journal_floor_rejects(ctx(NOW + dt.timedelta(minutes=30)), res) == 0
        assert _journal_floor_rejects(ctx(NOW + dt.timedelta(days=1)), res) == 1

    def test_scanner_reads_tier_floors(self, db: sqlite3.Connection) -> None:
        repo = CandidateRepo(db)
        for t, conf in (("NVDA", 0.4), ("LRCX", 0.45), ("RKLB", 0.6), ("OUTX", 0.9)):
            fields = {k: v for k, v in _item(t, conf).items() if k != "rationale"}
            cand = Candidate.model_validate({**fields, "created_at": NOW})
            store_candidate(repo, cand, day=DAY.isoformat(), run_id="r", source_key_of=lambda u: u)
        got = candidates_for_scanner(
            db,
            DAY.isoformat(),
            min_confidence=0.6,
            tier_floors={"NVDA": 0.4, "LRCX": 0.5, "RKLB": 0.6},
        )
        assert [c.ticker for c in got] == ["RKLB", "NVDA"]  # LRCX below, OUTX in no tier
