"""D51 (E12.1): every former ``settings.universe`` consumer reads the active list,
and the market reference (SPY, QQQ) keeps its regime entry when it is not a candidate."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from arc.config import DEFAULT_UNIVERSE, ArcSettings
from arc.context.store import ContextStore
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.universe.tiers import Tier, build_active, record_active
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3

NOW = dt.datetime(2026, 10, 5, 9, 0, tzinfo=ET)
EXTRA = "LRCX"  # a momentum name: in the active list, not in the core


def _settings(**kw: Any) -> ArcSettings:
    return ArcSettings(_env_file=None, env="paper", **kw)  # type: ignore[call-arg]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """A store whose resolved active list for NOW is core + LRCX."""
    from arc.universe.tiers import TierMember, UniverseTierPayload

    conn = connect(tmp_path / "arc.db")
    migrate(conn)
    store = ContextStore(conn)
    member = TierMember(ticker=EXTRA, tier=Tier.MOMENTUM, rank=1, source="t", as_of=NOW.date())
    store.write(
        kind="universe_tier",
        subject="momentum",
        payload=UniverseTierPayload(
            tier=Tier.MOMENTUM, members=[member], fetched_at=NOW, source="t"
        ),
        produced_by="t",
        ttl="35d",
        now=NOW,
    )
    active, _ = build_active(conn, _settings(), NOW)
    record_active(
        conn,
        active,
        at=NOW,
        write=lambda k, s, p: store.write(
            kind=k, subject=s, payload=p, produced_by="t", ttl="1 session", now=NOW
        ),
    )
    conn.commit()
    return conn


def test_no_runtime_settings_universe_reads_outside_universe_pkg() -> None:
    import re

    root = Path(__file__).resolve().parents[1] / "arc"
    pat = re.compile(r"\b(settings|s|ctx\.settings|eff)\.universe\b(?!_)")
    hits = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        if p.parts[-2] != "universe" and p.name != "config.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pat.search(line)
    ]
    assert hits == []


def test_ingest_universe_seed_is_active_list(db: sqlite3.Connection) -> None:
    from arc.universe.ingest import IngestUniverse

    uni = IngestUniverse.from_settings(_settings(), now=NOW, conn=db)
    assert list(uni.seed) == [*DEFAULT_UNIVERSE, EXTRA]


def test_universe_guard_seed_is_core_and_momentum(db: sqlite3.Connection) -> None:
    from arc.universe.guard import UniverseGuard

    guard = UniverseGuard.from_settings(_settings(), now=NOW, load_master=False, conn=db)
    assert EXTRA in guard.seed and "SPY" not in guard.seed
    strict = UniverseGuard.from_settings(
        _settings(universe_mode="strict"), now=NOW, load_master=False, conn=db
    )
    assert set(strict.seed) == {*DEFAULT_UNIVERSE, EXTRA}


def test_data_tickers_reads_active_list(db: sqlite3.Connection) -> None:
    from arc.routines.handlers import _data_tickers

    class Ctx:
        conn = db
        settings = _settings()
        now = NOW
        options: dict[str, Any] = {}

    assert _data_tickers(Ctx())[-1] == EXTRA  # type: ignore[arg-type]


def test_edgar_walks_active_list(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from arc.ingest import edgar

    seen: list[str] = []
    monkeypatch.setattr(edgar, "_company_tickers", lambda _s: {})
    monkeypatch.setattr(
        edgar.log, "warning", lambda ev, **kw: seen.append(kw.get("ticker", "")) or None
    )
    monkeypatch.setattr(
        edgar.IngestUniverse,
        "cik",
        lambda self, t: None,
    )
    edgar.fetch_edgar(db, _settings(), now=NOW)
    assert EXTRA in seen and seen[: len(DEFAULT_UNIVERSE)] == DEFAULT_UNIVERSE


def test_monitoring_earnings_universe_reads_active_list(db: sqlite3.Connection) -> None:
    from arc.monitoring.cli import _universe

    assert _universe(db, NOW)[-1] == EXTRA


def test_history_cli_default_tickers_read_active_list(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    from arc.universe.tiers import active_tickers_ro

    db.close()
    s = _settings(db_path=str(tmp_path / "arc.db"))
    assert active_tickers_ro(s, NOW)[-1] == EXTRA
    assert active_tickers_ro(_settings(db_path=str(tmp_path / "none.db")), NOW) == list(
        DEFAULT_UNIVERSE
    )


def test_brief_bridge_uses_given_active_list() -> None:
    from arc.ingest.channels.briefs import brief_to_candidates

    assert brief_to_candidates.__kwdefaults__ is not None
    assert "universe" in brief_to_candidates.__kwdefaults__


def test_sweep_watch_list_in_prompt(db: sqlite3.Connection) -> None:
    from arc.ingest.sweep import build_prompt
    from arc.universe.tiers import watch_tickers

    watch = watch_tickers(db, _settings(), NOW)
    prompt = build_prompt([], _settings(), NOW.date().isoformat(), universe=watch)
    assert "Watch list (core + momentum + trending)" in prompt
    assert f"MARA, {EXTRA}." in prompt
    assert "not a preference" in prompt


def test_spy_regime_written_when_spy_is_not_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """D51 rule 5: the fixture day has SPY as a candidate; drop it from the guard's
    seed so it is not, and the D33 guard must still read an SPY regime."""
    import arc.pipeline.env as env_mod
    import arc.pipeline.steps as steps
    from arc.pipeline.runner import fixture_run
    from arc.routines.config import load_routines

    monkeypatch.setattr(env_mod, "FIXTURE_SEED_EXTRA", frozenset())
    seen: list[Any] = []
    real = steps.market_guard

    def spy(snapshot: Any, settings: Any, **kw: Any) -> Any:
        seen.append(snapshot.latest("regime", "SPY"))
        return real(snapshot, settings, **kw)

    monkeypatch.setattr(steps, "market_guard", spy)
    conn, _ = fixture_run(_settings(account_profile="margin"), load_routines())
    cands = {r[0] for r in conn.execute("SELECT ticker FROM candidates")}
    assert "SPY" not in cands
    assert seen and seen[0] is not None
    assert seen[0].payload["ticker"] == "SPY"
