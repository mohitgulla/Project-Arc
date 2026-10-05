"""D51 tiered universe (E12.1): tier model, resolver, active list, consumers."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.store import ContextStore
from arc.control.registry import MAX_UNIVERSE, REGISTRY
from arc.journal.reasons import REASON_LABELS, ReasonCode
from arc.pipeline.portfolio_context import load_industries, load_sectors
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.config import load_universe_config
from arc.universe.ingest import IngestUniverse
from arc.universe.tiers import (
    ACTIVE_SUBJECT,
    DROP_OVER_ACTIVE_CAP,
    DROP_OVER_TIER_SIZE,
    MAX_CORE,
    TIER_ORDER,
    ActiveUniverse,
    Tier,
    TierMember,
    UniverseTierPayload,
    active_tickers,
    build_active,
    core_tickers,
    market_reference,
    record_active,
    resolve_active,
    seed_tickers,
    watch_tickers,
)
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=ET)
DAY = NOW.date()
CORE_25 = [
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "AVGO",
    "MU",
    "JPM",
    "XOM",
    "UNH",
    "BA",
    "ORCL",
    "COIN",
    "PLTR",
    "SOFI",
    "HOOD",
    "INTC",
    "SMCI",
    "NFLX",
    "UBER",
    "BAC",
    "MARA",
]
# SPMO top holdings at 2026-10-04 as listed on the card: each needs a sector + industry.
SPMO_TOP25 = [
    "MU",
    "AAPL",
    "AMD",
    "INTC",
    "GOOGL",
    "JNJ",
    "LRCX",
    "XOM",
    "AMAT",
    "SNDK",
    "CSCO",
    "MRK",
    "CAT",
    "STX",
    "PANW",
    "WDC",
    "UNH",
    "KO",
    "KLAC",
    "DELL",
    "MRVL",
    "MS",
    "LITE",
    "GS",
]  # the card list, verbatim
KNOWN_ETFS = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "SMH", "SCHD", "XLK", "XLF"}


def _m(ticker: str, tier: Tier, rank: int) -> TierMember:
    return TierMember(ticker=ticker, tier=tier, rank=rank, source="t", as_of=DAY)


def _tier(tier: Tier, names: list[str]) -> list[TierMember]:
    return [_m(t, tier, i) for i, t in enumerate(names, 1)]


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    return conn


def _write_tier(
    conn: sqlite3.Connection, tier: Tier, names: list[str], *, at: dt.datetime, ttl: str
) -> None:
    payload = UniverseTierPayload(
        tier=tier, members=_tier(tier, names), fetched_at=at, source="test"
    )
    ContextStore(conn).write(
        kind="universe_tier",
        subject=tier.value,
        payload=payload,
        produced_by="test",
        ttl=ttl,
        valid_from=at,
        now=at,
    )
    conn.commit()


def _candidate(conn: sqlite3.Connection, ticker: str, conf: float, corr: int = 1) -> None:
    conn.execute(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at, "
        "day, corroboration) VALUES (?, ?, 'bullish', 'news', ?, ?, ?, ?)",
        (f"c-{ticker}", ticker, conf, NOW.isoformat(), DAY.isoformat(), corr),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Config: core 25, market reference, registry, industries
# ---------------------------------------------------------------------------


class TestCoreConfig:
    def test_core_is_d51_list_and_matches_default(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert cfg.core == CORE_25 == DEFAULT_UNIVERSE
        assert len(set(cfg.core)) == 25

    def test_core_has_no_etfs_and_reference_is_spy_qqq(self) -> None:
        cfg = load_universe_config(REPO / "config" / "universe.yaml")
        assert not set(cfg.core) & KNOWN_ETFS
        assert cfg.tiers.market_reference == ["SPY", "QQQ"]
        assert market_reference(_settings()) == ["SPY", "QQQ"]

    def test_tier_order_fixed(self) -> None:
        from arc.universe.config import TiersConfig

        assert [t.value for t in TIER_ORDER] == ["core", "momentum", "trending", "discovery"]
        with pytest.raises(ValueError, match="fixed"):
            TiersConfig(order=["momentum", "core", "trending", "discovery"])  # type: ignore[list-item]

    def test_registry_keys_and_ceiling(self) -> None:
        assert MAX_UNIVERSE == MAX_CORE == 30
        for key, default in (
            ("universe_active_max", 50),
            ("universe_momentum_size", 25),
            ("universe_trending_size", 25),
        ):
            assert key in REGISTRY
            assert getattr(_settings(), key) == default

    def test_over_active_cap_reason_has_label(self) -> None:
        assert ReasonCode.UNIVERSE_OVER_ACTIVE_CAP.value == "universe:over_active_cap"
        assert REASON_LABELS[ReasonCode.UNIVERSE_OVER_ACTIVE_CAP]

    def test_core_and_spmo_have_sector_and_one_industry(self) -> None:
        sectors, industries = load_sectors(), load_industries()
        for t in {*CORE_25, *SPMO_TOP25}:
            assert t in sectors, t
            assert t in industries, t

    def test_industry_listed_twice_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "s.yaml"
        p.write_text(yaml.safe_dump({"industries": {"a": ["NVDA"], "b": ["nvda"]}}))
        with pytest.raises(ValueError, match="NVDA"):
            load_industries(p)


class TestCoreOverride:
    def test_d26_override_within_ceiling_is_the_core(self) -> None:
        assert core_tickers(_settings(universe=["aapl", "$msft", "AAPL"])) == ["AAPL", "MSFT"]

    def test_pre_d51_flat_override_is_ignored(self) -> None:
        flat = [f"T{i}" for i in range(MAX_CORE + 1)]
        assert core_tickers(_settings(universe=flat)) == CORE_25


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


class TestResolveActive:
    def test_dedupe_keeps_highest_tier_and_records_also_in(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, ["NVDA", "AAPL"]),
            momentum=_tier(Tier.MOMENTUM, ["NVDA", "LRCX"]),
            trending=_tier(Tier.TRENDING, ["LRCX", "NVDA", "RKLB"]),
            discoveries=_tier(Tier.DISCOVERY, ["RKLB", "CRWD"]),
            active_max=50,
            as_of=DAY,
        )
        assert a.tickers == ["NVDA", "AAPL", "LRCX", "RKLB", "CRWD"]
        by = {m.ticker: m for m in a.members}
        assert by["NVDA"].tier is Tier.CORE
        assert by["NVDA"].also_in == [Tier.MOMENTUM, Tier.TRENDING]
        assert by["LRCX"].tier is Tier.MOMENTUM and by["LRCX"].also_in == [Tier.TRENDING]
        assert by["RKLB"].also_in == [Tier.DISCOVERY]
        assert a.counts == {"core": 2, "momentum": 1, "trending": 1, "discovery": 1}
        assert a.raw_counts == {"core": 2, "momentum": 2, "trending": 3, "discovery": 2}
        assert a.dropped == []

    def test_rank_order_normalisation_and_in_tier_duplicates(self) -> None:
        a = resolve_active(
            core=[_m("brk-b", Tier.CORE, 2), _m("$nvda", Tier.CORE, 1), _m("NVDA", Tier.CORE, 3)],
            active_max=50,
            as_of=DAY,
        )
        assert a.tickers == ["NVDA", "BRK.B"]

    def test_tier_size_cut_is_listed(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, ["NVDA"]),
            momentum=_tier(Tier.MOMENTUM, ["NVDA", "A1", "A2", "A3"]),
            active_max=50,
            tier_sizes={Tier.MOMENTUM: 2},
            as_of=DAY,
        )
        # NVDA (deduped into core) does not use a momentum slot
        assert a.tier_tickers(Tier.MOMENTUM) == ["A1", "A2"]
        assert [(d.ticker, d.reason) for d in a.dropped] == [("A3", DROP_OVER_TIER_SIZE)]

    def test_active_cap_cuts_lowest_tier_last_rank_first(self) -> None:
        a = resolve_active(
            core=_tier(Tier.CORE, [f"C{i}" for i in range(25)]),
            momentum=_tier(Tier.MOMENTUM, [f"M{i}" for i in range(20)]),
            trending=_tier(Tier.TRENDING, [f"T{i}" for i in range(10)]),
            discoveries=_tier(Tier.DISCOVERY, ["D0", "D1"]),
            active_max=50,
            as_of=DAY,
        )
        assert len(a.members) == 50
        assert a.counts == {"core": 25, "momentum": 20, "trending": 5, "discovery": 0}
        assert [d.ticker for d in a.dropped] == ["T5", "T6", "T7", "T8", "T9", "D0", "D1"]
        assert {d.reason for d in a.dropped} == {DROP_OVER_ACTIVE_CAP}

    def test_payload_round_trips(self) -> None:
        a = resolve_active(core=_tier(Tier.CORE, ["NVDA"]), active_max=5, as_of=DAY)
        assert ActiveUniverse.model_validate_json(a.model_dump_json()) == a


_SYMS = st.sampled_from([f"S{i}" for i in range(40)])


@hsettings(max_examples=150, deadline=None)
@given(
    core=st.lists(_SYMS, max_size=30),
    mom=st.lists(_SYMS, max_size=30),
    trend=st.lists(_SYMS, max_size=30),
    disc=st.lists(_SYMS, max_size=30),
    cap=st.integers(min_value=1, max_value=60),
    msize=st.integers(min_value=0, max_value=30),
)
def test_resolver_properties(
    core: list[str],
    mom: list[str],
    trend: list[str],
    disc: list[str],
    cap: int,
    msize: int,
) -> None:
    kw: dict[str, Any] = {
        "core": _tier(Tier.CORE, core),
        "momentum": _tier(Tier.MOMENTUM, mom),
        "trending": _tier(Tier.TRENDING, trend),
        "discoveries": _tier(Tier.DISCOVERY, disc),
        "active_max": cap,
        "tier_sizes": {Tier.MOMENTUM: msize},
        "as_of": DAY,
    }
    a = resolve_active(**kw)
    assert resolve_active(**kw) == a  # deterministic
    tickers = a.tickers
    assert len(tickers) == len(set(tickers)) <= cap  # deduped, capped
    # tier order is monotone (core first, discovery last)
    idx = [TIER_ORDER.index(m.tier) for m in a.members]
    assert idx == sorted(idx)
    # every name any tier offered is either active or listed in dropped (no silent loss)
    offered = set(core) | set(mom) | set(trend) | set(disc)
    assert offered == set(tickers) | {d.ticker for d in a.dropped}
    assert not set(tickers) & {d.ticker for d in a.dropped}
    # a name keeps its highest tier (momentum may lose it to its size cut)
    for m in a.members:
        tiers = (core, mom, trend, disc)
        first = next(t for t, names in zip(TIER_ORDER, tiers, strict=True) if m.ticker in names)
        if first is not Tier.MOMENTUM:
            assert m.tier is first
    assert a.counts[Tier.MOMENTUM.value] <= msize
    # the core is never cut while the cap allows it
    assert tickers[: min(cap, len(set(core)))] == list(dict.fromkeys(core))[:cap]


# ---------------------------------------------------------------------------
# Store inputs, record, consumers
# ---------------------------------------------------------------------------


class TestStore:
    def test_empty_store_is_core_only(self, db: sqlite3.Connection) -> None:
        a, inputs = build_active(db, _settings(), NOW)
        assert a.tickers == CORE_25
        assert inputs.expired_tiers == []
        assert active_tickers(db, _settings(), NOW) == CORE_25  # nothing stored yet
        assert active_tickers(None, _settings(), NOW) == CORE_25

    def test_feeds_discoveries_and_expiry(self, db: sqlite3.Connection) -> None:
        week_ago = NOW - dt.timedelta(days=7)
        _write_tier(
            db, Tier.MOMENTUM, ["NVDA", "LRCX", "KLAC"], at=NOW - dt.timedelta(hours=1), ttl="8d"
        )
        _write_tier(db, Tier.TRENDING, ["RKLB"], at=week_ago, ttl="1 session")  # expired
        _candidate(db, "CRWD", 0.7)
        _candidate(db, "LRCX", 0.9)  # already momentum: not a discovery
        _candidate(db, "SNOW", 0.7, corr=3)  # same confidence, more corroboration first
        a, inputs = build_active(db, _settings(), NOW)
        assert inputs.expired_tiers == [Tier.TRENDING]
        assert a.expired_tiers == [Tier.TRENDING]
        assert a.tier_tickers(Tier.MOMENTUM) == ["LRCX", "KLAC"]
        assert a.tier_tickers(Tier.TRENDING) == []
        assert a.tier_tickers(Tier.DISCOVERY) == ["SNOW", "CRWD"]
        assert next(m for m in a.members if m.ticker == "NVDA").also_in == [Tier.MOMENTUM]
        # Scout admission: core ∪ momentum skip the screen; trending/discovery do not
        assert seed_tickers(db, _settings(), NOW) == [*CORE_25, "LRCX", "KLAC"]

    def test_record_writes_entry_and_journals_overflow_once(self, db: sqlite3.Connection) -> None:
        s = _settings(universe_active_max=26)
        _write_tier(db, Tier.MOMENTUM, ["LRCX", "KLAC", "AMAT"], at=NOW, ttl="8d")
        store = ContextStore(db)

        def write(kind: str, subject: str, payload: Any) -> None:
            store.write(
                kind=kind,
                subject=subject,
                payload=payload,
                produced_by="t",
                ttl="1 session",
                now=NOW,
            )

        a, _ = build_active(db, s, NOW)
        assert [d.ticker for d in a.dropped] == ["KLAC", "AMAT"]
        assert record_active(db, a, at=NOW, write=write, run_id="r1") == 2
        assert record_active(db, a, at=NOW + dt.timedelta(minutes=30), write=write) == 0
        rows = db.execute(
            "SELECT subject, reason_code, payload FROM decisions ORDER BY subject"
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            ("AMAT", "universe:over_active_cap"),
            ("KLAC", "universe:over_active_cap"),
        ]
        assert json.loads(rows[0][2])["tier"] == "momentum"
        # consumers read the stored resolve
        later = NOW + dt.timedelta(hours=1)
        assert active_tickers(db, s, later) == [*CORE_25, "LRCX"]
        assert watch_tickers(db, s, later) == [*CORE_25, "LRCX"]
        stored = store.query(as_of=later, kinds=["active_universe"], subjects=[ACTIVE_SUBJECT])
        assert len(stored) == 1  # supersede latest
        # tomorrow the stored list is stale: back to the core until the next resolve
        tomorrow = NOW + dt.timedelta(days=1)
        assert active_tickers(db, s, tomorrow.replace(hour=4)) == CORE_25

    def test_watch_list_excludes_discoveries(self, db: sqlite3.Connection) -> None:
        _candidate(db, "CRWD", 0.7)
        store = ContextStore(db)
        a, _ = build_active(db, _settings(), NOW)
        record_active(
            db,
            a,
            at=NOW,
            write=lambda k, s, p: store.write(
                kind=k, subject=s, payload=p, produced_by="t", now=NOW
            ),
        )
        assert active_tickers(db, _settings(), NOW)[-1] == "CRWD"
        assert "CRWD" not in watch_tickers(db, _settings(), NOW)


class TestIngestUniverse:
    def test_seed_is_active_list_and_reference_is_tagged_not_traded(
        self, db: sqlite3.Connection
    ) -> None:
        s = _settings(universe_mode="strict")
        uni = IngestUniverse.from_settings(s, now=NOW, conn=db)
        assert list(uni.seed) == CORE_25
        assert uni.reference == ("SPY", "QQQ")
        assert not uni.is_seed("SPY")
        assert uni.tickers_in("SPY and NVDA rallied") == ["NVDA", "SPY"]
        assert "SPY" in uni.mention_universe("SPY")


def test_cli_tiers_reads_store_read_only(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from arc.cli import main

    _write_tier(db, Tier.MOMENTUM, ["NVDA", "LRCX"], at=NOW, ttl="8d")
    _candidate(db, "CRWD", 0.7)
    db.close()
    path = tmp_path / "arc.db"
    before = path.stat().st_mtime_ns
    rc = main(["universe", "tiers", "--db", str(path), "--now", NOW.isoformat(), "--json"])
    raw = capsys.readouterr().out
    out = json.loads(raw[raw.index("{\n") :])  # structlog lines may precede the JSON
    assert rc == 0
    assert out["active"][-2:] == [
        {"ticker": "LRCX", "tier": "momentum", "rank": 2, "reason": ""},
        {"ticker": "CRWD", "tier": "discovery", "rank": 1, "reason": out["active"][-1]["reason"]},
    ]
    assert out["dedupe"] == {"NVDA": ["momentum"]}
    assert out["market_reference"] == ["SPY", "QQQ"]
    assert out["active_count"] == 27
    assert path.stat().st_mtime_ns == before  # wrote nothing
    assert main(["universe", "tiers", "--db", str(tmp_path / "missing.db")]) == 1
