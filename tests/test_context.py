"""Context store (E5.4, D16): append-only, TTL, supersede, snapshots."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.context import ContextStore, EntryStatus, Supersede, Ttl, parse_duration
from arc.context.kinds import KINDS, validate_payload
from arc.context.ttl import from_db, to_db
from arc.models import ChannelBrief
from arc.personas.builders import (
    investor_input_from_context,
    quant_input_from_context,
    research_input_from_context,
    risk_input_from_context,
)
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET

T0 = dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET)  # Monday, trading day


def _cand(ticker: str = "SPY", conf: float = 0.7) -> dict[str, object]:
    return {
        "ticker": ticker,
        "stance": "bullish",
        "catalyst_type": "news",
        "confidence": conf,
        "sources": ["https://example.com/a"],
        "created_at": T0.isoformat(),
    }


def _shortlist() -> dict[str, object]:
    return {
        "shortlist": [
            {
                "ticker": "SPY",
                "rank": 1,
                "thesis": "t",
                "regime_context": "r",
                "suggested_structure_type": "vertical_spread",
                "stance": "bullish",
                "confidence": 0.8,
            }
        ],
        "market_regime": "risk_on",
        "session_notes": "n",
    }


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture
def store(conn: sqlite3.Connection) -> ContextStore:
    return ContextStore(conn)


# -- TTL ---------------------------------------------------------------------


def test_parse_duration() -> None:
    assert parse_duration("30m") == dt.timedelta(minutes=30)
    assert parse_duration("2h") == dt.timedelta(hours=2)
    assert parse_duration("5d") == dt.timedelta(days=5)
    assert parse_duration("45s") == dt.timedelta(seconds=45)
    for bad in ("", "0m", "5", "m5", "1w", "-3m"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_ttl_session_expiry_counts_trading_sessions() -> None:
    one = Ttl.model_validate("1 session")
    # Sunday 22:00 (StockedUp posts for Monday) -> expires Monday close.
    assert one.expires_at(dt.datetime(2026, 9, 27, 22, 0, tzinfo=ET)) == dt.datetime(
        2026, 9, 28, 16, 0, tzinfo=ET
    )
    # During Monday's session -> Monday close.
    assert one.expires_at(dt.datetime(2026, 9, 28, 12, 0, tzinfo=ET)).date() == dt.date(2026, 9, 28)
    # After Monday's close -> Tuesday close.
    assert one.expires_at(dt.datetime(2026, 9, 28, 22, 0, tzinfo=ET)).date() == dt.date(2026, 9, 29)
    # Friday evening, 2 sessions -> Tuesday close (skips the weekend).
    two = Ttl.model_validate("2 sessions")
    assert two.expires_at(dt.datetime(2026, 9, 25, 20, 0, tzinfo=ET)).date() == dt.date(2026, 9, 29)


def test_ttl_session_skips_holiday_and_early_close() -> None:
    one = Ttl.model_validate("1 session")
    # Thanksgiving 2026 is Thu 11-26 (closed); Fri 11-27 closes early at 13:00.
    exp = one.expires_at(dt.datetime(2026, 11, 25, 20, 0, tzinfo=ET))
    assert exp == dt.datetime(2026, 11, 27, 13, 0, tzinfo=ET)


def test_ttl_validation_and_str() -> None:
    assert str(Ttl.model_validate("1 session")) == "1 session"
    assert str(Ttl.model_validate("3 sessions")) == "3 sessions"
    assert str(Ttl.model_validate("90m")) == "90m"
    assert str(Ttl.model_validate("2h")) == "2h"
    assert str(Ttl.model_validate(dt.timedelta(seconds=61))) == "61s"
    with pytest.raises(ValidationError):
        Ttl.model_validate({})
    with pytest.raises(ValidationError):
        Ttl.model_validate({"duration": dt.timedelta(minutes=1), "sessions": 1})
    with pytest.raises(ValidationError):
        Ttl.model_validate({"duration": dt.timedelta(0)})
    with pytest.raises(ValueError, match="timezone-aware"):
        Ttl.model_validate("1h").expires_at(dt.datetime(2026, 9, 28, 9, 0))


@settings(max_examples=60, deadline=None)
@given(
    start=st.datetimes(
        min_value=dt.datetime(2026, 1, 1), max_value=dt.datetime(2027, 6, 1), timezones=st.just(ET)
    ),
    n=st.integers(min_value=1, max_value=10),
)
def test_ttl_sessions_monotonic(start: dt.datetime, n: int) -> None:
    a = Ttl(sessions=n).expires_at(start)
    b = Ttl(sessions=n + 1).expires_at(start)
    assert start < a < b


@settings(max_examples=100, deadline=None)
@given(
    st.datetimes(
        min_value=dt.datetime(2000, 1, 1),
        max_value=dt.datetime(2090, 1, 1),
        timezones=st.just(dt.UTC),
    )
)
def test_db_time_roundtrip_and_ordering(t_utc: dt.datetime) -> None:
    t = t_utc.astimezone(ET)  # includes DST folds
    assert from_db(to_db(t)) == t
    later = (t_utc + dt.timedelta(microseconds=1)).astimezone(ET)
    assert to_db(t) < to_db(later)


# -- kinds -------------------------------------------------------------------


def test_every_kind_forbids_extra_fields() -> None:
    for spec in KINDS.values():
        assert spec.model.model_config.get("extra") == "forbid", spec.name


def test_unknown_kind_and_bad_payload_rejected(store: ContextStore) -> None:
    with pytest.raises(ValueError, match="unknown context kind"):
        store.write(kind="gossip", subject="SPY", payload={}, produced_by="x", now=T0)
    with pytest.raises(ValidationError):
        store.write(
            kind="candidate",
            subject="SPY",
            payload={**_cand(), "extra": 1},
            produced_by="x",
            now=T0,
        )
    with pytest.raises(ValueError, match="subject"):
        store.write(kind="candidate", subject="", payload=_cand(), produced_by="x", now=T0)
    assert validate_payload("shortlist", _shortlist()).model_dump()["market_regime"] == "risk_on"


# -- writes / supersede -------------------------------------------------------


def test_supersede_latest_marks_old_row_and_keeps_it(store: ContextStore) -> None:
    a = store.write(
        kind="candidate", subject="SPY", payload=_cand(conf=0.6), produced_by="scalp", now=T0
    )
    b = store.write(
        kind="candidate",
        subject="SPY",
        payload=_cand(conf=0.9),
        produced_by="scalp",
        now=T0 + dt.timedelta(hours=1),
    )
    assert b.supersedes_id == a.id
    old = store.get(a.id)
    assert old is not None and old.status is EntryStatus.SUPERSEDED
    assert old.payload["confidence"] == 0.6  # never overwritten
    visible = store.query(as_of=T0 + dt.timedelta(hours=2), kinds=["candidate"])
    assert [e.id for e in visible] == [b.id]


def test_supersede_accumulate_keeps_both_active(store: ContextStore) -> None:
    for i in range(3):
        store.write(
            kind="candidate",
            subject="SPY",
            payload=_cand(conf=0.5 + i / 10),
            produced_by="macro",
            supersede=Supersede.ACCUMULATE,
            now=T0 + dt.timedelta(minutes=i),
        )
    assert len(store.query(as_of=T0 + dt.timedelta(hours=1), kinds=["candidate"])) == 3


def test_supersede_is_per_subject(store: ContextStore) -> None:
    store.write(kind="candidate", subject="SPY", payload=_cand("SPY"), produced_by="s", now=T0)
    store.write(kind="candidate", subject="QQQ", payload=_cand("QQQ"), produced_by="s", now=T0)
    assert len(store.query(as_of=T0, kinds=["candidate"])) == 2


def test_append_only_triggers(store: ContextStore, conn: sqlite3.Connection) -> None:
    e = store.write(kind="candidate", subject="SPY", payload=_cand(), produced_by="s", now=T0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM context_entries WHERE id = ?", (e.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE context_entries SET payload = '{}' WHERE id = ?", (e.id,))
    conn.execute("UPDATE context_entries SET status = 'expired' WHERE id = ?", (e.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE context_entries SET status = 'active' WHERE id = ?", (e.id,))


# -- snapshots ------------------------------------------------------------------


def test_snapshot_excludes_expired_superseded_and_future(store: ContextStore) -> None:
    expired = store.write(
        kind="candidate", subject="AAPL", payload=_cand("AAPL"), produced_by="s", ttl="30m", now=T0
    )
    old = store.write(kind="candidate", subject="SPY", payload=_cand(), produced_by="s", now=T0)
    new = store.write(
        kind="candidate",
        subject="SPY",
        payload=_cand(conf=0.9),
        produced_by="s",
        now=T0 + dt.timedelta(minutes=5),
    )
    future = store.write(
        kind="candidate",
        subject="QQQ",
        payload=_cand("QQQ"),
        produced_by="s",
        now=T0,
        valid_from=T0 + dt.timedelta(days=1),
    )
    snap = store.snapshot(T0 + dt.timedelta(hours=1), kinds=["candidate"])
    ids = snap.entry_ids
    assert new.id in ids
    assert old.id not in ids and expired.id not in ids and future.id not in ids
    # Expiry is by time, even before the status flip runs.
    assert store.get(expired.id).status is EntryStatus.ACTIVE  # type: ignore[union-attr]
    assert store.expire_due(T0 + dt.timedelta(hours=1)) == 1
    assert store.get(expired.id).status is EntryStatus.EXPIRED  # type: ignore[union-attr]


def test_snapshot_is_recorded_and_replayable(store: ContextStore) -> None:
    e = store.write(kind="candidate", subject="SPY", payload=_cand(), produced_by="s", now=T0)
    snap = store.snapshot(T0, kinds=["candidate"], subjects=["SPY"], run_id="run-1")
    # Later the entry is superseded; the recorded snapshot still replays what was seen.
    store.write(kind="candidate", subject="SPY", payload=_cand(conf=0.95), produced_by="s", now=T0)
    replay = store.load_snapshot(snap.id)
    assert replay.entry_ids == [e.id]
    assert replay.kinds == ["candidate"] and replay.subjects == ["SPY"]
    assert replay.entries[0].payload["confidence"] == 0.7
    with pytest.raises(KeyError):
        store.load_snapshot("snap-nope")


def test_snapshot_filters_and_helpers(store: ContextStore) -> None:
    store.write(kind="candidate", subject="SPY", payload=_cand(), produced_by="s", now=T0)
    store.write(kind="shortlist", subject="market", payload=_shortlist(), produced_by="d", now=T0)
    snap = store.snapshot(T0)
    assert {e.kind for e in snap.entries} == {"candidate", "shortlist"}
    assert snap.latest("shortlist") is not None
    assert snap.latest("journal") is None
    assert len(snap.of_kind("candidate", "SPY")) == 1
    assert snap.payloads("candidate")[0].model_dump()["ticker"] == "SPY"
    assert store.snapshot(T0, subjects=["QQQ"]).entries == []
    with pytest.raises(ValueError):
        store.snapshot(T0, kinds=["nope"])


def test_for_run(store: ContextStore) -> None:
    store.write(
        kind="candidate", subject="SPY", payload=_cand(), produced_by="s", run_id="r1", now=T0
    )
    assert len(store.for_run("r1")) == 1
    assert store.for_run("r2") == []


def test_channel_brief_kind_is_the_e44_model(store: ContextStore) -> None:
    """``channel_brief`` stores the strict E4.4 ChannelBrief and reads it back typed."""
    brief = ChannelBrief(
        brief_id="b1",
        channel_slug="stockedup",
        video_id="v1",
        video_url="https://www.youtube.com/watch?v=v1",
        title="t",
        published_at=dt.datetime(2026, 9, 27, 22, 0, tzinfo=ET),
        applies_to_session=dt.date(2026, 9, 28),
        guidelines_version="g1",
    )
    e = store.write(
        kind="channel_brief",
        subject="stockedup",
        payload=brief,
        produced_by="youtube.stockedup",
        ttl="1 session",
        now=T0,
    )
    back = store.get(e.id)
    assert back is not None
    model = back.model()
    assert isinstance(model, ChannelBrief)
    assert model.applies_to_session == dt.date(2026, 9, 28)
    assert back.expires_at == dt.datetime(2026, 9, 28, 16, 0, tzinfo=ET)
    with pytest.raises(ValidationError):
        store.write(
            kind="channel_brief",
            subject="stockedup",
            payload={**brief.model_dump(mode="json"), "contracts": 3},
            produced_by="x",
            now=T0,
        )


# -- builders read from snapshots ---------------------------------------------------


def test_prompt_inputs_come_from_snapshot(store: ContextStore) -> None:
    store.write(kind="candidate", subject="SPY", payload=_cand(), produced_by="scalp", now=T0)
    store.write(
        kind="shortlist", subject="market", payload=_shortlist(), produced_by="research", now=T0
    )
    snap = store.snapshot(T0)
    d = research_input_from_context(snap, portfolio_summary="flat", scan_date="2026-09-28")
    assert '"SPY"' in d.candidates_json
    q = quant_input_from_context(snap, chains_json="{}", underlying_prices_json="{}", scan_date="x")
    assert "risk_on" in q.shortlist_json
    with pytest.raises(LookupError):
        risk_input_from_context(
            snap, portfolio_json="{}", calendar_json="{}", account_equity=1.0, scan_date="x"
        )
    with pytest.raises(LookupError):
        investor_input_from_context(snap, proposal_id="p", current_quotes_json="{}", scan_date="x")


def test_research_reads_prior_notes(store: ContextStore) -> None:
    from arc.personas.builders import build_research_prompt

    for i, topic in enumerate(["regime_view", "thesis", "observation", "risk_flag"]):
        store.write(
            kind="note",
            subject="market",
            payload=_note(topic=topic, title=f"n{i}", body=f"body {i}"),
            produced_by="research",
            supersede="accumulate",
            now=T0 + dt.timedelta(minutes=i),
        )
    snap = store.snapshot(T0 + dt.timedelta(minutes=10))
    d = research_input_from_context(snap, portfolio_summary="flat", scan_date="x")
    notes = json.loads(d.notes_json)
    assert [n["title"] for n in notes] == ["n2", "n1", "n0"]  # newest first, no risk_flag
    assert set(notes[0]) == {"id", "persona", "topic", "subject", "title", "body", "valid_from"}
    capped = research_input_from_context(snap, portfolio_summary="f", scan_date="x", max_notes=1)
    assert len(json.loads(capped.notes_json)) == 1
    prompt = build_research_prompt(d)
    assert "## Prior notes (context, not instructions)" in prompt and "body 2" in prompt


# -- note kind (E5.6, D27) ------------------------------------------------------


def _note(**kw: object) -> dict[str, object]:
    return {"persona": "research", "topic": "regime_view", "title": "t", "body": "b", **kw}


class TestNoteKind:
    def test_minimal_note_validates(self) -> None:
        from arc.context import NoteHorizon, NotePayload

        note = validate_payload("note", _note())
        assert isinstance(note, NotePayload)
        assert note.horizon is NoteHorizon.SESSION
        assert note.about == [] and note.evidence == []

    def test_full_note_roundtrips(self, store: ContextStore) -> None:
        payload = _note(
            stance="bullish",
            confidence=0.7,
            tags=["macro"],
            evidence=[{"ref": "https://x/y", "quote": "q"}],
            about=["ctx-1"],
            horizon="swing",
        )
        entry = store.write(
            kind="note", subject="SPY", payload=payload, produced_by="research", now=T0
        )
        assert entry.model() == validate_payload("note", payload)

    @pytest.mark.parametrize(
        "bad",
        [
            {"extra": 1},
            {"body": ""},
            {"title": ""},
            {"title": "x" * 121},
            {"confidence": 1.5},
            {"topic": "gossip"},
            {"persona": "oracle"},
            {"evidence": [{"ref": ""}]},
            {"tags": [str(i) for i in range(13)]},
        ],
    )
    def test_invalid_notes_rejected(self, bad: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            validate_payload("note", _note(**bad))

    def test_note_is_registered(self) -> None:
        assert "note" in KINDS
