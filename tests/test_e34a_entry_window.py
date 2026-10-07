"""E3.4a (Analyst A-4): one entry DTE window, read from config, in every entry persona prompt.

- Research / Quant / Risk prompts state the profile's window and delta bands, once.
- No literal DTE range or delta band in ``arc/personas``.
- The expiry-cluster flag and the portfolio block never put a bucket label next to "DTE".
- Persona prose carried between prompts (notes, shortlist, structures) cannot restate
  a window: rebuilding the 2026-10-01 prompts yields 30-60 and no 22-45.
- A Quant skip that cites DTE for an in-window menu item logs
  ``quant.dte_rule_outside_config`` (observability only).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import structlog

from arc.config import ArcSettings
from arc.context.store import ContextStore
from arc.ingest.llm import FixtureScalpLLM
from arc.personas.builders import (
    QuantInput,
    ResearchInput,
    RiskInput,
    build_quant_prompt,
    build_research_prompt,
    build_risk_prompt,
)
from arc.personas.entry_window import (
    EntryTerms,
    entry_terms,
    mentions_dte,
    scrub_carried_text,
)
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.env import FIXTURES_DIR
from arc.pipeline.runner import open_db
from arc.pipeline.steps import build_prompt
from arc.positions.portfolio import (
    BUCKET_DISPLAY,
    GreekUsage,
    PortfolioAggregates,
    bucket_display,
    relabel_buckets,
)
from arc.routines.config import load_routines

REPO = Path(__file__).resolve().parent.parent
DTE_RANGE = re.compile(r"\b(\d{1,3})\s*[-\u2013]\s*(\d{1,3})\s*DTE\b")


def _settings(profile: str) -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _no_gate_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    for var in ("ARC_DTE_MIN", "ARC_DTE_MAX", "ARC_ACCOUNT_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def _prompts(terms: EntryTerms) -> dict[str, str]:
    return {
        "research": build_research_prompt(
            ResearchInput(
                candidates_json="{}",
                regime_features_json="{}",
                portfolio_summary="flat",
                scan_date="2026-10-01",
                entry_terms=terms,
            )
        ),
        "quant": build_quant_prompt(
            QuantInput(
                shortlist_json="{}",
                chains_json="{}",
                underlying_prices_json="{}",
                scan_date="2026-10-01",
                entry_terms=terms,
            )
        ),
        "risk": build_risk_prompt(
            RiskInput(
                structures_json="{}",
                portfolio_json="{}",
                calendar_json="{}",
                account_equity=100_000.0,
                scan_date="2026-10-01",
                entry_terms=terms,
            )
        ),
    }


# ---------------------------------------------------------------------------
# Terms come from config
# ---------------------------------------------------------------------------


class TestEntryTerms:
    def test_cash_debit_window_and_bands(self) -> None:
        t = entry_terms(_settings("cash_debit"))
        assert (t.dte_min, t.dte_max) == (30, 60)
        assert t.window == "30-60 DTE"
        assert t.bands_text() == "debit-vertical short legs 20-35 delta; long legs 40-70 delta"

    def test_margin_window_and_bands(self) -> None:
        t = entry_terms(_settings("margin"))
        assert (t.dte_min, t.dte_max) == (30, 45)
        assert t.bands_text() == "short strikes 16-30 delta"

    def test_cash_long_only_has_long_band_only(self) -> None:
        t = entry_terms(_settings("cash_long_only"))
        assert t.window == "30-60 DTE"
        assert t.bands_text() == "long legs 40-70 delta"

    def test_window_follows_config_not_text(self) -> None:
        s = _settings("margin").model_copy(
            update={"dte_min": 21, "dte_max": 35, "scanner_short_delta_min": 0.10}
        )
        t = entry_terms(s)
        assert t.window == "21-35 DTE"
        assert "short strikes 10-30 delta" in t.bands_text()

    def test_no_strategy_renders_explicitly(self) -> None:
        t = EntryTerms(account_profile="x", dte_min=30, dte_max=60, bands=[])
        assert t.bands_text() == "no strike band (no strategy)"

    def test_round_trips_through_prompt_inputs(self) -> None:
        t = entry_terms(_settings("cash_debit"))
        assert EntryTerms.model_validate(json.loads(json.dumps(t.model_dump(mode="json")))) == t


# ---------------------------------------------------------------------------
# Prompts state the configured window exactly once
# ---------------------------------------------------------------------------


class TestPrompts:
    @pytest.mark.parametrize("persona", ["research", "quant", "risk"])
    def test_cash_debit_one_range_30_60(self, persona: str) -> None:
        p = _prompts(entry_terms(_settings("cash_debit")))[persona]
        assert DTE_RANGE.findall(p) == [("30", "60")]
        assert "30-45" not in p and "22-45" not in p and "16-30" not in p
        if persona != "research":
            assert "debit-vertical short legs 20-35 delta" in p
            assert "long legs 40-70 delta" in p

    @pytest.mark.parametrize("persona", ["research", "quant", "risk"])
    def test_margin_one_range_30_45(self, persona: str) -> None:
        p = _prompts(entry_terms(_settings("margin")))[persona]
        assert DTE_RANGE.findall(p) == [("30", "45")]
        if persona != "research":
            assert "short strikes 16-30 delta" in p

    def test_quant_says_menu_is_in_window(self) -> None:
        p = _prompts(entry_terms(_settings("cash_debit")))["quant"]
        assert "Every menu structure is inside the 30-60 DTE entry window" in p
        assert "do not reject or skip a menu item on DTE" in p

    def test_research_says_window_is_fixed(self) -> None:
        p = _prompts(entry_terms(_settings("cash_debit")))["research"]
        assert "fixed by config" in p and "may not be narrowed" in p
        assert "spread expiries within the entry window" in p

    def test_without_terms_no_window_is_stated(self) -> None:
        for p in _prompts(None).values():  # type: ignore[arg-type]
            assert not DTE_RANGE.search(p)

    def test_personas_package_has_no_literal_range(self) -> None:
        """A grep over arc/personas: no ``N-M DTE`` and no ``N-M delta`` literal."""
        pat = re.compile(r"\d+\s*[-\u2013]\s*\d+\s*(DTE|delta)", re.IGNORECASE)
        hits = [
            f"{f.name}:{i}: {line.strip()}"
            for f in sorted((REPO / "arc" / "personas").glob("*.py"))
            for i, line in enumerate(f.read_text().splitlines(), 1)
            if pat.search(line)
        ]
        assert hits == []


# ---------------------------------------------------------------------------
# Bucket labels cannot become a rule
# ---------------------------------------------------------------------------


def _aggregates(**kw: object) -> PortfolioAggregates:
    base: dict[str, object] = {
        "total_max_loss": 4656.0,
        "by_expiry_bucket": {"46+": 0.88, "22-45": 0.12},
        "delta": GreekUsage(net=0.0, cap=1.0, pct_used=0.0),
        "vega": GreekUsage(net=0.0, cap=1.0, pct_used=0.0),
        "gamma": 0.0,
        "theta": 0.0,
        "positions": 2,
        "max_positions": 8,
        "flags": ["expiry_cluster"],
        "flagged_expiry_buckets": ["46+"],
    }
    base.update(kw)
    return PortfolioAggregates.model_validate(base)


class TestExpiryBuckets:
    def test_week_labels(self) -> None:
        assert [bucket_display(b) for b in ("0-7", "8-21", "22-45", "46+")] == [
            "0-1w",
            "1-3w",
            "3-6w",
            "6w+",
        ]
        assert bucket_display("other") == "other"

    def test_cluster_text_has_no_dte_range(self) -> None:
        from arc.pipeline.portfolio_context import expiry_cluster_text

        text = expiry_cluster_text(_aggregates())
        assert text.startswith("expiry_cluster: 88% of open max loss expires in one bucket (6w+)")
        assert "Spread expiries within the configured entry window" in text
        assert not re.search(r"\d+\s*-\s*\d+", text) and "46+" not in text

    def test_relabel_legacy_block(self) -> None:
        old = (
            "- os-1 MU vertical_debit neutral x1 50 DTE (46+); sector unknown\n"
            "- os-2 SPY vertical_debit bearish x1 36 DTE (22-45); sector broad_market\n"
            "by expiry 46+ 88%, 22-45 12%. Flags: expiry_cluster (expiry 46+). "
            "Fixed 2026-10-01 entry 146.70; Δ +25.1"
        )
        new = relabel_buckets(old)
        assert "22-45" not in new and "46+" not in new
        assert "50 DTE (6w+)" in new and "36 DTE (3-6w)" in new
        assert "2026-10-01" in new and "146.70" in new  # dates and prices untouched
        assert set(BUCKET_DISPLAY.values()) >= {"6w+", "3-6w"}

    def test_rendered_block_uses_week_labels(self) -> None:
        """The E5.9 open-book block (real builder): bucket labels never sit next to DTE."""
        from arc.pipeline.portfolio_context import (
            build_portfolio_context,
            render_portfolio_context,
        )
        from tests.test_e59_research_portfolio import (
            IRON_CONDOR,
            LONG_CALL,
            _env,
            _open_structure,
        )

        settings = _settings("margin")
        conn = open_db(":memory:", copy=False)
        env = _env([])
        _open_structure(conn, env, IRON_CONDOR)
        _open_structure(conn, env, LONG_CALL, stance="bullish", entry="12.10", contracts=2)
        pc = build_portfolio_context(
            conn, env, settings, info=env.account(), now=FIXTURE_NOW, halted=False,
            budget_tier="normal",
        )  # fmt: skip
        text = render_portfolio_context(pc, settings)
        assert "expiry bucket 3-6w" in text and "22-45" not in text
        assert not DTE_RANGE.search(text)
        assert "by expiry bucket 3-6w 100%" in text
        assert "expiry_cluster: 100% of open max loss expires in one bucket (3-6w)" in text


# ---------------------------------------------------------------------------
# Carried persona prose cannot restate a window
# ---------------------------------------------------------------------------


class TestScrub:
    @pytest.mark.parametrize(
        "text",
        [
            "prefer 22-45 DTE expiries to ease the 46+ cluster",
            "Rules: debit-only, 22-45 DTE, non-negative managed EV",
            "price the Nov 06 chain (36 DTE, inside 22-45) after NFP",
            "fail Research's two rules: hold 22-45 days",
            "short strikes at 16-30 delta (220 area)",
            "0.249 delta, inside the 16-30 band",
            "Every structure offered is 50 DTE, past the 30-45 target",
            "outside the 16-30 range",
            "a 22 – 45 DTE window",
        ],
    )
    def test_ranges_are_scrubbed(self, text: str) -> None:
        out = scrub_carried_text(text)
        assert not re.search(r"\d+\s*[-\u2013]\s*\d+", out), out
        assert "configured-range" in out

    @pytest.mark.parametrize(
        "text",
        [
            "expiry 2026-11-20, 240/225 put spread",
            "confidence 0-1",
            "VIX 16.34 in contango (VIX3M/VIX 1.12)",
            "8-K filed Sep 24",
            "36 DTE",
        ],
    )
    def test_other_text_untouched(self, text: str) -> None:
        assert scrub_carried_text(text) == text

    def test_mentions_dte(self) -> None:
        assert mentions_dte("50 DTE, outside the window")
        assert mentions_dte("too many days to expiry")
        assert not mentions_dte("negative managed EV after costs")


# ---------------------------------------------------------------------------
# Pipeline wiring: recorded inputs, replay, observability
# ---------------------------------------------------------------------------


def _run(settings: ArcSettings, env: PipelineEnv):  # noqa: ANN202
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import run_propose
    from arc.routines.heartbeat import RecordingNotifier

    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    report = run_propose(
        conn, settings, load_routines(), env, now=FIXTURE_NOW, notifier=RecordingNotifier()
    )
    return conn, report


class TestPipeline:
    def test_margin_chain_prompts_and_recorded_inputs(self) -> None:
        settings = _settings("margin")
        env = PipelineEnv.fixtures()
        conn, report = _run(settings, env)
        assert {o.job: o.status for o in report.outcomes}["risk.open"] == "ok"
        for llm, persona in (("research", "research"), ("quant", "quant"), ("risk", "risk_open")):
            prompt = env.llms[llm].prompts[0]  # type: ignore[attr-defined]
            assert DTE_RANGE.findall(prompt) == [("30", "45")], persona
            row = conn.execute(
                "SELECT prompt_inputs FROM persona_calls WHERE persona=?", (persona,)
            ).fetchone()
            terms = json.loads(row["prompt_inputs"])["entry_terms"]
            assert (terms["dte_min"], terms["dte_max"]) == (30, 45)
        assert "short strikes 16-30 delta" in env.llms["quant"].prompts[0]  # type: ignore[attr-defined]

    def test_cash_debit_chain_prompts(self) -> None:
        from arc.pipeline.env import FIXTURE_SETS

        settings = _settings("cash_debit")
        env = PipelineEnv.fixtures(FIXTURE_SETS["bullish"])
        _, report = _run(settings, env)
        assert {o.job: o.status for o in report.outcomes}["quant.open"] == "ok"
        for persona in ("research", "quant"):
            prompt = env.llms[persona].prompts[0]  # type: ignore[attr-defined]
            assert DTE_RANGE.findall(prompt) == [("30", "60")], persona
            assert "30-45" not in prompt
        quant = env.llms["quant"].prompts[0]  # type: ignore[attr-defined]
        assert "debit-vertical short legs 20-35 delta; long legs 40-70 delta" in quant

    def test_replay_sha_matches(self) -> None:
        from arc.journal.report import replay

        conn, report = _run(_settings("margin"), PipelineEnv.fixtures())
        chain = conn.execute(
            "SELECT chain_run_id FROM routine_runs WHERE chain_run_id IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        results = replay(conn, chain)
        assert results and all(r.ok for r in results), results

    def test_rebuild_pre_e34a_inputs_with_drifted_prose(self) -> None:
        """A 10-01-shaped Research call (no entry_terms, legacy bucket labels, an improvised
        22-45 rule in a prior note) rebuilds with 30-60 and no 22-45 under cash_debit."""
        conn = open_db(":memory:", copy=False)
        store = ContextStore(conn)
        store.write(
            kind="note",
            subject="MCD",
            payload={
                "persona": "quant",
                "topic": "observation",
                "title": "no trade",
                "body": "No trade. Research's gate requires debit-only, 22-45 DTE, short "
                "strikes at 16-30 delta. Every structure is 50 DTE, outside the window.",
            },
            produced_by="quant",
            supersede="accumulate",
            now=FIXTURE_NOW,
        )
        snap = store.snapshot(FIXTURE_NOW)
        inputs = {
            "portfolio_summary": "x",
            "scan_date": "2026-10-01",
            "max_notes": 20,
            "portfolio_block": "- os-1 MU vertical_debit neutral x1 50 DTE (46+); "
            "by expiry 46+ 100%. Flags: expiry_cluster (expiry 46+).",
            "recent_ideas": "",
            "rules": ["prefer 22-45 DTE expiries to ease the 46+ cluster"],
        }
        p = build_prompt("research", snap, inputs, settings=_settings("cash_debit"))
        assert "30-60 DTE" in p
        body = p.split("## Hard constraints")[0]
        assert "22-45" not in body and "16-30" not in body and "46+" not in body
        assert DTE_RANGE.findall(body) == [("30", "60")]

    def test_quant_dte_skip_is_logged(self) -> None:
        quant = json.loads((FIXTURES_DIR / "quant.json").read_text())
        for sk in quant["skipped"]:
            if sk["ticker"] == "NVDA":
                sk["reason"] = "50 DTE, outside the window"
        env = PipelineEnv.fixtures()
        env.llms["quant"] = FixtureScalpLLM([json.dumps(quant)])
        with structlog.testing.capture_logs() as logs:
            conn, _ = _run(_settings("margin"), env)
        hits = [e for e in logs if e["event"] == "quant.dte_rule_outside_config"]
        assert [h["ticker"] for h in hits] == ["NVDA"]
        assert hits[0]["dte_window"] == "30-45" and hits[0]["menu_dtes"]
        # observability only: the skip is still journalled as the Quant's skip
        row = conn.execute(
            "SELECT reason_code FROM decisions WHERE subject='NVDA' AND stage='structure'"
        ).fetchone()
        assert row is not None and row[0] == "quant_skipped"

    def test_non_dte_skip_is_not_logged(self) -> None:
        with structlog.testing.capture_logs() as logs:
            _run(_settings("margin"), PipelineEnv.fixtures())
        assert not [e for e in logs if e["event"] == "quant.dte_rule_outside_config"]
