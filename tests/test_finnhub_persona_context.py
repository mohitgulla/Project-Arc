"""E4.8a (D46, D44): Finnhub facts in the Scalp/Research prompts, behind a default-off flag.

Pins: the flag defaults off and leaves both prompts byte-identical to origin/main's
(golden hashes taken from main with tests/finnhub_golden.py); with it on, the facts
appear for the right tickers inside the char budget, missing/stale parts are omitted,
the D31 digest moves on ``as_of`` only, and the gate never sees any of it.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.context import ContextStore
from arc.context.store import ContextSnapshot
from arc.control.effective import effective_routines
from arc.control.registry import REGISTRY, lookup, read_raw, write_raw
from arc.control.service import ControlService
from arc.experiments.overlay import arm_config_data, load_spec
from arc.ingest.llm import FixtureScalpLLM
from arc.ingest.scalp import run_scalp, scalp_facts_tickers
from arc.ingest.store import RawDocRepo
from arc.models import Candidate
from arc.personas.builders import (
    TICKER_FACTS_NOTE,
    EarningsFacts,
    FundamentalsFacts,
    InsiderFacts,
    RecsFacts,
    TickerFacts,
    render_ticker_facts,
    ticker_facts_block,
    ticker_facts_digest,
    ticker_facts_from_context,
)
from arc.routines.config import (
    DEFAULT_ROUTINES_PATH,
    FinnhubContextSettings,
    RoutinesConfig,
    load_routines,
)
from arc.routines.loop import LoopInputs
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests import finnhub_golden as g

REPO = Path(__file__).resolve().parent.parent
# sha256 of the prompts built by tests/finnhub_golden.py on origin/main 9834f58
# (before E4.8a). Flag off must reproduce them byte for byte.
# E5.12 (D54) re-pinned both: "Scout" -> "Sweep" in the role/label/schema-name lines
# only (diffed against origin/main 23be73a; no other byte changed).
# E13.1 (D56) re-pinned both: "Sweep" -> "Scalp" and "Director" -> "Research" in the
# role/label/schema-name lines only (diffed against origin/main f1c1429; no other byte).
MAIN_RESEARCH_SHA = "0e4ddd78d4c1651a599b8621180165e53bf3885f4499187414e1c85d7d356fbe"
# D51 (E12.1) re-pinned the Scalp sha: the watch list is the 25-name core and the
# task line says "Watch list (core + momentum + trending)" (a deliberate prompt change).
MAIN_SCALP_SHA = "dd8297ccf25a1af882ee2770f8e91ed22494a17f3428c3bc3c4a33d45c93107d"
ON = FinnhubContextSettings(enabled=True)
TODAY = g._now().date()


def _opts(tickers: list[str], **kw: Any) -> dict[str, Any]:
    cfg = FinnhubContextSettings(enabled=True, **kw)
    return cfg.prompt_options(tickers, cfg.research_max_tickers)


# ---------------------------------------------------------------------------
# Flag: config, registry, experiment overlay
# ---------------------------------------------------------------------------


def test_flag_defaults_off_in_the_shipped_config() -> None:
    raw = yaml.safe_load(DEFAULT_ROUTINES_PATH.read_text())
    assert raw["personas"]["finnhub_context"] == "off"
    cfg = load_routines(DEFAULT_ROUTINES_PATH)
    assert cfg.finnhub_context.enabled is False
    assert cfg.finnhub_context.max_chars_per_ticker == 300
    assert "finnhub_context" not in cfg.personas  # a switch, not a job
    assert RoutinesConfig().finnhub_context.enabled is False  # absent = off


@pytest.mark.parametrize(
    ("value", "enabled"), [("on", True), ("off", False), (True, True), (False, False)]
)
def test_flag_parses_on_off(value: object, enabled: bool) -> None:
    cfg = RoutinesConfig.model_validate({"personas": {"finnhub_context": value}})
    assert cfg.finnhub_context.enabled is enabled


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"personas": {"finnhub_context": "maybe"}}, "on | off"),
        (
            {"personas": {"finnhub_context": "on"}, "finnhub_context": {"enabled": True}},
            "set the switch as personas",
        ),
        ({"finnhub_context": {"max_age_days": {"nope": 3}}}, "unknown kinds"),
        ({"finnhub_context": {"max_chars_per_ticker": 10}}, "greater than or equal"),
    ],
)
def test_flag_and_knobs_are_validated(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RoutinesConfig.model_validate(data)


def test_loader_input_is_not_mutated() -> None:
    data = {"personas": {"finnhub_context": "on"}}
    RoutinesConfig.model_validate(data)
    assert data == {"personas": {"finnhub_context": "on"}}


def test_registry_choice_key_reads_and_writes_the_switch() -> None:
    t = lookup("personas.finnhub_context")
    assert t is REGISTRY["personas.finnhub_context"]
    assert lookup("routines.personas.finnhub_context") is t
    assert t.choices == ("off", "on") and t.bounds == "off | on"
    assert read_raw(t, {"personas": {"finnhub_context": "off"}}) == "off"
    assert read_raw(t, {"personas": {"finnhub_context": True}}) == "on"
    assert read_raw(t, {"personas": {}}) == "off"
    assert write_raw(t, "on", {}) == [(("personas", "finnhub_context"), "on")]


def test_slack_override_reaches_the_effective_routines() -> None:
    conn = connect(":memory:")
    migrate(conn)
    now = dt.datetime(2026, 10, 6, 9, 0, tzinfo=ET)
    base = ArcSettings(_env_file=None, approver_slack_user_ids=["U0OWNER"])  # type: ignore[call-arg]
    svc = ControlService(conn, base=base, now=lambda: now)
    assert svc.view("personas.finnhub_context").value == "off"
    r = svc.set("personas.finnhub_context", "on", actor="U0OWNER", source="slack")
    assert r.pending is not None  # off -> on is the riskier direction: confirm step
    svc.confirm(r.pending.code, actor="U0OWNER", source="slack")
    assert effective_routines(conn).finnhub_context.enabled is True
    assert svc.view("personas.finnhub_context").value == "on"
    r = svc.set("personas.finnhub_context", "off", actor="U0OWNER", source="slack")
    assert r.outcome == "applied"
    assert effective_routines(conn).finnhub_context.enabled is False


def test_xp2_draft_spec_turns_only_the_flag_on() -> None:
    spec = load_spec(REPO / "config" / "experiments" / "live" / "xp2_finnhub_context.yaml")
    assert spec.id == "XP-2" and spec.kind.value == "ab"
    assert spec.arms.treatment.overlay == {"routines": {"personas": {"finnhub_context": "on"}}}
    treat = RoutinesConfig.model_validate(arm_config_data(spec, "treatment", "routines"))
    base = load_routines(DEFAULT_ROUTINES_PATH)
    assert treat.finnhub_context.enabled is True
    assert treat.model_copy(update={"finnhub_context": base.finnhub_context}) == base


# ---------------------------------------------------------------------------
# Golden: flag off == main
# ---------------------------------------------------------------------------


def test_flag_off_research_prompt_is_byte_identical_to_main() -> None:
    snap = g.snapshot()  # Finnhub entries present in the snapshot, flag off
    assert g.sha(g.research_prompt(snap)) == MAIN_RESEARCH_SHA
    assert g.sha(g.research_prompt(snap, ticker_facts=None)) == MAIN_RESEARCH_SHA


def test_flag_off_scalp_prompt_is_byte_identical_to_main() -> None:
    assert g.sha(g.scalp_prompt()) == MAIN_SCALP_SHA
    assert g.sha(g.scalp_prompt(ticker_facts="")) == MAIN_SCALP_SHA


def test_flag_off_build_prompt_path_records_no_facts_input() -> None:
    from arc.pipeline.steps import build_prompt

    inputs = {"portfolio_summary": "flat", "scan_date": "2026-10-06", "rules": ["r"]}
    off = build_prompt("research", g.snapshot(), inputs)
    on = build_prompt("research", g.snapshot(), {**inputs, "ticker_facts": _opts(["AAPL"])})
    assert "Ticker facts" not in off
    assert "Ticker facts" in on and "AAPL: EPS surprise" in on


# ---------------------------------------------------------------------------
# Flag on: content, tickers, budget, staleness
# ---------------------------------------------------------------------------


def test_flag_on_research_prompt_shows_facts_for_the_given_tickers() -> None:
    p = g.research_prompt(g.snapshot(), ticker_facts=_opts(["AAPL"]))
    assert "### Ticker facts (Finnhub, code-built)" in p
    assert TICKER_FACTS_NOTE in p
    assert "weak evidence" in TICKER_FACTS_NOTE and "not signals" in TICKER_FACTS_NOTE
    facts = [ln for ln in p.splitlines() if ln.startswith(("AAPL:", "NVDA:"))]
    assert [ln.split(":")[0] for ln in facts] == ["AAPL"]  # NVDA has data but was not asked
    line = facts[0]
    assert line == (
        "AAPL: EPS surprise -0.9/+1.1/+4.2/+4.5% (3 beat/1 miss) [1d] | "
        "insider 90d net +$2.4M, cluster buy [1d] | analysts net -4 m/m, 64% bullish of 53 "
        "[1d] | beta 1.21, 5% off 52w high, 46% above 52w low, vs S&P 4w +2% 13w -3%, "
        "mega cap, fwd P/E 31 [1d]"
    )
    assert len(line) <= 300


def test_flag_on_scalp_prompt_shows_the_block_after_the_feeds() -> None:
    block = ticker_facts_block(g.snapshot(), _opts(["NVDA"]))
    p = g.scalp_prompt(ticker_facts=block)
    assert p.index("FEEDS>>>") < p.index("## Ticker facts (Finnhub, code-built)")
    assert "NVDA: EPS surprise" in p and "AAPL:" not in p


def test_earnings_uses_last_four_quarters_only() -> None:
    f = ticker_facts_from_context(g.snapshot(), ["AAPL"])["AAPL"]
    assert f.earnings is not None
    assert f.earnings.surprise_pct == [-0.9, 1.1, 4.2, 4.5]  # the 5th (+9.8) is dropped
    assert (f.earnings.beats, f.earnings.misses) == (3, 1)


def test_fundamentals_distances_need_a_last_close() -> None:
    facts = ticker_facts_from_context(g.snapshot(), ["AAPL", "NVDA"])
    a, n = facts["AAPL"].fundamentals, facts["NVDA"].fundamentals
    assert a is not None and n is not None
    assert (a.pct_off_high, a.pct_above_low) == (4.8, 46.3)  # regime last_close 247.5
    assert (n.pct_off_high, n.pct_above_low) == (None, None)  # no regime entry: omitted
    assert n.cap_bucket == "mega" and n.beta == 1.21


def _snap_with(parts: dict[str, dict[str, Any] | None], as_of: str = "2026-10-05") -> Any:
    from arc.context.store import ContextSnapshot

    entries = []
    i = 0
    for kind, payload in g.finnhub_payloads("MSFT", as_of=as_of):
        if kind in parts and parts[kind] is None:
            continue
        i += 1
        entries.append(g._entry(i, kind, "MSFT", {**payload, **(parts.get(kind) or {})}, 3))
    return ContextSnapshot(id="s", as_of=g._now(), entries=entries)


def test_missing_parts_are_omitted_never_zero_filled() -> None:
    snap = _snap_with({"insider_activity": None, "analyst_recs": None})
    f = ticker_facts_from_context(snap, ["MSFT"])["MSFT"]
    assert f.insider is None and f.recs is None
    line = render_ticker_facts(f, today=TODAY)
    assert "insider" not in line and "analysts" not in line
    assert "EPS surprise" in line and "beta" in line
    # a fundamentals payload with every value missing is no part at all
    blank = dict.fromkeys(
        ("beta", "high_52w", "low_52w", "market_cap_musd", "rel_sp500_4w", "rel_sp500_13w",
         "forward_pe"),
    )  # fmt: skip
    snap = _snap_with({"fundamentals": blank, "earnings_history": {"quarters": []}})
    f = ticker_facts_from_context(snap, ["MSFT"])["MSFT"]
    assert f.fundamentals is None and f.earnings is None
    # recs with zero analysts: omitted (a 0% bullish share would be invented)
    zero = {"strong_buy": 0, "buy": 0, "hold": 0, "sell": 0, "strong_sell": 0}
    snap = _snap_with({"analyst_recs": zero})
    assert ticker_facts_from_context(snap, ["MSFT"])["MSFT"].recs is None


def test_ticker_with_no_facts_is_absent() -> None:
    snap = _snap_with(dict.fromkeys(("earnings_history", "insider_activity", "analyst_recs",
                                     "fundamentals")))  # fmt: skip
    assert ticker_facts_from_context(snap, ["MSFT", "ZZZZ"]) == {}
    assert ticker_facts_block(snap, _opts(["MSFT"])) == ""


def test_parts_older_than_their_max_age_are_omitted() -> None:
    # fetched 3 days before the snapshot: insider (2d) is stale, the weekly kinds are not
    snap = _snap_with({}, as_of="2026-10-03")
    f = ticker_facts_from_context(snap, ["MSFT"], max_age_days={"insider_activity": 2})["MSFT"]
    assert f.insider is None
    assert f.earnings is not None and f.recs is not None and f.fundamentals is not None
    assert "[3d]" in render_ticker_facts(f, today=TODAY)
    # unparseable as_of: treated as stale
    snap = _snap_with({"fundamentals": {"as_of": "n/a"}})
    assert ticker_facts_from_context(snap, ["MSFT"])["MSFT"].fundamentals is None


def test_expired_entries_never_reach_the_prompt() -> None:
    conn = connect(":memory:")
    migrate(conn)
    store = ContextStore(conn)
    t0 = g._now() - dt.timedelta(days=3)
    for kind, payload in g.finnhub_payloads("AAPL", as_of="2026-10-03"):
        store.write(kind=kind, subject="AAPL", payload=payload, produced_by="finnhub",
                    ttl="2d" if kind == "insider_activity" else "8d", now=t0)  # fmt: skip
    snap = store.snapshot(g._now())
    f = ticker_facts_from_context(snap, ["AAPL"], max_age_days={"insider_activity": 30})["AAPL"]
    assert f.insider is None  # the entry expired (2d TTL); a long max_age cannot revive it
    assert f.earnings is not None


def test_render_drops_whole_parts_to_fit_the_budget() -> None:
    f = ticker_facts_from_context(g.snapshot(), ["AAPL"])["AAPL"]
    full = render_ticker_facts(f, today=TODAY, max_chars=1000)
    assert full.count(" | ") == 3
    short = render_ticker_facts(f, today=TODAY, max_chars=120)
    assert len(short) <= 120 and short.startswith("AAPL: ") and " …" not in short
    assert all(part in full for part in short.removeprefix("AAPL: ").split(" | "))
    assert render_ticker_facts(f, today=TODAY, max_chars=10) == ""


_num = st.one_of(st.none(), st.floats(-1e7, 1e7, allow_nan=False))


@settings(max_examples=150, deadline=None)
@given(
    surprise=st.lists(st.floats(-1e6, 1e6, allow_nan=False), max_size=4),
    beats=st.integers(0, 4),
    net=_num,
    shares=st.integers(-(10**12), 10**12),
    change=st.one_of(st.none(), st.integers(-500, 500)),
    analysts=st.integers(1, 10**6),
    beta=_num,
    pe=_num,
    rel=_num,
    cap=st.sampled_from([None, "mega", "large", "mid", "small", "micro"]),
    ticker=st.text("ABCDEFGHIJKLMNOPQRSTUVWXYZ.", min_size=1, max_size=10),
    max_chars=st.integers(80, 1000),
)
def test_rendered_line_never_exceeds_the_budget(
    surprise: list[float],
    beats: int,
    net: float | None,
    shares: int,
    change: int | None,
    analysts: int,
    beta: float | None,
    pe: float | None,
    rel: float | None,
    cap: str | None,
    ticker: str,
    max_chars: int,
) -> None:
    a = "2026-10-05"
    f = TickerFacts(
        ticker=ticker,
        earnings=EarningsFacts(as_of=a, surprise_pct=surprise, beats=beats, misses=4 - beats),
        insider=InsiderFacts(
            as_of=a, window_days=90, net_value_usd=net, net_shares=shares, cluster_buy=True
        ),  # fmt: skip
        recs=RecsFacts(as_of=a, net_change=change, bullish_share=0.5, analysts=analysts),
        fundamentals=FundamentalsFacts(
            as_of=a,
            beta=beta,
            pct_off_high=rel,
            pct_above_low=rel,
            rel_sp500_4w=rel,
            rel_sp500_13w=rel,
            cap_bucket=cap,
            forward_pe=pe,
        ),  # fmt: skip
    )
    assert len(render_ticker_facts(f, today=TODAY, max_chars=max_chars)) <= max_chars


def test_block_respects_the_configured_budget() -> None:
    block = ticker_facts_block(g.snapshot(), _opts(["AAPL", "NVDA"], max_chars_per_ticker=150))
    lines = block.splitlines()
    assert [ln.split(":")[0] for ln in lines] == ["AAPL", "NVDA"]
    assert all(len(ln) <= 150 for ln in lines)


def test_cap_buckets_follow_config_thresholds() -> None:
    snap = _snap_with({"fundamentals": {"market_cap_musd": 5_000.0}})
    f = ticker_facts_from_context(snap, ["MSFT"])["MSFT"]
    assert f.fundamentals is not None and f.fundamentals.cap_bucket == "mid"
    f = ticker_facts_from_context(snap, ["MSFT"], cap_buckets_musd={"large": 4_000.0})["MSFT"]
    assert f.fundamentals is not None and f.fundamentals.cap_bucket == "large"
    snap = _snap_with({"fundamentals": {"market_cap_musd": 50.0}})
    f = ticker_facts_from_context(snap, ["MSFT"])["MSFT"]
    assert f.fundamentals is not None and f.fundamentals.cap_bucket == "micro"


def test_tickers_are_normalised_and_deduped() -> None:
    facts = ticker_facts_from_context(g.snapshot(), [" aapl", "AAPL", "", "nvda"])
    assert list(facts) == ["AAPL", "NVDA"]


# ---------------------------------------------------------------------------
# D31 digest: as_of only
# ---------------------------------------------------------------------------


def _loop_inputs(facts: list[str]) -> LoopInputs:
    return LoopInputs(
        candidates=["c@2"], regimes=[], positions=[], pnl_bucket=0, pending_orders=0,
        budget_tier="normal", suppressed=[], facts=facts,
    )  # fmt: skip


def test_digest_is_unchanged_with_the_flag_off() -> None:
    from arc.routines.manifest import digest

    pre = _loop_inputs([]).model_dump(mode="json")
    pre.pop("facts")
    assert _loop_inputs([]).digest() == digest(pre)  # == the pre-E4.8a digest
    assert _loop_inputs([]).payload() == pre  # recorded input == pre-E4.8a payload
    assert digest(_loop_inputs(["k"]).payload()) == _loop_inputs(["k"]).digest()


def test_digest_moves_on_as_of_only() -> None:
    snap = g.snapshot()
    keys = ticker_facts_digest(snap, ["AAPL"])
    assert keys == sorted(f"{k}:AAPL@2026-10-05" for k in (
        "earnings_history", "insider_activity", "analyst_recs", "fundamentals"))  # fmt: skip
    # a re-fetch with the same as_of (new entry id, other numbers) is no change
    refetch = ContextSnapshot(
        id="s2",
        as_of=snap.as_of,
        entries=[
            e.model_copy(update={"id": e.id + "x", "payload": {**e.payload, "beta": 9.9}})
            if e.kind == "fundamentals"
            else e
            for e in snap.entries
        ],
    )
    assert ticker_facts_digest(refetch, ["AAPL"]) == keys
    same = ticker_facts_digest(refetch, ["AAPL"])
    assert _loop_inputs(keys).digest() == _loop_inputs(same).digest()
    # the weekly refresh (new as_of) is a change
    newer = _snap_with({}, as_of="2026-10-06")
    assert ticker_facts_digest(newer, ["MSFT"]) != ticker_facts_digest(_snap_with({}), ["MSFT"])


def test_research_facts_tickers_follow_candidates_and_the_flag() -> None:
    from types import SimpleNamespace

    from arc.pipeline.steps import _research_facts_tickers, _research_ticker_facts

    cands = [e for e in g.snapshot().entries if e.kind == "candidate"]
    off = SimpleNamespace(routines=load_routines(DEFAULT_ROUTINES_PATH))
    assert _research_facts_tickers(off, cands) == []  # type: ignore[arg-type]
    assert _research_ticker_facts(off, cands) is None  # type: ignore[arg-type]
    on_cfg = RoutinesConfig.model_validate(
        {"personas": {"finnhub_context": "on"}, "finnhub_context": {"research_max_tickers": 1}}
    )
    on = SimpleNamespace(routines=on_cfg)
    assert _research_facts_tickers(on, cands) == ["AAPL"]  # type: ignore[arg-type]  # conf 0.8 > 0.7
    opts = _research_ticker_facts(on, cands)  # type: ignore[arg-type]
    assert opts is not None and opts["tickers"] == ["AAPL"] and opts["max_chars"] == 300
    json.dumps(opts)  # recorded as a prompt input (journal replay)


# ---------------------------------------------------------------------------
# Scalp wiring
# ---------------------------------------------------------------------------


def _seed_docs(conn: Any, now: dt.datetime) -> None:
    repo = RawDocRepo(conn)
    for i, t in enumerate(("AAPL", "NVDA")):
        repo.insert(
            source="rss",
            url=f"https://example.com/{t.lower()}/{i}",
            published_at=(now - dt.timedelta(hours=2)).isoformat(),
            text=f"{t} news item {i}: topic{i} detail{i} angle{i}",
            tickers_hint=[t],
            id=f"doc-{i:03d}",
        )


@pytest.mark.parametrize("flag", ["off", "on"])
def test_run_scalp_adds_facts_only_with_the_flag_on(flag: str) -> None:
    conn = connect(":memory:")
    migrate(conn)
    now = g._now()
    store = ContextStore(conn)
    for kind, payload in g.finnhub_payloads("AAPL"):
        store.write(kind=kind, subject="AAPL", payload=payload, produced_by="finnhub",
                    now=now - dt.timedelta(hours=20))  # fmt: skip
    _seed_docs(conn, now)
    routines = RoutinesConfig.model_validate({"personas": {"finnhub_context": flag}})
    cfg = ArcSettings(env="paper", universe=["AAPL", "NVDA"], universe_mode="strict",
                      scalp_min_confidence=0.6)  # fmt: skip
    llm = FixtureScalpLLM([json.dumps({"candidates": [], "scan_summary": "s"})])
    run_scalp(conn, cfg, llm=llm, now=now, run_id="r1", routines=routines)
    (prompt,) = llm.prompts
    if flag == "off":
        assert "Ticker facts" not in prompt
        snaps = conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
        assert snaps == 0  # no extra read recorded with the flag off
    else:
        assert "## Ticker facts (Finnhub, code-built)" in prompt
        assert "AAPL: EPS surprise" in prompt and "NVDA:" not in prompt  # NVDA has no data
        row = conn.execute("SELECT kinds, run_id FROM context_snapshots").fetchone()
        assert row[1] == "r1" and "fundamentals" in json.loads(row[0])


def test_scalp_facts_tickers_cap_and_order() -> None:
    from arc.context.kinds import StoryPayload

    def story(i: int, tickers: list[str]) -> StoryPayload:
        return StoryPayload.model_validate({
            "story_id": f"s{i}", "headline": "h", "summary": "s", "category": "company_data",
            "tickers": tickers, "urls": [f"https://x/{i}"], "doc_ids": [f"d{i}"],
            "source_keys": ["rss"], "distinct_sources": 1,
            "first_published": "2026-10-06T08:00:00-04:00",
            "last_published": "2026-10-06T08:00:00-04:00",
        })  # fmt: skip

    batch = [story(1, ["nvda", "AAPL"]), story(2, ["AAPL", "MSFT", "TSLA"])]
    assert scalp_facts_tickers(batch, 3) == ["NVDA", "AAPL", "MSFT"]
    assert scalp_facts_tickers(batch, 0) == []


# ---------------------------------------------------------------------------
# The gate never sees TickerFacts; Candidate stays text-free
# ---------------------------------------------------------------------------


def test_gate_and_candidate_never_carry_ticker_facts() -> None:
    for path in (REPO / "arc" / "gate").rglob("*.py"):
        tree = ast.parse(path.read_text())
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not any(m.startswith("arc.personas") for m in names), path
        assert "TickerFacts" not in path.read_text(), path
    assert set(Candidate.model_fields) == {
        "ticker", "stance", "catalyst_type", "catalyst_date", "confidence", "sources",
        "created_at", "corroboration", "id",
    }  # fmt: skip
