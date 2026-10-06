"""E8.7d Ops & pipeline: ``/api/ops/*`` and ``arc.tower.data_ops`` loaders.

Every section runs on the fixture DB with its ops rows (``scripts/tower_fixture_db.py
--ops``: the dispatcher's dry-run plan for yesterday + today persisted with outcomes and a
D27 manifest per run, alerts, halts, context of every kind, LLM calls, D26 changes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.config import ArcSettings
from arc.context.kinds import KINDS
from arc.context.trace import trace_runs
from arc.routines.config import load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.data import connect_ro
from arc.tower.data_ops import (
    TIMELINE_START,
    load_alerts,
    load_budget,
    load_config,
    load_context,
    load_halts,
    load_health,
    load_llm,
    load_run,
    load_runs,
    load_session,
    load_sources,
    resolve_day,
)
from arc.tower.routes.ops import NOTICE_CHANNEL
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 9, 30, 13, 42, tzinfo=ET)
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


@pytest.fixture(scope="module")
def fx_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("ops") / "arc.db", NOW, ops=True)


@pytest.fixture
def conn(fx_db: Path):
    c = connect_ro(fx_db)
    yield c
    c.close()


@pytest.fixture
def client(fx_db: Path):
    with TestClient(create_app(fx_db, clock=lambda: NOW)) as c:
        yield c


@pytest.fixture(scope="module")
def routines():
    return load_routines()


def _undeclared(conn: sqlite3.Connection) -> str:
    rid = ops_fixture.undeclared_run_id(conn)
    assert rid
    return str(rid)


# ---------------------------------------------------------------------------
# Session timeline
# ---------------------------------------------------------------------------


def test_resolve_day() -> None:
    assert resolve_day(None, NOW) == TODAY
    assert resolve_day("today", NOW) == TODAY
    assert resolve_day("yesterday", NOW) == TODAY - dt.timedelta(days=1)
    assert resolve_day("2026-09-01", NOW) == dt.date(2026, 9, 1)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        resolve_day("last week", NOW)


def test_session_has_every_scheduled_job_and_the_loop_row(conn, routines) -> None:
    from arc.routines.schedule import slots_between

    s = load_session(conn, routines, now=NOW, day=TODAY)
    assert s.loop is not None and s.loop.job == routines.loop.job == s.loop_job
    assert s.loop.kind == "loop"
    start = dt.datetime.combine(TODAY, TIMELINE_START, tzinfo=ET)
    end = dt.datetime.combine(TODAY, dt.time(22), tzinfo=ET)
    expected = {
        name
        for name, (_, spec) in routines.jobs().items()
        if slots_between(spec, start - dt.timedelta(microseconds=1), end)
    }
    got = {r.job for r in s.rows} | {s.loop.job}
    assert got == expected
    for row in [*s.rows, s.loop]:
        assert all(start <= slot.at <= end for slot in row.slots)
        assert [x.at for x in row.slots] == sorted(x.at for x in row.slots)


def test_session_slot_statuses(conn, routines) -> None:
    s = load_session(conn, routines, now=NOW, day=TODAY)
    slots = [x for r in [*s.rows, s.loop] if r for x in r.slots]
    by = {x.status for x in slots}
    assert {"done", "no_change", "failed", "skipped", "future", "running"} <= by
    # past slots all have a run (the fixture persists the full plan) -> nothing missed
    assert s.counts.get("missed", 0) == 0
    assert all(x.at > NOW for x in slots if x.status == "future")
    assert all(x.run is None for x in slots if x.status == "future")
    loop = s.loop
    assert loop is not None
    full = [x for x in loop.slots if x.status == "done"]
    quiet = [x for x in loop.slots if x.status == "no_change"]
    assert full and quiet and len(quiet) > len(full)  # D31: most loop runs are no_change
    assert all(x.run and x.run.no_change for x in quiet)
    assert all(x.chain_steps >= 1 for x in full)  # the chain steps recorded under it
    failed = [x for x in slots if x.status == "failed"]
    assert [x.job for x in failed] == ["edgar"]
    assert failed[0].run and failed[0].run.error and "503" in failed[0].run.error
    assert sum(s.counts.values()) == len(slots)


def test_session_yesterday_is_all_past(conn, routines) -> None:
    s = load_session(conn, routines, now=NOW, day=TODAY - dt.timedelta(days=1))
    slots = [x for r in [*s.rows, s.loop] if r for x in r.slots]
    assert slots and not [x for x in slots if x.status in ("future", "running")]


def test_session_marks_missed_slots_without_runs(tmp_path, routines) -> None:
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    c.close()
    ro = connect_ro(db)
    s = load_session(ro, routines, now=NOW, day=TODAY)
    ro.close()
    slots = [x for r in [*s.rows, s.loop] if r for x in r.slots]
    assert {x.status for x in slots} == {"missed", "future"}
    assert s.counts["missed"] == sum(1 for x in slots if x.at <= NOW)


def test_session_endpoint(client) -> None:
    r = client.get("/api/ops/session")
    assert r.status_code == 200
    body = r.json()
    assert body["day"] == TODAY.isoformat()
    assert body["loop"]["job"] == body["loop_job"]
    assert client.get("/api/ops/session?day=yesterday").json()["day"] == "2026-09-29"
    bad = client.get("/api/ops/session?day=someday")
    assert bad.status_code == 422
    assert bad.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Health, alerts, halts
# ---------------------------------------------------------------------------


def test_health_strip(conn, tmp_path) -> None:
    log = tmp_path / "arc.jsonl"
    log.write_bytes(b"x" * 2_000)
    h = load_health(
        conn, now=NOW, tick_stale_s=900, health_every_s=1800, log_path=log, log_max_bytes=1_000
    )
    items = {i.key: i for i in h.items}
    assert list(items) == ["tick", "health", "gateway", "remote_access", "log"]
    assert items["tick"].status == "ok" and items["tick"].age_s == 60
    assert items["tick"].threshold == "stale after 15 min"
    assert items["health"].status == "failed"  # the heartbeat's own status
    assert items["health"].threshold == "runs every 30 min; stale after 90 min"
    assert items["gateway"].status == "ok"
    assert items["remote_access"].status == "ok"
    assert items["log"].status == "degraded"  # 2 KB > 1 KB rotation size
    assert h.checks["routine_windows"]["severity"] == "failed"
    # E8.8d: compact chip text (the long text stays in value/threshold for the ⓘ)
    chips = {i.key: i.chip for i in h.items}
    assert chips["tick"] == "Tick ok 1m"
    assert chips["health"].startswith("Health check failed ")
    assert chips["gateway"] == "Gateway ok" and chips["remote_access"] == "Remote ok"
    assert chips["log"] == "Log 0.002 / 0.001 MB"
    # a stale tick is failed whatever its recorded status
    late = load_health(conn, now=NOW + dt.timedelta(hours=1), tick_stale_s=900, health_every_s=1800)
    assert {i.key: i.status for i in late.items}["tick"] == "failed"
    assert {i.key: i.status for i in late.items}["log"] == "unknown"


def test_alerts_open_first_and_windowed(conn) -> None:
    a = load_alerts(conn, now=NOW)
    assert a.open == 1
    assert a.alerts[0].open and not any(x.open for x in a.alerts[1:])
    week = NOW - dt.timedelta(days=7)
    assert all(x.resolved_at is None or x.resolved_at >= week for x in a.alerts)
    assert "remote_access" not in {x.key for x in a.alerts}  # resolved 12 days ago
    stuck = next(x for x in a.alerts if x.key == "stuck:monitor")
    assert stuck.duration_s == 14 * 60
    assert stuck.open is False


def test_halts_active_first_with_trade_window(conn) -> None:
    h = load_halts(conn, now=NOW)
    assert h.active == 1 and h.halts[0].active
    cleared = next(x for x in h.halts if x.id == "halt-ops-daily-loss")
    assert cleared.cleared_by == "owner (Slack)"
    assert cleared.trades_route == "/trades?date=custom&date_from=2026-09-29&date_to=2026-09-29"
    assert h.halts[0].trades >= 0


# ---------------------------------------------------------------------------
# Runs + run detail
# ---------------------------------------------------------------------------


def test_runs_filters_and_paging(conn) -> None:
    all_ = load_runs(conn, now=NOW, size=10)
    assert all_.total == conn.execute("SELECT COUNT(*) FROM routine_runs").fetchone()[0]
    assert len(all_.rows) == 10
    keys = [(r.scheduled_for, r.step_index) for r in all_.rows]
    assert keys == sorted(keys, reverse=True)
    page2 = load_runs(conn, now=NOW, size=10, page=2)
    assert not {r.run_id for r in page2.rows} & {r.run_id for r in all_.rows}

    failed = load_runs(conn, now=NOW, statuses=["failed"])
    assert [r.job for r in failed.rows] == ["edgar"]
    quiet = load_runs(conn, now=NOW, statuses=["no_change"], size=200)
    assert quiet.total > 0 and all(r.no_change and r.status == "ok" for r in quiet.rows)
    ok = load_runs(conn, now=NOW, statuses=["ok"], size=200)
    assert not any(r.no_change for r in ok.rows)
    both = load_runs(conn, now=NOW, statuses=["ok", "no_change"])
    assert both.total == ok.total + quiet.total

    today = load_runs(conn, now=NOW, day=TODAY, jobs=["edgar", "rss"], size=200)
    assert {r.job for r in today.rows} <= {"edgar", "rss"}
    assert all(r.scheduled_for and r.scheduled_for.date() == TODAY for r in today.rows)

    chain_id = next(r.chain_run_id for r in all_.rows if r.chain_run_id)
    chain = load_runs(conn, now=NOW, chain=chain_id)
    assert chain.total >= 2 and all(r.chain_run_id == chain_id for r in chain.rows)
    assert "research" in chain.options.jobs


def test_runs_endpoint(client) -> None:
    body = client.get("/api/ops/runs?status=failed,no_change&job=edgar,research&size=5").json()
    assert body["size"] == 5
    assert {r["job"] for r in body["rows"]} <= {"edgar", "research"}
    assert client.get("/api/ops/runs?day=nope").status_code == 422
    assert client.get("/api/ops/runs?size=500").status_code == 422


def test_run_detail_reuses_the_trace_serializer(conn, fx_db) -> None:
    rid = _undeclared(conn)
    d = load_run(conn, rid, now=NOW)
    t = trace_runs(conn, rid)[0]
    assert d.step.run.run_id == rid == t["run_id"]
    assert d.step.contract.declared_writes == t["declared"]["writes"]
    assert d.step.contract.undeclared_writes == t["contract"]["undeclared_writes"]
    assert [e.id for e in d.step.read] == [e["id"] for e in t["read"]]
    assert d.step.manifest == t["manifest"]
    # the CLI's `arc context trace` is the same function
    from arc.routines import cli

    assert cli.trace_runs(conn, rid) == trace_runs(conn, rid)


def test_run_detail_highlights_the_undeclared_write(conn) -> None:
    d = load_run(conn, _undeclared(conn), now=NOW, slack_channel=NOTICE_CHANNEL)
    c = d.step.contract
    assert c.ok is False
    assert c.undeclared_writes == ["proposal"] and c.undeclared_reads == []
    assert "proposal" not in (c.declared_writes or [])
    assert [e.kind for e in d.step.wrote if e.undeclared] == ["proposal"]
    assert not [e for e in d.step.read if e.undeclared]
    # outputs link to context entries, the chain lists every step in order
    assert (d.step.outputs["proposal"][0].route or "").startswith("/ops/context/")
    assert [s.run.step_index for s in d.chain] == sorted(s.run.step_index for s in d.chain)
    assert len(d.chain) >= 2 and d.chain[0].run.run_id == d.run_id
    assert d.step.persona_calls and d.step.persona_calls[0].cost_usd
    assert (
        d.step.slack_posts[0].route
        == f"https://slack.com/archives/{NOTICE_CHANNEL}/p1790000000000300"
    )


def test_notice_channel_matches_the_slack_client() -> None:
    from arc.slack.client import CHANNEL_ARC_INVESTOR

    assert NOTICE_CHANNEL == CHANNEL_ARC_INVESTOR


def test_run_detail_declared_run_is_ok(conn) -> None:
    rid = conn.execute(
        "SELECT run_id FROM routine_runs WHERE job = 'edgar' AND status = 'ok' LIMIT 1"
    ).fetchone()[0]
    d = load_run(conn, rid, now=NOW)
    assert d.step.contract.ok and d.chain == []
    m = d.step.manifest
    assert m and m["git_sha"] and m["config_hashes"] and m["external_inputs"]


def test_run_detail_log_tail(conn, tmp_path) -> None:
    rid = _undeclared(conn)
    log = tmp_path / "logs" / "arc.jsonl"
    ops_fixture.write_log(tmp_path / "arc.db", rid, NOW)
    (tmp_path / "logs" / "arc.jsonl.1").write_text(
        json.dumps({"ts": "t0", "level": "info", "event": "old", "run_id": rid}) + "\nnot json\n"
    )
    d = load_run(conn, rid, now=NOW, log_path=log)
    assert d.log_available
    assert [x.event for x in d.log] == [
        "old",
        "routines.started",
        "context.undeclared_write",
        "routines.finished",
    ]
    assert d.log[2].level == "warning" and d.log[2].fields["kind"] == "proposal"
    none = load_run(conn, rid, now=NOW, log_path=tmp_path / "missing.jsonl")
    assert none.log == [] and not none.log_available


def test_run_detail_endpoint(client, conn) -> None:
    rid = _undeclared(conn)
    body = client.get(f"/api/ops/runs/{rid}").json()
    assert body["step"]["contract"]["undeclared_writes"] == ["proposal"]
    missing = client.get("/api/ops/runs/run-nope")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# Budget, context, sources, LLM, config
# ---------------------------------------------------------------------------


def test_budget_is_the_d32_local_count(conn) -> None:
    from arc.budget.orders import OrderBudgetConfig, count_orders

    s = ArcSettings()
    b = load_budget(conn, s, now=NOW)
    assert b is not None
    count = count_orders(conn, None, TODAY)
    cfg = OrderBudgetConfig.from_settings(s)
    assert (b.used, b.local, b.reserved) == (count.used, count.local, count.reserved)
    assert (b.limit, b.restrict_at) == (cfg.daily_max, cfg.restrict_at)
    assert b.open_limit == cfg.daily_max - cfg.close_reserve
    assert b.broker_checked is False
    assert all(o.created_at and o.created_at.date() == TODAY for o in b.orders)
    assert sum(b.by_state.values()) == len(b.orders)


def test_budget_hidden_without_execution_tables(tmp_path) -> None:
    db = tmp_path / "bare.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE routine_runs (run_id TEXT)")
    c.close()
    ro = connect_ro(db)
    assert load_budget(ro, ArcSettings(), now=NOW) is None
    ro.close()


def test_context_counts_every_kind(conn, routines) -> None:
    c = load_context(conn, routines, now=NOW)
    kinds = {k.kind: k for k in c.kinds}
    assert set(KINDS) <= set(kinds)
    assert all(k.active >= 1 for k in kinds.values() if k.kind in KINDS)
    nowdb = NOW.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    direct = conn.execute(
        "SELECT COUNT(*) FROM context_entries WHERE status = 'active' AND valid_from <= ?"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (nowdb, nowdb),
    ).fetchone()[0]
    assert c.total_active == direct
    assert c.expired_24h >= 3
    assert kinds["candidate"].expired_24h >= 1
    journal = kinds["journal"]
    assert journal.next_expiry is None and journal.remaining_fraction is None
    shortlist = kinds["shortlist"]
    assert shortlist.remaining_fraction is not None and 0 <= shortlist.remaining_fraction <= 1
    assert shortlist.latest_id and shortlist.latest_at


def test_context_entry_endpoint(client, conn) -> None:
    eid = conn.execute("SELECT id FROM context_entries LIMIT 1").fetchone()[0]
    body = client.get(f"/api/ops/context/{eid}").json()
    assert body["id"] == eid and body["payload"]
    assert client.get("/api/ops/context/nope").status_code == 404


def test_sources_registry_rows(conn, routines) -> None:
    from arc.ingest.sources import SourceRegistry

    s = load_sources(conn, routines, now=NOW)
    reg = SourceRegistry.from_routines(routines)
    raw = [x.key for x in s.sources if x.unit == "docs"]
    assert sorted(raw) == sorted(reg.sources)  # every registry source, once
    typed = [x for x in s.sources if x.unit == "entries"]
    assert {x.job for x in typed} >= {"vol_term", "put_call", "macro_calendar"}
    assert all(x.share_in_category is None for x in typed)
    by = {x.key: x for x in s.sources}
    # E8.8d: one stale doc (E4.7 skipped_stale) and the E4.6 per-channel brief states
    assert sum(x.skipped_stale_today for x in s.sources) == 1
    briefs = {x.brief.outcome for x in s.sources if x.brief is not None}
    assert {"ok", "pending", "no_video"} <= briefs
    assert sum(x.docs_today for x in s.sources) > 0
    assert sum(x.skipped_budget_today for x in s.sources) == 1
    edgar = next(x for x in s.sources if x.job == "edgar")
    assert edgar.failed_24h == 1 and edgar.error_rate and edgar.error_rate > 0
    yt = [x for x in by.values() if x.job.startswith("youtube")]
    assert yt and all(x.backoff and "429 cooldown" in x.backoff for x in yt)
    assert not [x for x in by.values() if x.late]  # every source fetched on schedule


def test_sources_late_after_twice_the_cadence(conn, routines) -> None:
    later = NOW + dt.timedelta(hours=3)  # rss (every 15m) has no run after 13:42
    s = load_sources(conn, routines, now=later)
    late = {x.job for x in s.sources if x.late}
    assert "rss" in late
    rss_cats = {x.category for x in s.sources if x.job == "rss"}
    assert all(c.status in ("late", "failed") for c in s.categories if c.key in rss_cats)


def test_sources_categories_share_parity_with_registry(conn, routines) -> None:
    """E8.8d: category shares and in-category shares are the registry's, never recomputed."""
    from arc.context.categories import CATEGORY_ORDER
    from arc.ingest.sources import SourceRegistry

    s = load_sources(conn, routines, now=NOW)
    reg = SourceRegistry.from_routines(routines)
    cat_w = reg.category_weights()
    eff = reg.effective_weights()
    order = [c.value for c in CATEGORY_ORDER]
    assert [c.key for c in s.categories] == [k for k in order if k in {c.key for c in s.categories}]
    assert {c.key for c in s.categories} == {x.category for x in s.sources}
    for c in s.categories:
        spec = reg.category_spec(next(x for x in CATEGORY_ORDER if x.value == c.key))
        assert c.label == spec.label and c.max_age == str(spec.max_age)
        want = cat_w.get(next(x for x in CATEGORY_ORDER if x.value == c.key))
        assert c.share == (pytest.approx(want, abs=1e-4) if want is not None else None)
        assert c.sources == sum(1 for x in s.sources if x.category == c.key)
    for x in s.sources:
        if x.unit == "docs" and x.key in eff:
            cat = next(c for c in CATEGORY_ORDER if c.value == x.category)
            assert x.share_in_category == pytest.approx(eff[x.key] / cat_w[cat], abs=1e-4)
            assert x.weight == pytest.approx(eff[x.key], abs=1e-4)
    # shares inside a Scalp category sum to 1
    for c in s.categories:
        if c.share is not None:
            inner = [x.share_in_category or 0 for x in s.sources if x.category == c.key]
            assert sum(inner) == pytest.approx(1.0, abs=1e-3)
    assert sum(c.share or 0 for c in s.categories) == pytest.approx(1.0, abs=1e-3)
    # D49: each YouTube channel shows its split of its own YouTube category
    for x in s.sources:
        if x.job == "youtube.briefs":
            assert x.share_in_category == pytest.approx(reg.share_in_category(x.key), abs=1e-4)
    for c in ("youtube_macro", "youtube_micro"):
        inner = [x.share_in_category or 0 for x in s.sources if x.category == c]
        if inner:
            assert sum(inner) == pytest.approx(1.0, abs=1e-3)


def test_sources_category_rollup_is_worst_status(conn, routines) -> None:
    from arc.tower.data_ops import SOURCE_STATUS_RANK

    s = load_sources(conn, routines, now=NOW)
    for c in s.categories:
        rows = [x for x in s.sources if x.category == c.key]
        assert SOURCE_STATUS_RANK[c.status] == max(SOURCE_STATUS_RANK[x.status] for x in rows)
    edgar = next(x for x in s.sources if x.job == "edgar")
    company = next(c for c in s.categories if c.key == edgar.category)
    if edgar.last_run_failed:
        assert edgar.status == "failed" and company.status == "failed"


def test_sources_youtube_brief_status_from_manifest(tmp_path, routines) -> None:
    """E4.6: per-channel brief state from today's youtube.briefs run manifest."""
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    at = dt.datetime.combine(TODAY, dt.time(5, 0), tzinfo=ET)
    from arc.context.ttl import to_db

    c.execute(
        "INSERT INTO routine_runs (run_id, job, scheduled_for, status, reason, started_at,"
        " finished_at, step_index, attempts) VALUES (?,?,?,?,?,?,?,?,?)",
        ("yt-1", "youtube.briefs", to_db(at), "ok", "schedule", to_db(at), to_db(at), 0, 1),
    )
    channels = {
        "stockedup": {"outcome": "briefed", "title": "Market wrap"},
        "fxevolution": {"outcome": "pending", "pending_reason": "no captions yet"},
        "tradebrigade": {"outcome": "no_video"},
        "arete": {"outcome": "error", "error": "listing failed"},
    }
    c.execute(
        "INSERT INTO run_manifests (id, run_id, attempt, job, status, schema_version, payload,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            "m-1",
            "yt-1",
            1,
            "youtube.briefs",
            "ok",
            1,
            json.dumps({"metrics": {"channels": channels}}),
            to_db(at),
        ),
    )
    c.commit()
    c.close()
    ro = connect_ro(db)
    s = load_sources(ro, routines, now=NOW)
    ro.close()
    yt = {x.key: x for x in s.sources if x.job == "youtube.briefs"}
    assert yt["youtube.stockedup"].brief and yt["youtube.stockedup"].brief.text == "brief ok"
    assert yt["youtube.fxevolution"].brief.text == "pending: no captions yet"  # type: ignore[union-attr]
    assert yt["youtube.fxevolution"].status == "pending"
    assert yt["youtube.tradebrigade"].brief.text == "no video in 24h"  # type: ignore[union-attr]
    assert yt["youtube.arete"].status == "failed"
    video = next(c for c in s.categories if c.key == yt["youtube.arete"].category)
    assert video.status == "failed" and video.share is None  # never in the Scalp budget


def test_llm_usage(conn) -> None:
    llm = load_llm(conn, now=NOW, days=30)
    assert len(llm.series) == 30 and llm.series[-1].day == TODAY
    assert llm.today_cost == llm.series[-1].cost_usd > 0
    assert llm.yesterday_cost == llm.series[-2].cost_usd > 0
    assert llm.total_cost == pytest.approx(sum(d.cost_usd for d in llm.series))
    assert {"research", "scalp", "scalp.digest"} <= set(llm.personas)
    assert {g.persona for g in llm.today} <= set(llm.personas)
    assert sum(g.calls for g in llm.today) == llm.series[-1].calls
    costs = [g.cost_usd for g in llm.period]
    assert costs == sorted(costs, reverse=True)
    for d in llm.series:
        assert sum(d.by_persona.values()) == pytest.approx(d.cost_usd)
        assert sum(d.by_model.values()) == pytest.approx(d.cost_usd)


def test_config_via_control_service(conn) -> None:
    from arc.control.registry import REGISTRY

    c = load_config(conn, ArcSettings(), now=NOW)
    assert c.config_version == 4 and c.note is None
    assert set(REGISTRY) <= {k.key for k in c.keys}  # + the per-routine keys
    assert [ch.id for ch in c.changes] == sorted((ch.id for ch in c.changes), reverse=True)
    revert = next(ch for ch in c.changes if ch.status == "reverted")
    assert revert.supersedes_id is not None
    override = {k.key: k for k in c.keys}["max_open_positions"]
    assert override.source == "override" and override.value == 4
    assert override.last_change_by == "U0OWNER"
    # E8.8d: the Auto-Approve widget's key/values, and the same verdict as the gate line
    aa = c.auto_approve
    assert aa is not None and c.scorecard_gate is not None
    assert aa.scorecard_gate in ("off", "met", "unmet")
    assert aa.reason and aa.reason in c.scorecard_gate
    assert aa.blocks == (aa.scorecard_gate == "unmet")
    assert aa.env == "paper"
    by = {k.key: k for k in c.keys}
    assert aa.paper == bool(by["auto_approve.paper"].value)


def test_config_auto_approve_reflects_a_flip(fx_db, tmp_path) -> None:
    """The last flip (key, actor, value) comes from the D26 change log."""
    import shutil

    from arc.control.store import ConfigChangeRepo

    db = tmp_path / "flip.db"
    shutil.copy(fx_db, db)
    rw = connect(db)
    ConfigChangeRepo(rw).append(
        key="auto_approve.scorecard_gate",
        old=False,
        new=True,
        is_default=False,
        actor="U0OWNER",
        reason="hold opens until the scorecard is met",
        source="slack",
        at=NOW,
        status="applied",
        direction="safer",
    )
    rw.commit()
    rw.close()
    ro = connect_ro(db)
    c = load_config(ro, ArcSettings(), now=NOW + dt.timedelta(minutes=1))
    ro.close()
    aa = c.auto_approve
    assert aa is not None
    assert aa.last_flip_key == "auto_approve.scorecard_gate" and aa.last_flip_by == "U0OWNER"
    assert aa.scorecard_gate in ("met", "unmet")


def test_config_without_override_tables(tmp_path) -> None:
    from arc.control.registry import REGISTRY

    db = tmp_path / "old.db"
    sqlite3.connect(db).close()
    ro = connect_ro(db)
    c = load_config(ro, ArcSettings(), now=NOW)
    ro.close()
    assert c.config_version == 0 and c.changes == [] and c.note
    assert set(REGISTRY) <= {k.key for k in c.keys}  # + the per-routine keys
    assert all(k.source == "yaml" for k in c.keys)


# ---------------------------------------------------------------------------
# Read-only + every route answers
# ---------------------------------------------------------------------------

ROUTES = [
    "/api/ops/session",
    "/api/ops/session?day=yesterday",
    "/api/ops/health",
    "/api/ops/alerts",
    "/api/ops/halts",
    "/api/ops/runs",
    "/api/ops/budget",
    "/api/ops/context",
    "/api/ops/sources",
    "/api/ops/llm?days=7",
    "/api/ops/config",
]


def test_every_route_answers_and_leaves_the_db_unchanged(fx_db, client, conn) -> None:
    before = hashlib.sha256(fx_db.read_bytes()).hexdigest()
    rid = _undeclared(conn)
    for path in [*ROUTES, f"/api/ops/runs/{rid}"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert r.json() is not None or path.endswith("budget")
        assert client.post(path.split("?")[0]).status_code == 405
    assert hashlib.sha256(fx_db.read_bytes()).hexdigest() == before


def test_routes_on_an_empty_store(tmp_path) -> None:
    db = tmp_path / "arc.db"
    c = connect(db)
    migrate(c)
    c.close()
    with TestClient(create_app(db, clock=lambda: NOW)) as cl:
        for path in ROUTES:
            r = cl.get(path)
            assert r.status_code == 200, (path, r.text[:300])
        assert cl.get("/api/ops/alerts").json()["alerts"] == []
        assert cl.get("/api/ops/runs").json()["total"] == 0
