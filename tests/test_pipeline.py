"""E5.2 pipeline runner: sizing (D18), step filters, gate wiring, `arc propose --fixtures`."""

from __future__ import annotations

import datetime as _dt
import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.ingest.llm import FixtureScalpLLM, LLMResult, ScalpLLMError
from arc.pipeline import FIXTURE_NOW, PipelineEnv
from arc.pipeline.env import FIXTURES_DIR
from arc.pipeline.market import (
    ETF_UNDERLYINGS,
    account_snapshot,
    limit_price,
    next_earnings,
)
from arc.pipeline.runner import fixture_run, open_db
from arc.pipeline.store import PersonaCallRepo, proposals_for_day
from arc.routines.config import load_routines
from arc.sizing import size_contracts

if TYPE_CHECKING:
    from pathlib import Path

GATE_SECRET = "x" * 40


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> ArcSettings:
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    return ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]


@pytest.fixture
def routines():  # noqa: ANN201
    return load_routines()


# ---------------------------------------------------------------------------
# D18 sizing
# ---------------------------------------------------------------------------


class TestSizing:
    def test_suggestion_capped(self) -> None:
        r = size_contracts(
            suggestion=20, max_loss_per_contract=Decimal(334), equity=Decimal(100_000), cap_pct=0.05
        )
        assert (r.contracts, r.cap_contracts) == (14, 14)
        assert r.max_loss_total == Decimal(4676)
        assert r.trade

    def test_suggestion_below_cap_wins(self) -> None:
        r = size_contracts(
            suggestion=3, max_loss_per_contract=Decimal(334), equity=Decimal(100_000), cap_pct=0.05
        )
        assert r.contracts == 3

    @pytest.mark.parametrize(
        ("loss", "equity", "suggestion", "reason"),
        [
            (None, Decimal(100_000), 5, "unbounded"),
            (Decimal(0), Decimal(100_000), 5, "not positive"),
            (Decimal(334), Decimal(0), 5, "equity"),
            (Decimal(6000), Decimal(100_000), 5, "exceeds"),
            (Decimal(334), Decimal(100_000), 0, "suggested 0"),
        ],
    )
    def test_no_trade(
        self, loss: Decimal | None, equity: Decimal, suggestion: int, reason: str
    ) -> None:
        r = size_contracts(
            suggestion=suggestion, max_loss_per_contract=loss, equity=equity, cap_pct=0.05
        )
        assert r.contracts == 0
        assert not r.trade
        assert reason in r.reason

    @given(
        suggestion=st.integers(-5, 500),
        loss=st.decimals(min_value=1, max_value=50_000, places=2),
        equity=st.decimals(min_value=1, max_value=10_000_000, places=2),
        cap=st.floats(min_value=0.001, max_value=0.5),
    )
    def test_never_exceeds_cap_or_suggestion(
        self, suggestion: int, loss: Decimal, equity: Decimal, cap: float
    ) -> None:
        r = size_contracts(
            suggestion=suggestion, max_loss_per_contract=loss, equity=equity, cap_pct=cap
        )
        assert r.contracts <= max(suggestion, 0)
        assert r.max_loss_total <= Decimal(str(cap)) * equity
        assert r.max_loss_total == loss * r.contracts
        if r.contracts == 0:
            assert r.reason


# ---------------------------------------------------------------------------
# Deterministic market helpers
# ---------------------------------------------------------------------------


class TestMarketHelpers:
    @pytest.mark.parametrize(
        ("net", "tick", "want"),
        [
            (Decimal("-1.6555"), 0.01, Decimal("-1.65")),
            (Decimal("-1.67"), 0.05, Decimal("-1.65")),
            (Decimal("2.031"), 0.05, Decimal("2.05")),
            (Decimal("2.00"), 0.05, Decimal("2.00")),
        ],
    )
    def test_limit_price(self, net: Decimal, tick: float, want: Decimal) -> None:
        assert limit_price(net, tick) == want

    def test_account_snapshot_missing_last_equity_fails_closed(self) -> None:
        from arc.broker.base import AccountInfo

        info = AccountInfo(
            account_id="a", equity=Decimal(100), buying_power=Decimal(0), cash=Decimal(0)
        )
        snap = account_snapshot(info, FIXTURE_NOW, baseline=None)
        assert snap.last_equity == 0

    def test_next_earnings(self) -> None:
        conn = open_db(":memory:", copy=False)
        today = _dt.date(2026, 9, 25)
        with conn:
            for i, (sym, day) in enumerate(
                [("AAPL", "2026-07-30"), ("AAPL", "2026-10-29"), ("NVDA", "2026-11-19")]
            ):
                conn.execute(
                    "INSERT INTO raw_docs (id, source, url, published_at, content_hash, text, "
                    "tickers_hint, ingested_at) VALUES (?, 'earnings', ?, ?, ?, '', ?, ?)",
                    (
                        f"e{i}",
                        f"https://finnhub.io/calendar/earnings/{sym}/{day}",
                        FIXTURE_NOW.isoformat(),
                        f"h{i}",
                        json.dumps([sym]),
                        FIXTURE_NOW.isoformat(),
                    ),
                )
        got = next_earnings(conn, ["aapl", "NVDA", "SPY", "TSLA"], today)
        assert got == {
            "AAPL": _dt.date(2026, 10, 29),
            "NVDA": _dt.date(2026, 11, 19),
            "SPY": None,
        }
        assert "TSLA" not in got  # unknown → gate fails closed on short premium
        assert "SPY" in ETF_UNDERLYINGS


# ---------------------------------------------------------------------------
# End to end (offline)
# ---------------------------------------------------------------------------


class TestFixtureRun:
    def test_personas_emit_notes(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        """D27: persona narrative is stored as typed ``note`` entries, each annotating a real
        entry, and every run's writes stay inside its declared contract."""
        conn, report = fixture_run(settings, routines)
        assert not report.failed
        rows = conn.execute(
            "SELECT id, subject, payload FROM context_entries WHERE kind = 'note'"
        ).fetchall()
        got = {
            (r["subject"], json.loads(r["payload"])["persona"], json.loads(r["payload"])["topic"])
            for r in rows
        }
        assert {
            ("market", "scalp", "observation"),
            ("session", "research", "regime_view"),
            ("SPY", "research", "thesis"),
            ("SPY", "quant", "thesis"),
            ("session", "quant", "observation"),
            ("session", "risk", "risk_flag"),
        } <= got
        ids = {r[0] for r in conn.execute("SELECT id FROM context_entries")}
        for r in rows:
            about = json.loads(r["payload"])["about"]
            assert about and set(about) <= ids
        scalp_notes = [s for s in got if s[1] == "scalp"]
        assert len(scalp_notes) == 1  # one per scalp run, not per batch

    def test_long_note_is_truncated_not_failed(self) -> None:
        from unittest.mock import MagicMock

        from arc.context.kinds import NoteTopic
        from arc.pipeline.steps import _note

        ctx = MagicMock()
        _note(
            ctx, "session", persona="quant", topic=NoteTopic.OBSERVATION, title="x" * 300,
            body="y" * 5000, about=["e1"],
        )  # fmt: skip
        payload = ctx.write.call_args.args[2]
        assert len(payload.body) == 4000 and len(payload.title) == 120

    def test_end_to_end(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        conn, report = fixture_run(settings, routines)
        assert [(o.job, o.status) for o in report.outcomes] == [
            ("scalp", "ok"),
            ("research", "ok"),
            ("quant", "ok"),
            ("risk", "ok"),
            ("propose", "ok"),
            ("execute", "ok"),  # D34: no Slack in fixtures -> publishes nothing, dispatches nothing
        ]
        assert not report.failed
        # every chain step (research → execute) shares one chain_run_id
        root = report.outcomes[1].run_id
        chain_ids = {o.chain_run_id for o in report.outcomes[1:]}
        assert len(chain_ids) == 1
        assert None not in chain_ids

        (p,) = report.proposals
        assert p["ticker"] == "SPY"
        assert p["day"] == "2026-09-25"
        assert p["gate_passed"] == 1
        assert not p["gate_token"]  # no ARC_GATE_SECRET → gate passes, no token minted
        sizing = json.loads(p["sizing_json"])
        assert sizing["contracts"] == 14  # min(Risk 20, floor(5% × 100k / 334.45))
        structure = json.loads(p["structure_json"])
        legs = sorted(leg["occ_symbol"] for leg in structure["legs"])
        assert legs == sorted(
            [
                "SPY261030P00740000",
                "SPY261030P00745000",
                "SPY261030C00798000",
                "SPY261030C00803000",
            ]
        )
        # Scanner numbers, not the LLM's made-up ones (Quant fixture claims max_loss 250)
        assert Decimal(structure["max_loss"]) == Decimal("334.45")

        calls = {c["persona"]: c for c in PersonaCallRepo(conn).for_run(root or "")}
        assert calls["research"]["status"] == "ok"
        assert json.loads(calls["research"]["dropped"]) == {"not_a_candidate": 1}  # BRK.B

        kinds = {
            r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM context_entries").fetchall()
        }
        assert {
            "candidate",
            "regime",
            "shortlist",
            "structures",
            "risk_review",
            "proposal",
        } <= kinds

    def test_idempotent_per_day_ticker(
        self, settings: ArcSettings, routines, tmp_path: Path
    ) -> None:  # noqa: ANN001
        db = tmp_path / "arc.db"
        fixture_run(settings, routines, db=db)
        # same slot: the dispatcher dedupes the whole run
        _, again = fixture_run(settings, routines, db=db)
        assert {o.status for o in again.outcomes} == {"duplicate"}
        # later the same day: steps run; E5.9 idea dedupe holds the repeat (same
        # fingerprint, spot unchanged, regime unchanged -> dedupe_proposed)
        later = FIXTURE_NOW + _dt.timedelta(minutes=5)
        conn, report = fixture_run(settings, routines, db=db, now=later)
        assert len(report.proposals) == 1
        propose = next(o for o in report.outcomes if o.job == "propose")
        assert "repeat idea" in (propose.summary or "")
        assert propose.metrics["dedupe"] == 1
        assert len(proposals_for_day(conn, "2026-09-25")) == 1
        row = conn.execute(
            "SELECT fingerprint, spot, regime, chain_run_id FROM proposals WHERE kind='open'"
        ).fetchone()
        assert row["fingerprint"].startswith("SPY|") and row["spot"] and row["chain_run_id"]
        codes = {
            r["reason_code"] for r in conn.execute("SELECT reason_code FROM decisions").fetchall()
        }
        assert "dedupe_proposed" in codes

    def test_fixtures_never_mint_token_even_with_secret(
        self,
        routines,
        monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        monkeypatch.setenv("ARC_GATE_SECRET", GATE_SECRET)
        _, report = fixture_run(ArcSettings(_env_file=None, account_profile="margin"), routines)  # type: ignore[call-arg]
        (p,) = report.proposals
        assert p["gate_passed"]
        assert not p["gate_token"]  # dry-run / fixtures: the gate verdict only, no permission

    def test_gate_token_minted_when_env_allows(
        self,
        routines,
        monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    ) -> None:
        from arc.ingest.scalp import load_fixture_docs
        from arc.pipeline.runner import run_propose
        from arc.routines.heartbeat import LogNotifier

        monkeypatch.setenv("ARC_GATE_SECRET", GATE_SECRET)
        settings = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        env = PipelineEnv.fixtures()
        env.mint_tokens = True  # what PipelineEnv.live(broker=True) sets
        report = run_propose(
            conn, settings, routines, env, now=FIXTURE_NOW, notifier=LogNotifier(), mode="live"
        )
        (p,) = report.proposals
        assert p["gate_passed"] and p["gate_token"]

    def test_halt_fails_gate(self, settings: ArcSettings, routines, tmp_path: Path) -> None:  # noqa: ANN001
        from arc.gate.halt import HaltSwitch
        from arc.store.repos import HaltRepo

        db = tmp_path / "arc.db"
        conn = open_db(db, copy=False)
        HaltSwitch(HaltRepo(conn)).halt(actor="test", reason="drill", now=FIXTURE_NOW)
        conn.close()
        # a manual run still records the proposal, but the gate refuses it (no token)
        _, report = fixture_run(settings, routines, db=db)
        (p,) = report.proposals
        assert not p["gate_passed"]
        assert not p["gate_token"]
        assert "halted" in p["gate_violations"]


class _Boom:
    model = "boom"

    def complete(self, prompt: str) -> LLMResult:
        raise ScalpLLMError("provider down")


class TestDigestCards:
    """E5.5: each persona step posts a digest card; the fallback one-liner is unchanged."""

    def test_fixture_chain_posts_a_card_per_persona(
        self,
        settings: ArcSettings,
        routines,  # noqa: ANN001
    ) -> None:
        from arc.ingest.scalp import load_fixture_docs
        from arc.pipeline.runner import run_propose
        from arc.routines.heartbeat import RecordingNotifier

        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        notes = RecordingNotifier()
        report = run_propose(
            conn, settings, routines, PipelineEnv.fixtures(), now=FIXTURE_NOW, notifier=notes
        )
        chain = report.outcomes[1].chain_run_id
        texts = [t for _, t in notes.posts]
        headers = [b[0]["text"]["text"] if b else None for b in notes.blocks]
        assert headers == [
            "[Scalp] Scan: 10 Sources → 6 Candidates",  # D54: earnings doc = slow feed
            "[Research] Ranked: 3 / 6 • Market Risk ON",
            "[Quant] Structures: SPY Iron Condor • PoP 62% • EV -$18.74",
            "[Risk] Review: SPY Moderate • 14 Contracts",  # D18-sized, not the advisory 20
            None,  # propose has no card (E6.1 posts the proposal card)
            None,  # execute (D34): summary only; the Investor posts the order card
        ]
        # Fallback text = the pre-E5.5 one-liners.
        assert texts[0] == ("[Scalp] scalp ✓ 10 docs (10 stories) → 7 accepted, 6 candidates today")
        assert texts[1] == (
            "[Research] research ✓ 6 candidates → ranked 3: SPY (neutral), NVDA (bullish), "
            "XOM (bearish); excluded 1; dropped {'not_a_candidate': 1}"
        )
        assert texts[2].startswith("[Quant] quant ✓ SPY iron_condor 740/745/798/803 2026-10-30")
        assert (
            texts[3] == "[Risk] risk ✓ SPY moderate, suggests 20; dropped {'unknown_structure': 1}"
        )
        # Footer links each chain post to its run and chain (E7.4 journal).
        for o, blocks in zip(report.outcomes[1:4], notes.blocks[1:4], strict=True):
            assert blocks is not None
            footer = blocks[-1]["elements"][0]["text"]
            assert footer == f"run `{o.run_id}` · chain `{chain}`"
        scalp = "\n".join(
            b["text"]["text"] for b in notes.blocks[0] or [] if b["type"] == "section"
        )
        # E5.7 open universe: UFPT fails the screen, ZZZQ is not a listed symbol; the
        # failed checks are shown for the illiquid name. D51: PLTR is core now, so it is
        # admitted without the "new" tag.
        assert "*PLTR*\nbullish · news · 90% confidence · 1 source\n" in scalp
        assert "PLTR*\nbullish · news · 90% confidence · 1 source · new" not in scalp
        # D47: grouped by category, equal shares, sources inside each
        assert (
            "*Source mix*\n*Market news* 50% · 8 read: rss 8\n"
            "*Company data* 50% · 2 read: EDGAR 2" in scalp  # D54: earnings = slow feed
        )
        assert "• failed liquidity screen (1): UFPT" in scalp
        assert "UFPT: relaxed screen: ADV 118k &lt; 500k; no expiry in the DTE window" in scalp
        assert "• unknown symbol (1): ZZZQ" in scalp
        assert "http" not in scalp  # no source links
        assert "before sizing" not in json.dumps(notes.blocks[2])
        assert "Buyback plus raised data-center guidance." in scalp  # Scalp rationale line
        assert "Evidence: Scalp neutral · macro catalyst Oct 28 · 62% confidence" in json.dumps(
            notes.blocks[1], ensure_ascii=False
        )
        research = json.dumps(notes.blocks[1], ensure_ascii=False)
        # every ranked name is listed, with its Research evidence (≤3 items)
        for t in ("*SPY*", "*NVDA*", "*XOM*"):
            assert t in research
        assert "Research evidence: 8-K: buyback $50B, Sep 24 · uptrend" in research
        assert "a fourth item is cut" not in research
        assert "*Excluded (1)*\\n• *PLTR*: Fixture: new open-universe name" in research
        assert "not a Scalp candidate (1): BRK.B" in research
        quant = json.dumps(notes.blocks[2], ensure_ascii=False)
        assert "*Skipped (1)*\\n• *NVDA*: Fixture: bull put credit is thin" in quant
        assert "no tradable chain (1): XOM" in quant
        risk = json.dumps(notes.blocks[3])
        assert "structure Quant did not propose (1): QQQ iron_condor" in risk


class TestFailures:
    def _env(self, **llms: object) -> PipelineEnv:
        env = PipelineEnv.fixtures()
        env.llms.update(llms)  # type: ignore[arg-type]
        return env

    def _run(self, settings: ArcSettings, routines, env: PipelineEnv):  # noqa: ANN001, ANN202
        from arc.ingest.scalp import load_fixture_docs
        from arc.pipeline.runner import run_propose
        from arc.routines.heartbeat import RecordingNotifier

        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        return conn, run_propose(
            conn, settings, routines, env, now=FIXTURE_NOW, notifier=RecordingNotifier()
        )

    def test_llm_error_fails_step_and_stops_chain(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        conn, report = self._run(settings, routines, self._env(quant=_Boom()))
        status = {o.job: o.status for o in report.outcomes}
        assert status["research"] == "ok"
        assert status["quant"] == "failed"
        assert "risk" not in status and "propose" not in status
        assert report.failed
        assert not report.proposals
        rows = conn.execute("SELECT status FROM persona_calls WHERE persona='quant'").fetchall()
        assert [r["status"] for r in rows] == ["llm_error"]

    def test_unparseable_reply_fails(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        _, report = self._run(
            settings, routines, self._env(research=FixtureScalpLLM(["not json at all"]))
        )
        assert {o.job: o.status for o in report.outcomes}["research"] == "failed"

    def test_risk_declines(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        risk = json.loads((FIXTURES_DIR / "risk.json").read_text())
        risk["assessments"][0]["sizing_suggestion"] = 0
        _, report = self._run(
            settings, routines, self._env(risk=FixtureScalpLLM([json.dumps(risk)]))
        )
        propose = next(o for o in report.outcomes if o.job == "propose")
        assert propose.status == "ok"
        assert "suggested 0" in (propose.summary or "")
        assert not report.proposals

    def test_empty_shortlist(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        d = {"shortlist": [], "market_regime": "risk_off", "session_notes": "nothing"}
        _, report = self._run(
            settings, routines, self._env(research=FixtureScalpLLM([json.dumps(d)]))
        )
        assert all(o.status == "ok" for o in report.outcomes)
        assert not report.proposals


def _decisions(conn, stage: str, code: str) -> dict[str, str]:  # noqa: ANN001
    """subject -> reason_text of the *stage* decisions journalled with *code*."""
    rows = conn.execute(
        "SELECT subject, reason_text FROM decisions WHERE stage = ? AND reason_code = ?",
        (stage, code),
    ).fetchall()
    return {r["subject"]: r["reason_text"] or "" for r in rows}


class TestFunnel:
    """E5.7: Research ranks everything; pipeline_max_shortlist budgets Quant/Risk."""

    _run = TestFailures._run
    _env = TestFailures._env

    def test_budget_cuts_quant_not_research(self, settings, routines) -> None:  # noqa: ANN001
        settings = settings.model_copy(update={"pipeline_max_shortlist": 1})
        env = self._env()
        conn, report = self._run(settings, routines, env)
        status = {o.job: o for o in report.outcomes}
        assert status["quant"].status == "ok"
        # Research saw every candidate; no budget in its prompt, all three ranked
        research_prompt = env.llms["research"].prompts[0]  # type: ignore[attr-defined]
        assert "pipeline_max_shortlist" not in research_prompt
        assert "do not cap" in research_prompt.lower()
        sl = _latest_payload(conn, "shortlist")
        assert [i["ticker"] for i in sl["shortlist"]] == ["SPY", "NVDA", "XOM"]
        assert sl["budget"] == 1
        # the Quant only saw SPY; NVDA and XOM are over budget, journalled with rank
        quant_prompt = env.llms["quant"].prompts[0]  # type: ignore[attr-defined]
        assert '"NVDA"' not in quant_prompt
        st = _latest_payload(conn, "structures")
        assert st["over_budget"] == ["NVDA", "XOM"]
        rows = _decisions(conn, "structure", "over_budget")
        assert set(rows) == {"NVDA", "XOM"}
        assert "ranked #2, beyond the Quant/Risk budget" in rows["NVDA"]
        assert status["quant"].metrics["over_budget"] == 2
        assert [p["ticker"] for p in report.proposals] == ["SPY"]

    def test_every_budgeted_ticker_is_accounted_for(self, settings, routines) -> None:  # noqa: ANN001
        conn, report = self._run(settings, routines, self._env())
        st = _latest_payload(conn, "structures")
        structured = {s["ticker"] for s in st["structures"]}
        skipped = {s["ticker"] for s in st["skipped"]}
        accounted = structured | skipped | set(st["not_structured"]) | set(st["over_budget"])
        sl = _latest_payload(conn, "shortlist")
        assert {i["ticker"] for i in sl["shortlist"] if i["ticker"] != "BRK.B"} <= accounted
        assert structured == {"SPY"}
        assert skipped == {"NVDA", "XOM"}  # NVDA by Quant (reason), XOM auto: no chain
        assert "credit is thin" in _decisions(conn, "structure", "quant_skipped")["NVDA"]

    def test_quant_silent_ticker_is_not_structured(self, settings, routines) -> None:  # noqa: ANN001
        quant = json.loads((FIXTURES_DIR / "quant.json").read_text())
        quant["skipped"] = []
        conn, _ = self._run(
            settings, routines, self._env(quant=FixtureScalpLLM([json.dumps(quant)]))
        )
        assert _latest_payload(conn, "structures")["not_structured"] == ["NVDA"]
        assert set(_decisions(conn, "structure", "not_structured")) == {"NVDA"}

    def test_research_exclusion_needs_reason_and_candidate(self, settings, routines) -> None:  # noqa: ANN001
        d = json.loads((FIXTURES_DIR / "research.json").read_text())
        d["excluded"] += [
            {"ticker": "TSLA", "reason": "not a candidate today"},
            {"ticker": "SPY", "reason": "ranked and excluded at once"},
        ]
        conn, _ = self._run(
            settings, routines, self._env(research=FixtureScalpLLM([json.dumps(d)]))
        )
        sl = _latest_payload(conn, "shortlist")
        assert [e["ticker"] for e in sl["excluded"]] == ["PLTR"]
        assert "SPY" in {i["ticker"] for i in sl["shortlist"]}

    def test_risk_repair_reask(self, settings, routines) -> None:  # noqa: ANN001
        quant = json.loads((FIXTURES_DIR / "quant.json").read_text())
        risk = json.loads((FIXTURES_DIR / "risk.json").read_text())
        first = {**risk, "assessments": []}  # Risk forgets the SPY structure
        risk_llm = FixtureScalpLLM([json.dumps(first), json.dumps(risk)])
        conn, report = self._run(
            settings,
            routines,
            self._env(quant=FixtureScalpLLM([json.dumps(quant)]), risk=risk_llm),
        )
        assert len(risk_llm.prompts) == 2
        assert (
            "REPAIR: your previous reply had no assessment for SPY iron_condor"
            in (risk_llm.prompts[1])
        )
        outcome = next(o for o in report.outcomes if o.job == "risk")
        assert outcome.metrics["repaired"] == 1 and outcome.metrics["not_assessed"] == 0
        n = conn.execute("SELECT COUNT(*) FROM persona_calls WHERE persona='risk'").fetchone()[0]
        assert n == 2
        assert [p["ticker"] for p in report.proposals] == ["SPY"]

    def test_risk_still_missing_after_repair(self, settings, routines) -> None:  # noqa: ANN001
        risk = json.loads((FIXTURES_DIR / "risk.json").read_text())
        empty = json.dumps({**risk, "assessments": []})
        risk_llm = FixtureScalpLLM([empty, empty])
        conn, report = self._run(settings, routines, self._env(risk=risk_llm))
        assert len(risk_llm.prompts) == 2  # exactly one re-ask
        outcome = next(o for o in report.outcomes if o.job == "risk")
        assert outcome.metrics["not_assessed"] == 1
        missing = _decisions(conn, "risk_review", "not_assessed")
        assert "after one repair re-ask" in missing["SPY"]
        assert not report.proposals


def test_cli_propose_fixtures(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:  # noqa: ANN001
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    assert main(["propose", "--fixtures", "--json", "--profile", "margin"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "fixtures"
    assert [p["ticker"] for p in out["proposals"]] == ["SPY"]


def test_cli_live_propose_requires_gate_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import arc.config
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    monkeypatch.setattr(
        arc.config, "get_settings", lambda: ArcSettings(_env_file=None, account_profile="margin")
    )  # type: ignore[call-arg]

    def _no_live_env(*_a: object, **_k: object) -> None:
        raise AssertionError("must refuse before touching Alpaca/Hermes")

    monkeypatch.setattr(PipelineEnv, "live", _no_live_env)
    assert main(["propose", "--no-scalp"]) == 2
    assert "ARC_GATE_SECRET" in capsys.readouterr().err


def test_cli_dry_run_does_not_require_gate_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    import arc.config
    from arc.cli import main

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    monkeypatch.setattr(
        arc.config, "get_settings", lambda: ArcSettings(_env_file=None, account_profile="margin")
    )  # type: ignore[call-arg]
    assert main(["propose", "--fixtures", "--json"]) == 0


# ---------------------------------------------------------------------------
# E2.4: managed-exit numbers in the Quant menu, structures and proposal context
# ---------------------------------------------------------------------------


def _latest_payload(conn, kind: str, subject: str | None = None) -> dict:  # noqa: ANN001
    row = conn.execute(
        "SELECT payload FROM context_entries WHERE kind = ? AND (? IS NULL OR subject = ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (kind, subject, subject),
    ).fetchone()
    return json.loads(row["payload"])


def _recording_fixture_run(settings: ArcSettings, routines):  # noqa: ANN001, ANN202
    """fixture_run, but keeping each persona prompt (persona_calls stores only a hash)."""
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.runner import run_propose
    from arc.routines.heartbeat import LogNotifier

    env = PipelineEnv.fixtures()
    prompts: dict[str, str] = {}

    class _Rec:
        def __init__(self, persona: str, inner) -> None:  # noqa: ANN001
            self.persona, self.inner = persona, inner
            self.model = getattr(inner, "model", "fixture")

        def complete(self, prompt: str):  # noqa: ANN202
            prompts[self.persona] = prompt
            return self.inner.complete(prompt)

    env.llms = {k: _Rec(k, v) for k, v in env.llms.items()}  # type: ignore[misc]
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    report = run_propose(
        conn, settings, routines, env, now=FIXTURE_NOW, notifier=LogNotifier(), mode="fixtures"
    )
    return conn, report, prompts


def test_exit_model_wired_into_pipeline(settings: ArcSettings, routines) -> None:  # noqa: ANN001
    conn, report, prompts = _recording_fixture_run(settings, routines)
    assert not report.failed
    (s,) = _latest_payload(conn, "structures")["structures"]
    ex = s["exits"]
    assert ex is not None
    assert "50% of max gain" in ex["policy"]
    assert 0 < ex["managed_pop"] < 1 and 0 < ex["static_pop"] < 1
    assert ex["p_take_profit"] + ex["p_stop"] + ex["p_dte_exit"] + ex["p_expiry"] == (
        pytest.approx(1.0, abs=1e-3)
    )
    # the Quant and Risk prompts saw the managed numbers next to the static ones
    for persona in ("quant", "risk"):
        assert "managed_net_ev" in prompts[persona]
        assert "static_net_ev" in prompts[persona]

    prop = _latest_payload(conn, "proposal")
    em = prop["exit_model"]
    assert em["model"] == "gbm_flat_iv"
    assert em["n_paths"] == 20_000
    assert em["policy"]["take_profit_pct_of_max_gain"] == 0.5
    assert em["take_profit"]["close_side"] == "debit"
    # the stored Proposal (and so the gate hash) is unchanged: exit_model is context only
    (p,) = report.proposals
    assert "exit_model" not in json.loads(p["structure_json"])


@pytest.mark.parametrize("by", ["managed_net_ev", "rorc_day"])
def test_rank_menu_by_flag(
    settings: ArcSettings,
    routines,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    by: str,
) -> None:
    import arc.pipeline.steps as steps
    from arc.exits import load_exit_config

    def menus(prompt: str) -> dict[str, list[float]]:
        # E5.7: several tickers get a menu; each menu is ordered on its own.
        chains = prompt.split("### Option chains with Greeks\n", 1)[1].split("\n\n### ", 1)[0]
        return {
            t: [float(x) for x in re.findall(rf'"{by}": (-?[0-9.e-]+)', json.dumps(c["menu"]))]
            for t, c in json.loads(chains).items()
        }

    _, _, off = _recording_fixture_run(settings, routines)
    cfg = load_exit_config()
    on_cfg = cfg.model_copy(
        update={"pipeline": cfg.pipeline.model_copy(update={"rank_menu_by": by})}
    )
    monkeypatch.setattr(steps, "exit_config", lambda _settings=None: on_cfg)
    _, _, on = _recording_fixture_run(settings, routines)
    on_menus, off_menus = menus(on["quant"]), menus(off["quant"])
    assert set(on_menus) == set(off_menus) and len(on_menus) >= 2
    for t, vals in on_menus.items():
        assert vals == sorted(vals, reverse=True), t
        assert sorted(off_menus[t]) == sorted(vals)  # same menu, reordered
    assert len(on_menus["SPY"]) >= 2


def test_menu_rank_key_unmodelled_last() -> None:
    from arc.pipeline.steps import _menu_rank_key

    assert _menu_rank_key(None, "vrp") == float("inf")


def test_realized_vol_from_regime_context(settings: ArcSettings, routines) -> None:  # noqa: ANN001
    conn, _, prompts = _recording_fixture_run(settings, routines)
    (s,) = _latest_payload(conn, "structures")["structures"]
    em = _latest_payload(conn, "proposal")["exit_model"]
    regime = _latest_payload(conn, "regime", "SPY")["vol"]
    from arc.exits import realized_vol_forecast

    rv = realized_vol_forecast(regime["hv20"], regime["hv60"])  # fixture: HV20 only
    assert rv is not None
    assert em["path_vol_source"] == "realized_forecast"
    assert em["path_vol"] == pytest.approx(rv)
    assert em["vrp"] == pytest.approx(em["iv_used"] - rv, abs=1e-4)
    assert s["exits"]["vrp"] is not None and s["exits"]["rorc_day"] is not None
    assert '"vrp"' in prompts["quant"]


# ---------------------------------------------------------------------------
# E5.2b: fresh clock for the propose step; live runs fail loudly without a secret
# ---------------------------------------------------------------------------


class TestLiveProposeClock:
    """Chain start (``ctx.now``) keys idempotency; ``ctx.clock()`` judges data age."""

    STARTED = FIXTURE_NOW - _dt.timedelta(minutes=2)  # before the LLM steps ran

    def _run(
        self,
        settings: ArcSettings,
        routines,  # noqa: ANN001
        *,
        now: _dt.datetime,
        clock=None,  # noqa: ANN001
        mint: bool = False,
        account=None,  # noqa: ANN001
    ):  # noqa: ANN202
        from arc.ingest.scalp import load_fixture_docs
        from arc.pipeline.runner import run_propose
        from arc.routines.heartbeat import RecordingNotifier

        conn = open_db(":memory:", copy=False)
        load_fixture_docs(conn)
        env = PipelineEnv.fixtures()
        env.mint_tokens = mint
        if account is not None:
            env.account = account
        notes = RecordingNotifier()
        report = run_propose(
            conn, settings, routines, env, now=now, clock=clock, notifier=notes, mode="live"
        )
        return conn, report, notes

    def test_frozen_start_time_fails_freshness(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        """The bug: with only the chain-start time, later-stamped quotes are 'in the future'."""
        _, report, _ = self._run(settings, routines, now=self.STARTED)
        (p,) = report.proposals
        assert not p["gate_passed"]
        assert "in the future" in p["gate_violations"]

    def test_propose_passes_when_chain_started_before_quotes(
        self,
        routines,  # noqa: ANN001
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ARC_GATE_SECRET", GATE_SECRET)
        settings = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        conn, report, _ = self._run(
            settings, routines, now=self.STARTED, clock=lambda: FIXTURE_NOW, mint=True
        )
        assert not report.failed
        (p,) = report.proposals
        assert p["gate_passed"], p["gate_violations"]
        assert p["gate_token"]
        assert p["day"] == self.STARTED.date().isoformat()  # idempotency keeps ctx.now
        ttl = _dt.timedelta(seconds=settings.approval_ttl_seconds)
        assert _dt.datetime.fromisoformat(p["expires_at"]) == FIXTURE_NOW + ttl
        g = conn.execute("SELECT decided_at FROM gate_decisions").fetchone()
        assert _dt.datetime.fromisoformat(g["decided_at"]) == FIXTURE_NOW

    def test_account_snapshot_as_of_is_fetch_time(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        from arc.pipeline.env import fixture_account

        ticks = iter(FIXTURE_NOW + _dt.timedelta(seconds=s) for s in range(0, 600, 10))
        events: list[tuple[str, _dt.datetime | None]] = []

        def clock() -> _dt.datetime:
            t = next(ticks)
            events.append(("clock", t))
            return t

        def account():  # noqa: ANN202
            events.append(("fetch", None))
            return fixture_account()

        conn, report, _ = self._run(
            settings, routines, now=self.STARTED, clock=clock, account=account
        )
        assert report.proposals
        row = conn.execute("SELECT account_snapshot FROM gate_decisions").fetchone()
        as_of = _dt.datetime.fromisoformat(json.loads(row["account_snapshot"])["as_of"])
        last_fetch = max(i for i, (kind, _) in enumerate(events) if kind == "fetch")
        # the first clock read after propose's broker fetch, not the chain start
        read_after = next(t for kind, t in events[last_fetch + 1 :] if kind == "clock")
        assert as_of == read_after
        assert as_of != self.STARTED

    def test_default_clock_is_ctx_now(self) -> None:
        from arc.routines.handlers import JobContext

        ctx = JobContext.__new__(JobContext)
        ctx.now = FIXTURE_NOW
        ctx.clock_fn = None
        assert ctx.clock() == FIXTURE_NOW
        ctx.clock_fn = lambda: FIXTURE_NOW + _dt.timedelta(minutes=1)
        assert ctx.clock() == FIXTURE_NOW + _dt.timedelta(minutes=1)

    def test_live_env_without_secret_fails_run(self, settings: ArcSettings, routines) -> None:  # noqa: ANN001
        conn, report, notes = self._run(settings, routines, now=FIXTURE_NOW, mint=True)
        propose = next(o for o in report.outcomes if o.job == "propose")
        assert propose.status == "failed"
        assert "GateSecretMissingError" in propose.summary
        assert "ARC_GATE_SECRET" in propose.summary
        assert report.failed
        row = conn.execute(
            "SELECT status, error FROM routine_runs WHERE run_id = ?", (propose.run_id,)
        ).fetchone()
        assert row["status"] == "failed"
        # no token-less PASS was stored (no proposal row at all)
        bad = conn.execute(
            "SELECT COUNT(*) FROM gate_decisions WHERE passed = 1 AND token IS NULL"
        ).fetchone()[0]
        assert bad == 0
        assert report.proposals == []
        assert any("ARC_GATE_SECRET" in t for _, t in notes.posts)  # heartbeat alert

    def test_offline_env_without_secret_still_records_gate_verdict(
        self,
        settings: ArcSettings,
        routines,  # noqa: ANN001
    ) -> None:
        _, report, _ = self._run(settings, routines, now=FIXTURE_NOW, mint=False)
        assert not report.failed
        (p,) = report.proposals
        assert p["gate_passed"] and not p["gate_token"]


def test_tick_exit_code_nonzero_when_propose_fails_for_missing_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``arc routines tick`` returns 1 when a step (e.g. a secret-less live propose) failed."""
    import argparse

    from arc.routines import cli as rcli
    from arc.routines.dispatcher import Outcome, TickReport

    class _Disp:
        def tick(self, now, *, dry_run, since):  # noqa: ANN001, ANN202
            r = TickReport(now=now, since=since, dry_run=dry_run, halted=False)
            r.outcomes.append(
                Outcome(
                    "propose",
                    now,
                    "failed",
                    "GateSecretMissingError: live propose cannot mint a gate token",
                )
            )
            return r

    monkeypatch.setattr(rcli, "_dispatcher", lambda *a, **k: _Disp())
    monkeypatch.setattr(rcli, "_approval_sweep", lambda *a, **k: None)
    args = argparse.Namespace(dry_run=False, json=False, db=str(tmp_path / "a.db"))
    conn = open_db(tmp_path / "a.db", copy=False)
    assert rcli._tick(args, FIXTURE_NOW, None, conn=conn) == 1


def test_routines_dispatcher_uses_wall_clock_only_when_not_pinned(tmp_path: Path) -> None:
    import argparse

    from arc.routines import cli as rcli
    from arc.utils.calendar import now_et

    conn = open_db(tmp_path / "a.db", copy=False)
    base = {"config": None, "no_slack": True, "lock_dir": str(tmp_path / "locks")}
    live = rcli._dispatcher(argparse.Namespace(**base, now=None), conn)
    assert live._clock is now_et
    pinned = rcli._dispatcher(argparse.Namespace(**base, now="2026-09-25T16:00"), conn)
    assert pinned._clock is None
    dry = rcli._dispatcher(argparse.Namespace(**base, now=None), conn, dry=True)
    assert dry._clock is None


# ---------------------------------------------------------------------------
# D25 account profiles through the pipeline (E3.4)
# ---------------------------------------------------------------------------


class TestAccountProfilePipeline:
    def _run(self, profile: str, fixture_set: str, monkeypatch: pytest.MonkeyPatch, routines):  # noqa: ANN001, ANN202
        monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
        s = ArcSettings(_env_file=None, account_profile=profile)  # type: ignore[call-arg]
        # The fixture debit spreads model at negative managed Net EV; these tests are
        # about the account profile, so the E6.4a live Net EV floor is switched off.
        _net_ev_floor_off(s)
        return fixture_run(s, routines, fixture_set=fixture_set)

    def test_cash_debit_bullish_proposes_a_debit_spread(
        self,
        monkeypatch: pytest.MonkeyPatch,
        routines,  # noqa: ANN001
    ) -> None:
        conn, report = self._run("cash_debit", "bullish", monkeypatch, routines)
        (p,) = report.proposals
        assert p["gate_passed"], p
        (row,) = proposals_for_day(conn, FIXTURE_NOW.date().isoformat())
        assert row["kind"] == "open"
        structure = json.loads(row["structure_json"])
        assert [leg["side"] for leg in structure["legs"]] == ["long", "short"]
        assert Decimal(str(structure["net_debit_credit"])) > 0
        prompts = [r["prompt_text"] for r in conn.execute("SELECT prompt_text FROM persona_calls")]
        assert prompts and all("account profile `cash_debit`" in t for t in prompts)

    def test_cash_debit_neutral_is_journaled_no_trade(
        self,
        monkeypatch: pytest.MonkeyPatch,
        routines,  # noqa: ANN001
    ) -> None:
        conn, report = self._run("cash_debit", "neutral", monkeypatch, routines)
        assert report.proposals == []
        rows = conn.execute(
            "SELECT subject, choice, reason_code FROM decisions WHERE reason_code LIKE 'profile:%'"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("SPY", "no_trade", "profile:no_neutral_structure")]

    def test_margin_neutral_still_trades_condor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        routines,  # noqa: ANN001
    ) -> None:
        _, report = self._run("margin", "neutral", monkeypatch, routines)
        (p,) = report.proposals
        assert p["gate_passed"]

    def test_strategies_follow_profile(self) -> None:
        from arc.pipeline.steps import _strategies, _structure_type
        from arc.scanner import ScanStrategy

        m = ArcSettings(_env_file=None, account_profile="margin")  # type: ignore[call-arg]
        c = m.with_profile("cash_debit")
        assert _strategies("neutral", m) == [ScanStrategy.IRON_CONDOR]
        assert _strategies("neutral", c) == []
        assert _strategies("bearish", c) == [ScanStrategy.BEAR_PUT_DEBIT, ScanStrategy.LONG_PUT]

        class _C:
            def __init__(self, v: str) -> None:
                self.strategy = ScanStrategy(v)

        assert _structure_type(_C("long_call")) == "long_call"  # type: ignore[arg-type]
        assert _structure_type(_C("bull_call_debit")) == "vertical_spread"  # type: ignore[arg-type]

    def test_account_snapshot_settled_cash(self) -> None:
        from arc.broker.base import AccountInfo
        from arc.pipeline.market import settled_cash

        base = {"account_id": "x", "equity": Decimal(100), "buying_power": Decimal(500)}
        info = AccountInfo(
            **base,
            cash=Decimal(80),
            options_buying_power=Decimal(60),
            non_marginable_buying_power=Decimal(70),
        )  # type: ignore[arg-type]
        assert settled_cash(info) == Decimal(60)
        assert account_snapshot(info, FIXTURE_NOW, baseline=None).settled_cash == Decimal(60)
        neg = AccountInfo(**base, cash=Decimal(-5))  # type: ignore[arg-type]
        assert settled_cash(neg) == Decimal(0)


# ---------------------------------------------------------------------------
# Sentinel S-7 / E6.4: sizing uses the remaining per-underlying budget
# ---------------------------------------------------------------------------


def test_propose_sizes_against_existing_exposure_and_keeps_realloc_source(
    monkeypatch: pytest.MonkeyPatch,
    routines,  # noqa: ANN001
) -> None:
    """An open SPY position using the whole 5% budget -> no trade (budget_exhausted),
    recorded with what risk.reallocate needs to re-propose it after a close."""
    from arc.broker.base import BrokerPosition
    from arc.ingest.scalp import load_fixture_docs
    from arc.pipeline.env import FIXTURE_SETS
    from arc.pipeline.runner import run_propose
    from arc.routines.heartbeat import LogNotifier

    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    _net_ev_floor_off(s)  # sizing under test, not the E6.4a Net EV floor
    conn = open_db(":memory:", copy=False)
    load_fixture_docs(conn)
    env = PipelineEnv.fixtures(FIXTURE_SETS["bullish"])
    # 5 long Nov 780 calls: max loss = premium paid, $5,355 > the $5,000 SPY budget
    env.positions = lambda: [
        BrokerPosition(
            symbol="SPY261106C00780000", qty=Decimal(5), side="long",
            avg_entry_price=Decimal("10.71"),
        )
    ]  # fmt: skip
    report = run_propose(
        conn, s, routines, env, now=FIXTURE_NOW, notifier=LogNotifier(), mode="fixtures"
    )
    assert report.proposals == []
    (row,) = conn.execute(
        "SELECT choice, reason_code, payload FROM decisions WHERE stage = 'sizing'"
    ).fetchall()
    assert (row["choice"], row["reason_code"]) == ("no_trade", "sizing:budget_exhausted")
    payload = json.loads(row["payload"])
    assert Decimal(payload["existing_max_loss"]) > 0
    src = payload["realloc_source"]
    assert src["legs"][0][0] == "SPY261030C00770000" and src["suggestion"] >= 1
    assert src["net_ev"] is not None and src["buying_power"] > 0


def _net_ev_floor_off(s: ArcSettings) -> None:
    """D26 override: ranking.filters.live = false (the E6.4a live Net EV floor off)."""
    s._yaml_overrides = {"ranking": {("ranking", "filters", "live"): False}}  # noqa: SLF001


def test_live_net_ev_floor_drops_negative_ev_structure(
    monkeypatch: pytest.MonkeyPatch,
    routines,  # noqa: ANN001
) -> None:
    """E6.4a: the fixture debit spread's managed Net EV is negative after costs, so the
    live propose step rejects it with the typed `net_ev_floor` reason (no proposal)."""
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    s = ArcSettings(_env_file=None, account_profile="cash_debit")  # type: ignore[call-arg]
    conn, report = fixture_run(s, routines, fixture_set="bullish")
    assert report.proposals == []
    (row,) = conn.execute(
        "SELECT choice, reason_code, payload FROM decisions WHERE reason_code = 'net_ev_floor'"
    ).fetchall()
    payload = json.loads(row["payload"])
    assert payload["managed_net_ev"] < 0 and payload["floor"] == 0.0
