"""E14.8 (D64): Ops › Universe tier tables + Today's Pick score + sentiment.

``GET /api/ops/universe`` rows gain structured Stocktwits sentiment (the UI never parses
the string), Picked / Trades / In-tier counts over the last 20 sessions, momentum's SPMO
weight (stored by the writer, parsed from pre-v5 reasons), discovery channel labels and
the carried flag; tiers say ``listed`` (feed rows before the size cut) and ``carried``.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING

import pytest

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.context.ttl import Ttl
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.data import connect_ro
from arc.tower.data_universe import (
    WINDOW_SESSIONS,
    in_tier_counts,
    load_universe,
    picked_counts,
    proposal_counts,
    weight_from_reason,
    window_sessions,
)
from arc.universe.momentum import MomentumFetch, MomentumPick, build_payload
from arc.universe.tiers import Tier, TierMember, UniverseTierPayload, resolve_active
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 10, 5, 13, 42, tzinfo=ET)  # Monday
TODAY = NOW.date()
FRI = dt.date(2026, 10, 2)


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


def _m(t: str, tier: Tier, rank: int, **kw) -> TierMember:  # noqa: ANN003
    return TierMember(ticker=t, tier=tier, rank=rank, source="x", as_of=TODAY, **kw)


def _candidate(c, cid: str, ticker: str, day: dt.date) -> None:  # noqa: ANN001
    c.execute(
        "INSERT INTO candidates (id, ticker, stance, catalyst_type, confidence, created_at, day)"
        " VALUES (?, ?, 'bullish', 'news', 0.7, ?, ?)",
        (cid, ticker, f"{day.isoformat()}T14:00:00Z", day.isoformat()),
    )


def _proposal(c, pid: str, cid: str, ticker: str, day: dt.date) -> None:  # noqa: ANN001
    c.execute(
        "INSERT INTO proposals (id, candidate_id, proposal_hash, structure_json, thesis,"
        " quant_json, sizing_json, expires_at, created_at, day, ticker)"
        " VALUES (?, ?, ?, '{}', 't', '{}', '{}', ?, ?, ?, ?)",
        (pid, cid, f"h-{pid}", "2030-01-01T00:00:00Z", f"{day.isoformat()}T15:00:00Z",
         day.isoformat(), ticker),
    )  # fmt: skip


def _sentiment(path: Path, ticker: str, *, bull: int, bear: int, at: dt.datetime) -> None:
    tagged = bull + bear
    payload = {
        "as_of": at.isoformat(),
        "messages": tagged + 5,
        "tagged": tagged,
        "bullish": bull,
        "bearish": bear,
        "bull_ratio": round(bull / tagged, 4) if tagged >= 5 else None,
        "min_tagged": 5,
        "window_minutes": 162.0,
    }
    _write(path, "retail_sentiment", ticker, payload, at=at, ttl=dt.timedelta(hours=26))


def _active():  # noqa: ANN202
    return resolve_active(
        core=[_m("NVDA", Tier.CORE, 1, reason="core list")],
        momentum=[
            _m("MU", Tier.MOMENTUM, 1, reason="SPMO weight 9.48% (row 1)"),  # pre-v5
            _m("GE", Tier.MOMENTUM, 2, reason="SPMO weight 3.10% (row 3)", weight_pct=3.1),
            _m("ZZ", Tier.MOMENTUM, 3, reason="no weight here"),
        ],
        discoveries=[
            _m(
                "RKLB",
                Tier.DISCOVERY,
                1,
                score=0.64,
                score_today=0.6,
                score_prev=0.7,
                runs=[FRI, TODAY],
                stance="bullish",
                origins=["youtube:arete", "youtube:fx"],
            ),
            _m(
                "QCOM",
                Tier.DISCOVERY,
                2,
                score=0.28,
                score_prev=0.7,
                runs=[FRI],
                stance="bearish",
                origins=["youtube:unknown"],
            ),
        ],  # fmt: skip
        trending=[_m("APLD", Tier.TRENDING, 1, inputs=2, score=0.98, score_today=0.98)],
        active_max=50,
        tier_sizes={Tier.MOMENTUM: 2},
        as_of=TODAY,
        config_version=3,
    )


def _seed(tmp_path: Path) -> Path:
    db = _db(tmp_path)
    _write(db, "universe_tier", "momentum",
           UniverseTierPayload(tier=Tier.MOMENTUM, members=[_m("MU", Tier.MOMENTUM, 1)],
                               fetched_at=dt.datetime(2026, 9, 28, 12, tzinfo=ET), source="sa"),
           at=dt.datetime(2026, 9, 28, 12, tzinfo=ET), ttl=dt.timedelta(days=35))  # fmt: skip
    fri_at = dt.datetime(2026, 10, 2, 6, tzinfo=ET)
    _write(db, "universe_tier", "discovery",
           UniverseTierPayload(tier=Tier.DISCOVERY, members=[_m("RKLB", Tier.DISCOVERY, 1),
                                                              _m("QCOM", Tier.DISCOVERY, 2)],
                               fetched_at=fri_at, source="scout"),
           at=fri_at, ttl=dt.timedelta(hours=48))  # fmt: skip
    mon_at = dt.datetime(2026, 10, 5, 6, tzinfo=ET)
    _write(db, "universe_tier", "discovery",
           UniverseTierPayload(tier=Tier.DISCOVERY, members=[_m("RKLB", Tier.DISCOVERY, 1)],
                               fetched_at=mon_at, source="scout"),
           at=mon_at, ttl=dt.timedelta(hours=48))  # fmt: skip
    _write(db, "active_universe", "active", _active(), at=NOW - dt.timedelta(minutes=5),
           ttl=dt.timedelta(hours=8))  # fmt: skip
    c = connect(db)
    try:
        _candidate(c, "c1", "RKLB", TODAY)
        _candidate(c, "c3", "RKLB", FRI)
        _candidate(c, "c4", "RKLB", dt.date(2026, 8, 3))  # outside 20 sessions
        _candidate(c, "c5", "NVDA", FRI)
        _proposal(c, "p1", "c1", "RKLB", TODAY)
        _proposal(c, "p2", "c1", "RKLB", TODAY)  # 2 proposals, 1 candidate day
        _proposal(c, "p3", "c3", "RKLB", FRI)
        _proposal(c, "p4", "c4", "RKLB", dt.date(2026, 8, 3))
        c.commit()
    finally:
        c.close()
    _sentiment(db, "APLD", bull=8, bear=2, at=NOW - dt.timedelta(hours=2))
    _sentiment(db, "RKLB", bull=1, bear=1, at=NOW - dt.timedelta(hours=1))  # too few tags
    return db


def _read(db: Path, **kw):  # noqa: ANN003, ANN202
    c = connect_ro(db)
    try:
        return load_universe(c, ArcSettings(), now=NOW, **kw)
    finally:
        c.close()


@pytest.fixture
def seeded(tmp_path: Path):  # noqa: ANN201
    return _read(_seed(tmp_path), channel_labels={"arete": "Arete Trading", "fx": "FX Evolution"})


def test_window_is_20_sessions_ending_today() -> None:
    s = window_sessions(TODAY)
    assert len(s) == WINDOW_SESSIONS == 20 and s[-1] == TODAY
    assert s[0] == dt.date(2026, 9, 8)  # 20 sessions back (no holidays in between)
    assert window_sessions(dt.date(2026, 10, 4))[-1] == FRI  # a Sunday ends at Friday


def test_picked_and_proposals_20d(seeded) -> None:  # noqa: ANN001
    rows = {r.ticker: r for r in seeded.active}
    assert rows["RKLB"].picked_20d == 2  # Mon + Fri; the August row is outside
    assert rows["RKLB"].proposals_20d == 3
    assert rows["NVDA"].picked_20d == 1 and rows["NVDA"].proposals_20d == 0
    assert rows["APLD"].picked_20d == 0


def test_in_tier_20d(seeded) -> None:  # noqa: ANN001
    rows = {r.ticker: r for r in seeded.active}
    assert rows["NVDA"].in_tier_20d is None  # core has no feed
    # momentum's monthly list is live 9/28 .. 10/5: 6 sessions
    assert rows["MU"].in_tier_20d == 6
    # discovery: Fri's list (RKLB, QCOM), then Monday's (RKLB): newest in force wins
    assert rows["RKLB"].in_tier_20d == 2
    assert rows["QCOM"].in_tier_20d == 1
    assert rows["APLD"].in_tier_20d == 0  # no trending entry stored


def test_counts_are_one_query_each(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    c = connect_ro(db)
    try:
        s = window_sessions(TODAY)
        assert picked_counts(c, s) == {"RKLB": 2, "NVDA": 1}
        assert proposal_counts(c, s) == {"RKLB": 3}
        it = in_tier_counts(c, s)
        assert it[("discovery", "RKLB")] == 2 and it[("momentum", "MU")] == 6
        assert picked_counts(c, []) == {} and in_tier_counts(c, []) == {}
    finally:
        c.close()


def test_structured_sentiment(seeded) -> None:  # noqa: ANN001
    rows = {r.ticker: r for r in seeded.active}
    a = rows["APLD"]
    assert a.sentiment is not None and a.sentiment.startswith("ST 80% bull (10 tagged")
    assert (a.sentiment_bull_pct, a.sentiment_tagged, a.sentiment_age_s) == (80.0, 10, 7200)
    r = rows["RKLB"]  # too few tags: tagged + age, no percent
    assert r.sentiment and "too few tags" in r.sentiment
    assert (r.sentiment_bull_pct, r.sentiment_tagged, r.sentiment_age_s) == (None, 2, 3600)
    n = rows["NVDA"]  # no entry
    assert (n.sentiment, n.sentiment_bull_pct, n.sentiment_tagged, n.sentiment_age_s) == (
        None,
        None,
        None,
        None,
    )


def test_momentum_weight_stored_or_parsed(seeded) -> None:  # noqa: ANN001
    rows = {r.ticker: r for r in seeded.active}
    assert rows["MU"].weight_pct == 9.48  # parsed from the pre-v5 reason
    assert rows["GE"].weight_pct == 3.1  # stored by the writer
    assert rows["NVDA"].weight_pct is None  # not momentum


@pytest.mark.parametrize(
    ("reason", "want"),
    [
        ("SPMO weight 9.48% (row 1)", 9.48),
        ("SPMO weight 12% (row 1, 2), incl. GOOG", 12.0),
        ("SPMO weight 0.75% (row 24)", 0.75),
        ("no weight here", None),
        ("", None),
    ],
)
def test_weight_from_reason(reason: str, want: float | None) -> None:
    assert weight_from_reason(reason) == want


def test_momentum_writer_stores_weight() -> None:
    fetch = MomentumFetch(
        picks=[MomentumPick(symbol="MU", name="Micron", weight=9.4812, source_ranks=(1,))],
        rows=[],
        dropped=[],
        size=20,
        source="stockanalysis",
        as_of=FRI,
        digest="d",
        url="https://x",
        partial=False,
    )
    m = build_payload(fetch, now=NOW).members[0]
    assert m.weight_pct == 9.4812 and m.reason.startswith("SPMO weight 9.48%")


def test_discovery_labels_carried_and_tier_counts(seeded) -> None:  # noqa: ANN001
    rows = {r.ticker: r for r in seeded.active}
    assert rows["RKLB"].origin_labels == ["Arete Trading", "FX Evolution"]
    assert rows["QCOM"].origin_labels == ["unknown"]  # unknown slug reads as itself
    assert rows["RKLB"].carried is False and rows["QCOM"].carried is True
    assert rows["APLD"].carried is False and rows["NVDA"].carried is False
    tiers = {t.name: t for t in seeded.tiers}
    assert (tiers["momentum"].listed, tiers["momentum"].active) == (3, 2)  # top 2 of 3 listed
    assert tiers["discovery"].carried == 1 and tiers["trending"].carried == 0


def test_route_serializes_new_fields(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arc.tower.api import create_app

    db = _seed(tmp_path)
    with TestClient(create_app(db, clock=lambda: NOW)) as client:
        res = client.get("/api/ops/universe")
    assert res.status_code == 200
    body = json.loads(res.text)
    rk = next(r for r in body["active"] if r["ticker"] == "RKLB")
    assert rk["picked_20d"] == 2 and rk["proposals_20d"] == 3 and rk["in_tier_20d"] == 2
    # the route maps origins through config/routines.yaml youtube.briefs labels
    assert rk["origin_labels"][0] == "Arete Trading"
    assert all("offered" not in t for t in body["tiers"])
