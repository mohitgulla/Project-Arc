"""E13.3 (D56): categories v3 — six categories, reference data, Scalp budget split."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
import structlog.testing

from arc.config import ArcSettings
from arc.context.categories import (
    CATEGORY_ORDER,
    DEFAULT_CATEGORIES,
    KIND_CATEGORY,
    REFERENCE,
    REFERENCE_KINDS,
    SCALP_CATEGORIES,
    SourceCategory,
    kind_category,
    normalize_category,
    parse_category,
)
from arc.context.kinds import KINDS
from arc.context.store import ContextEntry, ContextSnapshot
from arc.ingest.sources import REFERENCE_MAX_AGE, SourceRegistry
from arc.routines.config import DEFAULT_ROUTINES_PATH, RoutinesConfig, load_routines
from arc.store.db import connect
from arc.store.migrate import migrate

NOW = dt.datetime(2026, 10, 5, 19, 0, tzinfo=dt.UTC)


@pytest.fixture()
def conn():
    c = connect(":memory:")
    migrate(c)
    return c


@pytest.fixture(scope="module")
def shipped() -> RoutinesConfig:
    return load_routines(DEFAULT_ROUTINES_PATH)


def _cfg(sources: dict[str, Any]) -> RoutinesConfig:
    return RoutinesConfig.model_validate({"sources": sources})


# -- the enum and its aliases --------------------------------------------------


def test_seven_members_in_display_order() -> None:
    # D58 (E13.19): retail_buzz is the 7th category (Scout slow feed + trending tier)
    assert [c.value for c in CATEGORY_ORDER] == [
        "market_news",
        "company_data",
        "options_fast",
        "options_slow",
        "youtube_macro",
        "youtube_micro",
        "retail_buzz",
    ]
    assert len(SourceCategory) == 7
    ages = {c.value: str(s.max_age) for c, s in DEFAULT_CATEGORIES.items()}
    assert ages == {
        "market_news": "6h",
        "company_data": "12h",
        "options_fast": "30m",
        "options_slow": "1d",
        "youtube_macro": "2d",  # D60
        "youtube_micro": "2d",  # D60
        "retail_buzz": "1d",
    }  # Ttl prints 24h as 1d


@pytest.mark.parametrize("name", ["company", "company_news", "filings", "calendar", "options_data"])
def test_expired_aliases_are_refused_in_config_but_read_on_stored_rows(name: str) -> None:
    """E20.2 (D85): the D56 "one release" aliases no longer load from config; a stored
    row that still carries the old name keeps reading as its successor."""
    with pytest.raises(ValueError, match="unknown source category"):
        parse_category(name, where="sources.x")
    expected = (
        SourceCategory.OPTIONS_SLOW if name == "options_data" else SourceCategory.COMPANY_DATA
    )
    assert normalize_category(name) is expected


@pytest.mark.parametrize("name", ["macro_data", "macro"])
def test_removed_macro_names_refuse_with_a_pointer(name: str) -> None:
    with pytest.raises(ValueError, match=r"was removed \(D56\).*market_news.*reference: true"):
        parse_category(name, where="sources.x")
    # a stored story/doc row with the old name reads as no category (never crashes)
    assert normalize_category(name) is None


def test_video_still_refuses() -> None:
    with pytest.raises(ValueError, match="split in two"):
        parse_category("video")


def test_categories_block_refuses_macro_data() -> None:
    with pytest.raises(ValueError, match="was removed"):
        RoutinesConfig.model_validate({"categories": {"macro_data": {"weight": 1}}})


def test_kind_map_and_reference_kinds() -> None:
    assert dict(KIND_CATEGORY) == {
        "vol_term": SourceCategory.OPTIONS_SLOW,
        "options_daily": SourceCategory.OPTIONS_SLOW,  # E13.5
        "vx_curve": SourceCategory.OPTIONS_SLOW,
        "index_vols": SourceCategory.OPTIONS_FAST,  # E13.6
        "chain_snapshot": SourceCategory.OPTIONS_FAST,
        "exchange_volume": SourceCategory.OPTIONS_FAST,
        "market_movers": SourceCategory.OPTIONS_FAST,  # E14.3 (D60)
        "retail_buzz": SourceCategory.RETAIL_BUZZ,  # E13.19 (D58)
    }
    assert {"ex_dividend", "macro_calendar"} <= REFERENCE_KINDS
    assert not REFERENCE_KINDS & set(KIND_CATEGORY)
    assert kind_category("vol_term") == "options_slow"
    assert kind_category("macro_calendar") == REFERENCE
    assert kind_category("story") is None
    assert "unusual_options" not in KINDS


# -- reference / category exclusivity -------------------------------------------


def test_reference_source_loads_without_category() -> None:
    cfg = _cfg({"ex_div": {"schedule": ["06:15"], "reference": True, "writes": ["ex_dividend"]}})
    assert cfg.is_reference("ex_div")
    assert cfg.source_category("ex_div") is None


def test_reference_and_category_together_fail() -> None:
    with pytest.raises(ValueError, match="never both"):
        _cfg(
            {
                "ex_div": {
                    "schedule": ["06:15"],
                    "reference": True,
                    "category": "options_slow",
                    "writes": ["ex_dividend"],
                }
            }
        )


def test_neither_reference_nor_category_fails() -> None:
    with pytest.raises(ValueError, match="or `reference: true`"):
        _cfg({"ex_div": {"schedule": ["06:15"], "writes": ["ex_dividend"]}})


def test_reference_must_be_a_bool() -> None:
    with pytest.raises(ValueError, match="true or false"):
        _cfg({"ex_div": {"schedule": ["06:15"], "reference": "yes", "writes": ["ex_dividend"]}})


def test_shipped_yaml_classification(shipped: RoutinesConfig) -> None:
    for job in ("macro_calendar", "ex_dividend", "iv.record", "earnings"):
        assert shipped.is_reference(job), job
        assert shipped.source_category(job) is None
    for job in shipped.sources:
        if job.startswith("finnhub."):
            assert shipped.is_reference(job), job
    for job in ("vol_term",):
        assert shipped.source_category(job) is SourceCategory.OPTIONS_SLOW
        assert shipped.sources[job].options["feed"] == "scout"
    assert "unusual_options" not in shipped.sources
    fed = next(f for f in shipped.sources["rss"].options["feeds"] if f["name"] == "fed")
    assert fed["category"] == "market_news"
    assert "unusual_options" not in (shipped.personas["research"].reads or [])
    # Risk still reads the reference kinds it vetoes on
    assert {"ex_dividend", "macro_calendar"} <= set(shipped.steps["risk.open"].reads or [])


def test_funnel_block_defaults_and_bounds(shipped: RoutinesConfig) -> None:
    f = shipped.funnel
    assert f.scalp.doc_budget_split == "equal"
    assert (f.scout.max_discovery, f.scout.min_discovery_alert) == (25, 5)  # D58
    assert f.scout.video_budget_split == "equal"
    assert f.research.max_scout_only_ideas == 20
    for bad in (
        {"scout": {"max_discovery": 26}},
        {"scout": {"min_discovery_alert": 21}},
        {"scalp": {"doc_budget_split": "weighted"}},
        {"scout": {"most_active_input": True}},  # owner decision 1: not a key
        {"scout": {"discovery_backfill": True}},  # owner decision 2: not a key
    ):
        with pytest.raises(ValueError):
            RoutinesConfig.model_validate({"funnel": bad})


# -- registry ------------------------------------------------------------------


def test_registry_excludes_reference_from_weights(shipped: RoutinesConfig) -> None:
    reg = SourceRegistry.from_routines(shipped)
    earn = reg.spec_for("earnings")
    assert earn.reference and earn.category is None and earn.category_key == REFERENCE
    assert reg.share_in_category("earnings") == 0.0
    assert reg.max_age_for("earnings") == REFERENCE_MAX_AGE
    assert reg.spec_for("fed").category is SourceCategory.MARKET_NEWS
    # the Scalp's weights cover exactly its two doc categories, split equally
    w = reg.category_weights(set(SCALP_CATEGORIES))
    assert set(w) == {SourceCategory.MARKET_NEWS, SourceCategory.COMPANY_DATA}
    assert w[SourceCategory.MARKET_NEWS] == pytest.approx(0.5)
    assert w[SourceCategory.COMPANY_DATA] == pytest.approx(0.5)


def test_scalp_never_reads_reference_docs(shipped: RoutinesConfig) -> None:
    """Earnings calendar docs (reference data) never enter the Scalp's budget; the
    Fed feed's docs count under Market news, and the split covers two categories."""
    from arc.ingest.scalp import _load_docs, _select

    t = (NOW - dt.timedelta(minutes=30)).isoformat()
    rows = [
        {"id": "d1", "source": "rss", "url": "https://www.federalreserve.gov/a", "key": "fed"},
        {"id": "d2", "source": "rss", "url": "https://www.wsj.com/a", "key": "wsj"},
        {"id": "d3", "source": "rss", "url": "https://seekingalpha.com/a", "key": "seekingalpha"},
        {
            "id": "d4",
            "source": "earnings",
            "url": "https://finnhub.io/calendar/earnings/AAPL/2026-10-30",
            "key": None,
        },
    ]
    reg = SourceRegistry.from_routines(shipped)
    docs = _load_docs(
        [
            {
                "id": r["id"],
                "source": r["source"],
                "url": r["url"],
                "published_at": t,
                "text": "x",
                "tickers_hint": "[]",
                "title": "t",
                "source_key": r["key"],
                "ingested_at": t,
            }
            for r in rows
        ],
        reg,
    )
    by_id = {d.id: d for d in docs}
    assert by_id["d4"].category == REFERENCE
    assert by_id["d1"].category == "market_news"
    chosen, _, sel = _select(docs, reg, 10)
    ids = {d.id for d in chosen}
    assert "d4" not in ids
    assert {"d1", "d2", "d3"} <= ids
    assert sel.category_of[by_id["d1"].source_key] == "market_news"
    assert set(sel.category_weights) == {"market_news", "company_data"}
    assert sel.category_weights["market_news"] == pytest.approx(0.5)


def test_next_earnings_still_reads_reference_rows(conn) -> None:
    from arc.ingest.store import RawDocRepo
    from arc.pipeline.market import next_earnings

    RawDocRepo(conn).insert(
        source="earnings",
        url="https://finnhub.io/calendar/earnings/AAPL/2026-10-30",
        title="AAPL earnings",
        text="",
        published_at=NOW.isoformat(),
        tickers_hint=["AAPL"],
    )
    assert next_earnings(conn, ["AAPL"], dt.date(2026, 10, 5)) == {"AAPL": dt.date(2026, 10, 30)}


# -- Research prompt -----------------------------------------------------------


def _e(kind: str, subject: str, payload: dict[str, Any], age: dt.timedelta) -> ContextEntry:
    t = NOW - age
    return ContextEntry(
        id=f"{kind}-{subject}",
        kind=kind,
        subject=subject,
        payload=payload,
        schema_version=1,
        produced_by="test",
        created_at=t,
        valid_from=t,
    )


def test_research_block_order_and_no_reference_kinds() -> None:
    from arc.personas.builders import category_context_block

    snap = ContextSnapshot(
        id="s",
        as_of=NOW,
        entries=[
            _e("vol_term", "market", {}, dt.timedelta(hours=2)),
            _e("macro_calendar", "market", {"events": []}, dt.timedelta(hours=2)),
            _e("ex_dividend", "AAPL", {}, dt.timedelta(hours=2)),
            _e(
                "story",
                "fed",
                {
                    "category": "market_news",
                    "headline": "Fed holds",
                    "last_published": (NOW - dt.timedelta(hours=1)).isoformat(),
                },
                dt.timedelta(hours=1),
            ),
        ],
    )
    block = category_context_block(snap)
    heads = [ln.split(":", 1)[0] for ln in block.splitlines() if not ln.startswith("- ")]
    assert heads == [
        "Market news",
        "Company data",
        "Options fast",
        "Options slow",
        "YouTube macro",
        "YouTube micro",
    ]
    assert "Options slow: vol_term 2h" in block
    assert "Options fast: no fresh info" in block
    assert "macro_calendar" not in block and "ex_dividend" not in block


def test_research_prompt_headers_drop_unusual_options() -> None:
    from arc.personas.builders import build_research_prompt, research_input_from_context

    snap = ContextSnapshot(
        id="s",
        as_of=NOW,
        entries=[
            _e("vol_term", "market", {"x": 1}, dt.timedelta(hours=2)),
            _e("unusual_options", "AAPL", {"ticker": "AAPL", "flags": ["vol_oi"]}, dt.timedelta()),
        ],
    )
    inp = research_input_from_context(snap, portfolio_summary="", scan_date="2026-10-05")
    prompt = build_research_prompt(inp)
    assert "Context by category (D56" in prompt
    assert "unusual options" not in prompt.lower()
    assert "unusual_options" not in json.loads(inp.market_data_json)


def test_d49_replay_rebuilds_the_old_block() -> None:
    """A Research call recorded under the D49 six categories replays byte for byte."""
    from arc.personas.builders import build_research_prompt, research_input_from_context
    from arc.pipeline.steps import D49_ONLY_CATEGORIES

    assert {"macro_data", "options_data"} == D49_ONLY_CATEGORIES
    snap = ContextSnapshot(
        id="s",
        as_of=NOW,
        entries=[
            _e("vol_term", "market", {"x": 1}, dt.timedelta(hours=2)),
            _e("unusual_options", "AAPL", {"ticker": "AAPL", "flags": ["vol_oi"]}, dt.timedelta()),
        ],
    )
    inp = research_input_from_context(
        snap, portfolio_summary="", scan_date="2026-10-05", d49_replay=True
    )
    prompt = build_research_prompt(inp)
    assert "Context by category (D49" in prompt
    assert "Options data: vol_term 2h, unusual_options 1 flagged" in prompt
    assert "unusual options activity" in prompt
    assert json.loads(inp.market_data_json)["unusual_options"][0]["ticker"] == "AAPL"


def test_replay_routes_d49_inputs_to_the_d49_block() -> None:
    from arc.pipeline import steps

    kwargs: dict[str, Any] = {"categories": {"macro_data": {}, "options_data": {}}}
    steps._replay_flags("research", kwargs)
    assert kwargs.get("d49_replay") is True
    kwargs = {"categories": {"options_slow": {}}}
    steps._replay_flags("research", kwargs)
    assert "d49_replay" not in kwargs and "d47_replay" not in kwargs


# -- config change log ---------------------------------------------------------


def test_orphaned_overrides_are_logged_and_ignored(conn, tmp_path) -> None:
    from arc.control.effective import effective_routines, effective_settings
    from arc.control.store import ConfigChangeRepo

    p = tmp_path / "routines.yaml"
    p.write_text(DEFAULT_ROUTINES_PATH.read_text())
    repo = ConfigChangeRepo(conn)
    for key, new in (
        ("categories.macro_data.weight", 3),
        ("categories.options_data.max_age", 600),
        ("uoa_min_volume", 900),
    ):
        repo.append(
            key=key, old=1, new=new, is_default=False, actor="U0OWNER001", reason=None,
            at=NOW, source="slack", status="applied", direction="neutral",
        )  # fmt: skip
    with structlog.testing.capture_logs() as logs:
        r = effective_routines(conn, p)
        effective_settings(conn)
    assert r.categories[SourceCategory.OPTIONS_SLOW].max_age.duration == dt.timedelta(hours=24)
    unknown = {e["key"] for e in logs if e["event"] == "control.override_unknown_key"}
    assert unknown == {
        "categories.macro_data.weight",
        "categories.options_data.max_age",
        "uoa_min_volume",
    }


def test_new_keys_are_classified() -> None:
    from arc.control.registry import NOT_EXPOSED_PATHS, REGISTRY

    for key in (
        "categories.options_fast.weight",
        "categories.options_fast.max_age",
        "categories.options_slow.weight",
        "categories.options_slow.max_age",
        "funnel.scout.max_discovery",
        "funnel.scout.min_discovery_alert",
        "funnel.research.max_scout_only_ideas",
    ):
        assert key in REGISTRY, key
    assert REGISTRY["funnel.scout.max_discovery"].max == 25
    assert REGISTRY["funnel.scout.min_discovery_alert"].max == 20
    assert "funnel.scalp.doc_budget_split" in NOT_EXPOSED_PATHS
    assert "funnel.scout.video_budget_split" in NOT_EXPOSED_PATHS
    assert not any(k.startswith(("categories.macro_data", "uoa_")) for k in REGISTRY)


def test_funnel_tunable_round_trips(conn, tmp_path) -> None:
    from arc.control.effective import effective_routines
    from arc.control.service import ControlService

    owner = "U0OWNER001"
    p = tmp_path / "routines.yaml"
    p.write_text(DEFAULT_ROUTINES_PATH.read_text())
    svc = ControlService(
        conn,
        base=ArcSettings(_env_file=None, approver_slack_user_ids=[owner]),  # type: ignore[call-arg]
        now=lambda: NOW,
        optionable=lambda s: True,
        is_halted=lambda: False,
    )
    r = svc.set("funnel.scout.min_discovery_alert", "3", actor=owner, source="slack")
    if r.pending is not None:
        r = svc.confirm(r.pending.code, actor=owner, source="slack")
    assert r.outcome == "applied", r
    assert effective_routines(conn, p).funnel.scout.min_discovery_alert == 3


# -- arc context show ----------------------------------------------------------


def test_context_show_tolerates_retired_kind_and_prints_category(conn, tmp_path, capsys) -> None:
    import argparse

    from arc.context.store import ContextStore
    from arc.routines.cli import run_context

    store = ContextStore(conn)
    store.write(
        kind="vol_term",
        subject="market",
        payload={"as_of": "2026-10-05", "vix": 16.0, "vix3m": 18.0, "structure": "contango"},
        produced_by="vol_term",
        valid_from=NOW - dt.timedelta(hours=1),
        now=NOW - dt.timedelta(hours=1),
    )
    conn.execute(
        "INSERT INTO context_entries (id, kind, subject, payload, schema_version, produced_by,"
        " created_at, valid_from, status) VALUES ('old-uoa', 'unusual_options', 'AAPL', '{}', 1,"
        " 'unusual_options', ?, ?, 'active')",
        ((NOW - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),) * 2,
    )
    db = tmp_path / "x.db"
    conn.commit()
    conn.execute(f"VACUUM INTO '{db}'")

    def _args(**kw: Any) -> argparse.Namespace:
        base = {
            "context_command": "show",
            "db": str(db),
            "kind": [],
            "subject": [],
            "as_of": NOW.isoformat(),
            "snapshot": None,
            "latest": False,
            "json": False,
        }
        return argparse.Namespace(**{**base, **kw})

    assert run_context(_args(kind=["vol_term"], latest=True)) == 0
    out = capsys.readouterr().out
    assert "category: options_slow" in out
    assert run_context(_args(kind=["unusual_options"])) == 0
    out = capsys.readouterr().out
    assert "not registered" in out and "old-uoa" in out
    assert run_context(_args(kind=["vol_term"], json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["category"] == "options_slow"
