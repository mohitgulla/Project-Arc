"""E12.6: ``GET /api/ops/universe`` (D51 tiered universe, read-only).

The tower shows the stored resolve; it never re-resolves. Covered: today's resolve, a
stale (yesterday) resolve, an expired tier, the dropped list, a > 30 override flagged as
ignored, no resolve at all (core list, never blank), GET only.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import Ttl
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_universe import UniverseResponse, load_universe
from arc.universe.tiers import (
    DROP_OVER_ACTIVE_CAP,
    DROP_OVER_TIER_SIZE,
    MAX_CORE,
    Tier,
    TierMember,
    UniverseTierPayload,
    ignored_core_override,
    resolve_active,
    yaml_core,
)
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 5, 13, 42, tzinfo=ET)
TODAY = NOW.date()


def _load(name: str, path: Path):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db", REPO / "scripts" / "tower_fixture_db.py")
ops_fixture = _load("tower_fixture_ops", REPO / "scripts" / "tower_fixture_ops.py")


def _m(t: str, tier: Tier, rank: int, reason: str = "", source: str = "x") -> TierMember:
    return TierMember(ticker=t, tier=tier, rank=rank, source=source, reason=reason, as_of=TODAY)


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "arc.db"
    c = connect(p)
    migrate(c)
    c.close()
    return p


def _write(path: Path, kind: str, subject: str, payload, *, at: dt.datetime, ttl: dt.timedelta):  # noqa: ANN001, ANN202
    c = connect(path)
    try:
        ContextStore(c).write(
            kind=kind, subject=subject, payload=payload, produced_by="test",
            ttl=Ttl(duration=ttl), valid_from=at, now=at,
        )  # fmt: skip
    finally:
        c.close()


def _read(path: Path, settings: ArcSettings | None = None, now: dt.datetime = NOW):  # noqa: ANN202
    c = connect_ro(path)
    try:
        return load_universe(c, settings or ArcSettings(), now=now)
    finally:
        c.close()


def _small_active(day: dt.date, *, expired: tuple[Tier, ...] = ()):  # noqa: ANN202
    return resolve_active(
        core=[_m("NVDA", Tier.CORE, 1, "core list"), _m("AAPL", Tier.CORE, 2, "core list")],
        momentum=[
            _m("MU", Tier.MOMENTUM, 1, "SPMO weight 9.48% (row 1)"),
            _m("AAPL", Tier.MOMENTUM, 2, "SPMO weight 8.90% (row 2)"),
            _m("GE", Tier.MOMENTUM, 3, "SPMO weight 3.10% (row 3)"),
        ],
        discoveries=[
            _m("RKLB", Tier.DISCOVERY, 1, "YouTube call, confidence 0.8"),
            _m("QCOM", Tier.DISCOVERY, 2),
            _m("TEM", Tier.DISCOVERY, 3),
        ],
        active_max=5,
        tier_sizes={Tier.MOMENTUM: 20},
        as_of=day,
        config_version=7,
        expired_tiers=expired,
    )


# -- today's resolve -----------------------------------------------------------------


def test_todays_resolve_is_shown_as_stored(tmp_path: Path) -> None:
    db = _db(tmp_path)
    active = _small_active(TODAY)
    _write(db, "active_universe", "active", active, at=NOW - dt.timedelta(minutes=10),
           ttl=dt.timedelta(hours=8))  # fmt: skip
    r = _read(db)
    assert r.state == "today" and r.note is None
    assert r.resolved_for == TODAY and r.age_s == 600
    assert r.config_version == 7
    assert [a.ticker for a in r.active] == active.tickers  # stored order, never re-resolved
    aapl = next(a for a in r.active if a.ticker == "AAPL")
    assert aapl.tier == "core" and aapl.also_in == ["momentum"]
    tiers = {t.name: t for t in r.tiers}
    assert [t.name for t in r.tiers] == ["core", "momentum", "discovery", "trending"]
    assert (tiers["momentum"].listed, tiers["momentum"].active) == (3, 2)
    assert tiers["core"].size_cap == MAX_CORE
    assert tiers["momentum"].size_cap == ArcSettings().universe_momentum_size
    assert tiers["discovery"].size_cap == ArcSettings().universe_discovery_size
    assert tiers["trending"].size_cap == ArcSettings().universe_trending_size == 25
    assert sum(t.active for t in r.tiers) == len(r.active)
    assert r.market_reference == ["SPY", "QQQ", "IWM"]
    assert r.core_override_ignored is None


def test_dropped_list_carries_tier_and_reason(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _write(db, "active_universe", "active", _small_active(TODAY), at=NOW,
           ttl=dt.timedelta(hours=8))  # fmt: skip
    r = _read(db)
    # 2 core + 2 momentum (AAPL deduped) + 3 discovery = 7 > cap 5
    assert len(r.active) == 5
    assert [(d.ticker, d.tier, d.reason) for d in r.dropped] == [
        ("QCOM", "discovery", DROP_OVER_ACTIVE_CAP),
        ("TEM", "discovery", DROP_OVER_ACTIVE_CAP),
    ]


def test_feed_rows_come_from_the_latest_tier_entry(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fetched = NOW - dt.timedelta(days=2)
    _write(db, "universe_tier", "momentum", UniverseTierPayload(
        tier=Tier.MOMENTUM, members=[_m("MU", Tier.MOMENTUM, 1)], fetched_at=fetched,
        source="schwab", source_as_of=TODAY - dt.timedelta(days=3), url="https://x/spmo",
        partial=True,
    ), at=fetched, ttl=dt.timedelta(days=35))  # fmt: skip
    _write(db, "active_universe", "active", _small_active(TODAY), at=NOW,
           ttl=dt.timedelta(hours=8))  # fmt: skip
    m = next(t for t in _read(db).tiers if t.name == "momentum")
    assert m.source == "schwab" and m.partial and m.url == "https://x/spmo"
    assert m.fetched_at == fetched and m.age_s == 2 * 86400
    assert m.source_as_of == TODAY - dt.timedelta(days=3)
    assert not m.expired


# -- stale / none / expired ------------------------------------------------------------


def test_yesterdays_resolve_is_shown_stale_with_its_age(tmp_path: Path) -> None:
    db = _db(tmp_path)
    y_at = NOW - dt.timedelta(days=1, hours=1)
    _write(db, "active_universe", "active", _small_active(TODAY - dt.timedelta(days=1)),
           at=y_at, ttl=dt.timedelta(hours=8))  # fmt: skip
    r = _read(db)
    assert r.state == "stale"
    assert r.resolved_for == TODAY - dt.timedelta(days=1)
    assert r.age_s == 25 * 3600
    assert r.note and "Not resolved today" in r.note
    assert len(r.active) == 5  # the latest one is shown, never a blank page


def test_no_resolve_shows_the_core_list(tmp_path: Path) -> None:
    r = _read(_db(tmp_path))
    assert r.state == "none" and r.resolved_for is None
    assert [a.ticker for a in r.active] == ArcSettings().universe
    assert {a.tier for a in r.active} == {"core"}
    assert next(t for t in r.tiers if t.name == "core").active == len(r.active)
    assert r.note and "core list" in r.note


def test_expired_tier_is_flagged(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _write(db, "active_universe", "active", _small_active(TODAY, expired=(Tier.DISCOVERY,)),
           at=NOW, ttl=dt.timedelta(hours=8))  # fmt: skip
    tiers = {t.name: t for t in _read(db).tiers}
    assert tiers["discovery"].expired and not tiers["momentum"].expired


# -- ignored override ------------------------------------------------------------------


def test_override_over_30_is_flagged_ignored(tmp_path: Path) -> None:
    db = _db(tmp_path)
    long = [f"T{i}" for i in range(60)]
    s = ArcSettings(universe=long)
    assert ignored_core_override(s) == 60
    r = _read(db, s)
    assert r.core_override_ignored is not None
    assert r.core_override_ignored.count == 60
    assert "ignored" in r.core_override_ignored.note
    assert r.core_override_ignored.core_in_use == yaml_core(s)
    # the fallback core list is the yaml core, not the 60 names
    assert [a.ticker for a in r.active] == yaml_core(s)
    assert {a.source for a in r.active} == {"config"}


def test_override_at_30_is_not_flagged() -> None:
    s = ArcSettings(universe=[f"T{i}" for i in range(MAX_CORE)])
    assert ignored_core_override(s) is None


# -- route -----------------------------------------------------------------------------


def test_route_on_the_ops_fixture_is_get_only(tmp_path: Path) -> None:
    db = fixture.build(tmp_path / "fx.db", NOW, ops=True)
    with TestClient(create_app(db, clock=lambda: NOW)) as client:
        res = client.get("/api/ops/universe")
        assert res.status_code == 200
        body = UniverseResponse.model_validate(res.json())
        assert client.post("/api/ops/universe").status_code == 405
    assert body.state == "today"
    assert len(body.active) == body.active_max == 50
    counts = {t.name: t.active for t in body.tiers}
    assert counts == {"core": 20, "momentum": 11, "discovery": 19, "trending": 0}
    assert [(d.ticker, d.reason) for d in body.dropped] == [
        ("KKR", DROP_OVER_TIER_SIZE),
        ("VST", DROP_OVER_TIER_SIZE),
        ("CEG", DROP_OVER_TIER_SIZE),
        ("ANET", DROP_OVER_TIER_SIZE),
        ("BBAI", DROP_OVER_ACTIVE_CAP),
    ]
    # the fixture's 100-name `universe` override (E8.8e) is the pre-D51 list: ignored
    assert body.core_override_ignored is not None and body.core_override_ignored.count == 100
    mom = next(t for t in body.tiers if t.name == "momentum")
    assert mom.partial and mom.source == "stockanalysis" and mom.listed == 24
    disc = next(t for t in body.tiers if t.name == "discovery")
    assert disc.listed == len(ops_fixture.DISCOVERY_FIXTURE) and disc.source == "scout"
    assert body.director_diversification in {"strict", "relaxed"}
    rk = next(a for a in body.active if a.ticker == "RKLB")
    assert rk.tier == "discovery" and rk.reason.startswith("YouTube call")
