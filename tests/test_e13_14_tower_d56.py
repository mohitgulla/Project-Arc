"""E13.14 (D56) Tower pass: persona/category catalogue, exit path, funnel, universe.

Covers ``arc.tower.catalogue`` (``/api/meta`` personas + categories from config),
``arc.tower.data_exits`` (Positions exit path, deterministic short-circuit, latest-wins),
``arc.tower.data_funnel`` (``/api/performance/funnel`` + ``arc funnel report``), the
universe ``tail_cuts`` / ``discovery_fill`` fields (d51 unchanged, d56 filled), the close
trade's ``exit_review``, and the "no hard-coded persona names in web/src" lint.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc.config import ArcSettings
from arc.context.categories import SourceCategory
from arc.context.store import ContextStore
from arc.context.ttl import Ttl, to_db
from arc.routines.config import TIMELINE_PERSONAS, load_routines
from arc.slack.personas import PERSONA_EMOJI, persona_for
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower.api import create_app
from arc.tower.catalogue import (
    REFERENCE_LABEL,
    category_catalogue,
    category_feed,
    persona_catalogue,
)
from arc.tower.data import connect_ro
from arc.tower.data_exits import load_exit_path
from arc.tower.data_funnel import (
    FunnelReport,
    funnel_bounds,
    load_funnel_report,
    render_funnel_table,
)
from arc.tower.data_universe import UniverseResponse, discovery_fill_by_day, load_universe
from arc.universe.tiers import (
    DROP_OVER_ACTIVE_CAP,
    Tier,
    TierMember,
    UniverseTierPayload,
    resolve_active,
)
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 10, 6, 14, 20, tzinfo=ET)  # Tuesday, RTH
TODAY = NOW.date()


def _load(name: str):  # noqa: ANN202 - module loaded by path
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


fixture = _load("tower_fixture_db")


@pytest.fixture(scope="module")
def exits_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("exits") / "arc.db", NOW, exits=True)


@pytest.fixture(scope="module")
def plain_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return fixture.build(tmp_path_factory.mktemp("plain") / "arc.db", NOW)


def _client(db: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><div id=root></div>")
    return TestClient(create_app(db, static_dir=static, clock=lambda: NOW))


# -- persona / category catalogue ------------------------------------------------------


class TestCatalogue:
    def test_personas_follow_the_timeline_order_with_config_labels(self) -> None:
        routines = load_routines()
        cat = persona_catalogue(routines)
        assert [p.key for p in cat] == list(TIMELINE_PERSONAS)
        by = {p.key: p for p in cat}
        assert [by[k].label for k in ("scout", "scalp", "research", "quant", "risk", "broker")] == [
            "Scout", "Scalp", "Research", "Quant", "Risk", "Broker",
        ]  # fmt: skip
        for p in cat:
            if p.key in PERSONA_EMOJI:
                assert p.emoji == PERSONA_EMOJI[p.key]  # type: ignore[index]  # one emoji source (arc.slack.personas)
        assert by["research"].llm and by["scalp"].llm and by["risk"].llm
        assert not by["broker"].llm and not by["monitor"].llm
        # Risk is chain-only: its group falls back to the loop job's group.
        assert by["risk"].group == by["research"].group

    def test_labels_are_the_slack_persona_names_and_monitor_reads_config(
        self, tmp_path: Path
    ) -> None:
        routines = load_routines()
        by = {m.key: m for m in persona_catalogue(routines)}
        for key, meta in by.items():
            slack = persona_for(key)
            if slack is not None:  # one name source shared with the Slack cards
                assert meta.label == slack.value
        text = (REPO / "config" / "routines.yaml").read_text()
        assert text.count("    label: Intraday monitor") == 1
        p = tmp_path / "routines.yaml"
        p.write_text(text.replace("    label: Intraday monitor", "    label: watch list"))
        assert {m.key: m for m in persona_catalogue(load_routines(p))}["monitor"].label == (
            "Watch list"
        )

    def test_six_categories_then_reference(self) -> None:
        cats = category_catalogue(load_routines())
        assert [c.key for c in cats] == [*(c.value for c in SourceCategory), "reference"]
        by = {c.key: c for c in cats}
        assert {k for k, c in by.items() if c.feed == "scalp"} == {
            "market_news", "company_data", "options_fast",
        }  # fmt: skip
        assert by["reference"].reference and by["reference"].label == REFERENCE_LABEL
        assert by["reference"].weight == 0
        assert (by["market_news"].max_age_minutes, by["options_fast"].max_age_minutes) == (360, 30)
        assert category_feed(SourceCategory.OPTIONS_FAST) == "scalp"
        assert category_feed(SourceCategory.OPTIONS_SLOW) == "scout"

    def test_meta_route_serves_the_catalogue(self, plain_db: Path, tmp_path: Path) -> None:
        body = _client(plain_db, tmp_path).get("/api/meta").json()
        assert [p["key"] for p in body["personas"]] == list(TIMELINE_PERSONAS)
        assert body["categories"][-1]["key"] == "reference"
        flat = json.dumps(body["personas"]).lower()
        for old in ("director", "investor", "auditor", "sweep"):
            assert old not in flat


# -- exit path (Positions) ---------------------------------------------------------------


def _positions(db: Path, tmp_path: Path) -> dict:
    r = _client(db, tmp_path).get("/api/positions")
    assert r.status_code == 200
    return r.json()


class TestExitPath:
    def test_shadow_chain_rows_and_strip(self, exits_db: Path, tmp_path: Path) -> None:
        body = _positions(exits_db, tmp_path)
        strip = body["exit_path"]
        assert strip == {
            "mode": "shadow", "mandatory_pending": 1, "cases_today": 2,
            "closes_proposed_today": 1, "holds_today": 1,
        }  # fmt: skip
        rows = {r["ticker"]: r for r in body["items"]}
        spy, qqq, nvda = rows["SPY"], rows["QQQ"], rows["NVDA"]
        assert spy["exit_watch"]["action"] == "review"
        assert spy["exit_watch"]["thesis_status"] == "weakened"
        assert spy["exit_watch"]["evidence"][0].startswith("story:")
        assert spy["exit_case"]["recommendation"] == "close"
        assert spy["exit_case"]["remaining_ev_hold"] == -6.5
        assert spy["exit_case"]["remaining_ev_managed"] == 21.0
        assert spy["exit_case"]["close_now_net"] == 74.0
        assert spy["exit_case"]["triggers"][0].startswith("research_review: ")
        assert spy["exit_review"]["verdict"] == "close"
        assert spy["exit_review"]["reason_code"] == "thesis_broken"
        assert spy["mandatory_signal"] is None  # its latest review has no signal
        assert qqq["exit_case"]["recommendation"] == "hold"
        assert qqq["exit_review"]["verdict"] == "hold"
        assert qqq["mandatory_signal"] is None  # profit_target is not mandatory
        assert nvda["mandatory_signal"] == "stop"
        assert nvda["exit_case"] is None and nvda["exit_review"] is None

    def test_deterministic_mode_has_no_views(self, plain_db: Path, tmp_path: Path) -> None:
        body = _positions(plain_db, tmp_path)
        assert body["exit_path"] == {
            "mode": "deterministic", "mandatory_pending": 0, "cases_today": 0,
            "closes_proposed_today": 0, "holds_today": 0,
        }  # fmt: skip
        for r in body["items"]:
            assert (r["exit_watch"], r["exit_case"], r["exit_review"]) == (None, None, None)
            assert r["mandatory_signal"] is None

    def test_deterministic_mode_ignores_stored_entries(self, exits_db: Path) -> None:
        c = connect_ro(exits_db)
        try:
            ids = [r[0] for r in c.execute("SELECT id FROM open_structures WHERE status='open'")]
            strip, views = load_exit_path(c, ids, mode="deterministic", now=NOW)
            assert strip.mode == "deterministic" and strip.cases_today == 0 and views == {}
            strip, views = load_exit_path(c, ids, mode="research", now=NOW)
            assert strip.mode == "research" and strip.cases_today == 2 and len(views) == len(ids)
        finally:
            c.close()

    def test_latest_case_wins_and_bad_payloads_are_skipped(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        c = connect(db)
        migrate(c)
        c.close()
        c = connect(db)
        try:
            later = NOW - dt.timedelta(minutes=5)
            c.execute(
                "INSERT INTO context_entries (id, kind, subject, payload, schema_version,"
                " produced_by, created_at, valid_from) VALUES"
                " ('a', 'exit_case', 'os-1', ?, 1, 't', ?, ?),"
                " ('b', 'exit_case', 'os-1', ?, 1, 't', ?, ?),"
                " ('c', 'exit_case', 'os-1', 'not json', 1, 't', ?, ?)",
                (
                    json.dumps({"recommendation": "hold", "facts": {"close_now_net": 1.0}}),
                    "2026-10-06T17:00:00.000000Z", "2026-10-06T17:00:00.000000Z",
                    json.dumps({"recommendation": "close", "facts": {"close_now_net": 2.0}}),
                    to_db(later), to_db(later),
                    "2026-10-06T18:30:00.000000Z", "2026-10-06T18:30:00.000000Z",
                ),
            )  # fmt: skip
            c.commit()
        finally:
            c.close()
        c = connect_ro(db)
        try:
            _, views = load_exit_path(c, ["os-1", "os-2"], mode="shadow", now=NOW)
        finally:
            c.close()
        assert views["os-1"].exit_case is not None
        assert views["os-1"].exit_case.recommendation == "close"
        assert views["os-1"].exit_case.close_now_net == 2.0
        assert views["os-2"].exit_case is None

    def test_close_trade_shows_its_exit_review(self, exits_db: Path, tmp_path: Path) -> None:
        c = connect_ro(exits_db)
        try:
            h_close = c.execute(
                "SELECT proposal_hash FROM proposals WHERE kind='close' AND ticker='QQQ'"
            ).fetchone()[0]
            h_open = c.execute(
                "SELECT proposal_hash FROM proposals WHERE kind='open' LIMIT 1"
            ).fetchone()[0]
        finally:
            c.close()
        client = _client(exits_db, tmp_path)
        rv = client.get(f"/api/trades/{h_close}").json()["exit_review"]
        assert rv["verdict"] == "close" and rv["reason_code"] == "ev_exhausted"
        assert client.get(f"/api/trades/{h_open}").json()["exit_review"] is None


# -- funnel ------------------------------------------------------------------------------


def _seed_funnel(path: Path) -> None:
    c = connect(path)
    migrate(c)
    store = ContextStore(c)
    ttl = Ttl(duration=dt.timedelta(days=3))
    day = dt.datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    rows = [
        ("rd-1", "rss", "https://wsj.com/a", "scouted", "wsj"),
        ("rd-2", "rss", "https://wsj.com/b", "pending", "wsj"),
        ("rd-3", "youtube", "https://youtube.com/watch?v=x", "brief_only", "youtube.x"),
    ]
    cols = {r[1] for r in c.execute("PRAGMA table_info(raw_docs)")}
    for rid, source, url, status, key in rows:
        vals = {"id": rid, "source": source, "url": url, "title": "t", "text": "b",
                "published_at": to_db(day), "ingested_at": to_db(day),
                "content_hash": rid, "scalp_status": status, "source_key": key}  # fmt: skip
        use = {k: v for k, v in vals.items() if k in cols}
        c.execute(
            f"INSERT INTO raw_docs ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})",  # noqa: S608
            list(use.values()),
        )
    c.commit()
    at = day + dt.timedelta(hours=1)

    def w(kind: str, subject: str, payload: dict) -> None:
        c.execute(
            "INSERT INTO context_entries (id, kind, subject, payload, schema_version,"
            " produced_by, created_at, valid_from) VALUES (?, ?, ?, ?, 1, 't', ?, ?)",
            (f"{kind}-{subject}-{len(payload)}", kind, subject, json.dumps(payload),
             to_db(at), to_db(at)),
        )  # fmt: skip

    w("story", "NVDA", {"category": "market_news"})
    w("story", "AMD", {"category": "company_news"})  # pre-D56 name -> company_data
    w("candidate", "NVDA", {"ticker": "NVDA", "feed": "scalp", "sources": ["https://wsj.com/a"]})
    w("candidate", "AMD", {"ticker": "AMD", "feed": "scalp", "sources": []})
    w("candidate", "TEM", {"ticker": "TEM", "feed": "scout", "origins": ["youtube:x"]})
    w("shortlist", "market", {
        "shortlist": [{"ticker": "NVDA", "rank": 1}, {"ticker": "TEM", "rank": 2},
                      {"ticker": "AMD", "rank": 3}],
        "excluded": [], "budget": 2,
    })  # fmt: skip
    w("structures", "market", {"structures": [{"ticker": "NVDA"}]})
    c.commit()
    store.write(
        kind="universe_tier", subject=Tier.DISCOVERY.value, produced_by="scout", ttl=ttl,
        payload=UniverseTierPayload(
            tier=Tier.DISCOVERY, fetched_at=at, source="scout",
            members=[TierMember(ticker="TEM", tier=Tier.DISCOVERY, rank=1, source="scout",
                                reason="youtube:x", as_of=day.date())],
        ),
        valid_from=at, now=at,
    )  # fmt: skip
    c.close()


class TestFunnel:
    def test_counts_per_stage_and_feed(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        _seed_funnel(db)
        c = connect_ro(db)
        try:
            rep = load_funnel_report(
                c, since=dt.date(2026, 10, 5), until=dt.date(2026, 10, 5), routines=None
            )
            again = load_funnel_report(
                c, since=dt.date(2026, 10, 5), until=dt.date(2026, 10, 5), routines=None
            )
        finally:
            c.close()
        assert rep == again  # deterministic for a fixed range and store
        st = {s.stage: s for s in rep.stages}
        assert [s.stage for s in rep.stages] == [
            "docs_fresh", "docs_read", "stories", "candidates", "pool", "shortlist",
            "structures", "proposals", "approved", "filled",
        ]  # fmt: skip
        assert st["docs_fresh"].count == 3 and st["docs_read"].count == 2
        assert st["stories"].by_category == {"company_data": 1, "market_news": 1}
        assert st["candidates"].count == 3
        assert st["candidates"].by_feed == {"scalp": 2, "scout": 1}
        assert st["pool"].count == 3
        assert st["shortlist"].count == 2  # inside the budget only
        assert st["shortlist"].by_feed == {"scalp": 1, "scout": 1}
        assert st["structures"].count == 1
        assert dict(rep.top_sources) == {"wsj": 1, "youtube:x": 1}
        assert rep.discovery_fill == {"2026-10-05": 1}
        assert rep.sessions == 1
        text = render_funnel_table(rep)
        assert "Candidates" in text and "Discovery fill (Scout): 2026-10-05 1" in text

    def test_out_of_range_is_empty(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        _seed_funnel(db)
        c = connect_ro(db)
        try:
            rep = load_funnel_report(c, since=dt.date(2026, 10, 6), until=dt.date(2026, 10, 7))
        finally:
            c.close()
        assert all(s.count == 0 for s in rep.stages) and rep.discovery_fill == {}

    def test_bounds(self) -> None:
        assert funnel_bounds(TODAY, "1W") == (TODAY - dt.timedelta(days=6), TODAY)
        assert funnel_bounds(TODAY, "1D") == (TODAY, TODAY)
        d = dt.date(2026, 9, 29)
        assert funnel_bounds(TODAY, "1W", since=d) == (d, TODAY)
        with pytest.raises(ValueError, match="after"):
            funnel_bounds(TODAY, since=TODAY, until=d)

    def test_route_and_bad_range(self, exits_db: Path, tmp_path: Path) -> None:
        client = _client(exits_db, tmp_path)
        r = client.get("/api/performance/funnel?range=1W")
        assert r.status_code == 200
        rep = FunnelReport.model_validate(r.json())
        assert rep.until == TODAY.isoformat() and len(rep.stages) == 10
        assert (
            client.get("/api/performance/funnel?from=2026-10-06&to=2026-10-01").status_code == 422
        )
        assert client.post("/api/performance/funnel").status_code == 405

    def test_cli_report(self, tmp_path: Path) -> None:
        db = tmp_path / "arc.db"
        _seed_funnel(db)
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "arc.cli", "funnel", "report", "--db", str(db),
             "--since", "2026-10-05", "--until", "2026-10-05", "--json"],
            capture_output=True, text=True, check=True, cwd=REPO,
        )  # fmt: skip
        rep = FunnelReport.model_validate_json(out.stdout)
        assert {s.stage: s.count for s in rep.stages}["candidates"] == 3
        bad = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "arc.cli", "funnel", "report", "--db", str(db),
             "--since", "2026-10-07", "--until", "2026-10-05"],
            capture_output=True, text=True, check=False, cwd=REPO,
        )  # fmt: skip
        assert bad.returncode == 2 and "after" in bad.stderr


# -- universe tail cuts + discovery fill -------------------------------------------------


def _m(t: str, tier: Tier, rank: int) -> TierMember:
    return TierMember(ticker=t, tier=tier, rank=rank, source="x", reason="r", as_of=TODAY)


def _universe(tmp_path: Path, model: str) -> tuple[UniverseResponse, dict[str, int]]:
    db = tmp_path / f"{model}.db"
    c = connect(db)
    migrate(c)
    ttl = Ttl(duration=dt.timedelta(hours=8))
    active = resolve_active(
        core=[_m("NVDA", Tier.CORE, 1), _m("AAPL", Tier.CORE, 2)],
        momentum=[_m("MU", Tier.MOMENTUM, 1), _m("GE", Tier.MOMENTUM, 2)],
        discoveries=[_m("QCOM", Tier.DISCOVERY, 1), _m("TEM", Tier.DISCOVERY, 2)],
        active_max=5, as_of=TODAY, config_version=3,
    )  # fmt: skip
    if model == "d51":  # a stored pre-cutover resolve
        active = active.model_copy(update={"model": "d51"})
    store = ContextStore(c)
    at = NOW - dt.timedelta(minutes=10)
    store.write(kind="active_universe", subject="active", payload=active, produced_by="t",
                ttl=ttl, valid_from=at, now=at)  # fmt: skip
    if model == "d56":
        store.write(
            kind="universe_tier", subject="discovery", produced_by="scout", ttl=ttl,
            payload=UniverseTierPayload(
                tier=Tier.DISCOVERY, fetched_at=at, source="scout",
                members=[_m("QCOM", Tier.DISCOVERY, 1), _m("TEM", Tier.DISCOVERY, 2)],
            ),
            valid_from=at, now=at,
        )  # fmt: skip
    c.close()
    c = connect_ro(db)
    try:
        return load_universe(c, ArcSettings(), now=NOW), discovery_fill_by_day(c, TODAY, TODAY)
    finally:
        c.close()


class TestUniverse:
    def test_stored_d51_resolve_still_renders(self, tmp_path: Path) -> None:
        # a pre-E13.15 resolve stored as d51 still loads; no Scout fill is counted
        r, _ = _universe(tmp_path, "d51")
        assert r.model == "d51" and r.discovery_fill is None
        assert [(d.ticker, d.tier) for d in r.tail_cuts] == [("TEM", "discovery")]
        assert all(d.reason == DROP_OVER_ACTIVE_CAP for d in r.tail_cuts)

    def test_d56_fill_and_reference(self, tmp_path: Path) -> None:
        r, by_day = _universe(tmp_path, "d56")
        assert r.model == "d56"
        assert [t.name for t in r.tiers] == ["core", "momentum", "discovery"]
        assert r.market_reference == ["SPY", "QQQ", "IWM"]
        assert r.discovery_fill == 2 and by_day == {TODAY.isoformat(): 2}
        assert [d.ticker for d in r.tail_cuts] == ["TEM"]


# -- SPA lint: no hard-coded persona names -----------------------------------------------


def test_no_hardcoded_old_persona_names_in_web_src() -> None:
    pat = re.compile(r"\b(Director|Investor|Auditor|Sweep)\b")
    hits = []
    for p in sorted((REPO / "web" / "src").rglob("*")):
        if not p.is_file() or p.name == "api.gen.ts" or p.suffix not in {".ts", ".tsx"}:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()}")
    assert hits == []
